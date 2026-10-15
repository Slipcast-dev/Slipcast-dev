#!/usr/bin/env python3
"""Автоматически обновляет блок "Последние проекты" в профильном README.

Почему скрипт сделан отдельным файлом:
1) Workflow остаётся коротким и поддерживаемым.
2) Логику форматирования удобно развивать без изменения YAML.
3) Можно запускать локально перед публикацией изменений.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Маркеры ограничивают изменяемый участок README.
# Это защищает остальные разделы от случайной перезаписи.
START_MARKER: str = "<!-- LATEST-PROJECTS-START -->"
END_MARKER: str = "<!-- LATEST-PROJECTS-END -->"
GITHUB_API_BASE: str = "https://api.github.com"


@dataclass(frozen=True)
class RepoCard:
    """Нормализованное представление репозитория для рендера в Markdown."""

    name: str
    html_url: str
    description: str
    language: str
    stars: int
    pushed_at: datetime


def parse_args() -> argparse.Namespace:
    """Разбирает аргументы CLI.

    Вынесено в отдельную функцию, чтобы было проще тестировать и расширять.
    """

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Обновляет динамический блок последних проектов в README."
    )
    parser.add_argument(
        "--readme-ru",
        help="Русская версия README; обновляется из того же ответа API.",
    )
    parser.add_argument(
        "--readme",
        default="README.md",
        help="Путь к README (по умолчанию: README.md).",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("GITHUB_REPOSITORY_OWNER", "Slipcast-dev"),
        help=(
            "GitHub username. По умолчанию берётся из "
            "GITHUB_REPOSITORY_OWNER или fallback на Slipcast-dev."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=6,
        help="Сколько репозиториев показать в блоке (по умолчанию: 6).",
    )
    return parser.parse_args()


def build_request(url: str, token: str | None) -> Request:
    """Формирует HTTP-запрос к GitHub API.

    Почему добавляем токен:
    - Для публичных данных он не обязателен.
    - Но токен повышает лимит запросов и делает workflow стабильнее.
    """

    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Slipcast-dev-profile-readme-updater",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url=url, headers=headers)


def parse_next_link(link_header: str | None) -> str | None:
    """Извлекает ссылку следующей страницы из заголовка Link.

    GitHub API возвращает пагинацию в формате:
    <url>; rel="next", <url>; rel="last"
    """

    if not link_header:
        return None

    for part in link_header.split(","):
        part = part.strip()
        match: re.Match[str] | None = re.match(r"<([^>]+)>;\s*rel=\"([^\"]+)\"", part)
        if match and match.group(2) == "next":
            return match.group(1)
    return None


def fetch_repositories(username: str, token: str | None) -> list[dict[str, Any]]:
    """Загружает список репозиториев пользователя из GitHub API.

    Почему идём через пагинацию:
    - Публичных репозиториев может стать больше 100.
    - В этом случае без пагинации часть данных потеряется.
    """

    url: str | None = (
        f"{GITHUB_API_BASE}/users/{username}/repos"
        "?per_page=100&sort=updated&type=owner"
    )
    repositories: list[dict[str, Any]] = []

    while url:
        request: Request = build_request(url, token)
        try:
            with urlopen(request, timeout=20) as response:
                payload: Any = json.load(response)
                if not isinstance(payload, list):
                    raise RuntimeError("GitHub API вернул неожиданный формат ответа.")
                repositories.extend(payload)
                url = parse_next_link(response.headers.get("Link"))
        except HTTPError as error:
            raise RuntimeError(
                f"HTTP ошибка GitHub API: {error.code} {error.reason}"
            ) from error
        except URLError as error:
            raise RuntimeError(
                f"Сетевая ошибка при обращении к GitHub API: {error.reason}"
            ) from error

    return repositories


def normalize_description(raw_description: str | None) -> str:
    """Нормализует описание репозитория для однострочного Markdown.

    Почему удаляем переносы и лишние пробелы:
    - Иначе блок в README может "ломать" визуальную сетку.
    """

    if not raw_description:
        return "Описание пока не добавлено."
    compact: str = " ".join(raw_description.split())
    return compact.strip()


def parse_repo_card(repo_payload: dict[str, Any]) -> RepoCard | None:
    """Преобразует сырые данные API в RepoCard.

    Возвращает None для репозиториев, которые не хотим показывать:
    - форки;
    - архивные проекты;
    - проекты без базовых ключей.
    """

    if repo_payload.get("fork") or repo_payload.get("archived"):
        return None

    name: Any = repo_payload.get("name")
    html_url: Any = repo_payload.get("html_url")
    pushed_at_raw: Any = repo_payload.get("pushed_at")

    if not isinstance(name, str) or not isinstance(html_url, str):
        return None
    if not isinstance(pushed_at_raw, str):
        return None

    # Поддерживаем и формат с "Z", и формат с явным смещением.
    pushed_at: datetime = datetime.fromisoformat(pushed_at_raw.replace("Z", "+00:00"))

    return RepoCard(
        name=name,
        html_url=html_url,
        description=normalize_description(repo_payload.get("description")),
        language=str(repo_payload.get("language") or "N/A"),
        stars=int(repo_payload.get("stargazers_count") or 0),
        pushed_at=pushed_at,
    )


def build_projects_block(
    cards: list[RepoCard], limit: int, language: str = "en"
) -> str:
    """Локализует подписи, сохраняя исходные описания и общий порядок проектов."""

    if limit < 1:
        raise ValueError("Параметр --limit должен быть больше 0.")

    if not cards:
        return (
            "- Пока нет подходящих публичных репозиториев."
            if language == "ru"
            else "- No eligible public repositories yet."
        )

    selected_cards: list[RepoCard] = sorted(
        cards, key=lambda item: item.pushed_at, reverse=True
    )[:limit]

    lines: list[str] = []
    for card in selected_cards:
        pushed_label: str = card.pushed_at.strftime("%Y-%m-%d")
        # Экранируем HTML и Markdown из API: описание должно оставаться текстом,
        # а не превращаться в разметку или ссылки внутри профильного README.
        description: str = escape_markdown(card.description)
        if card.description == "Описание пока не добавлено." and language == "en":
            description = "No description provided."
        stars_label: str = "звёзды" if language == "ru" else "stars"
        pushed_title: str = "push" if language == "ru" else "pushed"
        line: str = (
            f"- [{escape_markdown(card.name)}]({card.html_url}) — {description} "
            f"· `{card.language}` · {stars_label}: {card.stars} "
            f"· {pushed_title}: {pushed_label}"
        )
        lines.append(line)

    generated_at: str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    updated_label: str = (
        "Обновлено автоматически" if language == "ru" else "Updated automatically"
    )
    lines.append(f"\n_{updated_label}: {generated_at}_")
    return "\n".join(lines)


def escape_markdown(value: str) -> str:
    """Защищает однострочный текст API от интерпретации GitHub Markdown."""

    # Сначала экранируем обратную косую черту, затем спецсимволы Markdown.
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_[\]{}()!|])", r"\\\1", value)


def replace_marked_block(readme_content: str, projects_block: str) -> str:
    """Заменяет содержимое между маркерами START/END в README.

    Регулярное выражение применяется с DOTALL, чтобы корректно обрабатывать
    многострочный блок любого размера.
    """

    pattern: re.Pattern[str] = re.compile(
        rf"({re.escape(START_MARKER)}\n)(.*?)(\n{re.escape(END_MARKER)})",
        flags=re.DOTALL,
    )
    if (
        readme_content.count(START_MARKER) != 1
        or readme_content.count(END_MARKER) != 1
    ):
        raise RuntimeError("Ожидается ровно одна пара маркеров LATEST-PROJECTS.")

    updated_readme: str
    replacements_count: int
    # Callback вставляет текст буквально: обратные слеши в описании не должны
    # трактоваться как ссылки на группы регулярного выражения (например, \\1).
    updated_readme, replacements_count = pattern.subn(
        lambda match: f"{match.group(1)}\n{projects_block}\n{match.group(3)}",
        readme_content,
        count=1,
    )
    if replacements_count != 1:
        raise RuntimeError(
            "Не найдено ровно одного блока маркеров LATEST-PROJECTS. "
            "Проверьте README."
        )
    return updated_readme


def update_readme_file(readme_path: str, projects_block: str) -> bool:
    """Обновляет README и возвращает True, если файл изменился."""

    with open(readme_path, "r", encoding="utf-8") as file:
        original_content: str = file.read()

    updated_content: str = replace_marked_block(original_content, projects_block)
    if updated_content == original_content:
        return False

    with open(readme_path, "w", encoding="utf-8") as file:
        file.write(updated_content)
    return True


def main() -> int:
    """Точка входа CLI."""

    args: argparse.Namespace = parse_args()
    token: str | None = os.getenv("GITHUB_TOKEN")

    try:
        raw_repositories: list[dict[str, Any]] = fetch_repositories(args.username, token)
        cards: list[RepoCard] = []
        for repo_payload in raw_repositories:
            card: RepoCard | None = parse_repo_card(repo_payload)
            if card is None:
                continue

            # Не показываем профильный репозиторий в блоке проектов, чтобы
            # список отражал именно рабочие репозитории, а не контейнер README.
            if card.name.lower() == args.username.lower():
                continue
            cards.append(card)

        targets: list[tuple[str, str]] = [(args.readme, "en")]
        if args.readme_ru:
            targets.append((args.readme_ru, "ru"))
        # Проверяем оба документа до записи, чтобы ошибка маркеров в одном
        # переводе не оставила второй перевод частично обновлённым.
        updates: list[tuple[Path, str, str]] = []
        for filename, language in targets:
            path: Path = Path(filename)
            original: str = path.read_text(encoding="utf-8")
            block: str = build_projects_block(cards, args.limit, language)
            updates.append((path, original, replace_marked_block(original, block)))
        changed: bool = False
        for path, original, updated in updates:
            if original != updated:
                path.write_text(updated, encoding="utf-8")
                changed = True

        if changed:
            print("README обновлён.")
        else:
            print("README уже актуален, изменений нет.")
        return 0
    except Exception as error:  # noqa: BLE001
        # В CI важно вернуть ненулевой код, чтобы ошибка не прошла незаметно.
        print(f"Ошибка обновления README: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

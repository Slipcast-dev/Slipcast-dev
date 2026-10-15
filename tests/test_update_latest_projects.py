"""Регрессии генератора: локализация, сохранность README и совместимость CLI."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import update_latest_projects as updater


class LatestProjectsTests(unittest.TestCase):
    """Проверяет наблюдаемый результат без сетевых запросов и токенов."""

    def setUp(self) -> None:
        """Фиксируем небольшой документ, чтобы обнаруживать изменения вне блока."""
        self.document = (
            "# Profile\n\n"
            f"{updater.START_MARKER}\nold\n{updater.END_MARKER}\n\n## Connect\n"
        )
        self.card = updater.RepoCard(
            name="Example",
            html_url="https://github.com/example/Example",
            description=r"Use C:\new\1 and [text] <script>",
            language="Python",
            stars=3,
            pushed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def test_literal_text_and_surrounding_content(self) -> None:
        """Слеши и разметка API не должны ломать замену или остальной профиль."""
        block = updater.build_projects_block([self.card], 6)
        result = updater.replace_marked_block(self.document, block)
        self.assertTrue(result.startswith("# Profile\n\n"))
        self.assertTrue(result.endswith("\n\n## Connect\n"))
        self.assertIn(r"C:\\new\\1", result)
        self.assertIn(r"\[text\] &lt;script&gt;", result)
        self.assertEqual(updater.replace_marked_block(result, block), result)

    def test_localized_labels_and_empty_results(self) -> None:
        """Обе версии имеют собственные подписи и одинаковые реальные данные."""
        english = updater.build_projects_block([self.card], 6)
        russian = updater.build_projects_block([self.card], 6, "ru")
        self.assertIn("stars: 3", english)
        self.assertIn("Updated automatically:", english)
        self.assertIn("звёзды: 3", russian)
        self.assertIn("Обновлено автоматически:", russian)
        self.assertIn(self.card.html_url, english)
        self.assertIn(self.card.html_url, russian)
        self.assertIn("No eligible", updater.build_projects_block([], 6))
        self.assertIn("Пока нет", updater.build_projects_block([], 6, "ru"))
        with self.assertRaises(ValueError):
            updater.build_projects_block([], 0)

    def test_sorting_limit_and_filtering(self) -> None:
        """Проекты ранжируются только по push, без приоритетов для отдельных имён."""
        payload = {
            "name": "Newer",
            "html_url": "https://github.com/example/Newer",
            "pushed_at": "2026-02-01T00:00:00Z",
        }
        newer = updater.parse_repo_card(payload)
        assert newer is not None
        block = updater.build_projects_block([self.card, newer], 1)
        self.assertIn("[Newer]", block)
        self.assertNotIn("[Example]", block)
        for flag in ("fork", "archived"):
            self.assertIsNone(updater.parse_repo_card({**payload, flag: True}))

    def test_invalid_markers_fail(self) -> None:
        """Отсутствующие, повторные и переставленные маркеры запрещают запись."""
        for document in (
            "# No markers",
            self.document + self.document,
            f"{updater.END_MARKER}\ntext\n{updater.START_MARKER}",
        ):
            with self.subTest(document=document), self.assertRaises(RuntimeError):
                updater.replace_marked_block(document, "new")

    def test_bilingual_cli_and_single_readme_compatibility(self) -> None:
        """Один запрос API обслуживает оба документа; старый CLI также работает."""
        with tempfile.TemporaryDirectory() as directory:
            english = Path(directory) / "README.md"
            russian = Path(directory) / "README.ru.md"
            for bilingual in (False, True):
                english.write_text(self.document, encoding="utf-8")
                russian.write_text(self.document, encoding="utf-8")
                argv = ["update", "--readme", str(english)]
                if bilingual:
                    argv.extend(["--readme-ru", str(russian)])
                with (
                    patch.object(sys, "argv", argv),
                    patch.object(updater, "fetch_repositories", return_value=[]) as fetch,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(updater.main(), 0)
                    fetch.assert_called_once()
                self.assertIn("No eligible", english.read_text(encoding="utf-8"))
                self.assertEqual(
                    "Пока нет" in russian.read_text(encoding="utf-8"), bilingual
                )

    def test_invalid_translation_leaves_both_files_untouched(self) -> None:
        """Сбой одного перевода не должен частично обновлять второй документ."""
        with tempfile.TemporaryDirectory() as directory:
            english = Path(directory) / "README.md"
            russian = Path(directory) / "README.ru.md"
            english.write_text(self.document, encoding="utf-8")
            russian.write_text("# Missing markers\n", encoding="utf-8")
            with (
                patch.object(sys, "argv", [
                    "update", "--readme", str(english), "--readme-ru", str(russian)
                ]),
                patch.object(updater, "fetch_repositories", return_value=[]),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(updater.main(), 1)
            self.assertEqual(english.read_text(encoding="utf-8"), self.document)
            self.assertEqual(russian.read_text(encoding="utf-8"), "# Missing markers\n")


if __name__ == "__main__":
    unittest.main()

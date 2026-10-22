from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_test_home = tempfile.TemporaryDirectory(prefix="subtext-file-menu-tests-")
os.environ["HOME"] = _test_home.name
os.environ["XDG_CONFIG_HOME"] = str(Path(_test_home.name) / ".config")

from PySide6.QtWidgets import QApplication  # noqa: E402


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("subtext_app_under_test", APP_PATH)
assert SPEC is not None and SPEC.loader is not None
subtext_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subtext_app)

application = QApplication.instance() or QApplication([])
application.setApplicationName("SubText Editor")
application.setOrganizationName("SubText Tests")


class FileMenuBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = subtext_app.MainWindow([], start_new=True)
        self.window.show()
        application.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        application.processEvents()

    def test_split_view_shares_the_active_document_and_toggles(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("original")
        editor.document().setModified(True)

        self.window.split_view_action.trigger()
        application.processEvents()

        self.assertTrue(self.window._split_view_enabled)
        self.assertEqual(self.window.editor_splitter.count(), 2)
        self.assertEqual(self.window.split_editor.document(), editor.document())
        self.window.split_editor.setFocus()
        application.processEvents()
        self.assertIs(self.window.current_editor(), self.window.split_editor)
        self.window.split_editor.setPlainText("edited from the split pane")
        self.assertEqual(editor.toPlainText(), "edited from the split pane")

        self.window.split_view_action.trigger()
        self.assertFalse(self.window._split_view_enabled)
        self.assertTrue(self.window.split_editor.isHidden())

    def test_save_as_suggests_the_untitled_text_as_filename(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("Meeting notes\nsecond line")
        editor.document().setModified(True)
        output_path = Path(_test_home.name) / "Meeting notes"

        with patch.object(
            subtext_app.QFileDialog,
            "getSaveFileName",
            return_value=(str(output_path), "All Files (*)"),
        ) as save_dialog:
            self.window.action_save_as.trigger()

        self.assertEqual(
            save_dialog.call_args.args[2],
            str(Path(_test_home.name) / "Meeting notes"),
        )
        self.assertEqual(output_path.read_text(encoding="utf-8"), "Meeting notes\nsecond line")

    def test_save_with_encoding_writes_bytes_using_selected_codec(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("café €")
        editor.document().setModified(True)
        output_path = Path(_test_home.name) / "encoding-check.txt"
        file_menu = self.window.menuBar().actions()[0].menu()
        encoding_menu = next(
            action.menu()
            for action in file_menu.actions()
            if action.text() == "Save with Encoding"
        )
        windows_1252 = next(
            action for action in encoding_menu.actions() if "1252" in action.text()
        )

        with patch.object(
            subtext_app.QFileDialog,
            "getSaveFileName",
            return_value=(str(output_path), "All Files (*)"),
        ):
            windows_1252.trigger()

        self.assertEqual(output_path.read_bytes(), "café €".encode("cp1252"))
        self.assertEqual(editor.property("fileEncoding"), "cp1252")
        self.assertFalse(editor.document().isModified())

    def test_cancelled_save_with_encoding_keeps_the_previous_encoding(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("draft")
        editor.document().setModified(True)

        with patch.object(
            subtext_app.QFileDialog,
            "getSaveFileName",
            return_value=("", "All Files (*)"),
        ):
            self.window.save_with_encoding("cp1252")

        self.assertEqual(editor.property("fileEncoding"), "utf-8")
        self.assertTrue(editor.document().isModified())
        self.assertFalse(editor.property("filePath"))


if __name__ == "__main__":
    unittest.main()

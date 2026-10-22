from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_test_home = tempfile.TemporaryDirectory(prefix="subtext-file-menu-tests-")
os.environ["HOME"] = _test_home.name
os.environ["XDG_CONFIG_HOME"] = str(Path(_test_home.name) / ".config")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402


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
        self.temp_home = tempfile.TemporaryDirectory(dir=_test_home.name)
        self.addCleanup(self.temp_home.cleanup)
        self.temp_dir = Path(self.temp_home.name)
        self.window = subtext_app.MainWindow([], start_new=True)
        self.window.settings.clear()
        self.window.recent_files.clear()
        self.window.populate_recent_menu()
        self.window.show()
        application.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        application.processEvents()

    def file_menu(self):
        return self.window.menuBar().actions()[0].menu()

    def menu_action(self, label: str):
        return next(action for action in self.file_menu().actions() if action.text() == label)

    def submenu(self, label: str):
        return next(
            action.menu()
            for action in self.file_menu().actions()
            if action.text() == label
        )

    def test_new_file_creates_an_untitled_tab(self) -> None:
        initial_count = self.window.tabs.count()

        self.menu_action("New File").trigger()

        self.assertEqual(self.window.tabs.count(), initial_count + 1)
        self.assertEqual(self.window.current_editor().property("displayName"), "Untitled 2")

    def test_open_file_action_loads_selected_file(self) -> None:
        path = self.temp_dir / "opened.txt"
        path.write_text("opened from File menu", encoding="utf-8")

        with patch.object(
            subtext_app.QFileDialog,
            "getOpenFileNames",
            return_value=([str(path)], "All Files (*)"),
        ):
            self.menu_action("Open File…").trigger()

        self.assertEqual(self.window.current_editor().toPlainText(), "opened from File menu")
        self.assertEqual(Path(self.window.current_editor().property("filePath")), path)

    def test_open_file_uses_windows_1252_fallback_for_legacy_text(self) -> None:
        path = self.temp_dir / "legacy.txt"
        path.write_bytes("café".encode("cp1252"))

        with patch.object(
            subtext_app.QFileDialog,
            "getOpenFileNames",
            return_value=([str(path)], "All Files (*)"),
        ):
            self.menu_action("Open File…").trigger()

        editor = self.window.current_editor()
        self.assertEqual(editor.toPlainText(), "café")
        self.assertEqual(editor.property("fileEncoding"), "cp1252")

    def test_open_folder_action_sets_the_project_root(self) -> None:
        folder = self.temp_dir / "project"
        folder.mkdir()

        with patch.object(
            subtext_app.QFileDialog, "getExistingDirectory", return_value=str(folder)
        ):
            self.menu_action("Open Folder…").trigger()

        self.assertEqual(self.window.project_root, folder.resolve())
        self.assertEqual(self.window.project_folders, [folder.resolve()])

    def test_open_recent_action_reopens_a_recent_file(self) -> None:
        path = self.temp_dir / "recent.txt"
        path.write_text("recent content", encoding="utf-8")
        self.window.remember_recent_file(str(path))
        self.window.populate_recent_menu()

        self.submenu("Open Recent").actions()[0].trigger()

        self.assertEqual(self.window.current_editor().toPlainText(), "recent content")

    def test_reopen_with_encoding_reads_a_legacy_file(self) -> None:
        path = self.temp_dir / "latin1.txt"
        path.write_bytes("café".encode("iso-8859-1"))
        self.window.open_path(str(path))
        editor = self.window.current_editor()
        reopen_action = next(
            action
            for action in self.submenu("Reopen with Encoding").actions()
            if "ISO 8859-1" in action.text()
        )

        reopen_action.trigger()

        self.assertEqual(editor.toPlainText(), "café")
        self.assertEqual(editor.property("fileEncoding"), "iso-8859-1")

    def test_split_view_shares_the_active_document_and_toggles(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("original")
        editor.document().setModified(True)

        self.menu_action("Split View").trigger()
        application.processEvents()

        self.assertTrue(self.window._split_view_enabled)
        self.assertEqual(self.window.editor_splitter.count(), 2)
        self.assertEqual(self.window.split_editor.document(), editor.document())
        self.window.split_editor.setFocus()
        application.processEvents()
        self.assertIs(self.window.current_editor(), self.window.split_editor)
        self.window.split_editor.setPlainText("edited from the split pane")
        self.assertEqual(editor.toPlainText(), "edited from the split pane")

        self.menu_action("Split View").trigger()
        self.assertFalse(self.window._split_view_enabled)
        self.assertTrue(self.window.split_editor.isHidden())

    def test_save_action_writes_the_current_file(self) -> None:
        path = self.temp_dir / "saved.txt"
        editor = self.window.current_editor()
        editor.setProperty("filePath", str(path))
        editor.setProperty("displayName", path.name)
        editor.setPlainText("saved content")
        editor.document().setModified(True)

        self.menu_action("Save").trigger()

        self.assertEqual(path.read_text(encoding="utf-8"), "saved content")
        self.assertFalse(editor.document().isModified())

    def test_save_as_suggests_the_untitled_text_as_filename(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("Meeting notes\nsecond line")
        editor.document().setModified(True)
        output_path = self.temp_dir / "Meeting notes"

        with patch.object(
            subtext_app.QFileDialog,
            "getSaveFileName",
            return_value=(str(output_path), "All Files (*)"),
        ) as save_dialog:
            self.menu_action("Save As…").trigger()

        self.assertEqual(save_dialog.call_args.args[2], str(self.window.project_root / "Meeting notes"))
        self.assertEqual(output_path.read_text(encoding="utf-8"), "Meeting notes\nsecond line")

    def test_save_with_encoding_writes_bytes_using_selected_codec(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("café €")
        editor.document().setModified(True)
        output_path = self.temp_dir / "encoding-check.txt"
        save_encoding_menu = self.submenu("Save with Encoding")
        windows_1252 = next(
            action for action in save_encoding_menu.actions() if "1252" in action.text()
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
            self.submenu("Save with Encoding").actions()[3].trigger()

        self.assertEqual(editor.property("fileEncoding"), "utf-8")
        self.assertTrue(editor.document().isModified())
        self.assertFalse(editor.property("filePath"))

    def test_save_all_writes_every_modified_named_tab(self) -> None:
        first_path = self.temp_dir / "first.txt"
        second_path = self.temp_dir / "second.txt"
        first = self.window.current_editor()
        first.setProperty("filePath", str(first_path))
        first.setPlainText("first")
        first.document().setModified(True)
        second = self.window.create_editor(second_path.name)
        second.setProperty("filePath", str(second_path))
        second.setPlainText("second")
        second.document().setModified(True)

        self.menu_action("Save All").trigger()

        self.assertEqual(first_path.read_text(encoding="utf-8"), "first")
        self.assertEqual(second_path.read_text(encoding="utf-8"), "second")
        self.assertFalse(first.document().isModified())
        self.assertFalse(second.document().isModified())

    def test_print_action_opens_dialog_and_prints_when_accepted(self) -> None:
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        with (
            patch.object(subtext_app, "QPrintDialog", return_value=dialog),
            patch.object(subtext_app.CodeEditor, "print_", autospec=True) as print_mock,
        ):
            self.menu_action("Print…").trigger()

        dialog.exec.assert_called_once_with()
        print_mock.assert_called_once()

    def test_new_window_action_creates_another_window(self) -> None:
        previous = set(getattr(application, "_subtext_windows", []))

        self.menu_action("New Window").trigger()
        application.processEvents()

        new_windows = [
            window
            for window in getattr(application, "_subtext_windows", [])
            if window not in previous
        ]
        self.assertEqual(len(new_windows), 1)
        self.assertTrue(new_windows[0].isVisible())
        new_windows[0].close()

    def test_close_window_action_closes_the_window(self) -> None:
        self.menu_action("Close Window").trigger()

        self.assertFalse(self.window.isVisible())

    def test_close_file_action_closes_the_active_tab(self) -> None:
        self.menu_action("Close File").trigger()

        self.assertEqual(self.window.tabs.count(), 1)
        self.assertEqual(self.window.current_editor().property("displayName"), "Untitled")

    def test_close_file_cancel_keeps_modified_tab_open(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("unsaved")
        editor.document().setModified(True)
        with patch.object(
            subtext_app.QMessageBox,
            "question",
            return_value=subtext_app.QMessageBox.StandardButton.Cancel,
        ) as question:
            self.menu_action("Close File").trigger()

        question.assert_called_once()
        self.assertEqual(self.window.tabs.count(), 1)
        self.assertIs(self.window.current_editor(), editor)

    def test_revert_file_restores_saved_contents(self) -> None:
        path = self.temp_dir / "revert.txt"
        path.write_text("disk contents", encoding="utf-8")
        self.window.open_path(str(path))
        editor = self.window.current_editor()
        editor.setPlainText("unsaved changes")
        editor.document().setModified(True)
        with patch.object(
            subtext_app.QMessageBox,
            "question",
            return_value=subtext_app.QMessageBox.StandardButton.Yes,
        ):
            self.menu_action("Revert File").trigger()

        self.assertEqual(editor.toPlainText(), "disk contents")
        self.assertFalse(editor.document().isModified())

    def test_close_all_files_closes_every_tab(self) -> None:
        self.window.new_file()
        self.assertEqual(self.window.tabs.count(), 2)

        self.menu_action("Close All Files").trigger()

        self.assertEqual(self.window.tabs.count(), 0)

    def test_quit_action_closes_all_windows(self) -> None:
        self.menu_action("Quit").trigger()

        self.assertFalse(self.window.isVisible())


class EditMenuBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = tempfile.TemporaryDirectory(dir=_test_home.name)
        self.addCleanup(self.temp_home.cleanup)
        self.window = subtext_app.MainWindow([], start_new=True)
        self.window.settings.clear()
        self.window.show()
        application.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        application.clipboard().clear()
        application.processEvents()

    def edit_menu(self):
        return self.window.menuBar().actions()[1].menu()

    def submenu(self, parent, label: str):
        return next(action.menu() for action in parent.actions() if action.text() == label)

    def action(self, parent, label: str):
        return next(action for action in parent.actions() if action.text() == label)

    @staticmethod
    def select_all(editor) -> None:
        editor.selectAll()
        application.processEvents()

    def test_undo_and_redo_restore_edits(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("base")
        cursor = editor.textCursor()
        cursor.movePosition(subtext_app.QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        editor.insertPlainText(" changed")

        self.action(self.edit_menu(), "Undo").trigger()
        self.assertEqual(editor.toPlainText(), "base")
        self.action(self.edit_menu(), "Redo").trigger()
        self.assertEqual(editor.toPlainText(), "base changed")

    def test_copy_cut_and_paste_work_through_menu(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("clipboard text")
        self.select_all(editor)

        self.action(self.edit_menu(), "Copy").trigger()
        self.assertEqual(application.clipboard().text(), "clipboard text")
        self.action(self.edit_menu(), "Cut").trigger()
        self.assertEqual(editor.toPlainText(), "")
        self.action(self.edit_menu(), "Paste").trigger()
        self.assertEqual(editor.toPlainText(), "clipboard text")

    def test_copy_as_html_copies_the_selected_text(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("selected html")
        self.select_all(editor)

        self.action(self.edit_menu(), "Copy as HTML").trigger()

        # Offscreen Qt exposes the selected text as plain text for stable tests.
        self.assertEqual(application.clipboard().text(), "selected html")

    def test_paste_and_indent_indents_continuation_lines(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("    existing\n    ")
        cursor = editor.textCursor()
        cursor.movePosition(subtext_app.QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        application.clipboard().setText("alpha\nbeta")
        application.processEvents()
        paste_indent = self.action(self.edit_menu(), "Paste and Indent")
        self.assertTrue(paste_indent.isEnabled())

        paste_indent.trigger()

        self.assertEqual(editor.toPlainText(), "    existing\n    alpha\n    beta")

    def test_paste_from_history_inserts_the_selected_entry(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("start ")
        cursor = editor.textCursor()
        cursor.movePosition(subtext_app.QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        self.window.clipboard_history = ["from history"]
        history_menu = self.submenu(self.edit_menu(), "Paste from History")
        history_menu.aboutToShow.emit()

        self.action(history_menu, "from history").trigger()

        self.assertEqual(editor.toPlainText(), "start from history")

    def test_line_indent_unindent_and_reindent(self) -> None:
        editor = self.window.current_editor()
        line_menu = self.submenu(self.edit_menu(), "Line")
        editor.setPlainText("first\nsecond")
        self.select_all(editor)
        self.action(line_menu, "Indent").trigger()
        self.assertEqual(editor.toPlainText(), "    first\n    second")

        self.select_all(editor)
        self.action(line_menu, "Unindent").trigger()
        self.assertEqual(editor.toPlainText(), "first\nsecond")

        editor.setPlainText("\tfirst\n\tsecond")
        self.select_all(editor)
        self.action(line_menu, "Reindent").trigger()
        self.assertEqual(editor.toPlainText(), "    first\n    second")

    def test_line_swap_duplicate_delete_and_join(self) -> None:
        editor = self.window.current_editor()
        line_menu = self.submenu(self.edit_menu(), "Line")
        editor.setPlainText("one\ntwo\nthree")
        cursor = editor.textCursor()
        cursor.setPosition(editor.document().findBlockByNumber(1).position())
        editor.setTextCursor(cursor)
        self.action(line_menu, "Swap Line Up").trigger()
        self.assertEqual(editor.toPlainText(), "two\none\nthree")

        editor.setPlainText("one\ntwo\nthree")
        cursor = editor.textCursor()
        cursor.setPosition(editor.document().findBlockByNumber(0).position())
        editor.setTextCursor(cursor)
        self.action(line_menu, "Swap Line Down").trigger()
        self.assertEqual(editor.toPlainText(), "two\none\nthree")

        editor.setPlainText("one\ntwo")
        self.action(line_menu, "Duplicate Line").trigger()
        self.assertEqual(editor.toPlainText(), "one\none\ntwo")
        cursor = editor.textCursor()
        cursor.setPosition(0)
        editor.setTextCursor(cursor)
        self.action(line_menu, "Delete Line").trigger()
        self.assertEqual(editor.toPlainText(), "one\ntwo")

        editor.setPlainText("one\ntwo")
        self.select_all(editor)
        self.action(line_menu, "Join Lines").trigger()
        self.assertEqual(editor.toPlainText(), "one two")

    def test_comment_submenu_toggles_line_and_block_comments(self) -> None:
        editor = self.window.current_editor()
        comment_menu = self.submenu(self.edit_menu(), "Comment")
        editor.setProperty("displayName", "sample.py")
        editor.setPlainText("print('hello')\n    value = 1")
        self.select_all(editor)
        self.window.update_edit_actions()

        self.action(comment_menu, "Toggle Comment").trigger()
        self.assertEqual(editor.toPlainText(), "# print('hello')\n    # value = 1")
        self.select_all(editor)
        self.action(comment_menu, "Toggle Comment").trigger()
        self.assertEqual(editor.toPlainText(), "print('hello')\n    value = 1")

        editor.setProperty("displayName", "sample.js")
        editor.setPlainText("const x = 1;")
        self.select_all(editor)
        self.action(comment_menu, "Toggle Block Comment").trigger()
        self.assertEqual(editor.toPlainText(), "/*const x = 1;*/")

    def test_text_submenu_edits_line_edges_and_transposes(self) -> None:
        editor = self.window.current_editor()
        text_menu = self.submenu(self.edit_menu(), "Text")
        editor.setPlainText("hello world")
        cursor = editor.textCursor()
        cursor.setPosition(5)
        editor.setTextCursor(cursor)
        self.action(text_menu, "Delete to End").trigger()
        self.assertEqual(editor.toPlainText(), "hello")

        editor.setPlainText("hello")
        cursor = editor.textCursor()
        cursor.setPosition(2)
        editor.setTextCursor(cursor)
        self.action(text_menu, "Delete to Beginning").trigger()
        self.assertEqual(editor.toPlainText(), "llo")

        editor.setPlainText("hello world")
        cursor = editor.textCursor()
        cursor.setPosition(0)
        editor.setTextCursor(cursor)
        self.action(text_menu, "Delete Word Forward").trigger()
        self.assertEqual(editor.toPlainText(), "world")

        editor.setPlainText("hello world")
        cursor = editor.textCursor()
        cursor.movePosition(subtext_app.QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        self.action(text_menu, "Delete Word Backward").trigger()
        self.assertEqual(editor.toPlainText(), "hello ")

        editor.setPlainText("ab")
        cursor = editor.textCursor()
        cursor.setPosition(2)
        editor.setTextCursor(cursor)
        self.action(text_menu, "Transpose").trigger()
        self.assertEqual(editor.toPlainText(), "ba")

        editor.setPlainText("alpha")
        cursor = editor.textCursor()
        cursor.setPosition(0)
        editor.setTextCursor(cursor)
        self.action(text_menu, "Insert Line Before").trigger()
        self.assertEqual(editor.toPlainText(), "\nalpha")

        editor.setPlainText("alpha")
        cursor = editor.textCursor()
        cursor.movePosition(subtext_app.QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        self.action(text_menu, "Insert Line After").trigger()
        self.assertEqual(editor.toPlainText(), "alpha\n")

    def test_convert_case_submenu_transforms_line_or_selection(self) -> None:
        editor = self.window.current_editor()
        convert_menu = self.submenu(self.edit_menu(), "Convert Case")
        cases = (
            ("Title Case", "hello world", "Hello World"),
            ("Upper Case", "hello world", "HELLO WORLD"),
            ("Lower Case", "Hello WORLD", "hello world"),
            ("Swap Case", "Hello World", "hELLO wORLD"),
            ("lowerCamelCase", "hello world", "helloWorld"),
            ("UpperCamelCase", "hello world", "HelloWorld"),
            ("snake_case", "helloWorld", "hello_world"),
            ("kebab-case", "helloWorld", "hello-world"),
        )
        for label, source, expected in cases:
            with self.subTest(command=label):
                editor.setPlainText(source)
                self.action(convert_menu, label).trigger()
                self.assertEqual(editor.toPlainText(), expected)

    def test_wrap_submenu_wraps_paragraph_to_selected_width(self) -> None:
        editor = self.window.current_editor()
        source = " ".join(["word"] * 30)
        wrap_menu = self.submenu(self.edit_menu(), "Wrap")

        for label, width in (
            ("Wrap Paragraph", 80),
            ("Wrap paragraph at 70 characters", 70),
            ("Wrap paragraph at 72 characters", 72),
            ("Wrap paragraph at 78 characters", 78),
            ("Wrap paragraph at 80 characters", 80),
            ("Wrap paragraph at 100 characters", 100),
            ("Wrap paragraph at 120 characters", 120),
        ):
            with self.subTest(command=label):
                editor.setPlainText(source)
                self.action(wrap_menu, label).trigger()
                self.assertEqual(" ".join(editor.toPlainText().split()), source)
                self.assertLessEqual(max(map(len, editor.toPlainText().splitlines())), width)

    def test_sort_and_permute_lines(self) -> None:
        editor = self.window.current_editor()
        editor.setPlainText("b\nB\na")
        self.select_all(editor)
        self.action(self.edit_menu(), "Sort Lines").trigger()
        self.assertEqual(editor.toPlainText(), "a\nb\nB")

        editor.setPlainText("b\nB\na")
        self.select_all(editor)
        self.action(self.edit_menu(), "Sort Lines (Case Sensitive)").trigger()
        self.assertEqual(editor.toPlainText(), "B\na\nb")

        editor.setPlainText("a\nb\nc")
        self.select_all(editor)
        reverse_menu = self.submenu(self.edit_menu(), "Permute Lines")
        self.action(reverse_menu, "Reverse").trigger()
        self.assertEqual(editor.toPlainText(), "c\nb\na")

        editor.setPlainText("a\nb\na")
        self.select_all(editor)
        self.action(reverse_menu, "Unique").trigger()
        self.assertEqual(editor.toPlainText(), "a\nb")

        editor.setPlainText("a\nb\nc")
        self.select_all(editor)
        with patch.object(subtext_app.random, "shuffle", side_effect=lambda lines: lines.reverse()):
            self.action(reverse_menu, "Shuffle").trigger()
        self.assertEqual(editor.toPlainText(), "c\nb\na")

    def test_unimplemented_edit_groups_are_explicitly_disabled(self) -> None:
        edit_menu = self.edit_menu()
        undo_selection = self.submenu(edit_menu, "Undo Selection")
        self.assertTrue(all(not action.isEnabled() for action in undo_selection.actions()))
        for label in ("Tag", "Mark", "Code Folding", "Permute Selections"):
            with self.subTest(menu=label):
                self.assertFalse(self.submenu(edit_menu, label).isEnabled())
        self.assertFalse(self.action(edit_menu, "Show Completions").isEnabled())


if __name__ == "__main__":
    unittest.main()

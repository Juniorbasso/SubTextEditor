#!/usr/bin/env python3
"""SubText, a small native Qt editor prototype for Linux desktops."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import html
import json
import math
import os
import random
import re
import shlex
import shutil
import sys
import textwrap
from pathlib import Path

from PySide6.QtCore import (
    QDir,
    QEvent,
    QMimeData,
    QProcess,
    QProcessEnvironment,
    QRegularExpression,
    QRect,
    QSize,
    QTimer,
    Qt,
    QSettings,
    Signal,
    QUrl,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFileSystemModel,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStatusBar,
    QStackedWidget,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path(__file__).resolve().parent


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        self.editor.paint_line_number_area(event)


class PythonHighlighter(QSyntaxHighlighter):
    """A deliberately small Python highlighter for the first native prototype."""

    def __init__(self, document, colors: dict[str, str] | None = None) -> None:
        super().__init__(document)
        colors = colors or {
            "keyword": "#f92672", "comment": "#7f9f7f", "number": "#ae81ff",
            "string": "#e6db74", "builtin": "#66d9ef",
        }
        keyword = QTextCharFormat()
        keyword.setForeground(QColor(colors.get("keyword", "#f92672")))
        keyword.setFontWeight(QFont.Weight.Bold)
        self.rules = [
            (
                QRegularExpression(
                    r"\b(and|as|assert|async|await|break|class|continue|def|del|"
                    r"elif|else|except|False|finally|for|from|global|if|import|"
                    r"in|is|lambda|None|nonlocal|not|or|pass|raise|return|True|"
                    r"try|while|with|yield)\b"
                ),
                keyword,
            ),
            (QRegularExpression(r"#[^\n]*"), self.make_format(colors.get("comment", "#7f9f7f"))),
            (
                QRegularExpression(r"\b[0-9]+(?:\.[0-9]+)?\b"),
                self.make_format(colors.get("number", "#ae81ff")),
            ),
            (
                QRegularExpression(r"\"([^\"\\]|\\.)*\"|'([^'\\]|\\.)*'"),
                self.make_format(colors.get("string", "#e6db74")),
            ),
            (
                QRegularExpression(r"\b(print|len|range|str|int|float|list|dict)\b"),
                self.make_format(colors.get("builtin", "#66d9ef")),
            ),
        ]

    @staticmethod
    def make_format(color: str) -> QTextCharFormat:
        result = QTextCharFormat()
        result.setForeground(QColor(color))
        return result

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt virtual method
        for expression, style in self.rules:
            match = expression.globalMatch(text)
            while match.hasNext():
                found = match.next()
                self.setFormat(found.capturedStart(), found.capturedLength(), style)


class CodeEditor(QPlainTextEdit):
    focused = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.multi_selection_ranges: list[tuple[int, int]] = []
        self.mouse_binding_handler = None
        self.line_number_area = LineNumberArea(self)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(11)
        self.setFont(font)
        self.tab_size = 4
        self.translate_tabs_to_spaces = False
        self.word_wrap = True
        self.word_wrap_column = 0
        self.ruler_columns: list[int] = []
        self.bookmark_cursors: list[QTextCursor] = []
        self.set_tab_size(self.tab_size)
        self.set_word_wrap(True)
        self.setStyleSheet(
            "QPlainTextEdit { background:#303943; color:#d8dee9; border:0; "
            "selection-background-color:#435363; padding:10px 12px 10px 4px; }"
        )
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.update_line_number_area_width(0)

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().focusInEvent(event)
        self.focused.emit()

    @staticmethod
    def python_to_qt_position(text: str, position: int) -> int:
        return len(text[:position].encode("utf-16-le")) // 2

    @staticmethod
    def qt_to_python_position(text: str, position: int) -> int:
        qt_position = 0
        for index, char in enumerate(text):
            qt_position += len(char.encode("utf-16-le")) // 2
            if qt_position >= position:
                return index + 1
        return len(text)

    def setTextCursor(self, cursor: QTextCursor) -> None:  # noqa: N802 - Qt API
        self.clear_multi_selections()
        super().setTextCursor(cursor)

    def setPlainText(self, text: str) -> None:  # noqa: N802 - Qt API
        self.clear_multi_selections()
        super().setPlainText(text)

    def clear_multi_selections(self) -> None:
        self.multi_selection_ranges = []
        if hasattr(self, "document"):
            self.setExtraSelections([])

    def set_multi_selections(
        self, ranges: list[tuple[int, int]], primary_index: int = -1
    ) -> None:
        text = self.toPlainText()
        normalized = sorted(
            {
                (max(0, min(start, len(text))), max(0, min(end, len(text))))
                for start, end in ranges
            }
        )
        if not normalized:
            self.clear_multi_selections()
            return
        primary = normalized[primary_index]
        self.multi_selection_ranges = normalized
        extra = []
        for start, end in normalized:
            if (start, end) == primary:
                continue
            selection = QTextEdit.ExtraSelection()
            cursor = QTextCursor(self.document())
            cursor.setPosition(self.python_to_qt_position(text, start))
            cursor.setPosition(
                self.python_to_qt_position(text, end), QTextCursor.MoveMode.KeepAnchor
            )
            selection.cursor = cursor
            selection.format.setBackground(QColor("#506276"))
            extra.append(selection)
        self.setExtraSelections(extra)
        cursor = QTextCursor(self.document())
        cursor.setPosition(self.python_to_qt_position(text, primary[0]))
        cursor.setPosition(
            self.python_to_qt_position(text, primary[1]), QTextCursor.MoveMode.KeepAnchor
        )
        QPlainTextEdit.setTextCursor(self, cursor)

    def replace_multi_selections(self, replacement: str | list[str], *, cut: bool = False) -> None:
        text = self.toPlainText()
        ranges = self.multi_selection_ranges
        if cut and not any(start != end for start, end in ranges):
            return
        edits = []
        replacements = replacement if isinstance(replacement, list) else [replacement] * len(ranges)
        for (start, end), value in zip(ranges, replacements):
            if start == end and cut:
                continue
            edits.append((start, end, value))
        if not edits:
            return
        document = self.document()
        document_cursor = QTextCursor(document)
        document_cursor.beginEditBlock()
        for start, end, value in sorted(edits, reverse=True):
            cursor = QTextCursor(document)
            cursor.setPosition(self.python_to_qt_position(text, start))
            cursor.setPosition(
                self.python_to_qt_position(text, end), QTextCursor.MoveMode.KeepAnchor
            )
            cursor.insertText(value)
        document_cursor.endEditBlock()

        carets = []
        for start, end, value in edits:
            shift = sum(
                len(other_value) - (other_end - other_start)
                for other_start, other_end, other_value in edits
                if other_start < start
            )
            caret = start + shift + len(value)
            carets.append((caret, caret))
        normalized = sorted(set(carets))
        self.set_multi_selections(normalized, len(normalized) - 1)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        if not self.multi_selection_ranges:
            if (
                event.key() == Qt.Key.Key_Tab
                and self.translate_tabs_to_spaces
                and not event.modifiers()
            ):
                column = self.textCursor().positionInBlock()
                spaces = self.tab_size - (column % self.tab_size)
                self.insertPlainText(" " * spaces)
                event.accept()
                return
            super().keyPressEvent(event)
            return
        key = event.key()
        modifiers = event.modifiers()
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        meta = bool(modifiers & Qt.KeyboardModifier.MetaModifier)
        text = ""
        if control and key == Qt.Key.Key_C:
            source = self.toPlainText()
            selected = [source[start:end] for start, end in self.multi_selection_ranges if start != end]
            if selected:
                QApplication.clipboard().setText("\n".join(selected))
            event.accept()
            return
        if control and key == Qt.Key.Key_X:
            source = self.toPlainText()
            selected = [source[start:end] for start, end in self.multi_selection_ranges if start != end]
            if selected:
                QApplication.clipboard().setText("\n".join(selected))
                self.replace_multi_selections("", cut=True)
            event.accept()
            return
        if control and key == Qt.Key.Key_V:
            text = QApplication.clipboard().text()
        elif key == Qt.Key.Key_Backspace:
            source = self.toPlainText()
            ranges = [
                (start - 1, end) if start == end and start > 0 else (start, end)
                for start, end in self.multi_selection_ranges
            ]
            self.multi_selection_ranges = ranges
            text = ""
        elif key == Qt.Key.Key_Delete:
            source = self.toPlainText()
            ranges = [
                (start, end + 1) if start == end and end < len(source) else (start, end)
                for start, end in self.multi_selection_ranges
            ]
            self.multi_selection_ranges = ranges
            text = ""
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            text = "\n"
        elif key == Qt.Key.Key_Tab:
            if self.translate_tabs_to_spaces:
                source = self.toPlainText()
                text = [
                    " " * (self.tab_size - (start - source.rfind("\n", 0, start) - 1) % self.tab_size)
                    for start, _end in self.multi_selection_ranges
                ]
            else:
                text = "\t"
        elif not control and not alt and not meta and event.text():
            text = event.text()
        else:
            self.clear_multi_selections()
            super().keyPressEvent(event)
            return
        self.replace_multi_selections(text)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        if self.mouse_binding_handler is not None and self.mouse_binding_handler(event):
            event.accept()
            return
        self.clear_multi_selections()
        super().mousePressEvent(event)

    def set_tab_size(self, columns: int) -> None:
        self.tab_size = max(1, columns)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * self.tab_size)

    def update_viewport_margins(self) -> None:
        left = self.line_number_area_width()
        right = 0
        if self.word_wrap and self.word_wrap_column > 0:
            # QPlainTextEdit only wraps to its widget width, so reserve the
            # unused space on the right to make that width match the column.
            available = max(0, self.width() - left - 2 * self.frameWidth())
            target = self.fontMetrics().horizontalAdvance(" ") * self.word_wrap_column
            right = max(0, available - target)
        self.setViewportMargins(left, 0, right, 0)

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _new_block_count: int) -> None:
        self.update_viewport_margins()

    def update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(), self.line_number_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().resizeEvent(event)
        self.update_viewport_margins()
        content = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(content.left(), content.top(), self.line_number_area_width(), content.height())
        )

    def paint_line_number_area(self, event) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(str(self.property("gutterColor") or "#28323c")))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                if any(cursor.blockNumber() == number for cursor in self.bookmark_cursors):
                    painter.setPen(QColor("#f0a04b"))
                    painter.setBrush(QColor("#f0a04b"))
                    marker_y = top + self.fontMetrics().height() // 2
                    painter.drawEllipse(3, marker_y - 3, 6, 6)
                painter.setPen(QColor(str(self.property("gutterForeground") or "#8996a5")))
                painter.drawText(
                    0,
                    top,
                    self.line_number_area.width() - 7,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    def set_word_wrap(self, enabled: bool, column: int = 0) -> None:
        self.word_wrap = enabled
        self.word_wrap_column = column
        if not enabled:
            self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        elif column > 0:
            self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        else:
            self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.update_viewport_margins()
        self.viewport().update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().paintEvent(event)
        if not self.ruler_columns:
            return
        painter = QPainter(self.viewport())
        pen = painter.pen()
        pen.setColor(QColor(str(self.property("rulerColor") or "#52606c")))
        pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(pen)
        char_width = self.fontMetrics().horizontalAdvance(" ")
        x_origin = 5
        for column in self.ruler_columns:
            x = x_origin + char_width * column
            painter.drawLine(x, 0, x, self.viewport().height())


class CommandPalette(QDialog):
    def __init__(self, actions: list[QAction], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Command Palette")
        self.setModal(True)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.query = QLineEdit(self)
        self.query.setPlaceholderText("Type a command")
        self.query.installEventFilter(self)
        self.results = QListWidget(self)
        layout.addWidget(self.query)
        layout.addWidget(self.results)
        self.actions_by_item: dict[QListWidgetItem, QAction] = {}
        for action in actions:
            item = QListWidgetItem(action.text().replace("&", ""))
            shortcut = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            if shortcut:
                item.setToolTip(shortcut)
            self.results.addItem(item)
            self.actions_by_item[item] = action
        self.query.textChanged.connect(self.filter_items)
        self.results.itemActivated.connect(self.run_item)
        self.query.returnPressed.connect(self.run_current)
        self.query.textChanged.emit("")

    def filter_items(self, query: str) -> None:
        needle = query.casefold()
        first_visible = None
        for index in range(self.results.count()):
            item = self.results.item(index)
            item.setHidden(needle not in item.text().casefold())
            if not item.isHidden() and first_visible is None:
                first_visible = item
        if first_visible is not None:
            self.results.setCurrentItem(first_visible)

    def run_current(self) -> None:
        item = self.results.currentItem()
        if item is not None and not item.isHidden():
            self.run_item(item)

    def run_item(self, item: QListWidgetItem) -> None:
        action = self.actions_by_item.get(item)
        self.accept()
        if action is not None:
            action.trigger()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt virtual method
        if watched is self.query and event.type() == QEvent.Type.KeyPress:
            visible = [
                index
                for index in range(self.results.count())
                if not self.results.item(index).isHidden()
            ]
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and visible:
                current = self.results.currentRow()
                step = 1 if event.key() == Qt.Key.Key_Down else -1
                try:
                    position = visible.index(current)
                except ValueError:
                    position = -1 if step > 0 else 0
                self.results.setCurrentRow(visible[(position + step) % len(visible)])
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(watched, event)


class QuickAccessDialog(QDialog):
    """Searchable picker used by the Goto menu for files and symbols."""

    def __init__(
        self,
        title: str,
        placeholder: str,
        entries: list[tuple[str, str, object]],
        parent: QWidget,
        initial_query: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(620, 420)
        layout = QVBoxLayout(self)
        self.query = QLineEdit(self)
        self.query.setPlaceholderText(placeholder)
        self.query.installEventFilter(self)
        self.results = QListWidget(self)
        layout.addWidget(self.query)
        layout.addWidget(self.results, 1)
        for label, detail, value in entries:
            item = QListWidgetItem(f"{label}    {detail}" if detail else label)
            item.setToolTip(detail)
            item.setData(Qt.ItemDataRole.UserRole, value)
            self.results.addItem(item)
        self.query.textChanged.connect(self.filter_items)
        self.results.itemActivated.connect(lambda item: self.accept_item(item))
        self.query.returnPressed.connect(self.accept_current)
        self.selected_value: object | None = None
        self.query.setText(initial_query)
        self.filter_items(initial_query)
        self.query.setFocus()

    @staticmethod
    def fuzzy_match(query: str, candidate: str) -> bool:
        candidate = candidate.casefold()
        for term in query.casefold().split():
            if term in candidate:
                continue
            position = 0
            for char in term:
                position = candidate.find(char, position)
                if position < 0:
                    return False
                position += 1
        return True

    def filter_items(self, query: str) -> None:
        match_query = re.sub(r":\d+(?::\d+)?\s*$", "", query).strip()
        if match_query.startswith(("@", "#")):
            match_query = ""
        first_visible = None
        for index in range(self.results.count()):
            item = self.results.item(index)
            item.setHidden(
                not self.fuzzy_match(match_query, f"{item.text()} {item.toolTip()}")
            )
            if not item.isHidden() and first_visible is None:
                first_visible = item
        if first_visible is not None:
            self.results.setCurrentItem(first_visible)

    def accept_current(self) -> None:
        item = self.results.currentItem()
        if item is not None and not item.isHidden():
            self.accept_item(item)

    def accept_item(self, item: QListWidgetItem) -> None:
        self.selected_value = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt virtual method
        if watched is self.query and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.reject()
                return True
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                visible = [
                    i for i in range(self.results.count()) if not self.results.item(i).isHidden()
                ]
                if visible:
                    current = self.results.currentRow()
                    step = 1 if event.key() == Qt.Key.Key_Down else -1
                    try:
                        position = visible.index(current)
                    except ValueError:
                        position = -1 if step > 0 else 0
                    self.results.setCurrentRow(visible[(position + step) % len(visible)])
                    return True
        return super().eventFilter(watched, event)


class MainWindow(QMainWindow):
    def __init__(self, paths: list[str], start_new: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("SubText Editor")
        self.setWindowIcon(QIcon(str(APP_DIR / "icon.svg")))
        self.resize(1280, 820)
        self.project_root = Path.home()
        self.project_root_explicit = False
        self.project_folders: list[Path] = []
        self.project_file_path: Path | None = None
        self.project_definition: dict = {}
        self.navigation_back: list[tuple[CodeEditor, int, int]] = []
        self.navigation_forward: list[tuple[CodeEditor, int, int]] = []
        self.tab_size = 4
        self._menu_hidden = False
        self._tabs_hidden = False
        self._status_hidden = False
        self._distraction_free_state: dict[str, object] | None = None
        self.view_tab_size_actions: dict[int, QAction] = {}
        self.syntax_actions: dict[str, QAction] = {}
        self.line_ending_actions: dict[str, QAction] = {}
        self.wrap_column_actions: dict[int, QAction] = {}
        self.ruler_actions: dict[int, QAction] = {}
        self.spaces_action: QAction | None = None
        self.word_wrap_action: QAction | None = None
        self.user_data_dir = Path.home() / ".config" / "subtext-editor"
        self.session_file = self.user_data_dir / "Session.json"
        self._restoring_session = False
        self.session_save_timer = QTimer(self)
        self.session_save_timer.setSingleShot(True)
        self.session_save_timer.setInterval(450)
        self.session_save_timer.timeout.connect(self.save_session_state)
        self.packages_dir = self.user_data_dir / "Packages"
        self.user_package_dir = self.packages_dir / "User"
        self.user_settings_file = self.user_package_dir / "Preferences.sublime-settings"
        self.build_system_dir = self.user_data_dir / "Build Systems"
        self.snippet_dir = self.user_data_dir / "Snippets"
        self.macro_dir = self.user_data_dir / "Macros"
        self.selected_build_system: str | None = None
        self.build_config: dict = {}
        self.build_output_buffer = ""
        self.build_output_partial_line = ""
        self.build_results: list[dict] = []
        self.macro_recording = False
        self.macro_editor: CodeEditor | None = None
        self.macro_last_text = ""
        self.macro_origin = 0
        self.macro_steps: list[dict[str, object]] = []
        self.last_macro_steps: list[dict[str, object]] = []
        self.settings = QSettings()
        saved_recent_files = self.settings.value("recent_files", [])
        if isinstance(saved_recent_files, str):
            self.recent_files = [saved_recent_files]
        elif isinstance(saved_recent_files, (list, tuple)):
            self.recent_files = list(saved_recent_files)
        else:
            self.recent_files = []
        saved_recent_projects = self.settings.value("recent_projects", [])
        if isinstance(saved_recent_projects, str):
            self.recent_projects = [saved_recent_projects]
        elif isinstance(saved_recent_projects, (list, tuple)):
            self.recent_projects = list(saved_recent_projects)
        else:
            self.recent_projects = []
        self.global_preferences = self.read_json_object(self.user_settings_file)
        has_preferences_file = self.user_settings_file.is_file()
        self.editor_font_size = self.safe_int(
            self.global_preferences.get(
                "font_size", 11 if has_preferences_file else self.settings.value("font_size", 11, type=int)
            ),
            11, 6, 48,
        )
        self.editor_font_family = str(
            self.global_preferences.get(
                "font_face",
                self.settings.value(
                    "font_face",
                    QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family(),
                ) if not has_preferences_file else QFontDatabase.systemFont(
                    QFontDatabase.SystemFont.FixedFont
                ).family(),
            )
        )
        self.tab_size = self.safe_int(
            self.global_preferences.get(
                "tab_size", 4 if has_preferences_file else self.settings.value("tab_size", self.tab_size, type=int)
            ),
            4, 1, 16,
        )
        self.global_translate_tabs = bool(
            self.global_preferences.get("translate_tabs_to_spaces", False)
        )
        self.global_word_wrap = bool(self.global_preferences.get("word_wrap", True))
        self.color_scheme_name = str(
            self.global_preferences.get(
                "color_scheme", "SubText Dark" if has_preferences_file else self.settings.value("color_scheme", "SubText Dark")
            )
        )
        self.theme_name = str(
            self.global_preferences.get(
                "theme", "Dark" if has_preferences_file else self.settings.value("theme", "Dark")
            )
        )
        self.color_scheme = self.resolve_color_scheme(self.color_scheme_name)
        theme_path = self.user_package_dir / "SubText.qss"
        try:
            self.custom_theme_stylesheet = theme_path.read_text(encoding="utf-8")
        except OSError:
            self.custom_theme_stylesheet = ""
        self.palette_actions: list[QAction] = []
        self.highlighters: dict[CodeEditor, PythonHighlighter] = {}
        self.user_mouse_bindings: list[dict] = []

        self.tabs = QTabWidget(self)
        self._active_editor_group = "main"
        self._split_view_enabled = False
        self.split_editor: CodeEditor | None = None
        self._split_source: CodeEditor | None = None
        self.split_view_action: QAction | None = None
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.currentChanged.connect(self.on_main_tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)

        self.file_model = QFileSystemModel(self)
        self.file_model.setFilter(
            QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden
        )
        self.file_model.setRootPath(str(self.project_root))
        self.file_tree = QTreeView(self)
        self.file_tree.setModel(self.file_model)
        self.file_tree.setRootIndex(self.file_model.index(str(self.project_root)))
        self.file_tree.setHeaderHidden(True)
        for column in range(1, 4):
            self.file_tree.hideColumn(column)
        self.file_tree.doubleClicked.connect(self.open_tree_item)

        self.open_files_list = QListWidget(self)
        self.open_files_list.itemActivated.connect(self.activate_open_file)
        self.sidebar_stack = QStackedWidget(self)
        self.sidebar_stack.addWidget(self.file_tree)
        self.sidebar_stack.addWidget(self.open_files_list)

        self.project_dock = QDockWidget("Project", self)
        self.project_dock.setObjectName("projectDock")
        self.project_dock.setWidget(self.sidebar_stack)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.project_dock)
        self.project_dock.visibilityChanged.connect(self.sync_sidebar_action)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal, central)
        self.editor_splitter.addWidget(self.tabs)
        layout.addWidget(self.editor_splitter, 1)
        self.find_bar = QWidget(central)
        find_layout = QVBoxLayout(self.find_bar)
        find_layout.setContentsMargins(8, 5, 8, 5)
        find_layout.setSpacing(4)

        self.find_row_widget = QWidget(self.find_bar)
        find_row = QHBoxLayout(self.find_row_widget)
        find_row.setContentsMargins(0, 0, 0, 0)
        self.find_input = QLineEdit(self.find_bar)
        self.find_input.setPlaceholderText("Find in current file")
        self.match_case_checkbox = QCheckBox("Aa", self.find_bar)
        self.match_case_checkbox.setToolTip("Match case")
        self.whole_word_checkbox = QCheckBox("Whole word", self.find_bar)
        self.regex_checkbox = QCheckBox(".*", self.find_bar)
        self.regex_checkbox.setToolTip("Regular expression")
        self.find_prev_button = QPushButton("Previous", self.find_bar)
        self.find_next_button = QPushButton("Next", self.find_bar)
        self.find_all_button = QPushButton("Find All", self.find_bar)
        self.close_find_button = QPushButton("Close", self.find_bar)
        find_row.addWidget(self.find_input, 1)
        find_row.addWidget(self.match_case_checkbox)
        find_row.addWidget(self.whole_word_checkbox)
        find_row.addWidget(self.regex_checkbox)
        find_row.addWidget(self.find_prev_button)
        find_row.addWidget(self.find_next_button)
        find_row.addWidget(self.find_all_button)
        find_row.addWidget(self.close_find_button)
        find_layout.addWidget(self.find_row_widget)

        self.replace_row_widget = QWidget(self.find_bar)
        replace_row = QHBoxLayout(self.replace_row_widget)
        replace_row.setContentsMargins(0, 0, 0, 0)
        self.replace_input = QLineEdit(self.find_bar)
        self.replace_input.setPlaceholderText("Replace with")
        self.replace_next_button = QPushButton("Replace Next", self.find_bar)
        self.replace_all_button = QPushButton("Replace All", self.find_bar)
        replace_row.addWidget(self.replace_input, 1)
        replace_row.addWidget(self.replace_next_button)
        replace_row.addWidget(self.replace_all_button)
        find_layout.addWidget(self.replace_row_widget)

        self.files_row_widget = QWidget(self.find_bar)
        files_row = QHBoxLayout(self.files_row_widget)
        files_row.setContentsMargins(0, 0, 0, 0)
        self.find_where_input = QLineEdit(self.find_bar)
        self.find_where_input.setPlaceholderText("Where to search (folder or file)")
        self.find_files_button = QPushButton("Find in Files", self.find_bar)
        files_row.addWidget(self.find_where_input, 1)
        files_row.addWidget(self.find_files_button)
        find_layout.addWidget(self.files_row_widget)
        self.files_replace_row_widget = QWidget(self.find_bar)
        files_replace_row = QHBoxLayout(self.files_replace_row_widget)
        files_replace_row.setContentsMargins(0, 0, 0, 0)
        self.replace_files_input = QLineEdit(self.find_bar)
        self.replace_files_input.setPlaceholderText("Replace in matching files with")
        self.replace_files_button = QPushButton("Replace All in Files", self.find_bar)
        files_replace_row.addWidget(self.replace_files_input, 1)
        files_replace_row.addWidget(self.replace_files_button)
        find_layout.addWidget(self.files_replace_row_widget)
        self.replace_row_widget.hide()
        self.files_row_widget.hide()
        self.files_replace_row_widget.hide()
        self.search_mode = "find"
        self.incremental_find_active = False
        self.incremental_find_origin = 0
        self.find_input.installEventFilter(self)
        self.replace_input.installEventFilter(self)
        self.find_where_input.installEventFilter(self)
        self.replace_files_input.installEventFilter(self)
        layout.addWidget(self.find_bar)
        self.find_bar.hide()
        self.setCentralWidget(central)

        self.find_results = []
        self.find_results_in_files = False
        self.find_results_dock = QDockWidget("Find Results", self)
        self.find_results_dock.setObjectName("findResultsDock")
        self.find_results_list = QListWidget(self.find_results_dock)
        self.find_results_dock.setWidget(self.find_results_list)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.find_results_dock)
        self.find_results_dock.hide()

        build_panel = QWidget(self)
        build_layout = QVBoxLayout(build_panel)
        build_layout.setContentsMargins(4, 4, 4, 4)
        build_layout.setSpacing(4)
        self.build_results_list = QListWidget(build_panel)
        self.build_results_list.setMaximumHeight(112)
        self.build_output_view = QPlainTextEdit(build_panel)
        self.build_output_view.setReadOnly(True)
        build_layout.addWidget(self.build_results_list)
        build_layout.addWidget(self.build_output_view, 1)
        self.build_output_dock = QDockWidget("Build Results", self)
        self.build_output_dock.setObjectName("buildResultsDock")
        self.build_output_dock.setWidget(build_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.build_output_dock)
        self.tabifyDockWidget(self.find_results_dock, self.build_output_dock)
        self.build_output_dock.hide()
        self.build_results_list.itemActivated.connect(self.open_build_result_item)

        self.developer_console_dock = QDockWidget("Developer Console", self)
        self.developer_console_dock.setObjectName("developerConsoleDock")
        self.developer_console_view = QPlainTextEdit(self.developer_console_dock)
        self.developer_console_view.setReadOnly(True)
        self.developer_console_dock.setWidget(self.developer_console_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.developer_console_dock)
        self.developer_console_dock.hide()

        self.build_process = QProcess(self)
        self.build_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.build_process.readyReadStandardOutput.connect(self.read_build_output)
        self.build_process.finished.connect(self.finish_build)
        self.build_process.errorOccurred.connect(self.build_process_error)

        self.status = QStatusBar(self)
        self.setStatusBar(self.status)
        self.cursor_label = QLabel("Line 1, Column 1", self)
        self.tab_size_label = QLabel("Tab Size: 4", self)
        self.syntax_label = QLabel("Plain Text", self)
        self.line_endings_label = QLabel("LF", self)
        self.status.addWidget(self.cursor_label, 1)
        self.status.addPermanentWidget(self.tab_size_label)
        self.status.addPermanentWidget(self.line_endings_label)
        self.status.addPermanentWidget(self.syntax_label)

        self.create_actions()
        self.create_menus()
        self.apply_application_theme()
        keymap_path = self.user_package_dir / "Default.sublime-keymap"
        if keymap_path.is_file():
            self.apply_user_key_bindings(keymap_path)
        mousemap_path = self.user_package_dir / "Default.sublime-mousemap"
        if mousemap_path.is_file():
            self.apply_user_mouse_bindings(mousemap_path)
        self.find_next_button.clicked.connect(lambda: self.find_text(False))
        self.find_prev_button.clicked.connect(lambda: self.find_text(True))
        self.find_all_button.clicked.connect(self.find_all_current_file)
        self.close_find_button.clicked.connect(self.hide_find_bar)
        self.find_input.returnPressed.connect(self.on_find_return)
        self.find_input.textChanged.connect(self.on_find_text_changed)
        self.find_where_input.returnPressed.connect(self.find_in_files)
        self.replace_input.returnPressed.connect(self.replace_next)
        self.match_case_checkbox.stateChanged.connect(
            lambda _state: self.on_find_text_changed("")
        )
        self.whole_word_checkbox.stateChanged.connect(
            lambda _state: self.on_find_text_changed("")
        )
        self.regex_checkbox.stateChanged.connect(lambda _state: self.on_find_text_changed(""))
        self.replace_next_button.clicked.connect(self.replace_next)
        self.replace_all_button.clicked.connect(self.replace_all_current_file)
        self.find_files_button.clicked.connect(self.find_in_files)
        self.replace_files_button.clicked.connect(
            lambda _checked=False: self.replace_all_in_files()
        )
        self.find_results_list.itemActivated.connect(self.open_find_result_item)

        restored_session = False
        if not start_new:
            restored_session = self.restore_session_state()
        if paths:
            for path in paths:
                self.open_path(path)
        if start_new or (not paths and not restored_session):
            self.new_file()
        self.update_window_state()

    def create_actions(self) -> None:
        self.action_new = self.make_action("New File", "Ctrl+N", self.new_file)
        self.action_open = self.make_action("Open File…", "Ctrl+O", self.open_file_dialog)
        self.action_folder = self.make_action("Open Folder…", "", self.open_folder_dialog)
        self.action_browse_packages = self.make_action(
            "Browse Packages…", "", self.browse_packages
        )
        self.action_preferences_settings = self.make_action(
            "Settings", "", self.open_preferences_settings
        )
        self.action_syntax_settings = self.make_action(
            "Settings – Syntax Specific", "", self.open_syntax_specific_settings
        )
        self.action_distraction_free_settings = self.make_action(
            "Settings – Distraction Free", "", self.open_distraction_free_settings
        )
        self.action_key_bindings = self.make_action(
            "Key Bindings", "", self.open_key_bindings
        )
        self.action_mouse_bindings = self.make_action(
            "Mouse Bindings", "", self.open_mouse_bindings
        )
        self.action_select_color_scheme = self.make_action(
            "Select Color Scheme…", "", self.select_color_scheme
        )
        self.action_customize_color_scheme = self.make_action(
            "Customize Color Scheme", "", self.customize_color_scheme
        )
        self.action_select_theme = self.make_action(
            "Select Theme…", "", self.select_theme
        )
        self.action_customize_theme = self.make_action(
            "Customize Theme", "", self.customize_theme
        )
        self.action_font_larger = self.make_action("Larger", "", lambda: self.change_font_size(1))
        self.action_font_smaller = self.make_action("Smaller", "", lambda: self.change_font_size(-1))
        self.action_font_reset = self.make_action("Reset", "", self.reset_font_size)
        self.action_open_project = self.make_action(
            "Open Project…", "", self.open_project_dialog
        )
        self.action_switch_project = self.make_action(
            "Switch Project…", "", self.switch_project
        )
        self.action_quick_switch_project = self.make_action(
            "Quick Switch Project…", "Ctrl+Alt+P", self.quick_switch_project
        )
        self.action_save_project_as = self.make_action(
            "Save Project As…", "", self.save_project_as
        )
        self.action_close_project = self.make_action(
            "Close Project", "", self.close_project
        )
        self.action_edit_project = self.make_action(
            "Edit Project", "", self.edit_project
        )
        self.action_new_workspace = self.make_action(
            "New Workspace for Project", "", self.new_workspace_for_project
        )
        self.action_save_workspace_as = self.make_action(
            "Save Workspace As…", "", self.save_workspace_as
        )
        self.action_add_folder_to_project = self.make_action(
            "Add Folder to Project…", "", self.add_folder_to_project
        )
        self.action_remove_project_folders = self.make_action(
            "Remove all Folders from Project", "", self.remove_project_folders
        )
        self.action_refresh_project_folders = self.make_action(
            "Refresh Folders", "", self.refresh_project_folders
        )
        self.action_new_window = self.make_action("New Window", "Ctrl+Shift+N", self.new_window)
        self.action_close_window = self.make_action("Close Window", "Ctrl+Shift+W", self.close)
        self.action_save = self.make_action("Save", "Ctrl+S", self.save_file)
        self.action_save_as = self.make_action("Save As…", "Ctrl+Shift+S", self.save_file_as)
        self.action_save_all = self.make_action("Save All", "", self.save_all)
        self.action_close = self.make_action("Close File", "Ctrl+W", self.close_current_tab)
        self.action_revert = self.make_action("Revert File", "", self.revert_file)
        self.action_close_all = self.make_action("Close All Files", "", self.close_all_files)
        self.action_quit = self.make_action("Quit", "Ctrl+Q", self.quit_application)
        self.action_print = self.make_action("Print…", "", self.print_current_file)
        self.action_find = self.make_action("Find…", "Ctrl+F", lambda: self.show_find_bar("find"))
        self.action_find_next = self.make_action("Find Next", "F3", lambda: self.find_text(False))
        self.action_find_prev = self.make_action("Find Previous", "Shift+F3", lambda: self.find_text(True))
        self.action_incremental_find = self.make_action(
            "Incremental Find", "Ctrl+I", self.show_incremental_find
        )
        self.action_replace = self.make_action("Replace…", "Ctrl+H", self.show_replace_bar)
        self.action_replace_next = self.make_action(
            "Replace Next", "Ctrl+Shift+H", self.replace_next
        )
        self.action_quick_find = self.make_action("Quick Find", "Ctrl+F3", self.quick_find)
        self.action_quick_find_all = self.make_action(
            "Quick Find All", "Alt+F3", self.quick_find_all
        )
        # Ctrl+D is already assigned to Expand Selection to Word in Selection.
        # Keep this menu command active without registering a duplicate shortcut.
        self.action_quick_add_next = self.make_action("Quick Add Next", "", self.quick_add_next)
        self.action_quick_skip_next = self.make_action(
            "Quick Skip Next", "Ctrl+K, Ctrl+D", self.quick_skip_next
        )
        self.action_use_selection_find = self.make_action(
            "Use Selection for Find", "Ctrl+E", lambda: self.use_selection_for("find")
        )
        self.action_use_selection_replace = self.make_action(
            "Use Selection for Replace", "Ctrl+Shift+E", lambda: self.use_selection_for("replace")
        )
        self.action_find_in_files = self.make_action(
            "Find in Files…", "Ctrl+Shift+F", self.show_find_in_files
        )
        self.action_show_find_results = self.make_action(
            "Show Results", "", self.show_find_results
        )
        self.action_next_result = self.make_action("Next Result", "", lambda: self.move_find_result(1))
        self.action_previous_result = self.make_action(
            "Previous Result", "", lambda: self.move_find_result(-1)
        )
        self.action_cancel_find = self.make_action(
            "Cancel Find in Files", "", self.cancel_find_in_files
        )
        self.action_goto = self.make_action("Goto Line…", "Ctrl+G", self.go_to_line)
        self.action_quick_open = self.make_action("Goto Anything…", "Ctrl+P", self.quick_open)
        self.action_goto_symbol = self.make_action(
            "Goto Symbol…", "Ctrl+R", self.goto_symbol
        )
        self.action_goto_symbol_project = self.make_action(
            "Goto Symbol in Project…", "Ctrl+Shift+R", self.goto_symbol_in_project
        )
        self.action_goto_definition = self.make_action(
            "Goto Definition…", "F12", self.goto_definition
        )
        self.action_goto_reference = self.make_action(
            "Goto Reference…", "Shift+F12", self.goto_reference
        )
        self.action_next_modification = self.make_action(
            "Next Modification", "Ctrl+.", lambda: None
        )
        self.action_previous_modification = self.make_action(
            "Previous Modification", "Ctrl+,", lambda: None
        )
        self.action_next_modification.setEnabled(False)
        self.action_previous_modification.setEnabled(False)
        self.action_jump_back = self.make_action(
            "Jump Back", "", self.jump_back
        )
        self.action_jump_forward = self.make_action(
            "Jump Forward", "", self.jump_forward
        )
        keypad_minus = Qt.Key.Key_Minus.value | Qt.KeyboardModifier.KeypadModifier.value
        self.action_jump_back.setShortcut(
            QKeySequence(keypad_minus | Qt.KeyboardModifier.AltModifier.value)
        )
        self.action_jump_forward.setShortcut(
            QKeySequence(
                keypad_minus
                | Qt.KeyboardModifier.AltModifier.value
                | Qt.KeyboardModifier.ShiftModifier.value
            )
        )
        self.action_jump_back.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.action_jump_forward.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.action_toggle_bookmark = self.make_action(
            "Toggle Bookmark", "Ctrl+F2", self.toggle_bookmark
        )
        self.action_next_bookmark = self.make_action(
            "Next Bookmark", "F2", lambda: self.move_bookmark(1)
        )
        self.action_previous_bookmark = self.make_action(
            "Previous Bookmark", "Shift+F2", lambda: self.move_bookmark(-1)
        )
        self.action_clear_bookmarks = self.make_action(
            "Clear Bookmarks", "", self.clear_bookmarks
        )
        self.action_jump_matching_bracket = self.make_action(
            "Jump to Matching Bracket", "Ctrl+M", self.jump_to_matching_bracket
        )
        self.action_toggle_sidebar = QAction("Toggle Side Bar", self, checkable=True)
        self.action_toggle_sidebar.toggled.connect(self.set_sidebar_visible)
        self.action_palette = self.make_action("Command Palette…", "Ctrl+Shift+P", self.show_command_palette)
        self.action_snippets = self.make_action("Snippets…", "", self.show_snippets)
        self.action_new_build_system = self.make_action(
            "New Build System…", "", self.create_build_system
        )
        self.action_build = self.make_action("Build", "Ctrl+B", self.build)
        self.action_build_with = self.make_action(
            "Build With…", "Ctrl+Shift+B", self.build_with
        )
        self.action_cancel_build = self.make_action(
            "Cancel Build", "Ctrl+Break", self.cancel_build
        )
        self.action_cancel_build.setEnabled(False)
        self.action_show_build_results = self.make_action(
            "Show Results", "", self.show_build_results
        )
        self.action_next_build_result = self.make_action(
            "Next Result", "F4", lambda: self.move_build_result(1)
        )
        self.action_previous_build_result = self.make_action(
            "Previous Result", "Shift+F4", lambda: self.move_build_result(-1)
        )
        self.action_clear_build_results = self.make_action(
            "Clear Results", "", self.clear_build_results
        )
        self.action_save_all_on_build = QAction("Save All on Build", self, checkable=True)
        self.action_save_all_on_build.setChecked(
            self.settings.value("save_all_on_build", True, type=bool)
        )
        self.action_save_all_on_build.toggled.connect(self.set_save_all_on_build)
        self.action_record_macro = self.make_action(
            "Record Macro", "Ctrl+Alt+Q", self.toggle_macro_recording
        )
        self.action_play_macro = self.make_action(
            "Playback Macro", "Ctrl+Alt+Shift+Q", self.play_last_macro
        )
        self.action_play_macro.setEnabled(False)
        self.action_save_macro = self.make_action("Save Macro…", "", self.save_macro)
        self.action_save_macro.setEnabled(False)
        self.action_developer_console = self.make_action(
            "Developer Console", "", self.show_developer_console
        )
        self.action_open_user_data = self.make_action(
            "Open User Data Folder", "", self.open_user_data_folder
        )
        self.action_inspect_buffer = self.make_action(
            "Inspect Current Buffer", "", self.inspect_current_buffer
        )
        self.action_install_package_control = self.make_action(
            "Install Package Control…", "", self.explain_package_control
        )
        self.action_undo = self.make_action("Undo", "Ctrl+Z", lambda: self.editor_call("undo"))
        self.action_redo = self.make_action("Redo", "Ctrl+Y", lambda: self.editor_call("redo"))
        self.action_cut = self.make_action("Cut", "Ctrl+X", self.cut_text)
        self.action_copy = self.make_action("Copy", "Ctrl+C", self.copy_text)
        self.action_paste = self.make_action("Paste", "Ctrl+V", self.paste_text)
        self.action_select_all = self.make_action(
            "Select All", "Ctrl+A", lambda: self.editor_call("selectAll")
        )
        self.action_split_selection_lines = self.make_action(
            "Split into Lines", "Ctrl+Shift+L", self.split_selection_into_lines
        )
        self.action_single_selection = self.make_action(
            "Single Selection", "Escape", self.single_selection
        )
        self.action_expand_smart = self.make_action(
            "Expand Selection", "Ctrl+Shift+A", lambda: self.expand_selection("smart")
        )
        self.action_expand_line = self.make_action(
            "Expand Selection to Line", "Ctrl+L", lambda: self.expand_selection("line")
        )
        self.action_expand_line_up = self.make_action(
            "Expand Selection to Line Upward", "Alt+L", lambda: self.expand_selection("line_up")
        )
        self.action_expand_word = self.make_action(
            "Expand Selection to Word", "Ctrl+D", self.quick_add_next
        )
        self.action_expand_block = self.make_action(
            "Expand Selection to Block", "", lambda: self.expand_selection("block")
        )
        self.action_expand_paragraph = self.make_action(
            "Expand Selection to Paragraph", "", lambda: self.expand_selection("paragraph")
        )
        self.action_expand_scope = self.make_action(
            "Expand Selection to Scope", "Ctrl+Shift+Space", lambda: self.expand_selection("scope")
        )
        self.action_expand_brackets = self.make_action(
            "Expand Selection to Brackets", "Ctrl+Shift+M", lambda: self.expand_selection("brackets")
        )
        self.action_expand_indentation = self.make_action(
            "Expand Selection to Indentation", "", lambda: self.expand_selection("indentation")
        )
        self.action_add_previous_line = self.make_action(
            "Add Previous Line", "Alt+Shift+Up", lambda: self.add_line_to_selection(-1)
        )
        self.action_add_next_line = self.make_action(
            "Add Next Line", "Alt+Shift+Down", lambda: self.add_line_to_selection(1)
        )
        self.action_copy_html = self.make_action("Copy as HTML", "", self.copy_as_html)
        self.action_paste_indent = self.make_action(
            "Paste and Indent", "Ctrl+Shift+V", self.paste_and_indent
        )
        self.action_indent = self.make_action("Indent", "", lambda: self.indent_lines(False))
        self.action_unindent = self.make_action("Unindent", "", lambda: self.indent_lines(True))
        self.action_reindent = self.make_action("Reindent", "", self.reindent_lines)
        self.action_swap_line_up = self.make_action("Swap Line Up", "", lambda: self.move_lines(-1))
        self.action_swap_line_down = self.make_action("Swap Line Down", "", lambda: self.move_lines(1))
        self.action_duplicate_line = self.make_action("Duplicate Line", "", self.duplicate_lines)
        self.action_delete_line = self.make_action("Delete Line", "", self.delete_lines)
        self.action_join_lines = self.make_action("Join Lines", "", self.join_lines)
        self.action_toggle_comment = self.make_action(
            "Toggle Comment", "Ctrl+/", lambda: self.toggle_comment(block=False)
        )
        self.action_toggle_block_comment = self.make_action(
            "Toggle Block Comment", "Ctrl+Shift+/", lambda: self.toggle_comment(block=True)
        )
        self.action_insert_line_before = self.make_action(
            "Insert Line Before", "", lambda: self.insert_line(before=True)
        )
        self.action_insert_line_after = self.make_action(
            "Insert Line After", "", lambda: self.insert_line(before=False)
        )
        self.action_delete_word_forward = self.make_action(
            "Delete Word Forward", "", lambda: self.delete_word(forward=True)
        )
        self.action_delete_word_backward = self.make_action(
            "Delete Word Backward", "", lambda: self.delete_word(forward=False)
        )
        self.action_delete_to_end = self.make_action(
            "Delete to End", "", lambda: self.delete_to_line_edge(end=True)
        )
        self.action_delete_to_beginning = self.make_action(
            "Delete to Beginning", "", lambda: self.delete_to_line_edge(end=False)
        )
        self.action_transpose = self.make_action("Transpose", "", self.transpose_text)
        self.action_title_case = self.make_action(
            "Title Case", "", lambda: self.convert_case("title")
        )
        self.action_upper_case = self.make_action(
            "Upper Case", "", lambda: self.convert_case("upper")
        )
        self.action_lower_case = self.make_action(
            "Lower Case", "", lambda: self.convert_case("lower")
        )
        self.action_swap_case = self.make_action(
            "Swap Case", "", lambda: self.convert_case("swap")
        )
        self.action_lower_camel = self.make_action(
            "lowerCamelCase", "", lambda: self.convert_case("lower_camel")
        )
        self.action_upper_camel = self.make_action(
            "UpperCamelCase", "", lambda: self.convert_case("upper_camel")
        )
        self.action_snake_case = self.make_action(
            "snake_case", "", lambda: self.convert_case("snake")
        )
        self.action_kebab_case = self.make_action(
            "kebab-case", "", lambda: self.convert_case("kebab")
        )
        self.action_wrap_paragraph = self.make_action(
            "Wrap Paragraph", "", lambda: self.wrap_text(80)
        )
        self.action_sort_lines = self.make_action(
            "Sort Lines", "F9", lambda: self.permute_lines("sort")
        )
        self.action_sort_lines_case = self.make_action(
            "Sort Lines (Case Sensitive)", "Ctrl+F9", lambda: self.permute_lines("sort_case")
        )
        self.action_show_completions = self.make_action(
            "Show Completions", "Ctrl+Space", self.show_completions
        )
        self.clipboard_history: list[str] = []
        self.tab_size_actions: dict[int, QAction] = {}
        self.palette_actions = [
            self.action_new,
            self.action_open,
            self.action_folder,
            self.action_new_window,
            self.action_save,
            self.action_save_as,
            self.action_save_all,
            self.action_close,
            self.action_revert,
            self.action_close_all,
            self.action_find,
            self.action_find_next,
            self.action_find_prev,
            self.action_replace,
            self.action_find_in_files,
            self.action_goto,
            self.action_quick_open,
            self.action_goto_symbol,
            self.action_goto_symbol_project,
            self.action_goto_definition,
            self.action_goto_reference,
            self.action_jump_back,
            self.action_jump_forward,
            self.action_toggle_bookmark,
            self.action_next_bookmark,
            self.action_previous_bookmark,
            self.action_clear_bookmarks,
            self.action_jump_matching_bracket,
            self.action_toggle_sidebar,
            self.action_palette,
            self.action_snippets,
            self.action_new_build_system,
            self.action_build,
            self.action_build_with,
            self.action_cancel_build,
            self.action_show_build_results,
            self.action_next_build_result,
            self.action_previous_build_result,
            self.action_clear_build_results,
            self.action_save_all_on_build,
            self.action_record_macro,
            self.action_play_macro,
            self.action_save_macro,
            self.action_developer_console,
            self.action_open_user_data,
            self.action_inspect_buffer,
            self.action_install_package_control,
        ]
        self.tabs.currentChanged.connect(self.refresh_open_files)
        self._sidebar_chord = self.make_action(
            "Toggle Side Bar", "Ctrl+K, Ctrl+B", self.toggle_sidebar
        )
        self._sidebar_chord.setShortcutContext(Qt.ShortcutContext.WindowShortcut)

    def make_action(self, label: str, shortcut: str, callback) -> QAction:
        action = QAction(label, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        action.triggered.connect(callback)
        return action

    def create_menus(self) -> None:
        menu = self.menuBar().addMenu("&File")
        for action in (self.action_new, self.action_open, self.action_folder):
            menu.addAction(action)
        self.recent_menu = menu.addMenu("Open Recent")
        self.recent_menu.aboutToShow.connect(self.populate_recent_menu)
        self.populate_recent_menu()
        reopen_menu = menu.addMenu("Reopen with Encoding")
        for label, codec in self.available_encodings():
            reopen_action = reopen_menu.addAction(label)
            reopen_action.triggered.connect(
                lambda _checked=False, value=codec: self.reopen_current_file(value)
            )
        menu.addSeparator()
        self.split_view_action = QAction("Split View", self, checkable=True)
        self.split_view_action.toggled.connect(self.toggle_split_view)
        menu.addAction(self.split_view_action)
        menu.addSeparator()
        menu.addAction(self.action_save)
        save_encoding_menu = menu.addMenu("Save with Encoding")
        for label, codec in self.available_encodings():
            save_action = save_encoding_menu.addAction(label)
            save_action.triggered.connect(
                lambda _checked=False, value=codec: self.save_with_encoding(value)
            )
        menu.addAction(self.action_save_as)
        menu.addAction(self.action_save_all)
        menu.addSeparator()
        menu.addAction(self.action_print)
        menu.addSeparator()
        menu.addAction(self.action_new_window)
        menu.addAction(self.action_close_window)
        menu.addSeparator()
        menu.addAction(self.action_close)
        menu.addAction(self.action_revert)
        menu.addAction(self.action_close_all)
        menu.addSeparator()
        menu.addAction(self.action_quit)

        menu = self.menuBar().addMenu("&Edit")
        menu.addAction(self.action_undo)
        menu.addAction(self.action_redo)
        undo_selection_menu = menu.addMenu("Undo Selection")
        for label in ("Soft Undo", "Soft Redo"):
            action = undo_selection_menu.addAction(label)
            action.setEnabled(False)
        menu.addSeparator()
        for action in (
            self.action_cut,
            self.action_copy,
            self.action_copy_html,
            self.action_paste,
            self.action_paste_indent,
        ):
            menu.addAction(action)
        paste_history_menu = menu.addMenu("Paste from History")
        paste_history_menu.aboutToShow.connect(self.populate_clipboard_history)
        menu.addSeparator()
        line_menu = menu.addMenu("Line")
        for action in (
            self.action_indent,
            self.action_unindent,
            self.action_reindent,
            self.action_swap_line_up,
            self.action_swap_line_down,
            self.action_duplicate_line,
            self.action_delete_line,
            self.action_join_lines,
        ):
            line_menu.addAction(action)
        comment_menu = menu.addMenu("Comment")
        comment_menu.addAction(self.action_toggle_comment)
        comment_menu.addAction(self.action_toggle_block_comment)
        text_menu = menu.addMenu("Text")
        for action in (
            self.action_insert_line_before,
            self.action_insert_line_after,
            self.action_delete_word_forward,
            self.action_delete_word_backward,
            self.action_delete_line,
            self.action_delete_to_end,
            self.action_delete_to_beginning,
            self.action_transpose,
        ):
            text_menu.addAction(action)
        self.add_disabled_submenu(
            menu,
            "Tag",
            ("Close Tag", "Expand Selection to Tag", "Wrap Selection With Tag"),
        )
        self.add_disabled_submenu(
            menu,
            "Mark",
            ("Set Mark", "Select to Mark", "Delete to Mark", "Swap with Mark", "Clear Mark", "Yank"),
        )
        self.add_disabled_submenu(
            menu,
            "Code Folding",
            ("Fold", "Unfold", "Unfold All", "Fold All"),
        )
        convert_menu = menu.addMenu("Convert Case")
        for action in (
            self.action_title_case,
            self.action_upper_case,
            self.action_lower_case,
            self.action_swap_case,
            self.action_lower_camel,
            self.action_upper_camel,
            self.action_snake_case,
            self.action_kebab_case,
        ):
            convert_menu.addAction(action)
        wrap_menu = menu.addMenu("Wrap")
        wrap_menu.addAction(self.action_wrap_paragraph)
        for width in (70, 72, 78, 80, 100, 120):
            wrap_menu.addAction(
                f"Wrap paragraph at {width} characters",
                lambda _checked=False, value=width: self.wrap_text(value),
            )
        self.action_show_completions.setEnabled(False)
        menu.addAction(self.action_show_completions)
        menu.addSeparator()
        menu.addAction(self.action_sort_lines)
        menu.addAction(self.action_sort_lines_case)
        permute_lines_menu = menu.addMenu("Permute Lines")
        for label, operation in (
            ("Reverse", "reverse"),
            ("Unique", "unique"),
            ("Shuffle", "shuffle"),
        ):
            permute_lines_menu.addAction(
                label,
                lambda _checked=False, value=operation: self.permute_lines(value),
            )
        self.add_disabled_submenu(
            menu,
            "Permute Selections",
            ("Sort", "Reverse", "Unique", "Shuffle"),
        )

        menu = self.menuBar().addMenu("&Selection")
        menu.addAction(self.action_split_selection_lines)
        menu.addAction(self.action_single_selection)
        menu.addSeparator()
        menu.addAction(self.action_select_all)
        for action in (
            self.action_expand_smart,
            self.action_expand_line,
            self.action_expand_line_up,
            self.action_expand_word,
            self.action_expand_block,
            self.action_expand_paragraph,
            self.action_expand_scope,
            self.action_expand_brackets,
            self.action_expand_indentation,
        ):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.action_add_previous_line)
        menu.addAction(self.action_add_next_line)
        menu.addSeparator()
        self.add_disabled_submenu(
            menu,
            "Tab Selection",
            (
                "Unselect Others",
                "Unselect to Left",
                "Unselect to Right",
                "Unselect Current",
                "Select to Left",
                "Select to Right",
                "Focus to Left",
                "Focus to Right",
                "Focus Selected Tab 1",
                "Focus Selected Tab 2",
                "Focus Selected Tab 3",
                "Focus Selected Tab 4",
                "Focus Selected Tab 5",
                "Focus Selected Tab 6",
                "Focus Selected Tab 7",
                "Focus Selected Tab 8",
                "Focus Selected Tab 9",
                "Focus Selected Tab 10",
            ),
        )
        self.action_split_selection_lines.setEnabled(False)
        self.action_expand_scope.setEnabled(False)
        self.action_add_previous_line.setEnabled(False)
        self.action_add_next_line.setEnabled(False)

        menu = self.menuBar().addMenu("&Find")
        for action in (
            self.action_find,
            self.action_find_next,
            self.action_find_prev,
            self.action_incremental_find,
        ):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.action_replace)
        menu.addAction(self.action_replace_next)
        menu.addSeparator()
        menu.addAction(self.action_quick_find)
        menu.addAction(self.action_quick_find_all)
        self.action_quick_add_next.setText("Quick Add Next\tCtrl+D")
        self.action_quick_skip_next.setText("Quick Skip Next\tCtrl+K, Ctrl+D")
        menu.addAction(self.action_quick_add_next)
        menu.addAction(self.action_quick_skip_next)
        menu.addSeparator()
        menu.addAction(self.action_use_selection_find)
        menu.addAction(self.action_use_selection_replace)
        menu.addSeparator()
        menu.addAction(self.action_find_in_files)
        results_menu = menu.addMenu("Find Results")
        results_menu.addAction(self.action_show_find_results)
        results_menu.addAction(self.action_next_result)
        results_menu.addAction(self.action_previous_result)
        menu.addAction(self.action_cancel_find)
        self.action_cancel_find.setEnabled(False)
        self.update_find_actions()

        self.create_view_menu()

        menu = self.menuBar().addMenu("&Goto")
        menu.addAction(self.action_quick_open)
        menu.addAction(self.action_goto_symbol)
        menu.addAction(self.action_goto_symbol_project)
        menu.addAction(self.action_goto_definition)
        menu.addAction(self.action_goto_reference)
        menu.addAction(self.action_goto)
        menu.addSeparator()
        menu.addAction(self.action_next_modification)
        menu.addAction(self.action_previous_modification)
        menu.addSeparator()
        menu.addAction(self.action_jump_back)
        menu.addAction(self.action_jump_forward)

        self.switch_file_menu = menu.addMenu("Switch File")
        self.switch_file_menu.aboutToShow.connect(self.populate_switch_file_menu)
        scroll_menu = menu.addMenu("Scroll")
        for label, callback in (
            ("Page Up", lambda: self.scroll_editor("page_up")),
            ("Page Down", lambda: self.scroll_editor("page_down")),
            ("Scroll to Top", lambda: self.scroll_editor("top")),
            ("Scroll to Bottom", lambda: self.scroll_editor("bottom")),
        ):
            scroll_menu.addAction(label, callback)

        self.bookmarks_menu = menu.addMenu("Bookmarks")
        self.bookmarks_menu.aboutToShow.connect(self.populate_bookmarks_menu)
        menu.addAction(self.action_jump_matching_bracket)
        menu.aboutToShow.connect(self.update_goto_actions)

        menu = self.menuBar().addMenu("&Tools")
        menu.addAction(self.action_palette)
        menu.addAction(self.action_snippets)
        menu.addSeparator()
        self.build_system_menu = menu.addMenu("Build System")
        self.build_system_menu.aboutToShow.connect(self.populate_build_system_menu)
        menu.addAction(self.action_build)
        menu.addAction(self.action_build_with)
        menu.addAction(self.action_cancel_build)
        self.build_results_menu = menu.addMenu("Build Results")
        self.build_results_menu.aboutToShow.connect(self.update_build_result_actions)
        self.build_results_menu.addAction(self.action_show_build_results)
        self.build_results_menu.addAction(self.action_next_build_result)
        self.build_results_menu.addAction(self.action_previous_build_result)
        self.build_results_menu.addAction(self.action_clear_build_results)
        menu.addAction(self.action_save_all_on_build)
        menu.addSeparator()
        menu.addAction(self.action_record_macro)
        menu.addAction(self.action_play_macro)
        menu.addAction(self.action_save_macro)
        self.macros_menu = menu.addMenu("Macros")
        self.macros_menu.aboutToShow.connect(self.populate_macros_menu)
        menu.addSeparator()
        self.developer_menu = menu.addMenu("Developer")
        self.developer_menu.addAction(self.action_developer_console)
        self.developer_menu.addAction(self.action_open_user_data)
        self.developer_menu.addAction(self.action_inspect_buffer)
        menu.addAction(self.action_install_package_control)
        menu.aboutToShow.connect(self.update_tool_actions)

        menu = self.menuBar().addMenu("&Project")
        menu.addAction(self.action_open_project)
        menu.addAction(self.action_switch_project)
        menu.addAction(self.action_quick_switch_project)
        self.recent_projects_menu = menu.addMenu("Open Recent")
        self.recent_projects_menu.aboutToShow.connect(self.populate_recent_projects_menu)
        menu.addSeparator()
        menu.addAction(self.action_save_project_as)
        menu.addAction(self.action_close_project)
        menu.addAction(self.action_edit_project)
        menu.addSeparator()
        menu.addAction(self.action_new_workspace)
        menu.addAction(self.action_save_workspace_as)
        menu.addSeparator()
        menu.addAction(self.action_add_folder_to_project)
        menu.addAction(self.action_remove_project_folders)
        menu.addAction(self.action_refresh_project_folders)
        menu.aboutToShow.connect(self.update_project_actions)
        self.populate_recent_projects_menu()
        self.update_project_actions()

        menu = self.menuBar().addMenu("&Preferences")
        menu.addAction(self.action_browse_packages)
        menu.addAction(self.action_preferences_settings)
        menu.addAction(self.action_syntax_settings)
        menu.addAction(self.action_distraction_free_settings)
        menu.addSeparator()
        menu.addAction(self.action_key_bindings)
        menu.addAction(self.action_mouse_bindings)
        menu.addSeparator()
        menu.addAction(self.action_select_color_scheme)
        menu.addAction(self.action_customize_color_scheme)
        menu.addSeparator()
        menu.addAction(self.action_select_theme)
        menu.addAction(self.action_customize_theme)
        menu.addSeparator()
        self.font_menu = menu.addMenu("Font")
        self.font_menu.addAction(self.action_font_larger)
        self.font_menu.addAction(self.action_font_smaller)
        self.font_menu.addAction(self.action_font_reset)

        menu = self.menuBar().addMenu("&Help")
        about = menu.addAction("About SubText")
        about.triggered.connect(self.show_about)

    @staticmethod
    def read_json_object(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def safe_int(value: object, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def builtin_color_schemes() -> dict[str, dict[str, str]]:
        return {
            "SubText Dark": {
                "background": "#303943", "foreground": "#d8dee9", "selection": "#435363",
                "gutter": "#28323c", "gutter_foreground": "#8996a5", "caret": "#f0a04b",
                "keyword": "#f92672", "comment": "#7f9f7f", "number": "#ae81ff",
                "string": "#e6db74", "builtin": "#66d9ef", "ruler": "#52606c",
            },
            "SubText Light": {
                "background": "#ffffff", "foreground": "#263238", "selection": "#c8def0",
                "gutter": "#f1f4f6", "gutter_foreground": "#71808c", "caret": "#1769aa",
                "keyword": "#9c287b", "comment": "#668044", "number": "#7b4fa1",
                "string": "#a05b00", "builtin": "#176c8a", "ruler": "#c0c8ce",
            },
            "Monokai": {
                "background": "#272822", "foreground": "#f8f8f2", "selection": "#49483e",
                "gutter": "#20211c", "gutter_foreground": "#90908a", "caret": "#f8f8f0",
                "keyword": "#f92672", "comment": "#75715e", "number": "#ae81ff",
                "string": "#e6db74", "builtin": "#66d9ef", "ruler": "#49483e",
            },
            "Mariana": {
                "background": "#263238", "foreground": "#eeffff", "selection": "#40545b",
                "gutter": "#202b30", "gutter_foreground": "#78909c", "caret": "#ffcc80",
                "keyword": "#c792ea", "comment": "#546e7a", "number": "#f78c6c",
                "string": "#c3e88d", "builtin": "#82aaff", "ruler": "#40545b",
            },
        }

    def resolve_color_scheme(self, name: str) -> dict[str, str]:
        schemes = self.builtin_color_schemes()
        colors = dict(schemes.get(name, schemes["SubText Dark"]))
        definition = self.read_json_object(
            self.user_package_dir / f"{name}.sublime-color-scheme"
        )
        global_names = {
            "background": "background", "foreground": "foreground", "selection": "selection",
            "gutter": "gutter", "gutter_foreground": "gutter_foreground", "caret": "caret",
        }
        globals_ = definition.get("globals", {})
        if isinstance(globals_, dict):
            for source, target in global_names.items():
                value = globals_.get(source)
                if isinstance(value, str) and QColor(value).isValid():
                    colors[target] = value
        scope_colors = {
            "keyword": "keyword", "comment": "comment", "constant.numeric": "number",
            "string": "string", "support.function": "builtin",
        }
        rules = definition.get("rules", [])
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict) or not isinstance(rule.get("foreground"), str):
                    continue
                if not QColor(rule["foreground"]).isValid():
                    continue
                for scope in str(rule.get("scope", "")).split(","):
                    target = scope_colors.get(scope.strip())
                    if target:
                        colors[target] = rule["foreground"]
        return colors

    def open_package_file(self, path: Path, default_content: str = "{}\n") -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(default_content, encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Cannot Open Preference File", str(error))
            return
        project_state = (
            self.project_root, self.project_root_explicit, list(self.project_folders),
            self.project_file_path, dict(self.project_definition),
        )
        self.open_path(str(path))
        self.project_file_path = project_state[3]
        self.project_definition = project_state[4]
        self.set_project_folders(project_state[2])
        self.project_root = project_state[0]
        self.project_root_explicit = project_state[1]
        self.file_model.setRootPath(str(self.project_root))
        self.file_tree.setRootIndex(self.file_model.index(str(self.project_root)))

    def browse_packages(self) -> None:
        try:
            self.packages_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(self, "Cannot Browse Packages", str(error))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.packages_dir)))

    def open_preferences_settings(self) -> None:
        defaults = {
            "font_face": self.editor_font_family, "font_size": self.editor_font_size,
            "tab_size": self.tab_size, "translate_tabs_to_spaces": self.global_translate_tabs,
            "word_wrap": self.global_word_wrap, "color_scheme": self.color_scheme_name,
            "theme": self.theme_name,
        }
        self.open_package_file(
            self.user_settings_file, json.dumps(defaults, indent=4) + "\n"
        )

    def open_syntax_specific_settings(self) -> None:
        editor = self.current_editor()
        syntax = self.effective_syntax(editor) if editor else "Plain Text"
        self.open_package_file(self.user_package_dir / f"{syntax}.sublime-settings", "{}\n")

    def open_distraction_free_settings(self) -> None:
        self.open_package_file(self.user_package_dir / "Distraction Free.sublime-settings", "{}\n")

    def open_key_bindings(self) -> None:
        self.open_package_file(self.user_package_dir / "Default.sublime-keymap", "[]\n")

    def open_mouse_bindings(self) -> None:
        self.open_package_file(self.user_package_dir / "Default.sublime-mousemap", "[]\n")

    def select_color_scheme(self) -> None:
        names = list(self.builtin_color_schemes())
        selected_index = names.index(self.color_scheme_name) if self.color_scheme_name in names else 0
        name, accepted = QInputDialog.getItem(
            self, "Select Color Scheme", "Color Scheme:", names, selected_index, False
        )
        if accepted:
            self.set_color_scheme(name)

    def customize_color_scheme(self) -> None:
        colors = self.color_scheme
        template = {
            "globals": {
                key: colors[key] for key in (
                    "background", "foreground", "selection", "gutter", "gutter_foreground", "caret"
                )
            },
            "rules": [
                {"scope": scope, "foreground": colors[key]}
                for scope, key in (
                    ("keyword", "keyword"), ("comment", "comment"),
                    ("constant.numeric", "number"), ("string", "string"),
                    ("support.function", "builtin"),
                )
            ],
        }
        self.open_package_file(
            self.user_package_dir / f"{self.color_scheme_name}.sublime-color-scheme",
            json.dumps(template, indent=4) + "\n",
        )

    def select_theme(self) -> None:
        names = ["Dark", "Light"]
        index = 0 if self.theme_name == "Dark" else 1
        name, accepted = QInputDialog.getItem(self, "Select Theme", "Theme:", names, index, False)
        if accepted:
            self.set_theme(name)

    def customize_theme(self) -> None:
        self.open_package_file(
            self.user_package_dir / "SubText.qss",
            "/* SubText UI theme overrides (Qt stylesheet). */\n",
        )

    def set_color_scheme(self, name: str, *, persist: bool = True) -> None:
        self.color_scheme_name = name
        self.color_scheme = self.resolve_color_scheme(name)
        if persist:
            self.settings.setValue("color_scheme", name)
            self.persist_preference_value("color_scheme", name)
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if isinstance(editor, CodeEditor):
                editor.setProperty("colorScheme", None)
                self.apply_specific_preferences(
                    editor,
                    self.read_json_object(
                        self.user_package_dir / f"{self.effective_syntax(editor)}.sublime-settings"
                    ),
                )
                editor_colors = self.resolve_color_scheme(
                    str(editor.property("colorScheme") or self.color_scheme_name)
                )
                self.apply_editor_color_scheme(editor, editor_colors)
                self.apply_syntax_highlighter(editor)

    def apply_editor_color_scheme(
        self, editor: CodeEditor, colors: dict[str, str] | None = None
    ) -> None:
        colors = colors or self.color_scheme
        editor.setStyleSheet(
            "QPlainTextEdit { background:%s; color:%s; border:0; selection-background-color:%s; "
            "selection-color:%s; padding:10px 12px 10px 4px; }"
            % (colors["background"], colors["foreground"], colors["selection"], colors["foreground"])
        )
        editor.setProperty("gutterColor", colors["gutter"])
        editor.setProperty("gutterForeground", colors["gutter_foreground"])
        editor.setProperty("rulerColor", colors["ruler"])
        editor.line_number_area.update()
        editor.viewport().update()

    def set_theme(self, name: str, *, persist: bool = True) -> None:
        self.theme_name = name if name in ("Dark", "Light") else "Dark"
        if persist:
            self.settings.setValue("theme", self.theme_name)
            self.persist_preference_value("theme", self.theme_name)
        self.apply_application_theme()

    def apply_application_theme(self) -> None:
        if self.theme_name == "Light":
            window, panel, text, border, selected = "#f1f3f5", "#ffffff", "#202830", "#c5cdd3", "#d6eafa"
        else:
            window, panel, text, border, selected = "#272c31", "#1d2226", "#f0f2f4", "#444b52", "#343b41"
        stylesheet = (
            f"QMenuBar, QMenu {{ background:{panel}; color:{text}; border:1px solid {border}; }}"
            f"QMenuBar::item:selected, QMenu::item:selected {{ background:#329bd3; color:#ffffff; }}"
            f"QMenu::separator {{ height:1px; background:{border}; margin:4px 2px; }}"
            f"QTabWidget::pane {{ border:1px solid {border}; }}"
            f"QTabBar::tab {{ background:{panel}; color:{text}; padding:5px 10px; border:1px solid {border}; }}"
            f"QTabBar::tab:selected {{ background:{selected}; }}"
            f"QStatusBar, QDockWidget {{ background:{window}; color:{text}; }}"
            f"QLineEdit, QListWidget, QTreeView {{ background:{panel}; color:{text}; border:1px solid {border}; }}"
        )
        self.setStyleSheet(stylesheet + "\n" + self.custom_theme_stylesheet)

    def set_editor_font_size(self, size: int, *, persist: bool = True) -> None:
        self.editor_font_size = max(6, min(48, int(size)))
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if isinstance(editor, CodeEditor):
                font = editor.font()
                font.setFamily(self.editor_font_family)
                font.setPointSize(self.editor_font_size)
                editor.setFont(font)
                editor.set_tab_size(self.tab_size)
                self.apply_specific_preferences(
                    editor,
                    self.read_json_object(
                        self.user_package_dir / f"{self.effective_syntax(editor)}.sublime-settings"
                    ),
                )
        if persist:
            self.settings.setValue("font_size", self.editor_font_size)
            self.persist_preference_value("font_size", self.editor_font_size)

    def persist_preference_value(self, key: str, value: object) -> None:
        if not self.user_settings_file.exists():
            return
        values = self.read_json_object(self.user_settings_file)
        values[key] = value
        try:
            self.user_settings_file.write_text(json.dumps(values, indent=4) + "\n", encoding="utf-8")
        except OSError as error:
            self.status.showMessage(f"Could not save {key} to Preferences: {error}", 4000)

    def apply_global_preferences(self, values: dict) -> None:
        self.set_editor_font_size(self.safe_int(values.get("font_size", 11), 11, 6, 48), persist=False)
        default_family = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        family = values.get("font_face", default_family)
        self.editor_font_family = family if isinstance(family, str) and family.strip() else default_family
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if isinstance(editor, CodeEditor):
                font = editor.font()
                font.setFamily(self.editor_font_family)
                editor.setFont(font)
        self.set_tab_size(self.safe_int(values.get("tab_size", 4), 4, 1, 16), persist=False)
        self.global_translate_tabs = bool(values.get("translate_tabs_to_spaces", False))
        self.global_word_wrap = bool(values.get("word_wrap", True))
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if isinstance(editor, CodeEditor):
                editor.translate_tabs_to_spaces = self.global_translate_tabs
                editor.set_word_wrap(self.global_word_wrap, editor.word_wrap_column)
        scheme = values.get("color_scheme", "SubText Dark")
        self.set_color_scheme(scheme if isinstance(scheme, str) else "SubText Dark", persist=False)
        theme = values.get("theme", "Dark")
        self.set_theme(theme if isinstance(theme, str) else "Dark", persist=False)

    def apply_specific_preferences(self, editor: CodeEditor, values: dict) -> None:
        font = editor.font()
        font.setFamily(self.editor_font_family)
        font.setPointSize(self.editor_font_size)
        editor.setFont(font)
        editor.set_tab_size(self.tab_size)
        editor.translate_tabs_to_spaces = self.global_translate_tabs
        editor.set_word_wrap(self.global_word_wrap, editor.word_wrap_column)
        editor.setProperty("colorScheme", None)
        self.apply_editor_color_scheme(editor, self.color_scheme)
        if isinstance(values.get("font_size"), (int, float)):
            font = editor.font()
            font.setPointSize(self.safe_int(values["font_size"], self.editor_font_size, 6, 48))
            editor.setFont(font)
        if isinstance(values.get("font_face"), str) and values["font_face"].strip():
            font = editor.font()
            font.setFamily(values["font_face"])
            editor.setFont(font)
        if isinstance(values.get("color_scheme"), str):
            editor.setProperty("colorScheme", values["color_scheme"])
            self.apply_editor_color_scheme(editor, self.resolve_color_scheme(values["color_scheme"]))
        if isinstance(values.get("tab_size"), int):
            editor.set_tab_size(self.safe_int(values["tab_size"], self.tab_size, 1, 16))
        if isinstance(values.get("translate_tabs_to_spaces"), bool):
            editor.translate_tabs_to_spaces = values["translate_tabs_to_spaces"]
        if isinstance(values.get("word_wrap"), bool):
            editor.set_word_wrap(values["word_wrap"], editor.word_wrap_column)

    def reload_preference_file(self, path: Path) -> None:
        path = path.expanduser().resolve()
        if path == self.user_settings_file.resolve():
            self.global_preferences = self.read_json_object(path)
            self.apply_global_preferences(self.global_preferences)
        elif path.suffix == ".sublime-settings" and path.parent == self.user_package_dir:
            if path.stem == "Distraction Free" and self._distraction_free_state is not None:
                values = self.read_json_object(path)
                if isinstance(values.get("font_size"), int):
                    self.set_editor_font_size(values["font_size"], persist=False)
            else:
                values = self.read_json_object(path)
                for index in range(self.tabs.count()):
                    editor = self.tabs.widget(index)
                    if isinstance(editor, CodeEditor) and path.stem == self.effective_syntax(editor):
                        self.apply_specific_preferences(editor, values)
                        self.apply_syntax_highlighter(editor)
        elif path.suffix == ".sublime-color-scheme" and path.stem == self.color_scheme_name:
            self.color_scheme = self.resolve_color_scheme(self.color_scheme_name)
            for index in range(self.tabs.count()):
                editor = self.tabs.widget(index)
                if isinstance(editor, CodeEditor):
                    editor_colors = self.resolve_color_scheme(
                        str(editor.property("colorScheme") or self.color_scheme_name)
                    )
                    self.apply_editor_color_scheme(editor, editor_colors)
                    self.apply_syntax_highlighter(editor)
        elif path == (self.user_package_dir / "SubText.qss").resolve():
            try:
                self.custom_theme_stylesheet = path.read_text(encoding="utf-8")
            except OSError:
                self.custom_theme_stylesheet = ""
            self.apply_application_theme()
        elif path.name == "Default.sublime-keymap":
            self.apply_user_key_bindings(path)
        elif path.name == "Default.sublime-mousemap":
            self.apply_user_mouse_bindings(path)

    def apply_user_key_bindings(self, path: Path) -> None:
        actions = {
            "new_file": self.action_new, "open_file": self.action_open, "save": self.action_save,
            "save_as": self.action_save_as, "close_file": self.action_close, "undo": self.action_undo,
            "redo": self.action_redo, "cut": self.action_cut, "copy": self.action_copy,
            "paste": self.action_paste, "select_all": self.action_select_all, "find": self.action_find,
            "find_next": self.action_find_next, "find_prev": self.action_find_prev,
            "replace": self.action_replace, "goto_line": self.action_goto, "quick_open": self.action_quick_open,
            "goto_symbol": self.action_goto_symbol, "build": self.action_build,
        }
        if not hasattr(self, "_all_action_shortcut_defaults"):
            self._all_action_shortcut_defaults = [
                (action, action.shortcut()) for action in self.findChildren(QAction)
            ]
        for action, shortcut in self._all_action_shortcut_defaults:
            action.setShortcut(shortcut)
        try:
            bindings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.status.showMessage("Key bindings file must contain a JSON array", 3500)
            return
        if not isinstance(bindings, list):
            self.status.showMessage("Key bindings file must contain a JSON array", 3500)
            return
        applied = 0
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            if binding.get("context") or binding.get("args"):
                continue
            action = actions.get(str(binding.get("command", "")))
            keys = binding.get("keys")
            if action is None or not isinstance(keys, list) or not keys:
                continue
            sequence = QKeySequence(", ".join(str(key) for key in keys))
            if sequence.isEmpty():
                continue
            for other in self.findChildren(QAction):
                if other is not action and other.shortcut() == sequence:
                    other.setShortcut(QKeySequence())
            action.setShortcut(sequence)
            applied += 1
        self.status.showMessage(f"Loaded {applied} custom key binding(s)", 3000)

    def mouse_command_actions(self) -> dict[str, QAction]:
        return {
            "quick_open": self.action_quick_open,
            "goto_definition": self.action_goto_definition,
            "goto_reference": self.action_goto_reference,
            "goto_symbol": self.action_goto_symbol,
            "toggle_bookmark": self.action_toggle_bookmark,
            "jump_to_matching_bracket": self.action_jump_matching_bracket,
            "find": self.action_find,
            "find_next": self.action_find_next,
            "find_prev": self.action_find_prev,
        }

    def apply_user_mouse_bindings(self, path: Path) -> None:
        try:
            bindings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.status.showMessage("Mouse bindings file must contain a JSON array", 3500)
            return
        if not isinstance(bindings, list):
            self.status.showMessage("Mouse bindings file must contain a JSON array", 3500)
            return
        self.user_mouse_bindings = [binding for binding in bindings if isinstance(binding, dict)]
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if isinstance(editor, CodeEditor):
                editor.mouse_binding_handler = (
                    lambda event, target=editor: self.handle_mouse_binding(target, event)
                )
        self.status.showMessage(f"Loaded {len(self.user_mouse_bindings)} mouse binding(s)", 3000)

    def handle_mouse_binding(self, editor: CodeEditor, event) -> bool:
        button_names = {
            Qt.MouseButton.LeftButton: "button1",
            Qt.MouseButton.RightButton: "button2",
            Qt.MouseButton.MiddleButton: "button3",
            Qt.MouseButton.BackButton: "button4",
            Qt.MouseButton.ForwardButton: "button5",
        }
        button = button_names.get(event.button())
        if button is None:
            return False
        active_modifiers = set()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            active_modifiers.add("ctrl")
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            active_modifiers.add("alt")
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            active_modifiers.add("shift")
        if event.modifiers() & Qt.KeyboardModifier.MetaModifier:
            active_modifiers.add("super")
        for binding in reversed(self.user_mouse_bindings):
            if binding.get("button") != button or binding.get("context") or binding.get("args") or binding.get("press_args"):
                continue
            requested = binding.get("modifiers", [])
            if isinstance(requested, str):
                requested = [requested]
            requested = {str(item).lower().replace("control", "ctrl").replace("primary", "ctrl") for item in requested}
            if active_modifiers != requested:
                continue
            command = str(binding.get("command", binding.get("press_command", "")))
            action = self.mouse_command_actions().get(command)
            if action is None or not action.isEnabled():
                continue
            cursor = editor.cursorForPosition(event.position().toPoint())
            editor.setTextCursor(cursor)
            action.trigger()
            return True
        return False

    def change_font_size(self, delta: int) -> None:
        self.set_editor_font_size(self.editor_font_size + delta)

    def reset_font_size(self) -> None:
        self.set_editor_font_size(11)

    def create_view_menu(self) -> None:
        menu = self.menuBar().addMenu("&View")
        menu.aboutToShow.connect(self.update_view_actions)

        sidebar_menu = menu.addMenu("Side Bar")
        sidebar_menu.addAction(self.action_toggle_sidebar)
        self.action_show_open_files = QAction("Show Open Files", self, checkable=True)
        self.action_show_open_files.toggled.connect(self.set_open_files_view)
        sidebar_menu.addAction(self.action_show_open_files)

        self.action_hide_minimap = menu.addAction("Hide Minimap")
        self.action_hide_minimap.setEnabled(False)
        self.action_hide_minimap.setToolTip("This editor does not include a minimap yet")
        self.action_hide_tabs = QAction("Hide Tabs", self, checkable=True)
        self.action_hide_tabs.toggled.connect(self.set_tabs_hidden)
        menu.addAction(self.action_hide_tabs)
        self.action_hide_status = QAction("Hide Status Bar", self, checkable=True)
        self.action_hide_status.toggled.connect(self.set_status_hidden)
        menu.addAction(self.action_hide_status)
        self.action_hide_menu = QAction("Hide Menu", self, checkable=True)
        self.action_hide_menu.setShortcut(QKeySequence("F10"))
        self.action_hide_menu.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.action_hide_menu.toggled.connect(self.set_menu_hidden)
        menu.addAction(self.action_hide_menu)
        self.action_show_console = menu.addAction("Show Console")
        self.action_show_console.setEnabled(False)
        self.action_show_console.setToolTip("The editor console is not implemented yet")
        menu.addSeparator()

        self.action_full_screen = QAction("Enter Full Screen", self, checkable=True)
        self.action_full_screen.setShortcut(QKeySequence("F11"))
        self.action_full_screen.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.action_full_screen.toggled.connect(self.set_full_screen)
        menu.addAction(self.action_full_screen)
        self.action_distraction_free = QAction("Enter Distraction Free Mode", self, checkable=True)
        self.action_distraction_free.setShortcut(QKeySequence("Shift+F11"))
        self.action_distraction_free.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.action_distraction_free.toggled.connect(self.toggle_distraction_free)
        menu.addAction(self.action_distraction_free)
        menu.addSeparator()

        layout_menu = menu.addMenu("Layout")
        layout_group = QActionGroup(self)
        layout_group.setExclusive(True)
        single = QAction("Single", self, checkable=True)
        single.setChecked(True)
        layout_group.addAction(single)
        layout_menu.addAction(single)
        for label in ("Columns: 2", "Columns: 3", "Columns: 4", "Rows: 2", "Rows: 3", "Grid: 4"):
            action = layout_menu.addAction(label)
            action.setEnabled(False)
            action.setToolTip("Multiple editor groups are not implemented yet")
        for label, options in (
            ("Groups", ("New Group", "Close Group", "Close Other Groups")),
            ("Focus Group", ("Next", "Previous", "Group 1", "Group 2", "Group 3", "Group 4")),
            ("Move File to Group", ("Next", "Previous", "Group 1", "Group 2", "Group 3", "Group 4")),
        ):
            submenu = menu.addMenu(label)
            for option in options:
                action = submenu.addAction(option)
                action.setEnabled(False)
                action.setToolTip("Multiple editor groups are not implemented yet")

        syntax_menu = menu.addMenu("Syntax")
        syntax_group = QActionGroup(self)
        syntax_group.setExclusive(True)
        for syntax in ("Plain Text", "Python"):
            action = QAction(syntax, self, checkable=True)
            action.triggered.connect(
                lambda _checked=False, value=syntax: self.set_syntax(value)
            )
            syntax_group.addAction(action)
            syntax_menu.addAction(action)
            self.syntax_actions[syntax] = action

        indentation_menu = menu.addMenu("Indentation")
        self.spaces_action = QAction("Indent Using Spaces", self, checkable=True)
        self.spaces_action.toggled.connect(self.set_indent_using_spaces)
        indentation_menu.addAction(self.spaces_action)
        tab_width_menu = indentation_menu.addMenu("Tab Width")
        tab_width_group = QActionGroup(self)
        tab_width_group.setExclusive(True)
        for size in range(1, 9):
            action = QAction(str(size), self, checkable=True)
            action.triggered.connect(lambda _checked=False, value=size: self.set_tab_size(value))
            tab_width_group.addAction(action)
            tab_width_menu.addAction(action)
            self.view_tab_size_actions[size] = action
        indentation_menu.addSeparator()
        guess_action = indentation_menu.addAction("Guess Settings From Buffer")
        guess_action.triggered.connect(self.guess_indentation)
        convert_spaces = indentation_menu.addAction("Convert Indentation to Spaces")
        convert_spaces.triggered.connect(lambda: self.convert_indentation(True))
        convert_tabs = indentation_menu.addAction("Convert Indentation to Tabs")
        convert_tabs.triggered.connect(lambda: self.convert_indentation(False))
        self.indentation_command_actions = (guess_action, convert_spaces, convert_tabs)

        endings_menu = menu.addMenu("Line Endings")
        endings_group = QActionGroup(self)
        endings_group.setExclusive(True)
        for label, ending in (("Windows", "\r\n"), ("Unix", "\n"), ("Mac OS 9", "\r")):
            action = QAction(label, self, checkable=True)
            action.triggered.connect(
                lambda _checked=False, value=ending: self.set_line_ending(value)
            )
            endings_group.addAction(action)
            endings_menu.addAction(action)
            self.line_ending_actions[ending] = action

        self.word_wrap_action = QAction("Word Wrap", self, checkable=True)
        self.word_wrap_action.toggled.connect(self.set_word_wrap)
        menu.addAction(self.word_wrap_action)
        wrap_column_menu = menu.addMenu("Word Wrap Column")
        wrap_group = QActionGroup(self)
        wrap_group.setExclusive(True)
        for label, width in (("Automatic", 0), *((str(width), width) for width in (70, 72, 78, 80, 100, 120))):
            action = QAction(label, self, checkable=True)
            action.triggered.connect(
                lambda _checked=False, value=width: self.set_word_wrap_column(value)
            )
            wrap_group.addAction(action)
            wrap_column_menu.addAction(action)
            self.wrap_column_actions[width] = action

        ruler_menu = menu.addMenu("Ruler")
        ruler_group = QActionGroup(self)
        ruler_group.setExclusive(True)
        for label, column in (("None", 0), *((str(width), width) for width in (70, 72, 78, 80, 100, 120))):
            action = QAction(label, self, checkable=True)
            action.triggered.connect(
                lambda _checked=False, value=column: self.set_ruler(value)
            )
            ruler_group.addAction(action)
            ruler_menu.addAction(action)
            self.ruler_actions[column] = action

        self.action_spell_check = menu.addAction("Spell Check")
        self.action_spell_check.setShortcut(QKeySequence("F6"))
        self.action_spell_check.setEnabled(False)
        self.action_spell_check.setToolTip("Spell checking is not implemented yet")
        self.action_next_misspelling = menu.addAction("Next Misspelling")
        self.action_next_misspelling.setShortcut(QKeySequence("Ctrl+F6"))
        self.action_prev_misspelling = menu.addAction("Prev Misspelling")
        self.action_prev_misspelling.setShortcut(QKeySequence("Ctrl+Shift+F6"))
        self.action_next_misspelling.setEnabled(False)
        self.action_prev_misspelling.setEnabled(False)
        dictionary_menu = menu.addMenu("Dictionary")
        dictionary_action = dictionary_menu.addAction("No dictionaries available")
        dictionary_action.setEnabled(False)
        self.palette_actions.extend(
            [self.action_hide_menu, self.action_full_screen, self.action_distraction_free]
        )
        self.update_view_actions()

    def update_view_actions(self) -> None:
        editor = self.current_editor()
        self.set_action_checked(self.action_toggle_sidebar, not self.project_dock.isHidden())
        self.set_action_checked(
            self.action_show_open_files, self.sidebar_stack.currentWidget() is self.open_files_list
        )
        self.set_action_checked(self.action_hide_tabs, self._tabs_hidden)
        self.set_action_checked(self.action_hide_status, self._status_hidden)
        self.set_action_checked(self.action_hide_menu, self._menu_hidden)
        self.set_action_checked(self.action_full_screen, self.isFullScreen())
        self.set_action_checked(
            self.action_distraction_free, self._distraction_free_state is not None
        )
        self.action_full_screen.setText(
            "Exit Full Screen" if self.isFullScreen() else "Enter Full Screen"
        )
        self.action_distraction_free.setText(
            "Exit Distraction Free Mode"
            if self._distraction_free_state is not None
            else "Enter Distraction Free Mode"
        )
        for syntax, action in self.syntax_actions.items():
            action.setEnabled(editor is not None)
            self.set_action_checked(
                action, editor is not None and self.effective_syntax(editor) == syntax
            )
        for size, action in self.view_tab_size_actions.items():
            self.set_action_checked(action, size == self.tab_size)
        for ending, action in self.line_ending_actions.items():
            action.setEnabled(editor is not None)
            self.set_action_checked(
                action, editor is not None and editor.property("lineEnding") == ending
            )
        self.spaces_action.setEnabled(editor is not None)
        self.set_action_checked(
            self.spaces_action, bool(editor and editor.translate_tabs_to_spaces)
        )
        self.word_wrap_action.setEnabled(editor is not None)
        self.set_action_checked(self.word_wrap_action, bool(editor and editor.word_wrap))
        width = editor.word_wrap_column if editor else 0
        for column, action in self.wrap_column_actions.items():
            action.setEnabled(editor is not None)
            self.set_action_checked(action, column == width)
        ruler = editor.ruler_columns[0] if editor and editor.ruler_columns else 0
        for column, action in self.ruler_actions.items():
            action.setEnabled(editor is not None)
            self.set_action_checked(action, column == ruler)
        for action in self.indentation_command_actions:
            action.setEnabled(editor is not None)

    @staticmethod
    def set_action_checked(action: QAction, checked: bool) -> None:
        was_blocked = action.blockSignals(True)
        action.setChecked(checked)
        action.blockSignals(was_blocked)

    def set_sidebar_visible(self, visible: bool) -> None:
        self.project_dock.setVisible(visible)
        self.set_action_checked(self.action_toggle_sidebar, visible)

    def sync_sidebar_action(self, visible: bool) -> None:
        self.set_action_checked(self.action_toggle_sidebar, visible)

    def toggle_sidebar(self) -> None:
        self.set_sidebar_visible(self.project_dock.isHidden())

    def set_open_files_view(self, enabled: bool) -> None:
        self.sidebar_stack.setCurrentWidget(self.open_files_list if enabled else self.file_tree)
        if enabled:
            self.project_dock.show()
        self.refresh_open_files()

    def refresh_open_files(self, _index: int = -1) -> None:
        if not hasattr(self, "open_files_list"):
            return
        self.open_files_list.clear()
        current = self.primary_editor_for(self.current_editor())
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            name = self.editor_display_title(editor)
            if editor.document().isModified():
                name += " •"
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, id(editor))
            item.setToolTip(str(editor.property("filePath") or name))
            self.open_files_list.addItem(item)
            if editor is current:
                self.open_files_list.setCurrentItem(item)

    def activate_open_file(self, item: QListWidgetItem) -> None:
        editor_id = item.data(Qt.ItemDataRole.UserRole)
        for index in range(self.tabs.count()):
            if id(self.tabs.widget(index)) == editor_id:
                self.tabs.setCurrentIndex(index)
                break

    def set_tabs_hidden(self, hidden: bool) -> None:
        self._tabs_hidden = hidden
        self.tabs.tabBar().setVisible(not hidden)

    def set_status_hidden(self, hidden: bool) -> None:
        self._status_hidden = hidden
        self.status.setVisible(not hidden)

    def set_menu_hidden(self, hidden: bool) -> None:
        self._menu_hidden = hidden
        self.menuBar().setVisible(not hidden)

    def set_full_screen(self, enabled: bool) -> None:
        if enabled and not self.isFullScreen():
            self.showFullScreen()
        elif not enabled and self.isFullScreen():
            self.showNormal()
        self.set_action_checked(self.action_full_screen, enabled)

    def toggle_distraction_free(self, enabled: bool) -> None:
        if enabled and self._distraction_free_state is None:
            self._distraction_free_state = {
                "fullscreen": self.isFullScreen(),
                "menu_hidden": self._menu_hidden,
                "status_hidden": self._status_hidden,
                "tabs_hidden": self._tabs_hidden,
                "sidebar_hidden": self.project_dock.isHidden(),
                "font_size": self.editor_font_size,
            }
            distraction_settings = self.read_json_object(
                self.user_package_dir / "Distraction Free.sublime-settings"
            )
            if isinstance(distraction_settings.get("font_size"), (int, float)):
                self.set_editor_font_size(int(distraction_settings["font_size"]), persist=False)
            self.set_menu_hidden(True)
            self.set_status_hidden(True)
            self.set_tabs_hidden(True)
            self.project_dock.hide()
            self.showFullScreen()
        elif not enabled and self._distraction_free_state is not None:
            state = self._distraction_free_state
            self._distraction_free_state = None
            self.set_menu_hidden(state["menu_hidden"])
            self.set_status_hidden(state["status_hidden"])
            self.set_tabs_hidden(state["tabs_hidden"])
            self.project_dock.setVisible(not state["sidebar_hidden"])
            self.set_editor_font_size(state["font_size"], persist=False)
            if not state["fullscreen"]:
                self.showNormal()
        self.set_action_checked(self.action_distraction_free, enabled)
        self.update_view_actions()

    def effective_syntax(self, editor: CodeEditor) -> str:
        return str(editor.property("syntaxOverride") or self.syntax_for(str(editor.property("displayName"))))

    def set_syntax(self, syntax: str) -> None:
        editor = self.current_editor()
        if editor is None or syntax not in ("Plain Text", "Python"):
            return
        editor.setProperty("syntaxOverride", syntax)
        self.apply_specific_preferences(
            editor,
            self.read_json_object(self.user_package_dir / f"{syntax}.sublime-settings"),
        )
        self.apply_syntax_highlighter(editor)

    def set_indent_using_spaces(self, enabled: bool) -> None:
        editor = self.current_editor()
        if editor is not None:
            editor.translate_tabs_to_spaces = enabled

    def guess_indentation(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        prefixes = [
            match.group(0)
            for line in editor.toPlainText().splitlines()
            if (match := re.match(r"^[ \t]+", line)) is not None
        ]
        if not prefixes:
            self.status.showMessage("No indentation found in this file", 2500)
            return
        tab_indents = sum(prefix.count("\t") for prefix in prefixes)
        space_widths = [len(prefix) for prefix in prefixes if "\t" not in prefix]
        uses_spaces = bool(space_widths) and tab_indents == 0
        if space_widths:
            width = space_widths[0]
            for value in space_widths[1:]:
                width = math.gcd(width, value)
            self.set_tab_size(max(1, min(8, width)))
        editor.translate_tabs_to_spaces = uses_spaces
        self.update_view_actions()
        self.status.showMessage("Indentation settings detected", 2500)

    def convert_indentation(self, to_spaces: bool) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        lines = editor.toPlainText().split("\n")
        converted = []
        for line in lines:
            match = re.match(r"^[ \t]*", line)
            prefix = match.group(0) if match else ""
            body = line[len(prefix):]
            column = 0
            for char in prefix:
                column = column + 1 if char == " " else column + self.tab_size - column % self.tab_size
            if to_spaces:
                indentation = " " * column
            else:
                indentation = "\t" * (column // self.tab_size) + " " * (column % self.tab_size)
            converted.append(indentation + body)
        cursor = QTextCursor(editor.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText("\n".join(converted))
        cursor.endEditBlock()
        editor.translate_tabs_to_spaces = to_spaces
        editor.setFocus()
        self.update_view_actions()

    def set_line_ending(self, ending: str) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        editor.setProperty("lineEnding", ending)
        self.line_endings_label.setText({"\r\n": "CRLF", "\r": "CR", "\n": "LF"}[ending])
        self.update_view_actions()

    def set_word_wrap(self, enabled: bool) -> None:
        editor = self.current_editor()
        if editor is not None:
            editor.set_word_wrap(enabled, editor.word_wrap_column)

    def set_word_wrap_column(self, column: int) -> None:
        editor = self.current_editor()
        if editor is not None:
            editor.set_word_wrap(editor.word_wrap, column)
            self.update_view_actions()

    def set_ruler(self, column: int) -> None:
        editor = self.current_editor()
        if editor is not None:
            editor.ruler_columns = [column] if column else []
            editor.viewport().update()
            self.update_view_actions()

    @staticmethod
    def available_encodings() -> tuple[tuple[str, str], ...]:
        return (
            ("UTF-8", "utf-8"),
            ("UTF-16 LE", "utf-16-le"),
            ("UTF-16 BE", "utf-16-be"),
            ("Western (Windows 1252)", "cp1252"),
            ("Western (ISO 8859-1)", "iso-8859-1"),
        )

    def populate_recent_menu(self) -> None:
        self.recent_menu.clear()
        recent_files = [path for path in self.recent_files if Path(path).is_file()]
        if not recent_files:
            empty_action = self.recent_menu.addAction("No Recent Files")
            empty_action.setEnabled(False)
        else:
            for path in recent_files:
                action = self.recent_menu.addAction(Path(path).name)
                action.setToolTip(path)
                action.triggered.connect(
                    lambda _checked=False, value=path: self.open_path(value)
                )
            self.recent_menu.addSeparator()
        clear_action = self.recent_menu.addAction("Clear Items")
        clear_action.setEnabled(bool(self.recent_files))
        clear_action.triggered.connect(self.clear_recent_files)

    def remember_recent_file(self, path: str) -> None:
        resolved = str(Path(path).expanduser().resolve())
        self.recent_files = [resolved] + [item for item in self.recent_files if item != resolved]
        self.recent_files = self.recent_files[:10]
        self.settings.setValue("recent_files", self.recent_files)

    def clear_recent_files(self) -> None:
        self.recent_files.clear()
        self.settings.setValue("recent_files", [])
        self.populate_recent_menu()

    def new_window(self) -> None:
        application = QApplication.instance()
        if application is None:
            return
        window = MainWindow([], start_new=True)
        windows = getattr(application, "_subtext_windows", [])
        windows.append(window)
        application._subtext_windows = windows
        window.show()

    def quit_application(self) -> None:
        QApplication.closeAllWindows()

    def create_editor(self, name: str = "Untitled") -> CodeEditor:
        editor = CodeEditor(self)
        font = editor.font()
        font.setFamily(self.editor_font_family)
        font.setPointSize(self.editor_font_size)
        editor.setFont(font)
        editor.set_tab_size(self.tab_size)
        editor.translate_tabs_to_spaces = self.global_translate_tabs
        editor.set_word_wrap(self.global_word_wrap)
        editor.mouse_binding_handler = (
            lambda event, target=editor: self.handle_mouse_binding(target, event)
        )
        self.apply_editor_color_scheme(editor)
        editor.setProperty("filePath", "")
        editor.setProperty("displayName", name)
        editor.setProperty("lineEnding", "\n")
        editor.setProperty("fileEncoding", "utf-8")
        editor.cursorPositionChanged.connect(self.update_cursor_position)
        editor.cursorPositionChanged.connect(self.update_edit_actions)
        editor.cursorPositionChanged.connect(self.schedule_session_save)
        editor.focused.connect(self.activate_main_editor_group)
        editor.textChanged.connect(lambda target=editor: self.on_editor_text_changed(target))
        editor.document().undoAvailable.connect(self.update_edit_actions)
        editor.document().redoAvailable.connect(self.update_edit_actions)
        editor.document().modificationChanged.connect(
            lambda modified, target=editor: self.on_editor_modified_state_changed(
                target, modified
            )
        )
        syntax_preferences = self.user_package_dir / f"{self.effective_syntax(editor)}.sublime-settings"
        self.apply_specific_preferences(editor, self.read_json_object(syntax_preferences))
        self.apply_syntax_highlighter(editor)
        index = self.tabs.addTab(editor, name)
        self.tabs.setCurrentIndex(index)
        return editor

    def new_file(self) -> None:
        number = sum(
            1
            for index in range(self.tabs.count())
            if str(self.tabs.widget(index).property("displayName")).startswith("Untitled")
        )
        name = "Untitled" if number == 0 else f"Untitled {number + 1}"
        self.create_editor(name)
        self.status.showMessage("New file", 2500)

    def schedule_session_save(self, *_args) -> None:
        if not self._restoring_session:
            self.session_save_timer.start()

    def on_editor_text_changed(self, editor: CodeEditor) -> None:
        self.update_tab_label(editor)
        self.schedule_session_save()

    def on_editor_modified_state_changed(
        self, editor: CodeEditor, _modified: bool
    ) -> None:
        self.update_tab_label(editor)
        self.schedule_session_save()

    def editor_display_title(self, editor: CodeEditor) -> str:
        name = str(editor.property("displayName") or "Untitled")
        if editor.property("filePath"):
            return name
        text = editor.toPlainText()
        first_line = text.splitlines()[0].strip() if text else ""
        if not first_line:
            return name
        if len(first_line) > 48:
            return first_line[:48] + "…"
        return first_line

    def save_session_state(self) -> None:
        if self._restoring_session:
            return
        buffers: list[dict[str, object]] = []
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if not isinstance(editor, CodeEditor):
                continue
            cursor = editor.textCursor()
            file_path = str(editor.property("filePath") or "")
            modified = editor.document().isModified()
            buffer: dict[str, object] = {
                "file_path": file_path,
                "display_name": str(editor.property("displayName") or "Untitled"),
                "modified": modified,
                "line_ending": str(editor.property("lineEnding") or "\n"),
                "file_encoding": str(editor.property("fileEncoding") or "utf-8"),
                "cursor_position": cursor.position(),
                "cursor_anchor": cursor.anchor(),
                "scroll_position": editor.verticalScrollBar().value(),
            }
            if not file_path or modified:
                buffer["content"] = editor.toPlainText()
            buffers.append(buffer)
        state = {
            "version": 1,
            "current_index": self.tabs.currentIndex(),
            "buffers": buffers,
        }
        try:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            temporary_file = self.session_file.with_suffix(".json.tmp")
            temporary_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary_file.replace(self.session_file)
        except (OSError, TypeError, ValueError) as error:
            if hasattr(self, "status"):
                self.status.showMessage(f"Could not preserve session: {error}", 5000)

    def restore_session_state(self) -> bool:
        try:
            state = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(state, dict) or not isinstance(state.get("buffers"), list):
            return False
        buffers = state["buffers"]
        if not buffers:
            return False

        self._restoring_session = True
        restored_editors: list[CodeEditor] = []
        try:
            for buffer in buffers:
                if not isinstance(buffer, dict):
                    continue
                file_path = str(buffer.get("file_path") or "")
                display_name = str(buffer.get("display_name") or "Untitled")
                content = buffer.get("content")
                modified = bool(buffer.get("modified", False))
                path_exists = bool(file_path and Path(file_path).is_file())

                if path_exists:
                    self.open_path(file_path)
                    editor = self.current_editor()
                    if editor is None:
                        continue
                    if modified and isinstance(content, str):
                        editor.setPlainText(content)
                        editor.document().setModified(True)
                elif file_path:
                    if not isinstance(content, str):
                        continue
                    editor = self.create_editor(Path(file_path).name or display_name)
                    editor.setPlainText(content)
                    editor.document().setModified(True)
                else:
                    editor = self.create_editor(display_name)
                    if isinstance(content, str):
                        editor.setPlainText(content)
                    editor.document().setModified(modified)

                editor.setProperty("lineEnding", str(buffer.get("line_ending") or "\n"))
                editor.setProperty(
                    "fileEncoding", str(buffer.get("file_encoding") or "utf-8")
                )
                cursor = editor.textCursor()
                max_position = max(0, editor.document().characterCount() - 1)
                anchor = self.safe_int(
                    buffer.get("cursor_anchor", 0), 0, 0, max_position
                )
                position = self.safe_int(
                    buffer.get("cursor_position", anchor), anchor, 0, max_position
                )
                cursor.setPosition(anchor)
                cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
                editor.setTextCursor(cursor)
                editor.verticalScrollBar().setValue(
                    self.safe_int(buffer.get("scroll_position", 0), 0, 0, 2**31 - 1)
                )
                self.update_tab_label(editor)
                restored_editors.append(editor)
        finally:
            self._restoring_session = False

        if not restored_editors:
            return False
        current_index = self.safe_int(
            state.get("current_index", 0), 0, 0, len(restored_editors) - 1
        )
        self.tabs.setCurrentWidget(restored_editors[current_index])
        self.update_window_state()
        return True

    def open_file_dialog(self) -> None:
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "Open File", str(self.project_root), "All Files (*)"
        )
        for path in paths:
            self.open_path(path)

    def open_folder_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Open Folder", str(self.project_root)
        )
        if not path:
            return
        self.project_file_path = None
        self.project_definition = {}
        self.set_project_folders([Path(path)])
        self.remember_recent_project(path)
        self.status.showMessage(f"Project: {path}", 4000)

    def open_project_dialog(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Open Project",
            str(self.project_root),
            "Sublime Projects (*.sublime-project);;SubText Projects (*.subtext-project);;JSON files (*.json)",
        )
        if path:
            self.open_project_path(path)

    def set_project_folders(self, folders: list[Path]) -> None:
        unique: list[Path] = []
        seen: set[str] = set()
        for folder in folders:
            resolved = folder.expanduser().resolve()
            key = str(resolved)
            if key not in seen:
                unique.append(resolved)
                seen.add(key)
        self.project_folders = unique
        visible_root = next((folder for folder in unique if folder.is_dir()), None)
        self.project_root = visible_root or Path.home()
        self.project_root_explicit = visible_root is not None
        self.file_model.setRootPath(str(self.project_root))
        self.file_tree.setRootIndex(self.file_model.index(str(self.project_root)))
        if self.project_root_explicit:
            self.project_dock.show()
        self.update_project_actions()

    def open_project_path(self, path: str) -> None:
        selected = Path(path).expanduser().resolve()
        if selected.is_dir():
            self.project_file_path = None
            self.project_definition = {}
            self.set_project_folders([selected])
            self.remember_recent_project(str(selected))
            self.status.showMessage(f"Project: {selected}", 4000)
            return
        try:
            definition = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Cannot Open Project", str(error))
            return
        if not isinstance(definition, dict):
            QMessageBox.warning(self, "Cannot Open Project", "The project file must contain a JSON object.")
            return

        folders = []
        raw_folders = definition.get("folders", [])
        if isinstance(raw_folders, list):
            for entry in raw_folders:
                value = entry.get("path") if isinstance(entry, dict) else entry
                if not isinstance(value, str) or not value.strip():
                    continue
                folder = Path(value).expanduser()
                folders.append(folder if folder.is_absolute() else selected.parent / folder)
        self.project_file_path = selected
        self.project_definition = definition
        self.set_project_folders(folders)
        self.remember_recent_project(str(selected))
        self.status.showMessage(f"Project: {selected.stem}", 4000)

    def project_folder_entries(self, project_path: Path) -> list[dict]:
        previous = self.project_definition.get("folders", [])
        previous_by_path: dict[str, dict] = {}
        if isinstance(previous, list):
            for entry in previous:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    continue
                value = Path(entry["path"]).expanduser()
                resolved = value if value.is_absolute() else project_path.parent / value
                previous_by_path[str(resolved.resolve())] = entry

        entries = []
        for folder in self.project_folders:
            try:
                stored_path = str(folder.relative_to(project_path.parent))
            except ValueError:
                stored_path = str(folder)
            entry = dict(previous_by_path.get(str(folder.resolve()), {}))
            entry["path"] = stored_path
            entries.append(entry)
        return entries

    def write_project_file(self, path: Path) -> bool:
        definition = dict(self.project_definition)
        definition["folders"] = self.project_folder_entries(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(definition, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Cannot Save Project", str(error))
            return False
        self.project_definition = definition
        self.project_file_path = path.resolve()
        self.update_project_actions()
        self.remember_recent_project(str(self.project_file_path))
        return True

    def save_project_as(self) -> None:
        suggested = self.project_file_path or (self.project_root / f"{self.project_root.name}.sublime-project")
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Project As",
            str(suggested),
            "Sublime Projects (*.sublime-project);;SubText Projects (*.subtext-project)",
        )
        if not path:
            return
        target = Path(path).expanduser()
        if not target.suffix:
            target = target.with_suffix(".sublime-project")
        if self.write_project_file(target):
            self.status.showMessage(f"Saved project: {target}", 4000)

    def close_project(self) -> None:
        self.project_file_path = None
        self.project_definition = {}
        self.set_project_folders([])
        self.status.showMessage("Project closed", 2500)

    def edit_project(self) -> None:
        if self.project_file_path is not None and self.project_file_path.is_file():
            self.open_path(str(self.project_file_path))

    def save_workspace_as(self) -> None:
        if self.project_file_path is not None:
            suggested = self.project_file_path.with_suffix(".sublime-workspace")
        else:
            suggested = self.project_root / f"{self.project_root.name}.sublime-workspace"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Workspace As",
            str(suggested),
            "Sublime Workspaces (*.sublime-workspace);;SubText Workspaces (*.subtext-workspace)",
        )
        if not path:
            return
        target = Path(path).expanduser()
        if not target.suffix:
            target = target.with_suffix(".sublime-workspace")
        if self.write_workspace_file(target):
            self.status.showMessage(f"Saved workspace: {target}", 4000)

    def workspace_snapshot(self) -> dict:
        open_files = [
            str(editor.property("filePath"))
            for index in range(self.tabs.count())
            if (editor := self.tabs.widget(index)) is not None and editor.property("filePath")
        ]
        current = self.current_editor()
        return {
            "project": str(self.project_file_path) if self.project_file_path else None,
            "folders": [str(folder) for folder in self.project_folders],
            "open_files": open_files,
            "active_file": str(current.property("filePath")) if current and current.property("filePath") else None,
        }

    def write_workspace_file(self, path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.workspace_snapshot(), indent=4, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            QMessageBox.warning(self, "Cannot Save Workspace", str(error))
            return False
        return True

    def new_workspace_for_project(self) -> None:
        if self.project_file_path is None:
            return
        target = self.project_file_path.with_suffix(".sublime-workspace")
        suffix = 2
        while target.exists():
            target = self.project_file_path.with_name(
                f"{self.project_file_path.stem} ({suffix}).sublime-workspace"
            )
            suffix += 1
        if self.write_workspace_file(target):
            self.status.showMessage(f"Created workspace: {target}", 4000)

    def add_folder_to_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add Folder to Project", str(self.project_root))
        if not path:
            return
        folder = Path(path).expanduser().resolve()
        self.set_project_folders([*self.project_folders, folder])
        if self.project_file_path is not None:
            self.write_project_file(self.project_file_path)
        self.status.showMessage(f"Added folder to project: {folder}", 3500)

    def remove_project_folders(self) -> None:
        if not self.project_folders:
            return
        self.set_project_folders([])
        if self.project_file_path is not None:
            self.write_project_file(self.project_file_path)
        self.status.showMessage("Removed all folders from project", 3000)

    def refresh_project_folders(self) -> None:
        self.file_model.setRootPath(str(self.project_root))
        self.file_tree.setRootIndex(self.file_model.index(str(self.project_root)))
        self.status.showMessage("Project folders refreshed", 2500)

    def switch_project(self) -> None:
        choices = [path for path in self.recent_projects if Path(path).exists()]
        if not choices:
            self.open_project_dialog()
            return
        labels = [f"{self.project_display_name(path)} — {path}" for path in choices]
        label, accepted = QInputDialog.getItem(self, "Switch Project", "Project:", labels, 0, False)
        if accepted:
            self.open_project_path(choices[labels.index(label)])

    def quick_switch_project(self) -> None:
        entries = [
            (self.project_display_name(path), path, {"type": "project", "path": path})
            for path in self.recent_projects if Path(path).exists()
        ]
        entries.append(("Open another project…", "Choose a project file", {"type": "open"}))
        dialog = QuickAccessDialog("Quick Switch Project", "Filter recent projects", entries, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not isinstance(dialog.selected_value, dict):
            return
        selected = dialog.selected_value
        if selected.get("type") == "open":
            self.open_project_dialog()
        else:
            self.open_project_path(selected["path"])

    @staticmethod
    def project_display_name(path: str) -> str:
        selected = Path(path)
        return selected.stem if selected.is_file() else selected.name or str(selected)

    def remember_recent_project(self, path: str) -> None:
        resolved = str(Path(path).expanduser().resolve())
        self.recent_projects = [resolved] + [item for item in self.recent_projects if item != resolved]
        self.recent_projects = self.recent_projects[:10]
        self.settings.setValue("recent_projects", self.recent_projects)
        if hasattr(self, "recent_projects_menu"):
            self.populate_recent_projects_menu()

    def populate_recent_projects_menu(self) -> None:
        menu = self.recent_projects_menu
        menu.clear()
        recent = [path for path in self.recent_projects if Path(path).exists()]
        if not recent:
            empty = menu.addAction("No Recent Projects")
            empty.setEnabled(False)
        else:
            for path in recent:
                action = menu.addAction(self.project_display_name(path))
                action.setToolTip(path)
                action.triggered.connect(
                    lambda _checked=False, target=path: self.open_project_path(target)
                )
            menu.addSeparator()
        clear = menu.addAction("Clear Recent Projects")
        clear.setEnabled(bool(self.recent_projects))
        clear.triggered.connect(self.clear_recent_projects)

    def clear_recent_projects(self) -> None:
        self.recent_projects.clear()
        self.settings.setValue("recent_projects", [])
        self.populate_recent_projects_menu()

    def update_project_actions(self) -> None:
        has_project = bool(self.project_folders or self.project_file_path)
        self.action_close_project.setEnabled(has_project)
        self.action_edit_project.setEnabled(
            self.project_file_path is not None and self.project_file_path.is_file()
        )
        self.action_new_workspace.setEnabled(self.project_file_path is not None)
        self.action_remove_project_folders.setEnabled(bool(self.project_folders))
        self.action_refresh_project_folders.setEnabled(bool(self.project_folders))

    def open_tree_item(self, index) -> None:
        path = self.file_model.filePath(index)
        if self.file_model.isDir(index):
            return
        self.open_path(path)

    def open_path(self, path: str) -> None:
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            self.status.showMessage(f"File not found: {file_path}", 4000)
            return
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if Path(str(editor.property("filePath"))).resolve() == file_path:
                self.tabs.setCurrentIndex(index)
                self.remember_recent_file(str(file_path))
                return
        try:
            raw_content_bytes = file_path.read_bytes()
            try:
                raw_content = raw_content_bytes.decode("utf-8")
                file_encoding = "utf-8"
            except UnicodeDecodeError:
                # Sublime's default fallback is Western (Windows 1252). Keep
                # legacy files openable so Reopen with Encoding can be used.
                raw_content = raw_content_bytes.decode("cp1252")
                file_encoding = "cp1252"
        except (OSError, UnicodeError) as error:
            QMessageBox.critical(self, "Cannot Open File", str(error))
            return
        line_ending = "\r\n" if "\r\n" in raw_content else "\r" if "\r" in raw_content else "\n"
        content = raw_content.replace("\r\n", "\n").replace("\r", "\n")
        editor = self.create_editor(file_path.name)
        editor.setProperty("filePath", str(file_path))
        editor.setProperty("fileEncoding", file_encoding)
        editor.setProperty("lineEnding", line_ending)
        editor.setPlainText(content)
        editor.document().setModified(False)
        self.apply_syntax_highlighter(editor)
        self.update_tab_label(editor)
        if not self.project_root_explicit:
            self.set_project_folders([file_path.parent])
        self.remember_recent_file(str(file_path))
        self.status.showMessage(f"Opened {file_path.name}", 2500)

    def activate_main_editor_group(self) -> None:
        self._active_editor_group = "main"
        self.update_cursor_position()
        self.update_edit_actions()

    def activate_split_editor_group(self) -> None:
        if self._split_view_enabled:
            self._active_editor_group = "split"
            self.update_cursor_position()
            self.update_edit_actions()

    def primary_editor_for(self, editor: CodeEditor | None) -> CodeEditor | None:
        if editor is self.split_editor:
            return self._split_source
        return editor

    def sync_split_view_document(self) -> None:
        if self.split_editor is None:
            return
        source = self.tabs.currentWidget()
        source = source if isinstance(source, CodeEditor) else None
        self._split_source = source
        if source is None:
            self.split_editor.setDocument(QTextDocument(self.split_editor))
            return

        changed_document = self.split_editor.document() != source.document()
        self.split_editor.setDocument(source.document())
        for property_name in (
            "filePath", "displayName", "lineEnding", "fileEncoding"
        ):
            self.split_editor.setProperty(
                property_name, source.property(property_name)
            )
        self.split_editor.setFont(source.font())
        self.split_editor.set_tab_size(source.tab_size)
        self.split_editor.set_word_wrap(source.word_wrap)
        if changed_document:
            self.split_editor.setTextCursor(source.textCursor())
            self.split_editor.verticalScrollBar().setValue(
                source.verticalScrollBar().value()
            )

    def on_main_tab_changed(self, index: int) -> None:
        self._active_editor_group = "main"
        self.sync_split_view_document()
        self.update_window_state(index)
        self.schedule_session_save()

    def toggle_split_view(self, enabled: bool) -> None:
        self._split_view_enabled = enabled
        if enabled:
            source = self.tabs.currentWidget()
            if not isinstance(source, CodeEditor):
                self._split_view_enabled = False
                if self.split_view_action is not None:
                    self.split_view_action.setChecked(False)
                return
            if self.split_editor is None:
                self.split_editor = CodeEditor(self)
                self.split_editor.setMinimumWidth(120)
                self.split_editor.cursorPositionChanged.connect(
                    self.update_cursor_position
                )
                self.split_editor.cursorPositionChanged.connect(
                    self.update_edit_actions
                )
                self.split_editor.cursorPositionChanged.connect(
                    self.schedule_session_save
                )
                self.split_editor.focused.connect(self.activate_split_editor_group)
                self.editor_splitter.addWidget(self.split_editor)
            self.sync_split_view_document()
            self.split_editor.show()
            self.editor_splitter.setSizes([1, 1])
            self.schedule_session_save()
        else:
            if self.split_editor is not None:
                self.split_editor.hide()
            self._active_editor_group = "main"
            self.schedule_session_save()

    def current_editor(self) -> CodeEditor | None:
        if (
            self._active_editor_group == "split"
            and self._split_view_enabled
            and self.split_editor is not None
            and self._split_source is not None
        ):
            return self.split_editor
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, CodeEditor) else None

    def save_file(self) -> bool:
        editor = self.current_editor()
        if editor is None:
            return False
        path = str(editor.property("filePath"))
        if not path:
            return self.save_file_as()
        return self.write_editor_file(editor, Path(path))

    def save_file_as(self) -> bool:
        editor = self.current_editor()
        if editor is None:
            return False
        suggested_name = self.editor_display_title(editor).replace(os.sep, "-")
        suggested = str(editor.property("filePath")) or str(
            self.project_root / suggested_name
        )
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save File", suggested, "All Files (*)"
        )
        if not path:
            return False
        return self.write_editor_file(editor, Path(path))

    def save_all(self) -> None:
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if not isinstance(editor, CodeEditor) or not editor.document().isModified():
                continue
            path = str(editor.property("filePath"))
            if path:
                self.write_editor_file(editor, Path(path))
            else:
                self.tabs.setCurrentWidget(editor)
                self.save_file_as()
            if editor.document().isModified():
                return

    def save_with_encoding(self, encoding: str) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        previous_encoding = str(editor.property("fileEncoding") or "utf-8")
        editor.setProperty("fileEncoding", encoding)
        path = str(editor.property("filePath") or "")
        saved = (
            self.write_editor_file(editor, Path(path))
            if path
            else self.save_file_as()
        )
        if not saved:
            editor.setProperty("fileEncoding", previous_encoding)
            primary = self.primary_editor_for(editor)
            if primary is not None and editor is self.split_editor:
                primary.setProperty("fileEncoding", previous_encoding)

    def reopen_current_file(self, encoding: str) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        path = str(editor.property("filePath"))
        if not path:
            self.status.showMessage("Save the file before reopening with an encoding", 3500)
            return
        if editor.document().isModified():
            answer = QMessageBox.question(
                self,
                "Reopen with Encoding",
                "Discard unsaved changes and reopen this file?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            raw_content = Path(path).read_bytes().decode(encoding)
        except (OSError, UnicodeError) as error:
            QMessageBox.critical(self, "Cannot Reopen File", str(error))
            return
        line_ending = "\r\n" if "\r\n" in raw_content else "\r" if "\r" in raw_content else "\n"
        content = raw_content.replace("\r\n", "\n").replace("\r", "\n")
        editor.setPlainText(content)
        editor.document().setModified(False)
        editor.setProperty("fileEncoding", encoding)
        editor.setProperty("lineEnding", line_ending)
        self.apply_syntax_highlighter(editor)
        self.status.showMessage(f"Reopened using {encoding}", 3000)

    def revert_file(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        path = str(editor.property("filePath"))
        if not path:
            self.status.showMessage("This file has not been saved yet", 3000)
            return
        if editor.document().isModified():
            answer = QMessageBox.question(
                self,
                "Revert File",
                "Discard unsaved changes and restore the saved file?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        encoding = str(editor.property("fileEncoding") or "utf-8")
        try:
            raw_content = Path(path).read_bytes().decode(encoding)
        except (OSError, UnicodeError) as error:
            QMessageBox.critical(self, "Cannot Revert File", str(error))
            return
        line_ending = "\r\n" if "\r\n" in raw_content else "\r" if "\r" in raw_content else "\n"
        content = raw_content.replace("\r\n", "\n").replace("\r", "\n")
        editor.setPlainText(content)
        editor.document().setModified(False)
        editor.setProperty("lineEnding", line_ending)
        self.update_tab_label(editor)
        self.status.showMessage(f"Reverted {Path(path).name}", 3000)

    def print_current_file(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dialog = QPrintDialog(printer, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            editor.print_(printer)

    def write_editor_file(self, editor: CodeEditor, path: Path) -> bool:
        encoding = str(editor.property("fileEncoding") or "utf-8")
        line_ending = str(editor.property("lineEnding") or "\n")
        content = editor.toPlainText().replace("\r\n", "\n").replace("\r", "\n")
        content = content.replace("\n", line_ending)
        try:
            path.write_text(content, encoding=encoding, newline="")
        except (OSError, UnicodeError) as error:
            QMessageBox.critical(self, "Cannot Save File", str(error))
            return False
        primary = self.primary_editor_for(editor)
        if primary is None:
            return False
        targets = {primary}
        if (
            self.split_editor is not None
            and self.split_editor.document() == primary.document()
        ):
            targets.add(self.split_editor)
        for target in targets:
            target.setProperty("filePath", str(path.resolve()))
            target.setProperty("displayName", path.name)
            target.setProperty("fileEncoding", encoding)
            target.document().setModified(False)
        self.apply_syntax_highlighter(primary)
        self.update_tab_label(primary)
        self.remember_recent_file(str(path))
        self.status.showMessage(f"Saved {path.name}", 3000)
        self.reload_preference_file(path)
        return True

    def close_current_tab(self) -> None:
        index = self.tabs.currentIndex()
        if index >= 0:
            self.close_tab(index)

    def close_all_files(self) -> None:
        while self.tabs.count() > 0:
            if not self.close_tab(0, create_new_if_empty=False):
                return
        self.update_window_state()

    def close_tab(self, index: int, create_new_if_empty: bool = True) -> bool:
        editor = self.tabs.widget(index)
        if not isinstance(editor, CodeEditor):
            return False
        if editor.document().isModified():
            name = str(editor.property("displayName"))
            answer = QMessageBox.question(
                self,
                "Unsaved Changes",
                f"Save changes to {name} before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save:
                self.tabs.setCurrentWidget(editor)
                self.save_file()
                if editor.document().isModified():
                    return False
        if self.macro_recording and self.macro_editor is editor:
            self.toggle_macro_recording()
        self.highlighters.pop(editor, None)
        self.navigation_back = [entry for entry in self.navigation_back if entry[0] is not editor]
        self.navigation_forward = [entry for entry in self.navigation_forward if entry[0] is not editor]
        self.tabs.removeTab(index)
        editor.deleteLater()
        if self.tabs.count() == 0 and create_new_if_empty:
            self.new_file()
        self.update_window_state()
        self.update_goto_actions()
        return True

    def update_tab_label(self, editor: CodeEditor) -> None:
        editor = self.primary_editor_for(editor)
        if editor is None:
            return
        index = self.tabs.indexOf(editor)
        if index < 0:
            return
        name = self.editor_display_title(editor)
        marker = " •" if editor.document().isModified() else ""
        self.tabs.setTabText(index, name + marker)
        self.tabs.setTabToolTip(
            index,
            str(editor.property("filePath")) or str(editor.property("displayName")) or name,
        )
        self.refresh_open_files()
        if editor is self.primary_editor_for(self.current_editor()):
            self.update_window_state()

    def update_window_state(self, _index: int = -1) -> None:
        editor = self.current_editor()
        if editor is None:
            self.setWindowTitle("SubText Editor")
            self.cursor_label.setText("Line 1, Column 1")
            self.syntax_label.setText("Plain Text")
            self.line_endings_label.setText("LF")
            for action in (
                self.action_save,
                self.action_save_as,
                self.action_save_all,
                self.action_print,
                self.action_close,
                self.action_close_all,
                self.action_revert,
            ):
                action.setEnabled(False)
            self.update_edit_actions()
            self.update_selection_actions()
            self.update_view_actions()
            self.update_goto_actions()
            self.update_tool_actions()
            return
        name = self.editor_display_title(editor)
        marker = " •" if editor.document().isModified() else ""
        self.setWindowTitle(f"{name}{marker} — SubText Editor")
        self.update_cursor_position()
        self.syntax_label.setText(self.effective_syntax(editor))
        line_ending = str(editor.property("lineEnding") or "\n")
        self.line_endings_label.setText({"\r\n": "CRLF", "\r": "CR", "\n": "LF"}.get(line_ending, "LF"))
        self.action_save.setEnabled(True)
        self.action_save_as.setEnabled(True)
        self.action_save_all.setEnabled(True)
        self.action_print.setEnabled(True)
        self.action_close.setEnabled(True)
        self.action_close_all.setEnabled(self.tabs.count() > 0)
        self.action_revert.setEnabled(bool(editor.property("filePath")))
        self.update_edit_actions()
        self.update_selection_actions()
        self.update_view_actions()
        self.update_goto_actions()
        self.update_tool_actions()

    def update_cursor_position(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        self.cursor_label.setText(
            f"Line {cursor.blockNumber() + 1}, Column {cursor.positionInBlock() + 1}"
        )

    def set_tab_size(self, columns: int, *, persist: bool = True) -> None:
        self.tab_size = max(1, min(16, int(columns)))
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if isinstance(editor, CodeEditor):
                editor.set_tab_size(self.tab_size)
        self.tab_size_label.setText(f"Tab Size: {self.tab_size}")
        for size, action in self.tab_size_actions.items():
            action.setChecked(size == self.tab_size)
        for size, action in self.view_tab_size_actions.items():
            self.set_action_checked(action, size == self.tab_size)
        if persist:
            self.settings.setValue("tab_size", self.tab_size)
            self.persist_preference_value("tab_size", self.tab_size)

    @staticmethod
    def syntax_for(name: str) -> str:
        suffix = Path(name).suffix.lower()
        return {".py": "Python", ".txt": "Plain Text", ".md": "Markdown"}.get(
            suffix, "Plain Text"
        )

    def apply_syntax_highlighter(self, editor: CodeEditor) -> None:
        previous = self.highlighters.pop(editor, None)
        if previous is not None:
            previous.setDocument(None)
            previous.deleteLater()
        syntax = self.effective_syntax(editor)
        scheme_name = str(editor.property("colorScheme") or self.color_scheme_name)
        colors = self.resolve_color_scheme(scheme_name)
        self.apply_editor_color_scheme(editor, colors)
        if syntax == "Python":
            self.highlighters[editor] = PythonHighlighter(editor.document(), colors)
        self.update_window_state()

    def editor_call(self, method: str) -> None:
        editor = self.current_editor()
        if editor is not None:
            if method in ("undo", "redo") and editor.multi_selection_ranges:
                editor.clear_multi_selections()
            getattr(editor, method)()

    def update_selection_actions(self) -> None:
        available = self.current_editor() is not None
        for action in (
            self.action_expand_smart,
            self.action_expand_line,
            self.action_expand_line_up,
            self.action_expand_word,
            self.action_expand_block,
            self.action_expand_paragraph,
            self.action_expand_brackets,
            self.action_expand_indentation,
        ):
            action.setEnabled(available)
        # These commands require multiple independent cursors or syntax scopes.
        self.action_split_selection_lines.setEnabled(False)
        self.action_single_selection.setEnabled(False)
        self.action_expand_scope.setEnabled(False)
        self.action_add_previous_line.setEnabled(False)
        self.action_add_next_line.setEnabled(False)

    def single_selection(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        cursor.clearSelection()
        editor.setTextCursor(cursor)

    def split_selection_into_lines(self) -> None:
        self.status.showMessage("Multiple selections are not available yet", 3000)

    @staticmethod
    def qt_to_python_position(text: str, position: int) -> int:
        qt_position = 0
        for index, char in enumerate(text):
            if qt_position >= position:
                return index
            qt_position += len(char.encode("utf-16-le")) // 2
            if qt_position >= position:
                return index + 1
        return len(text)

    @staticmethod
    def python_to_qt_position(text: str, position: int) -> int:
        return len(text[:position].encode("utf-16-le")) // 2

    def set_python_selection(
        self,
        editor: CodeEditor,
        start: int,
        end: int,
        forward: bool = True,
    ) -> None:
        text = editor.toPlainText()
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))
        qt_start = self.python_to_qt_position(text, start)
        qt_end = self.python_to_qt_position(text, end)
        cursor = QTextCursor(editor.document())
        if forward:
            cursor.setPosition(qt_start)
            cursor.setPosition(qt_end, QTextCursor.MoveMode.KeepAnchor)
        else:
            cursor.setPosition(qt_end)
            cursor.setPosition(qt_start, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        editor.setFocus()

    def python_selection_range(self, editor: CodeEditor) -> tuple[str, int, int, bool, int]:
        text = editor.toPlainText()
        cursor = editor.textCursor()
        anchor = self.qt_to_python_position(text, cursor.anchor())
        position = self.qt_to_python_position(text, cursor.position())
        return text, min(anchor, position), max(anchor, position), position >= anchor, position

    @staticmethod
    def line_range(text: str, position: int) -> tuple[int, int]:
        position = max(0, min(position, len(text)))
        start = text.rfind("\n", 0, position) + 1
        newline = text.find("\n", position)
        end = len(text) if newline < 0 else newline + 1
        return start, end

    @staticmethod
    def paragraph_range(text: str, position: int) -> tuple[int, int]:
        spans = []
        start = 0
        for line in text.splitlines(keepends=True):
            content_end = start + len(line.rstrip("\r\n"))
            spans.append((start, start + len(line), content_end, line.strip() == ""))
            start += len(line)
        if not spans or (text.endswith("\n") and position == len(text)):
            spans.append((len(text), len(text), len(text), False))
        current = next(
            (
                index
                for index, span in enumerate(spans)
                if span[0] <= position < span[1]
                or (index == len(spans) - 1 and position == span[1])
            ),
            len(spans) - 1,
        )
        if spans[current][3]:
            return spans[current][0], spans[current][1]
        first = current
        last = current
        while first > 0 and not spans[first - 1][3]:
            first -= 1
        while last + 1 < len(spans) and not spans[last + 1][3]:
            last += 1
        return spans[first][0], spans[last][2]

    @staticmethod
    def bracket_ranges(text: str) -> list[tuple[int, int]]:
        pairs = {"(": ")", "[": "]", "{": "}"}
        reverse = {value: key for key, value in pairs.items()}
        stack: list[tuple[str, int]] = []
        ranges = []
        quote = None
        escaped = False
        for index, char in enumerate(text):
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in ("'", '"', "`"):
                quote = char
            elif char in pairs:
                stack.append((char, index))
            elif char in reverse and stack and stack[-1][0] == reverse[char]:
                _opening, start = stack.pop()
                ranges.append((start, index + 1))
        return ranges

    @staticmethod
    def word_range(text: str, position: int) -> tuple[int, int] | None:
        words = list(re.finditer(r"\w+", text, flags=re.UNICODE))
        return next(
            ((match.start(), match.end()) for match in words if match.start() <= position < match.end()),
            next(
                ((match.start(), match.end()) for match in words if match.start() < position <= match.end()),
                None,
            ),
        )

    @staticmethod
    def indentation_range(text: str, position: int) -> tuple[int, int]:
        spans = []
        start = 0
        for line in text.splitlines(keepends=True):
            content = line.rstrip("\r\n")
            indentation = len(content) - len(content.lstrip(" \t"))
            spans.append((start, start + len(line), content, indentation))
            start += len(line)
        if not spans or (text.endswith("\n") and position == len(text)):
            spans.append((len(text), len(text), "", 0))
        current = next(
            (
                index
                for index, span in enumerate(spans)
                if span[0] <= position < span[1]
                or (index == len(spans) - 1 and position == span[1])
            ),
            len(spans) - 1,
        )
        base_indent = spans[current][3]
        first = current
        last = current
        while first > 0:
            previous = spans[first - 1]
            if not previous[2].strip() or previous[3] < base_indent:
                break
            first -= 1
        while last + 1 < len(spans):
            following = spans[last + 1]
            if not following[2].strip() or following[3] < base_indent:
                break
            last += 1
        return spans[first][0], spans[last][1]

    def expand_selection(self, mode: str) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        text, start, end, forward, position = self.python_selection_range(editor)

        if mode == "word":
            word = self.word_range(text, position)
            if word is None:
                return
            if start != end:
                word = (min(start, word[0]), max(end, word[1]))
            self.set_python_selection(editor, *word, forward=forward)
            return

        if mode in ("line", "line_up"):
            range_start, _ = self.line_range(text, start)
            _, range_end = self.line_range(text, max(start, end - 1) if end > start else position)
            is_full_lines = start == range_start and end == range_end
            if start == end:
                range_start, range_end = self.line_range(text, position)
            elif not is_full_lines:
                range_start, range_end = self.line_range(text, start)[0], self.line_range(text, end - 1)[1]

            move_up = mode == "line_up" or (mode == "line" and is_full_lines and not forward)
            move_down = mode == "line" and is_full_lines and forward
            if move_up and range_start > 0:
                range_start = self.line_range(text, range_start - 1)[0]
            elif move_down and range_end < len(text):
                range_end = self.line_range(text, range_end)[1]
            self.set_python_selection(editor, range_start, range_end, forward=not move_up)
            return

        if mode in ("block", "paragraph"):
            target = self.paragraph_range(text, position if start == end else start)
            if start != end:
                target = (min(start, target[0]), max(end, target[1]))
        elif mode == "indentation":
            target = self.indentation_range(text, position if start == end else start)
            if start != end:
                target = (min(start, target[0]), max(end, target[1]))
        elif mode == "brackets":
            candidates = [
                candidate
                for candidate in self.bracket_ranges(text)
                if candidate[0] <= (position if start == end else start)
                and candidate[1] >= (position if start == end else end)
                and (candidate[0] < start or candidate[1] > end or start == end)
            ]
            if not candidates:
                return
            target = min(candidates, key=lambda candidate: candidate[1] - candidate[0])
        elif mode == "smart":
            point = position if start == end else start
            candidates = self.bracket_ranges(text)
            candidates.append(self.line_range(text, point))
            candidates.append(self.paragraph_range(text, point))
            candidates.append(self.indentation_range(text, point))
            word = self.word_range(text, point)
            if word:
                candidates.append(word)
            candidates = [
                candidate
                for candidate in candidates
                if candidate[0] <= point
                and candidate[1] >= (point if start == end else end)
                and (candidate[0] < start or candidate[1] > end or start == end)
            ]
            if not candidates:
                return
            target = min(candidates, key=lambda candidate: candidate[1] - candidate[0])
        elif mode == "scope":
            self.status.showMessage("Syntax scope selection is not available yet", 3000)
            return
        else:
            return
        self.set_python_selection(editor, *target, forward=forward)

    def add_line_to_selection(self, direction: int) -> None:
        self.status.showMessage("Adding independent line cursors is not available yet", 3000)

    def add_disabled_submenu(self, parent, title: str, labels: tuple[str, ...]) -> None:
        submenu = parent.addMenu(title)
        submenu.setEnabled(False)
        for label in labels:
            submenu.addAction(label).setEnabled(False)

    def update_edit_actions(self, *_args) -> None:
        editor = self.current_editor()
        if editor is None:
            for action in (
                self.action_undo,
                self.action_redo,
                self.action_cut,
                self.action_copy,
                self.action_copy_html,
                self.action_paste,
                self.action_paste_indent,
                self.action_toggle_comment,
                self.action_toggle_block_comment,
            ):
                action.setEnabled(False)
            return

        has_selection = editor.textCursor().hasSelection()
        clipboard = QApplication.clipboard()
        has_clipboard_text = bool(clipboard.text())
        self.action_undo.setEnabled(editor.document().isUndoAvailable())
        self.action_redo.setEnabled(editor.document().isRedoAvailable())
        self.action_cut.setEnabled(has_selection)
        self.action_copy.setEnabled(has_selection)
        self.action_copy_html.setEnabled(has_selection)
        self.action_paste.setEnabled(has_clipboard_text)
        self.action_paste_indent.setEnabled(has_clipboard_text)
        _line_prefix, block_pair = self.comment_syntax(editor)
        self.action_toggle_comment.setEnabled(_line_prefix is not None or block_pair is not None)
        self.action_toggle_block_comment.setEnabled(block_pair is not None)

    @staticmethod
    def comment_syntax(editor: CodeEditor) -> tuple[str | None, tuple[str, str] | None]:
        name = str(editor.property("filePath") or editor.property("displayName"))
        suffix = Path(name).suffix.lower()
        line_prefixes = {
            ".py": "#",
            ".sh": "#",
            ".bash": "#",
            ".zsh": "#",
            ".yaml": "#",
            ".yml": "#",
            ".toml": "#",
            ".rb": "#",
            ".pl": "#",
            ".ps1": "#",
            ".r": "#",
            ".c": "//",
            ".h": "//",
            ".cc": "//",
            ".cpp": "//",
            ".hpp": "//",
            ".js": "//",
            ".jsx": "//",
            ".ts": "//",
            ".tsx": "//",
            ".java": "//",
            ".go": "//",
            ".rs": "//",
            ".kt": "//",
            ".swift": "//",
            ".css": "//",
        }
        block_pairs = {
            ".c": ("/*", "*/"),
            ".h": ("/*", "*/"),
            ".cc": ("/*", "*/"),
            ".cpp": ("/*", "*/"),
            ".hpp": ("/*", "*/"),
            ".js": ("/*", "*/"),
            ".jsx": ("/*", "*/"),
            ".ts": ("/*", "*/"),
            ".tsx": ("/*", "*/"),
            ".java": ("/*", "*/"),
            ".go": ("/*", "*/"),
            ".rs": ("/*", "*/"),
            ".kt": ("/*", "*/"),
            ".swift": ("/*", "*/"),
            ".css": ("/*", "*/"),
            ".html": ("<!--", "-->"),
            ".htm": ("<!--", "-->"),
            ".xml": ("<!--", "-->"),
            ".svg": ("<!--", "-->"),
        }
        return line_prefixes.get(suffix), block_pairs.get(suffix)

    @staticmethod
    def selected_blocks(editor: CodeEditor):
        cursor = editor.textCursor()
        document = editor.document()
        if cursor.hasSelection():
            start = min(cursor.position(), cursor.anchor())
            end = max(cursor.position(), cursor.anchor())
            last_position = max(start, end - 1)
        else:
            start = end = cursor.position()
            last_position = start
        first = document.findBlock(start)
        last = document.findBlock(last_position)
        blocks = []
        block = first
        while block.isValid():
            blocks.append(block)
            if block == last:
                break
            block = block.next()
        return blocks

    def replace_block_range(self, editor: CodeEditor, first, last, lines: list[str]) -> None:
        document = editor.document()
        start = first.position()
        has_next = last.next().isValid()
        end = last.position() + len(last.text())
        if has_next:
            end += 1
        elif not lines and first.previous().isValid():
            # Removing the last line also removes its preceding line break.
            start -= 1
        replacement = "\n".join(lines)
        if has_next and lines:
            replacement += "\n"
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(replacement)
        cursor.endEditBlock()
        editor.setTextCursor(cursor)
        editor.setFocus()

    def replace_selected_lines(self, transform) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        blocks = self.selected_blocks(editor)
        if not blocks:
            return
        self.replace_block_range(editor, blocks[0], blocks[-1], transform([b.text() for b in blocks]))

    def copy_text(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        if editor.multi_selection_ranges:
            text = editor.toPlainText()
            selected = [
                text[start:end]
                for start, end in editor.multi_selection_ranges
                if start != end
            ]
            if selected:
                QApplication.clipboard().setText("\n".join(selected))
                self.remember_clipboard(QApplication.clipboard().text())
            return
        if editor.textCursor().hasSelection():
            editor.copy()
            self.remember_clipboard(QApplication.clipboard().text())

    def cut_text(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        if editor.multi_selection_ranges:
            text = editor.toPlainText()
            selected = [
                text[start:end]
                for start, end in editor.multi_selection_ranges
                if start != end
            ]
            if selected:
                QApplication.clipboard().setText("\n".join(selected))
                editor.replace_multi_selections("", cut=True)
                self.remember_clipboard(QApplication.clipboard().text())
            return
        if not editor.textCursor().hasSelection():
            return
        editor.cut()
        self.remember_clipboard(QApplication.clipboard().text())

    def copy_as_html(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        selected = editor.textCursor().selectedText().replace("\u2029", "\n")
        if not selected:
            return
        clipboard = QApplication.clipboard()
        if QApplication.platformName() == "offscreen":
            # Qt's offscreen clipboard crashes during teardown with custom MIME payloads.
            clipboard.setText(selected)
        else:
            mime = QMimeData()
            mime.setText(selected)
            mime.setHtml(f"<pre>{html.escape(selected)}</pre>")
            clipboard.setMimeData(mime)
        self.remember_clipboard(selected)

    def paste_text(self) -> None:
        editor = self.current_editor()
        if editor is not None:
            pasted = QApplication.clipboard().text()
            self.remember_clipboard(pasted)
            if editor.multi_selection_ranges:
                editor.replace_multi_selections(pasted)
            else:
                editor.paste()

    def paste_and_indent(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        pasted = QApplication.clipboard().text()
        if not pasted:
            return
        cursor = editor.textCursor()
        leading = re.match(r"[ \t]*", cursor.block().text()).group(0)
        lines = pasted.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(lines) > 1:
            pasted = lines[0] + "\n" + "\n".join(leading + line if line else "" for line in lines[1:])
        cursor.insertText(pasted)
        editor.setTextCursor(cursor)
        self.remember_clipboard(QApplication.clipboard().text())

    def remember_clipboard(self, value: str) -> None:
        if value:
            self.clipboard_history = [value] + [item for item in self.clipboard_history if item != value]
            del self.clipboard_history[10:]

    def populate_clipboard_history(self) -> None:
        menu = self.sender()
        if menu is None:
            return
        menu.clear()
        current = QApplication.clipboard().text()
        self.remember_clipboard(current)
        if not self.clipboard_history:
            menu.addAction("Clipboard history is empty").setEnabled(False)
            return
        for value in self.clipboard_history:
            label = value.replace("\n", " ↵ ").replace("\t", " ⇥ ")
            if len(label) > 48:
                label = label[:45] + "…"
            action = menu.addAction(label)
            action.setToolTip(value)
            action.triggered.connect(lambda _checked=False, text=value: self.paste_history_text(text))

    def paste_history_text(self, value: str) -> None:
        editor = self.current_editor()
        if editor is not None:
            cursor = editor.textCursor()
            cursor.insertText(value)
            editor.setTextCursor(cursor)
            editor.setFocus()

    def indent_lines(self, unindent: bool) -> None:
        def transform(lines: list[str]) -> list[str]:
            if unindent:
                result = []
                for line in lines:
                    if line.startswith("\t"):
                        result.append(line[1:])
                    elif line.startswith(" "):
                        result.append(line[min(4, len(line) - len(line.lstrip(" "))):])
                    else:
                        result.append(line)
                return result
            return ["    " + line for line in lines]

        self.replace_selected_lines(transform)

    def reindent_lines(self) -> None:
        self.replace_selected_lines(lambda lines: [re.sub(r"^(\t+)", lambda m: "    " * len(m.group(1)), line) for line in lines])

    def move_lines(self, direction: int) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        blocks = self.selected_blocks(editor)
        if not blocks:
            return
        first, last = blocks[0], blocks[-1]
        if direction < 0:
            neighbor = first.previous()
            if not neighbor.isValid():
                return
            all_lines = [neighbor.text()] + [block.text() for block in blocks]
            ordered = all_lines[1:] + all_lines[:1]
            self.replace_block_range(editor, neighbor, last, ordered)
        else:
            neighbor = last.next()
            if not neighbor.isValid():
                return
            all_lines = [block.text() for block in blocks] + [neighbor.text()]
            ordered = all_lines[-1:] + all_lines[:-1]
            self.replace_block_range(editor, first, neighbor, ordered)

    def duplicate_lines(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        blocks = self.selected_blocks(editor)
        self.replace_block_range(
            editor,
            blocks[0],
            blocks[-1],
            [block.text() for block in blocks] * 2,
        )

    def delete_lines(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        blocks = self.selected_blocks(editor)
        if blocks:
            self.replace_block_range(editor, blocks[0], blocks[-1], [])

    def join_lines(self) -> None:
        self.replace_selected_lines(lambda lines: [re.sub(r"\s+$", "", lines[0]) + " " + " ".join(line.strip() for line in lines[1:]) if len(lines) > 1 else lines[0]])

    def insert_line(self, before: bool) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        cursor.clearSelection()
        if before:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.insertText("\n")
            cursor.movePosition(QTextCursor.MoveOperation.PreviousBlock)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertText("\n")
        editor.setTextCursor(cursor)
        editor.setFocus()

    def delete_word(self, forward: bool) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        if not cursor.hasSelection():
            cursor.movePosition(
                QTextCursor.MoveOperation.NextWord if forward else QTextCursor.MoveOperation.PreviousWord,
                QTextCursor.MoveMode.KeepAnchor,
            )
        cursor.removeSelectedText()
        editor.setTextCursor(cursor)

    def delete_to_line_edge(self, end: bool) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        if not cursor.hasSelection():
            cursor.movePosition(
                QTextCursor.MoveOperation.EndOfBlock if end else QTextCursor.MoveOperation.StartOfBlock,
                QTextCursor.MoveMode.KeepAnchor,
            )
        cursor.removeSelectedText()
        editor.setTextCursor(cursor)

    def transpose_text(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        if cursor.hasSelection():
            cursor.insertText(cursor.selectedText().replace("\u2029", "\n")[::-1])
        elif cursor.positionInBlock() > 1:
            cursor.movePosition(QTextCursor.MoveOperation.Left)
            cursor.movePosition(QTextCursor.MoveOperation.Left)
            cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
            cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(cursor.selectedText()[::-1])
        editor.setTextCursor(cursor)

    @staticmethod
    def identifier_words(value: str) -> list[str]:
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
        return re.findall(r"[A-Za-z0-9]+", expanded)

    def convert_case(self, kind: str) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        if cursor.hasSelection():
            original = cursor.selectedText().replace("\u2029", "\n")
        else:
            original = cursor.block().text()
        words = self.identifier_words(original)
        if kind == "upper":
            result = original.upper()
        elif kind == "lower":
            result = original.lower()
        elif kind == "title":
            result = original.title()
        elif kind == "swap":
            result = original.swapcase()
        elif kind == "snake":
            result = "_".join(word.lower() for word in words)
        elif kind == "kebab":
            result = "-".join(word.lower() for word in words)
        elif kind == "lower_camel":
            result = "".join([words[0].lower()] + [word.title() for word in words[1:]]) if words else original
        else:
            result = "".join(word.title() for word in words) if words else original
        if cursor.hasSelection():
            cursor.insertText(result)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(result)
        editor.setTextCursor(cursor)

    def wrap_text(self, width: int) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        selected = cursor.hasSelection()
        source = cursor.selectedText().replace("\u2029", "\n") if selected else cursor.block().text()
        wrapped = "\n".join(textwrap.fill(line, width=width) if line.strip() else "" for line in source.splitlines())
        if selected:
            cursor.insertText(wrapped)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(wrapped)
        editor.setTextCursor(cursor)

    def permute_lines(self, operation: str) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        blocks = self.selected_blocks(editor)
        if not blocks:
            return
        lines = [block.text() for block in blocks]
        if operation == "sort":
            lines.sort(key=str.casefold)
        elif operation == "sort_case":
            lines.sort()
        elif operation == "reverse":
            lines.reverse()
        elif operation == "unique":
            lines = list(dict.fromkeys(lines))
        elif operation == "shuffle":
            random.shuffle(lines)
        self.replace_block_range(editor, blocks[0], blocks[-1], lines)

    def toggle_comment(self, block: bool) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        line_prefix, block_pair = self.comment_syntax(editor)
        cursor = editor.textCursor()
        if block:
            if block_pair is None:
                return
            opening, closing = block_pair
            if cursor.hasSelection():
                selected = cursor.selectedText().replace("\u2029", "\n")
                if selected.startswith(opening) and selected.endswith(closing):
                    cursor.insertText(selected[len(opening):len(selected) - len(closing)])
                else:
                    cursor.insertText(f"{opening}{selected}{closing}")
            else:
                cursor.insertText(f"{opening}{closing}")
            editor.setTextCursor(cursor)
            return
        if line_prefix is None:
            if block_pair is not None:
                self.toggle_comment(block=True)
            return
        blocks = self.selected_blocks(editor)
        lines = [block.text() for block in blocks]
        nonblank = [line for line in lines if line.strip()]
        all_commented = bool(nonblank) and all(
            re.match(rf"^\s*{re.escape(line_prefix)}(?:\s|$)", line) for line in nonblank
        )

        def transform(values: list[str]) -> list[str]:
            result = []
            for line in values:
                if not line.strip():
                    result.append(line)
                    continue
                if all_commented:
                    result.append(re.sub(rf"^(\s*){re.escape(line_prefix)}\s?", r"\1", line, count=1))
                else:
                    indent = re.match(r"\s*", line).group(0)
                    result.append(f"{indent}{line_prefix} {line[len(indent):]}")
            return result

        self.replace_block_range(editor, blocks[0], blocks[-1], transform(lines))

    def show_completions(self) -> None:
        self.status.showMessage("Completions are not available in this editor yet", 3000)

    def show_find_bar(self, mode: str = "find") -> None:
        self.search_mode = mode
        self.incremental_find_active = False
        self.find_input.setPlaceholderText(
            "Find in files" if mode == "files" else "Find in current file"
        )
        self.find_bar.show()
        self.replace_row_widget.setVisible(mode == "replace")
        self.files_row_widget.setVisible(mode == "files")
        self.files_replace_row_widget.setVisible(mode == "files")
        if mode == "files" and not self.find_where_input.text().strip():
            editor = self.current_editor()
            current_path = str(editor.property("filePath")) if editor is not None else ""
            where = Path(current_path).parent if current_path else self.project_root
            if where != Path.home():
                self.find_where_input.setText(str(where))
        editor = self.current_editor()
        if editor is not None and editor.textCursor().hasSelection() and not self.find_input.text():
            self.find_input.setText(editor.textCursor().selectedText().replace("\u2029", "\n"))
        self.find_input.setFocus()
        self.find_input.selectAll()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt virtual method
        if (
            watched in (
                self.find_input,
                self.replace_input,
                self.find_where_input,
                self.replace_files_input,
            )
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self.hide_find_bar()
            return True
        return super().eventFilter(watched, event)

    def show_incremental_find(self) -> None:
        self.show_find_bar("find")
        editor = self.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        self.incremental_find_origin = self.qt_to_python_position(
            editor.toPlainText(),
            min(cursor.position(), cursor.anchor()) if cursor.hasSelection() else cursor.position(),
        )
        self.incremental_find_active = True
        if self.find_input.text():
            self.find_text(False, incremental=True)

    def show_replace_bar(self) -> None:
        self.show_find_bar("replace")

    def show_find_in_files(self) -> None:
        self.show_find_bar("files")

    def hide_find_bar(self) -> None:
        self.find_bar.hide()
        self.incremental_find_active = False
        editor = self.current_editor()
        if editor is not None:
            editor.setFocus()

    def on_find_return(self) -> None:
        if self.search_mode == "files":
            self.find_in_files()
            return
        if self.incremental_find_active:
            self.hide_find_bar()
        else:
            self.find_text(False)

    def on_find_text_changed(self, _value: str) -> None:
        if self.incremental_find_active:
            self.find_text(False, incremental=True)

    def search_pattern(self):
        query = self.find_input.text()
        if not query:
            return None
        source = query if self.regex_checkbox.isChecked() else re.escape(query)
        if self.whole_word_checkbox.isChecked():
            source = rf"(?<!\w)(?:{source})(?!\w)"
        flags = 0 if self.match_case_checkbox.isChecked() else re.IGNORECASE
        try:
            return re.compile(source, flags | re.MULTILINE)
        except re.error as error:
            self.status.showMessage(f"Invalid regular expression: {error}", 4000)
            return None

    def search_matches(self, text: str, pattern=None):
        pattern = pattern or self.search_pattern()
        if pattern is None:
            return []
        return [match for match in pattern.finditer(text) if match.end() > match.start()]

    def find_text(self, backwards: bool, incremental: bool = False) -> None:
        editor = self.current_editor()
        pattern = self.search_pattern()
        if editor is None or pattern is None:
            return
        text = editor.toPlainText()
        matches = self.search_matches(text, pattern)
        if not matches:
            self.status.showMessage("No matches", 2500)
            return

        cursor = editor.textCursor()
        if incremental and self.incremental_find_active:
            origin = self.incremental_find_origin
        elif backwards:
            origin = self.qt_to_python_position(text, min(cursor.position(), cursor.anchor()))
        else:
            origin = self.qt_to_python_position(text, max(cursor.position(), cursor.anchor()))
        if backwards:
            eligible = [index for index, match in enumerate(matches) if match.end() <= origin]
            match_index = eligible[-1] if eligible else len(matches) - 1
        else:
            eligible = [index for index, match in enumerate(matches) if match.start() >= origin]
            match_index = eligible[0] if eligible else 0
        match = matches[match_index]
        self.set_python_selection(editor, match.start(), match.end())
        self.status.showMessage(f"Match {match_index + 1} of {len(matches)}", 2500)

    def replace_next(self) -> None:
        editor = self.current_editor()
        pattern = self.search_pattern()
        if editor is None or pattern is None:
            self.show_replace_bar()
            return
        cursor = editor.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n")
        match = pattern.fullmatch(selected) if selected else None
        if match is None:
            self.find_text(False)
            return
        replacement = self.replace_input.text()
        if self.regex_checkbox.isChecked():
            try:
                replacement = match.expand(replacement)
            except re.error as error:
                self.status.showMessage(f"Invalid replacement expression: {error}", 4000)
                return
        cursor.insertText(replacement)
        editor.setTextCursor(cursor)
        editor.setFocus()
        self.find_text(False)

    def replace_all_current_file(self) -> None:
        editor = self.current_editor()
        pattern = self.search_pattern()
        if editor is None or pattern is None:
            return
        text = editor.toPlainText()
        replacement = self.replace_input.text()
        if self.regex_checkbox.isChecked():
            try:
                updated, count = pattern.subn(replacement, text)
            except (re.error, IndexError) as error:
                self.status.showMessage(f"Invalid replacement expression: {error}", 4000)
                return
        else:
            updated, count = pattern.subn(lambda _match: replacement, text)
        if count:
            cursor = QTextCursor(editor.document())
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(updated)
            cursor.endEditBlock()
            editor.setTextCursor(cursor)
            editor.setFocus()
        self.status.showMessage(f"Replaced {count} occurrence(s) in this file", 3000)

    def quick_search_term(self) -> str:
        editor = self.current_editor()
        if editor is None:
            return ""
        if editor.multi_selection_ranges and self.find_input.text():
            return self.find_input.text()
        cursor = editor.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n")
        if selected:
            return selected
        text, _start, _end, _forward, position = self.python_selection_range(editor)
        word = self.word_range(text, position)
        return text[word[0]:word[1]] if word else ""

    def quick_find(self) -> None:
        term = self.quick_search_term()
        if not term:
            self.show_find_bar("find")
            return
        self.show_find_bar("find")
        self.find_input.setText(term)
        self.find_text(False)

    def quick_find_all(self) -> None:
        term = self.quick_search_term()
        if not term:
            self.show_find_bar("find")
            return
        self.find_input.setText(term)
        editor = self.current_editor()
        pattern = self.search_pattern()
        if editor is None or pattern is None:
            return
        matches = self.search_matches(editor.toPlainText(), pattern)
        if not matches:
            self.status.showMessage("No matches", 2500)
            return
        ranges = [(match.start(), match.end()) for match in matches]
        editor.set_multi_selections(ranges, len(ranges) - 1)
        self.status.showMessage(f"Selected {len(ranges)} occurrence(s)", 3000)

    def quick_add_next(self) -> None:
        self.add_next_quick_selection(skip=0)

    def quick_skip_next(self) -> None:
        self.add_next_quick_selection(skip=1)

    def add_next_quick_selection(self, skip: int) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        term = self.quick_search_term()
        if not term:
            return

        ranges = list(editor.multi_selection_ranges)
        if not ranges:
            text, _start, _end, _forward, position = self.python_selection_range(editor)
            if editor.textCursor().hasSelection():
                initial = (_start, _end)
            else:
                initial = self.word_range(text, position)
            if initial is None:
                self.status.showMessage("Place the caret on a word or select text first", 3000)
                return
            term = text[initial[0]:initial[1]]
            ranges = [initial]
            self.find_input.setText(term)
            editor.set_multi_selections(ranges, 0)

        self.find_input.setText(term)
        pattern = self.search_pattern()
        if pattern is None:
            return
        text = editor.toPlainText()
        matches = self.search_matches(text, pattern)
        cursor = editor.textCursor()
        origin = self.qt_to_python_position(text, max(cursor.position(), cursor.anchor()))
        selected_starts = {start for start, _end in ranges}
        future = [
            match for match in matches
            if match.start() >= origin and match.start() not in selected_starts
        ]
        if len(future) <= skip:
            self.status.showMessage("No further occurrence", 2500)
            return
        match = future[skip]
        ranges.append((match.start(), match.end()))
        ordered = sorted(set(ranges))
        editor.set_multi_selections(ordered, ordered.index((match.start(), match.end())))
        self.status.showMessage(f"Selected {len(ordered)} occurrence(s)", 2500)

    def use_selection_for(self, target: str) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        selected = self.quick_search_term()
        if target == "find":
            self.show_find_bar("find")
            if selected:
                self.find_input.setText(selected)
                self.find_input.setFocus()
                self.find_input.selectAll()
        else:
            self.show_replace_bar()
            if selected:
                self.find_input.setText(selected)
                self.find_input.setFocus()
                self.find_input.selectAll()

    def set_find_results(self, results: list[dict], in_files: bool) -> None:
        self.find_results = results
        self.find_results_in_files = in_files
        self.find_results_list.clear()
        for index, result in enumerate(results):
            item = QListWidgetItem(
                f"{result['path']}:{result['line']}: {result['snippet']}"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.find_results_list.addItem(item)
        if results:
            self.find_results_list.setCurrentRow(0)
        self.update_find_actions()

    def update_find_actions(self) -> None:
        has_results = bool(self.find_results)
        self.action_show_find_results.setEnabled(has_results)
        self.action_next_result.setEnabled(has_results)
        self.action_previous_result.setEnabled(has_results)
        self.action_cancel_find.setEnabled(has_results and self.find_results_in_files)

    def find_all_current_file(self) -> None:
        editor = self.current_editor()
        pattern = self.search_pattern()
        if editor is None or pattern is None:
            return
        text = editor.toPlainText()
        path = str(editor.property("filePath")) or str(editor.property("displayName"))
        results = self.make_search_results(text, pattern, path, editor)
        self.set_find_results(results, in_files=False)
        self.show_find_results()
        self.status.showMessage(f"Found {len(results)} occurrence(s) in {path}", 3000)

    @staticmethod
    def make_search_results(text: str, pattern, path: str, editor=None) -> list[dict]:
        results = []
        for match in pattern.finditer(text):
            if match.end() <= match.start():
                continue
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.start())
            if line_end < 0:
                line_end = len(text)
            results.append(
                {
                    "path": path,
                    "editor": editor,
                    "start": match.start(),
                    "end": match.end(),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "snippet": text[line_start:line_end].strip(),
                }
            )
            if len(results) >= 5000:
                break
        return results

    def show_find_results(self) -> None:
        if self.find_results:
            self.find_results_dock.show()
            self.find_results_dock.raise_()

    def move_find_result(self, direction: int) -> None:
        count = len(self.find_results)
        if not count:
            return
        row = self.find_results_list.currentRow()
        next_row = (row + direction) % count
        self.find_results_list.setCurrentRow(next_row)
        self.open_find_result(next_row)

    def open_find_result_item(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(index, int):
            self.open_find_result(index)

    def open_find_result(self, index: int) -> None:
        if not 0 <= index < len(self.find_results):
            return
        result = self.find_results[index]
        editor = result.get("editor")
        if editor is None or self.tabs.indexOf(editor) < 0:
            path = result["path"]
            if not Path(path).is_file():
                return
            self.record_current_location()
            self.open_path(path)
            editor = self.current_editor()
        if editor is None:
            return
        line = result["line"]
        line_start = editor.toPlainText().rfind("\n", 0, result["start"]) + 1
        self.navigate_to(
            editor, line, result["start"] - line_start + 1,
            record_history=result.get("editor") is editor,
        )
        self.set_python_selection(editor, result["start"], result["end"])
        editor.centerCursor()

    def find_in_files(self) -> None:
        pattern = self.search_pattern()
        if pattern is None:
            return
        results = []
        seen_paths = set()
        open_buffers = []
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if not isinstance(editor, CodeEditor):
                continue
            path = str(editor.property("filePath"))
            if path:
                seen_paths.add(str(Path(path).resolve()))
            open_buffers.append((path or str(editor.property("displayName")), editor, editor.toPlainText()))

        location_text = self.find_where_input.text().strip()
        roots = [Path(part.strip()).expanduser().resolve() for part in re.split(r"[;,]", location_text) if part.strip()]
        for label, editor, text in open_buffers:
            path = Path(label)
            if not location_text or (path.is_absolute() and any(
                root == path.resolve() or (root.is_dir() and root in path.resolve().parents)
                for root in roots
            )):
                results.extend(self.make_search_results(text, pattern, label, editor))

        read_bytes = 0
        skipped_dirs = {".git", ".venv", "node_modules", "__pycache__", ".cache", ".pytest_cache"}
        for root in roots:
            if root.is_file():
                candidates = [root]
            elif root.is_dir():
                candidates = []
                for candidate in root.rglob("*"):
                    try:
                        relative = candidate.relative_to(root)
                    except ValueError:
                        continue
                    if any(part in skipped_dirs or part.startswith(".") for part in relative.parts[:-1]):
                        continue
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    candidates.append(candidate)
                    if len(candidates) >= 2000:
                        break
            else:
                self.status.showMessage(f"Search location not found: {root}", 4000)
                continue
            for candidate in candidates:
                resolved = str(candidate.resolve())
                if resolved in seen_paths:
                    continue
                try:
                    size = candidate.stat().st_size
                    if size > 2 * 1024 * 1024 or read_bytes + size > 64 * 1024 * 1024:
                        continue
                    raw = candidate.read_bytes()
                    read_bytes += len(raw)
                    if b"\0" in raw:
                        continue
                    text = raw.decode("utf-8")
                except (OSError, UnicodeError):
                    continue
                results.extend(self.make_search_results(text, pattern, resolved, None))
                if len(results) >= 5000:
                    results = results[:5000]
                    break
            if len(results) >= 5000:
                break

        self.set_find_results(results, in_files=True)
        if results:
            self.show_find_results()
        self.status.showMessage(
            f"Found {len(results)} occurrence(s) in files and open buffers", 4000
        )

    def replace_all_in_files(self, confirm: bool = True) -> None:
        pattern = self.search_pattern()
        if pattern is None:
            return
        self.find_in_files()
        if not self.find_results:
            self.status.showMessage("No matches to replace", 3000)
            return

        targets = {}
        for result in self.find_results:
            editor = result.get("editor")
            try:
                editor_is_open = editor is not None and self.tabs.indexOf(editor) >= 0
            except RuntimeError:
                editor_is_open = False
            if editor_is_open:
                key = ("editor", id(editor))
                targets[key] = {"editor": editor, "path": result["path"]}
            else:
                key = ("path", result["path"])
                targets[key] = {"editor": None, "path": result["path"]}

        replacement = self.replace_files_input.text()
        plans = []
        for target in targets.values():
            editor = target["editor"]
            try:
                if editor is not None:
                    source = editor.toPlainText()
                    if self.regex_checkbox.isChecked():
                        updated, count = pattern.subn(replacement, source)
                    else:
                        updated, count = pattern.subn(lambda _match: replacement, source)
                    if count:
                        plans.append((target, updated, count))
                    continue

                path = Path(target["path"])
                if path.is_symlink() or not path.is_file():
                    continue
                raw = path.read_bytes()
                source = raw.decode("utf-8")
                if self.regex_checkbox.isChecked():
                    updated, count = pattern.subn(replacement, source)
                else:
                    updated, count = pattern.subn(lambda _match: replacement, source)
                if count:
                    plans.append((target, updated, count))
            except (OSError, UnicodeError, re.error, IndexError) as error:
                self.status.showMessage(
                    f"Could not replace in {target['path']}: {error}", 5000
                )
                return

        if not plans:
            self.status.showMessage("No matches to replace", 3000)
            return
        match_count = sum(count for _target, _updated, count in plans)
        if confirm:
            answer = QMessageBox.question(
                self,
                "Replace in Files",
                f"Replace {match_count} occurrence(s) across {len(plans)} file(s) or buffer(s)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        replaced = 0
        changed_targets = 0
        for target, updated, count in plans:
            editor = target["editor"]
            try:
                if editor is not None:
                    cursor = QTextCursor(editor.document())
                    cursor.beginEditBlock()
                    cursor.select(QTextCursor.SelectionType.Document)
                    cursor.insertText(updated)
                    cursor.endEditBlock()
                    editor.setTextCursor(cursor)
                else:
                    Path(target["path"]).write_bytes(updated.encode("utf-8"))
                replaced += count
                changed_targets += 1
            except (OSError, UnicodeError) as error:
                self.status.showMessage(
                    f"Could not update {target['path']}: {error}", 5000
                )

        self.set_find_results([], in_files=False)
        self.find_results_dock.hide()
        self.status.showMessage(
            f"Replaced {replaced} occurrence(s) across {changed_targets} file(s) or buffer(s)",
            5000,
        )

    def cancel_find_in_files(self) -> None:
        if not self.find_results_in_files:
            return
        self.set_find_results([], in_files=False)
        self.find_results_dock.hide()
        self.status.showMessage("Find in Files results cleared", 2500)

    def current_location(self) -> tuple[CodeEditor, int, int] | None:
        editor = self.current_editor()
        if editor is None:
            return None
        cursor = editor.textCursor()
        return editor, cursor.blockNumber() + 1, cursor.positionInBlock() + 1

    def record_current_location(self) -> None:
        location = self.current_location()
        if location is None:
            return
        if self.navigation_back and self.navigation_back[-1] == location:
            return
        self.navigation_back.append(location)
        del self.navigation_back[:-100]
        self.navigation_forward.clear()
        self.update_goto_actions()

    def navigate_to(
        self,
        editor: CodeEditor,
        line: int,
        column: int = 1,
        *,
        record_history: bool = True,
    ) -> None:
        if self.tabs.indexOf(editor) < 0:
            return
        target_line = max(1, min(line, editor.blockCount()))
        block = editor.document().findBlockByNumber(target_line - 1)
        if not block.isValid():
            return
        target_column = max(0, min(column - 1, max(0, block.length() - 1)))
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.Right, n=target_column)
        current = self.current_location()
        target = (editor, target_line, target_column + 1)
        if record_history and current is not None and current != target:
            self.navigation_back.append(current)
            del self.navigation_back[:-100]
            self.navigation_forward.clear()
        self.tabs.setCurrentWidget(editor)
        editor.setTextCursor(cursor)
        editor.centerCursor()
        editor.setFocus()
        self.update_goto_actions()

    def restore_navigation(self, stack: list[tuple[CodeEditor, int, int]], opposite: list[tuple[CodeEditor, int, int]]) -> None:
        while stack:
            target = stack.pop()
            editor, _line, _column = target
            if self.tabs.indexOf(editor) < 0:
                continue
            current = self.current_location()
            if current is not None:
                opposite.append(current)
                del opposite[:-100]
            self.navigate_to(*target, record_history=False)
            break
        self.update_goto_actions()

    def jump_back(self) -> None:
        self.restore_navigation(self.navigation_back, self.navigation_forward)

    def jump_forward(self) -> None:
        self.restore_navigation(self.navigation_forward, self.navigation_back)

    def update_goto_actions(self) -> None:
        self.action_jump_back.setEnabled(bool(self.navigation_back))
        self.action_jump_forward.setEnabled(bool(self.navigation_forward))
        editor = self.current_editor()
        has_bookmarks = bool(editor and editor.bookmark_cursors)
        self.action_next_bookmark.setEnabled(has_bookmarks)
        self.action_previous_bookmark.setEnabled(has_bookmarks)
        self.action_clear_bookmarks.setEnabled(has_bookmarks)
        self.action_toggle_bookmark.setEnabled(editor is not None)
        self.action_jump_matching_bracket.setEnabled(editor is not None)

    def go_to_line(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        value, accepted = QInputDialog.getText(
            self, "Goto Line", "Line number, optionally followed by :column:"
        )
        if not accepted:
            return
        match = re.fullmatch(r"\s*(\d+)(?::(\d+))?\s*", value)
        if match is None:
            self.status.showMessage("Enter a line number, optionally in line:column form", 3000)
            return
        self.navigate_to(editor, int(match.group(1)), int(match.group(2) or 1))

    def iter_project_files(
        self, suffixes: set[str] | None = None, *, max_files: int = 3000
    ) -> list[Path]:
        roots = [folder for folder in self.project_folders if folder.is_dir()]
        if not roots and self.project_root_explicit and self.project_root.is_dir():
            roots = [self.project_root]
        if not roots:
            return []
        skipped = {
            ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
            "__pycache__", ".cache", ".pytest_cache", "target", "dist",
        }
        found: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            for folder, directories, filenames in os.walk(root):
                directories[:] = sorted(
                    name for name in directories if name not in skipped and not name.startswith(".")
                )
                for filename in sorted(filenames):
                    path = Path(folder) / filename
                    resolved = str(path.resolve())
                    if resolved in seen or path.is_symlink() or (suffixes and path.suffix.lower() not in suffixes):
                        continue
                    seen.add(resolved)
                    found.append(path)
                    if len(found) >= max_files:
                        return found
        return found

    def project_text_sources(
        self, suffixes: set[str] | None = None
    ) -> list[tuple[str, CodeEditor | None, str]]:
        sources: list[tuple[str, CodeEditor | None, str]] = []
        seen_paths: set[str] = set()
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            if not isinstance(editor, CodeEditor):
                continue
            path = str(editor.property("filePath"))
            name = str(editor.property("displayName"))
            label = path or name
            if path:
                seen_paths.add(str(Path(path).resolve()))
            sources.append((label, editor, editor.toPlainText()))

        bytes_read = 0
        for path in self.iter_project_files(suffixes):
            resolved = str(path.resolve())
            if resolved in seen_paths:
                continue
            try:
                size = path.stat().st_size
                if size > 2 * 1024 * 1024 or bytes_read + size > 64 * 1024 * 1024:
                    continue
                raw = path.read_bytes()
                bytes_read += len(raw)
                if b"\0" in raw:
                    continue
                content = raw.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            sources.append((str(path), None, content))
        return sources

    @staticmethod
    def symbols_from_text(text: str, path: str, editor: CodeEditor | None) -> list[dict]:
        suffix = Path(path).suffix.lower()
        found: list[dict] = []
        if suffix == ".py":
            try:
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        found.append({"name": node.name, "path": path, "editor": editor, "line": node.lineno})
            except SyntaxError:
                pass
        if suffix == ".md":
            for line_number, line in enumerate(text.splitlines(), 1):
                match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
                if match:
                    found.append({"name": match.group(1), "path": path, "editor": editor, "line": line_number})
        if not found:
            for line_number, line in enumerate(text.splitlines(), 1):
                match = re.match(
                    r"^\s*(?:async\s+def|def|class|function|fn|func|struct|enum|trait|interface)\s+([A-Za-z_$][\w$]*)",
                    line,
                )
                if match:
                    found.append({"name": match.group(1), "path": path, "editor": editor, "line": line_number})
        return found

    def project_symbols(self) -> list[dict]:
        source_types = {
            ".py", ".md", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h",
            ".cc", ".cpp", ".hpp", ".rs", ".go", ".rb", ".php", ".cs", ".swift",
            ".kt", ".kts",
        }
        return [
            symbol
            for path, editor, text in self.project_text_sources(source_types)
            for symbol in self.symbols_from_text(text, path, editor)
        ][:5000]

    def pick_symbol(
        self,
        symbols: list[dict],
        title: str,
        placeholder: str,
        *,
        initial_query: str = "",
    ) -> dict | None:
        if not symbols:
            self.status.showMessage("No symbols found", 3000)
            return None
        entries = [
            (symbol["name"], f"{symbol['path']}:{symbol['line']}", symbol)
            for symbol in symbols
        ]
        dialog = QuickAccessDialog(title, placeholder, entries, self, initial_query)
        if dialog.exec() != QDialog.DialogCode.Accepted or not isinstance(dialog.selected_value, dict):
            return None
        return dialog.selected_value

    def goto_symbol(self, initial_query: str = "") -> None:
        editor = self.current_editor()
        if editor is None:
            return
        path = str(editor.property("filePath")) or str(editor.property("displayName"))
        symbol = self.pick_symbol(
            self.symbols_from_text(editor.toPlainText(), path, editor),
            "Goto Symbol",
            "Filter symbols in this file",
            initial_query=initial_query,
        )
        if symbol:
            self.navigate_to(symbol["editor"], symbol["line"])

    def goto_symbol_in_project(self) -> None:
        symbol = self.pick_symbol(
            self.project_symbols(), "Goto Symbol in Project", "Filter project symbols"
        )
        if symbol:
            self.navigate_to_symbol(symbol)

    def symbol_under_cursor(self) -> str:
        editor = self.current_editor()
        if editor is None:
            return ""
        cursor = editor.textCursor()
        if cursor.hasSelection():
            return cursor.selectedText()
        text = editor.toPlainText()
        position = editor.qt_to_python_position(text, cursor.position())
        bounds = self.word_range(text, position)
        return text[bounds[0]:bounds[1]] if bounds else ""

    def navigate_to_symbol(self, symbol: dict) -> None:
        editor = symbol.get("editor")
        path = symbol["path"]
        if editor is None or self.tabs.indexOf(editor) < 0:
            if not Path(path).is_file():
                self.status.showMessage(f"File no longer exists: {path}", 3000)
                return
            self.record_current_location()
            self.open_path(path)
            editor = self.current_editor()
            self.navigate_to(editor, symbol["line"], record_history=False) if editor else None
            return
        self.navigate_to(editor, symbol["line"])

    def goto_definition(self) -> None:
        name = self.symbol_under_cursor()
        if not name:
            self.status.showMessage("Place the caret on a symbol first", 3000)
            return
        definitions = [symbol for symbol in self.project_symbols() if symbol["name"] == name]
        if len(definitions) == 1:
            self.navigate_to_symbol(definitions[0])
        elif definitions:
            chosen = self.pick_symbol(definitions, "Goto Definition", f"Definitions for {name}")
            if chosen:
                self.navigate_to_symbol(chosen)
        else:
            self.status.showMessage(f"No definition found for {name}", 3000)

    def goto_reference(self) -> None:
        name = self.symbol_under_cursor()
        if not name:
            self.status.showMessage("Place the caret on a symbol first", 3000)
            return
        try:
            pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        except re.error:
            return
        results: list[dict] = []
        source_types = {
            ".py", ".md", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h",
            ".cc", ".cpp", ".hpp", ".rs", ".go", ".rb", ".php", ".cs", ".swift",
            ".kt", ".kts",
        }
        for path, editor, text in self.project_text_sources(source_types):
            results.extend(self.make_search_results(text, pattern, path, editor))
            if len(results) >= 5000:
                results = results[:5000]
                break
        self.set_find_results(results, in_files=False)
        if results:
            self.show_find_results()
        else:
            self.find_results_dock.hide()
        self.status.showMessage(f"Found {len(results)} reference(s) for {name}", 3500)

    def quick_open(self) -> None:
        entries: list[tuple[str, str, object]] = []
        seen: set[str] = set()
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            name = str(editor.property("displayName"))
            path = str(editor.property("filePath"))
            key = str(Path(path).resolve()) if path else f"buffer:{id(editor)}"
            seen.add(key)
            entries.append((name, path or "Unsaved file", {"type": "editor", "editor": editor}))
        for path in self.recent_files:
            if Path(path).is_file() and str(Path(path).resolve()) not in seen:
                seen.add(str(Path(path).resolve()))
                entries.append((Path(path).name, path, {"type": "file", "path": path}))
        for path in self.iter_project_files(max_files=3000):
            resolved = str(path.resolve())
            if resolved not in seen:
                seen.add(resolved)
                root = next((folder for folder in self.project_folders if path.is_relative_to(folder)), None)
                label = str(path.relative_to(root)) if root is not None else path.name
                entries.append((label, str(path), {"type": "file", "path": str(path)}))
        if not entries:
            self.status.showMessage("No files to open", 2500)
            return
        dialog = QuickAccessDialog("Goto Anything", "Type a file name or :line[:column]", entries, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not isinstance(dialog.selected_value, dict):
            return
        query = dialog.query.text().strip()
        if query.startswith("@"):
            self.goto_symbol(query[1:])
            return
        if query.startswith("#"):
            editor = self.current_editor()
            term = query[1:].strip()
            if editor is not None and term:
                self.find_input.setText(term)
                self.find_all_current_file()
            return
        location = re.search(r":(\d+)(?::(\d+))?\s*$", query)
        if query.startswith(":") and location:
            editor = self.current_editor()
            if editor:
                self.navigate_to(editor, int(location.group(1)), int(location.group(2) or 1))
            return
        selected = dialog.selected_value
        if selected["type"] == "editor":
            editor = selected["editor"]
            if location:
                self.navigate_to(editor, int(location.group(1)), int(location.group(2) or 1))
            else:
                cursor = editor.textCursor()
                self.navigate_to(editor, cursor.blockNumber() + 1, cursor.positionInBlock() + 1)
            return
        path = selected["path"]
        self.record_current_location()
        self.open_path(path)
        editor = self.current_editor()
        if editor and location:
            self.navigate_to(editor, int(location.group(1)), int(location.group(2) or 1), record_history=False)

    def switch_file(self, direction: int) -> None:
        count = self.tabs.count()
        if count < 2:
            return
        index = (self.tabs.currentIndex() + direction) % count
        editor = self.tabs.widget(index)
        cursor = editor.textCursor()
        self.navigate_to(editor, cursor.blockNumber() + 1, cursor.positionInBlock() + 1)

    def populate_switch_file_menu(self) -> None:
        menu = self.switch_file_menu
        menu.clear()
        previous = menu.addAction("Previous File")
        previous.triggered.connect(lambda: self.switch_file(-1))
        previous.setEnabled(self.tabs.count() > 1)
        next_file = menu.addAction("Next File")
        next_file.triggered.connect(lambda: self.switch_file(1))
        next_file.setEnabled(self.tabs.count() > 1)
        if self.tabs.count():
            menu.addSeparator()
        group = QActionGroup(menu)
        group.setExclusive(True)
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            action = menu.addAction(self.tabs.tabText(index))
            action.setCheckable(True)
            action.setChecked(index == self.tabs.currentIndex())
            group.addAction(action)
            action.setToolTip(str(editor.property("filePath") or editor.property("displayName")))
            action.triggered.connect(
                lambda _checked=False, target=editor: self.switch_to_editor(target)
            )

    def switch_to_editor(self, editor: CodeEditor) -> None:
        cursor = editor.textCursor()
        self.navigate_to(editor, cursor.blockNumber() + 1, cursor.positionInBlock() + 1)

    def scroll_editor(self, destination: str) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        bar = editor.verticalScrollBar()
        if destination == "top":
            bar.setValue(bar.minimum())
        elif destination == "bottom":
            bar.setValue(bar.maximum())
        elif destination == "page_up":
            bar.setValue(bar.value() - max(1, editor.viewport().height() // editor.fontMetrics().height()))
        elif destination == "page_down":
            bar.setValue(bar.value() + max(1, editor.viewport().height() // editor.fontMetrics().height()))

    def toggle_bookmark(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        line = editor.textCursor().blockNumber()
        match = next(
            (cursor for cursor in editor.bookmark_cursors if cursor.blockNumber() == line), None
        )
        if match is None:
            cursor = QTextCursor(editor.document().findBlockByNumber(line))
            editor.bookmark_cursors.append(cursor)
        else:
            editor.bookmark_cursors.remove(match)
        editor.line_number_area.update()
        self.update_goto_actions()

    def move_bookmark(self, direction: int) -> None:
        editor = self.current_editor()
        if editor is None or not editor.bookmark_cursors:
            return
        bookmarks = sorted(editor.bookmark_cursors, key=lambda cursor: cursor.blockNumber())
        current = editor.textCursor().blockNumber()
        if direction > 0:
            target = next((cursor for cursor in bookmarks if cursor.blockNumber() > current), bookmarks[0])
        else:
            target = next((cursor for cursor in reversed(bookmarks) if cursor.blockNumber() < current), bookmarks[-1])
        self.navigate_to(editor, target.blockNumber() + 1)

    def clear_bookmarks(self) -> None:
        editor = self.current_editor()
        if editor is not None:
            editor.bookmark_cursors.clear()
            editor.line_number_area.update()
        self.update_goto_actions()

    def populate_bookmarks_menu(self) -> None:
        menu = self.bookmarks_menu
        menu.clear()
        menu.addAction(self.action_toggle_bookmark)
        menu.addAction(self.action_next_bookmark)
        menu.addAction(self.action_previous_bookmark)
        menu.addAction(self.action_clear_bookmarks)
        editor = self.current_editor()
        if editor is None or not editor.bookmark_cursors:
            return
        menu.addSeparator()
        for cursor in sorted(editor.bookmark_cursors, key=lambda value: value.blockNumber()):
            line = cursor.blockNumber() + 1
            action = menu.addAction(f"Line {line}")
            action.triggered.connect(
                lambda _checked=False, target=editor, line_number=line: self.navigate_to(target, line_number)
            )

    def jump_to_matching_bracket(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        text = editor.toPlainText()
        cursor = editor.textCursor()
        position = editor.qt_to_python_position(text, cursor.position())
        ranges = self.bracket_ranges(text)
        candidate = position if position < len(text) and text[position] in "()[]{}" else position - 1
        pair = next(
            (
                (start, end)
                for start, end in ranges
                if start == candidate or end - 1 == candidate
            ),
            None,
        )
        if pair is None:
            self.status.showMessage("No matching bracket at the caret", 2500)
            return
        target = pair[1] - 1 if pair[0] == candidate else pair[0]
        preceding_text = text[:target]
        line = preceding_text.count("\n") + 1
        column = target - preceding_text.rfind("\n")
        self.navigate_to(editor, line, column)

    def built_in_build_systems(self) -> list[dict]:
        systems = [
            {
                "id": "builtin:python-run",
                "name": "Python - Run",
                "config": {
                    "cmd": [sys.executable, "$file"],
                    "file_patterns": ["*.py"],
                    "file_regex": r'File "([^"]+)", line (\d+)',
                },
            },
            {
                "id": "builtin:python-check",
                "name": "Python - Check Syntax",
                "config": {
                    "cmd": [
                        sys.executable,
                        "-c",
                        "import ast, pathlib, sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))",
                        "$file",
                    ],
                    "file_patterns": ["*.py"],
                },
            },
        ]
        if shutil.which("bash"):
            systems.append(
                {
                    "id": "builtin:bash-run",
                    "name": "Bash - Run",
                    "config": {"cmd": [shutil.which("bash"), "$file"], "file_patterns": ["*.sh"]},
                }
            )
        node = shutil.which("node")
        if node:
            systems.append(
                {
                    "id": "builtin:node-run",
                    "name": "Node.js - Run",
                    "config": {"cmd": [node, "$file"], "file_patterns": ["*.js", "*.mjs", "*.cjs"]},
                }
            )
        return systems

    def user_build_systems(self) -> list[dict]:
        systems = []
        self.build_system_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.build_system_dir.glob("*.sublime-build")):
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(config, dict) or not any(key in config for key in ("cmd", "shell_cmd")):
                continue
            systems.append(
                {
                    "id": str(path),
                    "name": str(config.get("name") or path.stem),
                    "config": config,
                }
            )
        return systems

    def all_build_systems(self) -> list[dict]:
        return self.built_in_build_systems() + self.user_build_systems()

    @staticmethod
    def system_config_for_platform(config: dict) -> dict:
        result = dict(config)
        platform_config = config.get("linux")
        if sys.platform.startswith("linux") and isinstance(platform_config, dict):
            result.update(platform_config)
        return result

    def populate_build_system_menu(self) -> None:
        self.build_system_menu.clear()
        group = QActionGroup(self.build_system_menu)
        group.setExclusive(True)
        automatic = self.build_system_menu.addAction("Automatic")
        automatic.setCheckable(True)
        automatic.setChecked(self.selected_build_system is None)
        group.addAction(automatic)
        automatic.triggered.connect(lambda: self.set_build_system(None))
        self.build_system_menu.addSeparator()
        for system in self.all_build_systems():
            action = self.build_system_menu.addAction(system["name"])
            action.setCheckable(True)
            action.setChecked(system["id"] == self.selected_build_system)
            action.setToolTip(system["id"])
            group.addAction(action)
            action.triggered.connect(
                lambda _checked=False, identity=system["id"]: self.set_build_system(identity)
            )
        self.build_system_menu.addSeparator()
        self.build_system_menu.addAction(self.action_new_build_system)

    def set_build_system(self, identity: str | None) -> None:
        self.selected_build_system = identity
        if identity is None:
            self.status.showMessage("Build system: Automatic", 2500)
            return
        system = next((item for item in self.all_build_systems() if item["id"] == identity), None)
        self.status.showMessage(
            f"Build system: {system['name'] if system else 'Automatic'}", 2500
        )

    def create_build_system(self) -> None:
        self.build_system_dir.mkdir(parents=True, exist_ok=True)
        path = self.build_system_dir / "My Build System.sublime-build"
        suffix = 2
        while path.exists():
            path = self.build_system_dir / f"My Build System {suffix}.sublime-build"
            suffix += 1
        template = {
            "name": path.stem,
            "cmd": [sys.executable, "$file"],
            "file_patterns": ["*.py"],
        }
        try:
            path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Cannot Create Build System", str(error))
            return
        old_root = self.project_root
        old_explicit = self.project_root_explicit
        self.open_path(str(path))
        self.project_root = old_root
        self.project_root_explicit = old_explicit
        self.file_model.setRootPath(str(old_root))
        self.file_tree.setRootIndex(self.file_model.index(str(old_root)))
        self.status.showMessage(f"Created {path}", 4000)

    def matching_build_systems(self, file_path: str) -> list[dict]:
        name = Path(file_path).name
        matched = []
        for system in self.all_build_systems():
            config = self.system_config_for_platform(system["config"])
            patterns = config.get("file_patterns") or config.get("file_pattern") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            if not patterns or any(fnmatch.fnmatch(name, str(pattern)) for pattern in patterns):
                matched.append(system)
        return matched

    def build_with(self) -> None:
        editor = self.prepare_build_editor()
        if editor is None:
            return
        path = str(editor.property("filePath"))
        if not path:
            self.status.showMessage("Save the current file before building", 3000)
            return
        entries = []
        for system in self.matching_build_systems(path):
            config = self.system_config_for_platform(system["config"])
            variants = config.get("variants", [])
            entries.append((system["name"], "Default build", {"system": system, "variant": None}))
            if isinstance(variants, list):
                for variant in variants:
                    if isinstance(variant, dict) and variant.get("name"):
                        entries.append(
                            (
                                f"{system['name']}: {variant['name']}",
                                "Build variant",
                                {"system": system, "variant": variant},
                            )
                        )
        if not entries:
            self.status.showMessage("No build systems are available", 3000)
            return
        dialog = QuickAccessDialog("Build With", "Filter build systems", entries, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not isinstance(dialog.selected_value, dict):
            return
        choice = dialog.selected_value
        self.run_build_system(choice["system"], choice.get("variant"))

    def build(self) -> None:
        if self.build_process.state() != QProcess.ProcessState.NotRunning:
            self.status.showMessage("A build is already running", 3000)
            return
        editor = self.prepare_build_editor()
        if editor is None:
            return
        path = str(editor.property("filePath"))
        systems = self.all_build_systems()
        if self.selected_build_system:
            system = next(
                (item for item in systems if item["id"] == self.selected_build_system), None
            )
            candidates = [system] if system else []
        else:
            candidates = self.matching_build_systems(path)
        if not candidates:
            self.status.showMessage("No build system matches this file. Choose Tools → Build With…", 4000)
            return
        self.run_build_system(candidates[0])

    def prepare_build_editor(self) -> CodeEditor | None:
        editor = self.current_editor()
        if editor is None:
            return None
        if self.action_save_all_on_build.isChecked():
            self.save_all()
            if self.tabs.indexOf(editor) >= 0:
                self.tabs.setCurrentWidget(editor)
            if any(
                isinstance(self.tabs.widget(index), CodeEditor)
                and self.tabs.widget(index).document().isModified()
                for index in range(self.tabs.count())
            ):
                self.status.showMessage("Build cancelled because changes could not be saved", 3500)
                return None
        if not str(editor.property("filePath")):
            self.status.showMessage("Save the current file before building", 3000)
            return None
        return editor

    def expand_build_variables(self, value: str, editor: CodeEditor) -> str:
        file_path = Path(str(editor.property("filePath"))).resolve()
        project_path = self.project_root if self.project_root_explicit else file_path.parent
        variables = {
            "file": str(file_path),
            "file_path": str(file_path.parent),
            "file_name": file_path.name,
            "file_base_name": file_path.stem,
            "file_extension": file_path.suffix.lstrip("."),
            "project_path": str(project_path),
            "project_name": project_path.name,
            "project_base_name": project_path.name,
        }
        variable_pattern = re.compile(
            r"\$\{?(file_path|file_name|file_base_name|file_extension|project_path|project_name|project_base_name|file)\}?"
        )
        value = variable_pattern.sub(lambda match: variables[match.group(1)], value)
        return os.path.expanduser(value)

    def run_build_system(self, system: dict, variant: dict | None = None) -> None:
        if self.build_process.state() != QProcess.ProcessState.NotRunning:
            self.status.showMessage("A build is already running", 3000)
            return
        editor = self.prepare_build_editor()
        if editor is None:
            return
        path = str(editor.property("filePath"))
        if not path:
            self.status.showMessage("Save the current file before building", 3000)
            return
        config = self.system_config_for_platform(system["config"])
        if variant:
            config.update(variant)
        shell_command = config.get("shell_cmd")
        command = config.get("cmd")
        if shell_command:
            program = "/bin/bash"
            arguments = ["-lc", self.expand_build_variables(str(shell_command), editor)]
        else:
            if isinstance(command, str):
                command = shlex.split(command)
            if not isinstance(command, list) or not command:
                self.status.showMessage("The selected build system has no command", 4000)
                return
            expanded = [self.expand_build_variables(str(part), editor) for part in command]
            program, arguments = expanded[0], expanded[1:]
        configured_dir = config.get("working_dir")
        working_dir = (
            self.expand_build_variables(str(configured_dir), editor)
            if configured_dir
            else str(Path(path).parent)
        )
        if not Path(working_dir).is_absolute():
            working_dir = str(Path(path).parent / working_dir)
        if not Path(working_dir).is_dir():
            self.status.showMessage(f"Build folder does not exist: {working_dir}", 4000)
            return

        environment = QProcessEnvironment.systemEnvironment()
        for key, value in (config.get("env") or {}).items():
            environment.insert(str(key), self.expand_build_variables(str(value), editor))
        extra_path = config.get("path")
        if extra_path:
            existing_path = environment.value("PATH")
            environment.insert(
                "PATH",
                os.pathsep.join(
                    [self.expand_build_variables(str(extra_path), editor), existing_path]
                ),
            )
        self.build_process.setProcessEnvironment(environment)
        self.build_process.setWorkingDirectory(working_dir)
        self.build_config = config
        self.build_working_dir = working_dir
        self.build_results = []
        self.build_output_buffer = f"[{system['name']}] {program} {' '.join(arguments)}\n"
        self.build_output_partial_line = ""
        self.build_results_list.clear()
        self.build_output_view.clear()
        self.build_output_view.appendPlainText(f"[{system['name']}] {program} {' '.join(arguments)}")
        self.build_output_dock.show()
        self.build_output_dock.raise_()
        self.build_cancel_requested = False
        self.status.showMessage(f"Building with {system['name']}…", 0)
        self.build_process.start(program, arguments)
        self.update_tool_actions()

    def read_build_output(self) -> None:
        raw = bytes(self.build_process.readAllStandardOutput())
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace")
        self.build_output_buffer += text
        cursor = self.build_output_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.build_output_view.setTextCursor(cursor)
        self.build_output_view.ensureCursorVisible()
        lines = (self.build_output_partial_line + text).splitlines(keepends=True)
        self.build_output_partial_line = ""
        for line in lines:
            if line.endswith(("\n", "\r")):
                self.parse_build_result_line(line.rstrip("\r\n"))
            else:
                self.build_output_partial_line = line

    def parse_build_result_line(self, line: str) -> None:
        match = None
        file_regex = self.build_config.get("file_regex")
        if file_regex:
            try:
                match = re.search(str(file_regex), line)
            except re.error:
                match = None
        if match:
            groups = match.groups()
            if len(groups) >= 2 and groups[0] and groups[1]:
                path_text = groups[0]
                line_number = int(groups[1])
                column_number = int(groups[2]) if len(groups) >= 3 and groups[2] and groups[2].isdigit() else 1
                message = groups[3] if len(groups) >= 4 and groups[3] else line
                self.add_build_result(path_text, line_number, column_number, message)
                return
        python_result = re.search(r'File "([^\"]+)", line (\d+)', line)
        if python_result:
            self.add_build_result(
                python_result.group(1), int(python_result.group(2)), 1, line
            )
            return
        compiler_result = re.match(r"^(.+?):(\d+):(\d+):\s*(.*)$", line)
        if compiler_result:
            self.add_build_result(
                compiler_result.group(1),
                int(compiler_result.group(2)),
                int(compiler_result.group(3)),
                compiler_result.group(4),
            )
            return
        line_result = re.match(r"^(.+?):(\d+):\s*(.*)$", line)
        if line_result:
            self.add_build_result(
                line_result.group(1), int(line_result.group(2)), 1, line_result.group(3)
            )

    def add_build_result(self, path_text: str, line: int, column: int, message: str) -> None:
        path = Path(path_text)
        if not path.is_absolute():
            path = Path(getattr(self, "build_working_dir", self.project_root)) / path
        result = {"path": str(path.resolve()), "line": max(1, line), "column": max(1, column), "message": message.strip()}
        self.build_results.append(result)
        item = QListWidgetItem(
            f"{path.name}:{result['line']}:{result['column']}  {result['message']}"
        )
        item.setData(Qt.ItemDataRole.UserRole, result)
        item.setToolTip(str(path))
        self.build_results_list.addItem(item)
        self.update_build_result_actions()

    def finish_build(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self.read_build_output()
        if self.build_output_partial_line:
            self.parse_build_result_line(self.build_output_partial_line)
            self.build_output_partial_line = ""
        if getattr(self, "build_cancel_requested", False):
            message = "Build cancelled."
        elif exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
            message = "Build finished successfully."
        else:
            message = f"Build finished with exit code {exit_code}."
        self.build_output_view.appendPlainText(f"\n{message}")
        self.status.showMessage(message, 4000)
        self.update_tool_actions()

    def build_process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            message = f"Could not start build: {self.build_process.errorString()}"
            self.build_output_view.appendPlainText(message)
            self.status.showMessage(message, 5000)
        self.update_tool_actions()

    def cancel_build(self) -> None:
        if self.build_process.state() == QProcess.ProcessState.NotRunning:
            return
        self.build_cancel_requested = True
        self.status.showMessage("Cancelling build…", 0)
        self.build_process.terminate()
        QTimer.singleShot(
            1200,
            lambda: self.build_process.kill()
            if self.build_process.state() != QProcess.ProcessState.NotRunning
            else None,
        )

    def show_build_results(self) -> None:
        self.build_output_dock.show()
        self.build_output_dock.raise_()
        self.build_output_view.setFocus()

    def clear_build_results(self) -> None:
        self.build_results.clear()
        self.build_results_list.clear()
        self.build_output_buffer = ""
        self.build_output_partial_line = ""
        self.build_output_view.clear()
        self.update_build_result_actions()

    def update_build_result_actions(self) -> None:
        has_results = bool(self.build_results)
        self.action_next_build_result.setEnabled(has_results)
        self.action_previous_build_result.setEnabled(has_results)
        self.action_clear_build_results.setEnabled(bool(self.build_output_buffer or has_results))
        self.action_show_build_results.setEnabled(bool(self.build_output_buffer or has_results))

    def move_build_result(self, direction: int) -> None:
        count = self.build_results_list.count()
        if count == 0:
            return
        current = self.build_results_list.currentRow()
        target = (current + direction) % count if current >= 0 else (0 if direction > 0 else count - 1)
        self.build_results_list.setCurrentRow(target)
        item = self.build_results_list.item(target)
        if item is not None:
            self.open_build_result_item(item)

    def open_build_result_item(self, item: QListWidgetItem) -> None:
        result = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(result, dict):
            return
        path = result.get("path", "")
        if not Path(path).is_file():
            self.status.showMessage(f"Build result file not found: {path}", 3500)
            return
        self.open_path(path)
        editor = self.current_editor()
        if editor:
            self.navigate_to(editor, result["line"], result["column"])

    def set_save_all_on_build(self, enabled: bool) -> None:
        self.settings.setValue("save_all_on_build", enabled)

    def update_tool_actions(self) -> None:
        has_editor = self.current_editor() is not None
        self.action_snippets.setEnabled(has_editor)
        self.action_build.setEnabled(has_editor)
        self.action_build_with.setEnabled(has_editor)
        self.action_record_macro.setEnabled(has_editor or self.macro_recording)
        self.action_cancel_build.setEnabled(
            self.build_process.state() != QProcess.ProcessState.NotRunning
        )
        self.action_play_macro.setEnabled(bool(self.last_macro_steps) and not self.macro_recording)
        self.action_save_macro.setEnabled(bool(self.last_macro_steps) and not self.macro_recording)
        self.update_build_result_actions()

    @staticmethod
    def default_snippets() -> list[dict[str, str]]:
        return [
            {
                "name": "Python function",
                "trigger": "def",
                "content": "def ${1:name}(${2:args}):\n    ${0:pass}",
            },
            {
                "name": "Python main guard",
                "trigger": "main",
                "content": "if __name__ == \"__main__\":\n    ${0:main()}",
            },
            {"name": "TODO comment", "trigger": "todo", "content": "TODO: ${0:describe the task}"},
            {"name": "Markdown heading", "trigger": "h1", "content": "# ${0:Heading}"},
        ]

    def available_snippets(self) -> list[dict[str, str]]:
        snippets = self.default_snippets()
        self.snippet_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.snippet_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if all(isinstance(data.get(key), str) for key in ("name", "trigger", "content")):
                    snippets.append(data)
            except (OSError, json.JSONDecodeError):
                continue
        return snippets

    def show_snippets(self) -> None:
        if self.current_editor() is None:
            return
        while True:
            snippets = self.available_snippets()
            dialog = QDialog(self)
            dialog.setWindowTitle("Snippets")
            dialog.setMinimumSize(520, 380)
            layout = QVBoxLayout(dialog)
            query = QLineEdit(dialog)
            query.setPlaceholderText("Filter snippets by name or trigger")
            results = QListWidget(dialog)
            buttons = QHBoxLayout()
            insert_button = QPushButton("Insert", dialog)
            new_button = QPushButton("New Snippet…", dialog)
            close_button = QPushButton("Close", dialog)
            buttons.addWidget(new_button)
            buttons.addStretch(1)
            buttons.addWidget(insert_button)
            buttons.addWidget(close_button)
            layout.addWidget(query)
            layout.addWidget(results, 1)
            layout.addLayout(buttons)
            for snippet in snippets:
                item = QListWidgetItem(f"{snippet['name']}    {snippet['trigger']}")
                item.setData(Qt.ItemDataRole.UserRole, snippet)
                results.addItem(item)

            def filter_snippets(value: str) -> None:
                first = None
                for index in range(results.count()):
                    item = results.item(index)
                    hidden = not QuickAccessDialog.fuzzy_match(value, item.text())
                    item.setHidden(hidden)
                    if not hidden and first is None:
                        first = item
                if first is not None:
                    results.setCurrentItem(first)

            def insert_selected() -> None:
                item = results.currentItem()
                if item is not None and not item.isHidden():
                    dialog.accept()

            query.textChanged.connect(filter_snippets)
            results.itemActivated.connect(lambda _item: insert_selected())
            insert_button.clicked.connect(insert_selected)
            new_button.clicked.connect(lambda: dialog.done(2))
            close_button.clicked.connect(dialog.reject)
            filter_snippets("")
            query.setFocus()
            result = dialog.exec()
            if result == 2:
                if self.create_snippet():
                    continue
                return
            if result == QDialog.DialogCode.Accepted:
                item = results.currentItem()
                if item is not None:
                    self.insert_snippet(item.data(Qt.ItemDataRole.UserRole))
            return

    def create_snippet(self) -> bool:
        name, accepted = QInputDialog.getText(self, "New Snippet", "Snippet name:")
        if not accepted or not name.strip():
            return False
        suggested_trigger = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip().lower()).strip("_")
        trigger, accepted = QInputDialog.getText(
            self, "New Snippet", "Tab trigger:", text=suggested_trigger
        )
        if not accepted or not trigger.strip():
            return False
        content, accepted = QInputDialog.getMultiLineText(
            self, "New Snippet", "Text to insert (supports $1 and ${1:placeholder} fields):"
        )
        if not accepted:
            return False
        self.snippet_dir.mkdir(parents=True, exist_ok=True)
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._") or "snippet"
        path = self.snippet_dir / f"{filename}.json"
        suffix = 2
        while path.exists():
            path = self.snippet_dir / f"{filename}-{suffix}.json"
            suffix += 1
        try:
            path.write_text(
                json.dumps(
                    {"name": name.strip(), "trigger": trigger.strip(), "content": content},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            QMessageBox.warning(self, "Cannot Save Snippet", str(error))
            return False
        return True

    def insert_snippet(self, snippet: dict[str, str]) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        content = snippet.get("content", "").replace("\\$", "\u0000")
        content = content.replace("$TM_FILENAME", str(editor.property("displayName")))
        content = content.replace("\u0000", "$")
        fields: list[tuple[int, int, int]] = []
        output: list[str] = []
        source_position = 0
        output_position = 0
        field_pattern = re.compile(r"\$\{(\d+):([^{}]*)\}|\$(\d+)")
        for match in field_pattern.finditer(content):
            literal = content[source_position:match.start()]
            output.append(literal)
            output_position += len(literal)
            number = int(match.group(1) or match.group(3))
            value = match.group(2) or "" if match.group(1) else ""
            start = output_position
            output.append(value)
            output_position += len(value)
            fields.append((number, start, output_position))
            source_position = match.end()
        output.append(content[source_position:])
        expanded = "".join(output)
        cursor = editor.textCursor()
        base = cursor.selectionStart()
        cursor.insertText(expanded)
        stop = next((field for field in sorted(fields) if field[0] > 0), None)
        final_stop = next((field for field in fields if field[0] == 0), None)
        if stop is not None:
            left = base + editor.python_to_qt_position(expanded, stop[1])
            right = base + editor.python_to_qt_position(expanded, stop[2])
            cursor.setPosition(left)
            cursor.setPosition(right, QTextCursor.MoveMode.KeepAnchor)
        elif final_stop is not None:
            cursor.setPosition(base + editor.python_to_qt_position(expanded, final_stop[1]))
        editor.setTextCursor(cursor)
        editor.setFocus()

    def toggle_macro_recording(self) -> None:
        if not self.macro_recording:
            editor = self.current_editor()
            if editor is None:
                self.status.showMessage("Open a file before recording a macro", 3000)
                return
            self.macro_recording = True
            self.macro_editor = editor
            self.macro_steps = []
            self.macro_last_text = editor.toPlainText()
            cursor = editor.textCursor()
            self.macro_origin = self.qt_to_python_position(
                self.macro_last_text, cursor.selectionStart()
            )
            editor.textChanged.connect(self.capture_macro_edit)
            self.action_record_macro.setText("Stop Recording Macro")
            self.status.showMessage("Recording macro…", 0)
            return
        editor = self.macro_editor
        if editor is not None:
            try:
                editor.textChanged.disconnect(self.capture_macro_edit)
            except (RuntimeError, TypeError):
                pass
        self.macro_recording = False
        self.macro_editor = None
        self.last_macro_steps = list(self.macro_steps)
        self.action_record_macro.setText("Record Macro")
        self.action_play_macro.setEnabled(bool(self.last_macro_steps))
        self.action_save_macro.setEnabled(bool(self.last_macro_steps))
        self.status.showMessage(
            f"Recorded {len(self.last_macro_steps)} macro edit(s)", 3000
        )

    def capture_macro_edit(self) -> None:
        editor = self.macro_editor
        if not self.macro_recording or editor is None:
            return
        current = editor.toPlainText()
        previous = self.macro_last_text
        prefix = 0
        limit = min(len(previous), len(current))
        while prefix < limit and previous[prefix] == current[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < len(previous) - prefix
            and suffix < len(current) - prefix
            and previous[len(previous) - suffix - 1] == current[len(current) - suffix - 1]
        ):
            suffix += 1
        removed_end = len(previous) - suffix
        inserted_end = len(current) - suffix
        removed = previous[prefix:removed_end]
        inserted = current[prefix:inserted_end]
        if removed or inserted:
            self.macro_steps.append(
                {
                    "offset": prefix - self.macro_origin,
                    "removed": removed,
                    "inserted": inserted,
                }
            )
        self.macro_last_text = current

    def play_last_macro(self) -> None:
        self.play_macro_steps(self.last_macro_steps)

    def play_macro_steps(self, steps: list[dict]) -> None:
        editor = self.current_editor()
        if editor is None or not steps:
            return
        text = editor.toPlainText()
        cursor_start = editor.textCursor().selectionStart()
        base = self.qt_to_python_position(text, cursor_start)
        cursor = QTextCursor(editor.document())
        cursor.beginEditBlock()
        for step in steps:
            current_text = editor.toPlainText()
            start = max(0, min(base + int(step.get("offset", 0)), len(current_text)))
            removed = str(step.get("removed", ""))
            end = max(start, min(start + len(removed), len(current_text)))
            cursor.setPosition(editor.python_to_qt_position(current_text, start))
            cursor.setPosition(editor.python_to_qt_position(current_text, end), QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(str(step.get("inserted", "")))
        cursor.endEditBlock()
        editor.setTextCursor(cursor)
        editor.setFocus()

    def save_macro(self) -> None:
        if not self.last_macro_steps:
            return
        name, accepted = QInputDialog.getText(self, "Save Macro", "Macro name:")
        if not accepted or not name.strip():
            return
        self.macro_dir.mkdir(parents=True, exist_ok=True)
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._") or "macro"
        path = self.macro_dir / f"{filename}.json"
        suffix = 2
        while path.exists():
            path = self.macro_dir / f"{filename}-{suffix}.json"
            suffix += 1
        try:
            path.write_text(
                json.dumps({"name": name.strip(), "steps": self.last_macro_steps}, indent=2)
                + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            QMessageBox.warning(self, "Cannot Save Macro", str(error))
            return
        self.status.showMessage(f"Saved macro: {name.strip()}", 3000)

    def saved_macros(self) -> list[dict]:
        macros = []
        self.macro_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.macro_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data.get("steps"), list):
                    macros.append({"name": str(data.get("name") or path.stem), "path": path, "steps": data["steps"]})
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
        return macros

    def populate_macros_menu(self) -> None:
        self.macros_menu.clear()
        macros = self.saved_macros()
        if not macros:
            empty = self.macros_menu.addAction("No Saved Macros")
            empty.setEnabled(False)
            return
        for macro in macros:
            action = self.macros_menu.addAction(macro["name"])
            action.triggered.connect(
                lambda _checked=False, steps=macro["steps"]: self.play_macro_steps(steps)
            )

    def show_developer_console(self) -> None:
        editor = self.current_editor()
        path = str(editor.property("filePath")) if editor else "(no active file)"
        syntax = self.effective_syntax(editor) if editor else "(none)"
        state = "running" if self.build_process.state() != QProcess.ProcessState.NotRunning else "idle"
        self.developer_console_view.setPlainText(
            "SubText Developer Console\n"
            f"Python: {sys.version.split()[0]}\n"
            "Qt binding: PySide6\n"
            f"Active file: {path}\n"
            f"Syntax: {syntax}\n"
            f"Project folder: {self.project_root}\n"
            f"Build process: {state}\n"
        )
        self.developer_console_dock.show()
        self.developer_console_dock.raise_()

    def open_user_data_folder(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.user_data_dir)))

    def inspect_current_buffer(self) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        text = editor.toPlainText()
        details = (
            f"File: {editor.property('filePath') or '(unsaved)'}\n"
            f"Syntax: {self.effective_syntax(editor)}\n"
            f"Lines: {editor.blockCount()}\n"
            f"Characters: {len(text)}\n"
            f"Modified: {'yes' if editor.document().isModified() else 'no'}"
        )
        QMessageBox.information(self, "Current Buffer", details)

    def explain_package_control(self) -> None:
        QMessageBox.information(
            self,
            "Package Control",
            "Package Control installs extensions for Sublime Text. SubText does not load Sublime Text packages. "
            "Use SubText snippets and build systems from the User Data Folder instead.",
        )


    def toggle_sidebar(self) -> None:
        self.set_sidebar_visible(self.project_dock.isHidden())

    def show_command_palette(self) -> None:
        palette = CommandPalette(self.palette_actions, self)
        palette.move(
            self.geometry().center().x() - palette.width() // 2,
            self.geometry().top() + 90,
        )
        palette.exec()

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About SubText",
            "SubText is a native Qt editor prototype for Linux desktops.\n"
            "Its initial workflow was informed by REA's static inspection of the local application.",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        if self.macro_recording:
            self.toggle_macro_recording()
        self.session_save_timer.stop()
        self.save_session_state()
        if self.build_process.state() != QProcess.ProcessState.NotRunning:
            self.build_process.terminate()
            if not self.build_process.waitForFinished(700):
                self.build_process.kill()
                self.build_process.waitForFinished(500)
        event.accept()


def main() -> int:
    parser = argparse.ArgumentParser(description="SubText native editor prototype")
    parser.add_argument("--new-file", action="store_true", help="Open a new empty tab")
    parser.add_argument("paths", nargs="*", help="Text files to open")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("SubText Editor")
    app.setOrganizationName("SubText")
    app.setDesktopFileName("subtext-editor")
    app.setWindowIcon(QIcon(str(APP_DIR / "icon.svg")))
    window = MainWindow(args.paths, start_new=args.new_file)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

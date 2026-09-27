#####################################################################
#                                                                   #
# /tests/test_value_column.py                                       #
#                                                                   #
# Copyright 2026, JQI                                               #
# Author: Ian Spielman                                              #
#                                                                   #
# This file is part of runmanager, in the labscript suite           #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""The Value(s) column, which shows a global's Default and Scan in one cell.

A group tab is shown offscreen, so the view, its delegate and the tab's handlers
all run. Of the application behind the tab only globals_changed is stood in for,
because RunManager's startup builds the whole window.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qtutils.qt import QtCore, QtGui, QtWidgets

import runmanager.globals_file as globals_file
# fixtures stubs the splash and does the guarded import of the application.
from fixtures import Editor, FingerTabWidget, GroupTab, RunManager, main_module


def send_click(view, pos):
    for event_type in (
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.QEvent.Type.MouseButtonRelease,
    ):
        event = QtGui.QMouseEvent(
            event_type,
            QtCore.QPointF(pos),
            QtCore.QPointF(view.viewport().mapToGlobal(pos)),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        QtWidgets.QApplication.sendEvent(view.viewport(), event)


def send_key(widget, key, text=''):
    for event_type in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.KeyRelease):
        modifiers = QtCore.Qt.KeyboardModifier.NoModifier
        event = QtGui.QKeyEvent(event_type, key, modifiers, text)
        QtWidgets.QApplication.sendEvent(widget, event)


class App:
    ensure_editable_globals_file = RunManager.ensure_editable_globals_file

    def globals_changed(self):
        pass


_qapplication = None


class ValueColumnTests(unittest.TestCase):
    def setUp(self):
        global _qapplication
        if QtWidgets.QApplication.instance() is None:
            # Held for the life of the process: a QApplication that is garbage
            # collected takes every widget built under it down with it.
            _qapplication = QtWidgets.QApplication([])
        self.qapplication = QtWidgets.QApplication.instance()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name, 'globals.toml'))
        globals_file.new_globals_file(self.path)
        globals_file.new_group(self.path, 'group')
        for name, default, scan_enabled, scan in [
            ('freq', '10', False, 'linspace(0, 1, 3)'),
            ('power', '2', False, ''),
            ('time', '5', True, '[1, 2]'),
        ]:
            globals_file.new_global(self.path, 'group', name)
            record = dict(default=default, scan_enabled=scan_enabled, scan=scan)
            globals_file.set_global_record(self.path, 'group', name, record)
        for name, value in [('app', App()), ('qapplication', self.qapplication)]:
            patcher = mock.patch.object(main_module, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        tabs = FingerTabWidget(None)
        # deleteLater needs an event loop, so its posted deletion is sent here:
        self.addCleanup(
            QtCore.QCoreApplication.sendPostedEvents,
            None,
            QtCore.QEvent.Type.DeferredDelete.value,
        )
        self.addCleanup(tabs.deleteLater)
        self.tab = GroupTab(tabs, self.path, 'group')
        self.view = self.tab.ui.tableView_globals
        tabs.resize(900, 300)
        tabs.show()
        self.qapplication.processEvents()

    def value_rect(self, name):
        row = self.tab.get_global_item_by_name(name, self.tab.GLOBALS_COL_NAME).row()
        index = self.tab.globals_model.index(row, self.tab.GLOBALS_COL_DEFAULT)
        return index, self.view.visualRect(index)

    def editor(self):
        self.qapplication.processEvents()
        editors = self.view.findChildren(Editor)
        visible = [editor for editor in editors if editor.isVisible()]
        self.assertEqual(len(visible), 1)
        return visible[0]

    def commit(self, text):
        editor = self.editor()
        editor.setPlainText(text)
        send_key(editor, QtCore.Qt.Key.Key_Return)

    def record(self, name):
        return globals_file.get_global_record(self.path, 'group', name)

    def test_a_click_on_a_line_edits_that_expression(self):
        _, rect = self.value_rect('freq')
        send_click(self.view, QtCore.QPoint(rect.left() + 6, rect.top() + 6))
        _, rect = self.value_rect('freq')
        send_click(self.view, QtCore.QPoint(rect.center().x(), rect.bottom() - 6))
        self.commit('linspace(0, 1, 5)')

        self.assertEqual(self.record('freq')['scan'], 'linspace(0, 1, 5)')

    def test_a_key_edits_the_expression_in_use_even_when_both_show(self):
        _, rect = self.value_rect('time')
        send_click(self.view, QtCore.QPoint(rect.left() + 6, rect.top() + 6))
        index, _ = self.value_rect('time')
        self.view.setCurrentIndex(index)
        send_key(self.view, QtCore.Qt.Key.Key_7, '7')
        send_key(self.editor(), QtCore.Qt.Key.Key_Return)

        self.assertEqual(self.record('time')['scan'], '7')

    def test_tab_moves_past_the_hidden_scan_column(self):
        # The Scan column only stores the scan the Value(s) cell shows. Stopping on
        # it would open an editor no one can see, and type into the scan.
        _, rect = self.value_rect('freq')
        send_click(self.view, rect.center())
        send_key(self.editor(), QtCore.Qt.Key.Key_Tab)

        self.assertEqual(self.view.currentIndex().column(), self.tab.GLOBALS_COL_UNITS)

    def test_ticking_scan_with_no_scan_opens_its_line_for_typing(self):
        column = self.tab.GLOBALS_COL_SCAN_ENABLED
        item = self.tab.get_global_item_by_name('power', column)
        item.setCheckState(QtCore.Qt.CheckState.Checked)
        self.commit('[1, 2, 3]')

        self.assertEqual(self.record('power')['scan'], '[1, 2, 3]')

    def test_an_open_editor_follows_its_global_until_typed_in(self):
        # freq's default is in use, so a change to its scan leaves its Value(s) item
        # as it was, and Qt would not reload the scan line's editor by itself:
        _, rect = self.value_rect('freq')
        send_click(self.view, QtCore.QPoint(rect.left() + 6, rect.top() + 6))
        _, rect = self.value_rect('freq')
        send_click(self.view, QtCore.QPoint(rect.center().x(), rect.bottom() - 6))
        editor = self.editor()
        scan = self.record('freq')['scan']
        self.tab.change_global_scan('freq', scan, '[3]', interactive=False)
        self.assertEqual(editor.toPlainText(), '[3]')
        editor.insertPlainText('0')
        typed = editor.toPlainText()
        # What a preparse does to each row as its results come back:
        self.tab.update_expression_backgrounds('freq')

        self.assertEqual(editor.toPlainText(), typed)

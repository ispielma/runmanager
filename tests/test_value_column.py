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
"""The Value(s) column, which holds a global's Default and Scan in one cell.

A group tab is shown offscreen and driven with real mouse and key events, so the
view, its delegate and the tab's handlers all run. Only the application behind
the tab is a stand-in, because RunManager's startup builds the whole window.
"""
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from qtutils.qt import QtCore, QtGui, QtWidgets

import runmanager.globals_file as globals_file
# fixtures stubs the splash and does the guarded import of the application.
from fixtures import FingerTabWidget, main_module


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


class ValueColumnTests(unittest.TestCase):
    def setUp(self):
        self.qapplication = QtWidgets.QApplication.instance()
        if self.qapplication is None:
            self.qapplication = QtWidgets.QApplication([])
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.path = os.path.join(directory, 'globals.toml')
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
        app = types.SimpleNamespace(
            globals_changed=lambda: None,
            ensure_editable_globals_file=lambda path, parent=None: path,
        )
        for name, value in [('app', app), ('qapplication', self.qapplication)]:
            patcher = mock.patch.object(main_module, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        tabs = FingerTabWidget(None)
        self.addCleanup(tabs.deleteLater)
        self.tab = main_module.GroupTab(tabs, self.path, 'group')
        self.view = self.tab.ui.tableView_globals
        tabs.resize(900, 300)
        tabs.show()
        self.qapplication.processEvents()

    def value_rect(self, name):
        row = self.tab.get_global_item_by_name(name, self.tab.GLOBALS_COL_NAME).row()
        index = self.tab.globals_model.index(row, self.tab.GLOBALS_COL_DEFAULT)
        return index, self.view.visualRect(index)

    def open_editor(self):
        self.qapplication.processEvents()
        editors = self.view.findChildren(main_module.Editor)
        return next(editor for editor in editors if editor.isVisible())

    def commit(self, text):
        editor = self.open_editor()
        editor.setPlainText(text)
        send_key(editor, QtCore.Qt.Key.Key_Return)
        # A default is saved on a zero-length timer:
        for _ in range(5):
            QtCore.QThread.msleep(2)
            self.qapplication.processEvents()

    def record(self, name):
        return globals_file.get_global_record(self.path, 'group', name)

    def test_a_click_on_a_line_edits_that_expression(self):
        _, rect = self.value_rect('freq')
        send_click(self.view, QtCore.QPoint(rect.left() + 6, rect.top() + 6))
        _, rect = self.value_rect('freq')
        send_click(self.view, QtCore.QPoint(rect.center().x(), rect.bottom() - 6))
        self.commit('linspace(0, 1, 5)')

        self.assertEqual(self.record('freq')['scan'], 'linspace(0, 1, 5)')
        self.assertEqual(self.record('freq')['default'], '10')

    def test_a_key_edits_the_expression_in_use_even_when_both_show(self):
        _, rect = self.value_rect('time')
        send_click(self.view, QtCore.QPoint(rect.left() + 6, rect.top() + 6))
        index, _ = self.value_rect('time')
        self.view.setCurrentIndex(index)
        send_key(self.view, QtCore.Qt.Key.Key_7, '7')
        send_key(self.open_editor(), QtCore.Qt.Key.Key_Return)

        self.assertEqual(self.record('time')['scan'], '7')
        self.assertEqual(self.record('time')['default'], '5')

    def test_tab_moves_past_the_hidden_scan_column(self):
        # The Scan column only stores the scan the value cell shows. Stopping on
        # it would open an editor no one can see, and type into the scan.
        _, rect = self.value_rect('freq')
        send_click(self.view, rect.center())
        send_key(self.open_editor(), QtCore.Qt.Key.Key_Tab)

        self.assertEqual(self.view.currentIndex().column(), self.tab.GLOBALS_COL_UNITS)

    def test_ticking_scan_with_no_scan_opens_its_line_for_typing(self):
        column = self.tab.GLOBALS_COL_SCAN_ENABLED
        item = self.tab.get_global_item_by_name('power', column)
        item.setCheckState(QtCore.Qt.CheckState.Checked)
        self.commit('[1, 2, 3]')

        self.assertEqual(self.record('power')['scan'], '[1, 2, 3]')
        self.assertEqual(self.record('power')['default'], '2')

    def test_a_preparse_leaves_what_is_being_typed(self):
        _, rect = self.value_rect('time')
        send_click(self.view, rect.center())
        editor = self.open_editor()
        editor.insertPlainText('0')
        typed = editor.toPlainText()
        # What a preparse does to each row as its results come back:
        self.tab.update_expression_backgrounds('time')
        self.qapplication.processEvents()

        self.assertEqual(editor.toPlainText(), typed)

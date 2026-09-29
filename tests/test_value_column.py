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
"""The foldable Default and Scan expressions in a group tab."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qtutils.qt import QtCore, QtGui, QtWidgets

import runmanager.globals_file as globals_file
# fixtures stubs the splash and does the guarded import of the application.
from fixtures import Editor, FingerTabWidget, GroupTab, RunManager, main_module


def send_click(view, pos, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
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
            modifiers,
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
        self.view = self.tab.ui.treeView_globals
        tabs.resize(900, 300)
        tabs.show()
        self.qapplication.processEvents()

    def expression_item(self, name, expression):
        return self.tab.get_global_expression_item(name, expression)

    def value_rect(self, name):
        index = self.tab.get_active_value_item(name).index()
        return index, self.view.visualRect(index)

    def editor(self):
        self.qapplication.processEvents()
        visible = [editor for editor in self.view.findChildren(Editor) if editor.isVisible()]
        self.assertEqual(len(visible), 1)
        return visible[0]

    def commit(self, text):
        editor = self.editor()
        editor.setPlainText(text)
        send_key(editor, QtCore.Qt.Key.Key_Return)
        self.qapplication.processEvents()

    def record(self, name):
        return globals_file.get_global_record(self.path, 'group', name)

    def test_native_fold_shows_two_distinct_editable_expressions(self):
        _, rect = self.value_rect('freq')
        scan = self.expression_item('freq', 'scan').index()
        self.assertFalse(self.view.visualRect(scan).isValid())

        send_click(self.view, QtCore.QPoint(rect.left() - 10, rect.center().y()))
        self.qapplication.processEvents()
        scan_rect = self.view.visualRect(scan)
        self.assertTrue(scan_rect.isValid())

        send_click(self.view, rect.center())
        self.editor().setPlainText('11')
        send_click(self.view, scan_rect.center())
        self.assertEqual(self.editor().toPlainText(), 'linspace(0, 1, 3)')
        self.assertEqual(self.record('freq')['default'], '11')
        self.commit('linspace(0, 1, 5)')
        self.assertEqual(self.record('freq')['scan'], 'linspace(0, 1, 5)')

    def test_rendered_label_and_text_follow_active_expression(self):
        active, rect = self.value_rect('freq')
        option = QtWidgets.QStyleOptionViewItem()
        self.tab.value_delegate.initStyleOption(option, active)
        offset = self.tab.value_delegate.text_offset(option)
        label_rect = QtCore.QRect(rect.left(), rect.top(), offset, rect.height())
        expression_rect = rect.adjusted(offset, 0, 0, 0)
        self.view.clearSelection()
        self.view.setCurrentIndex(QtCore.QModelIndex())
        self.qapplication.processEvents()
        default_render = self.view.viewport().grab().toImage()

        checkbox = self.tab.get_global_item_by_name(
            'freq', self.tab.GLOBALS_COL_SCAN_ENABLED
        )
        checkbox.setCheckState(QtCore.Qt.CheckState.Checked)
        self.qapplication.processEvents()
        scan_render = self.view.viewport().grab().toImage()
        self.assertNotEqual(
            default_render.copy(label_rect), scan_render.copy(label_rect)
        )
        self.assertNotEqual(
            default_render.copy(expression_rect), scan_render.copy(expression_rect)
        )

    def test_toggling_scan_commits_editor_and_swaps_active_expression(self):
        parent, rect = self.value_rect('freq')
        send_click(self.view, rect.center())
        self.editor().setPlainText('12')
        checkbox = self.tab.get_global_item_by_name('freq', self.tab.GLOBALS_COL_SCAN_ENABLED)
        checkbox.setCheckState(QtCore.Qt.CheckState.Checked)
        self.qapplication.processEvents()

        active, _ = self.value_rect('freq')
        self.assertEqual(active.data(self.tab.GLOBALS_ROLE_EXPRESSION), 'scan')
        self.assertEqual(self.expression_item('freq', 'default').text(), '12')
        self.assertEqual(self.record('freq')['default'], '12')
        self.assertTrue(self.record('freq')['scan_enabled'])
        self.assertFalse(self.view.isExpanded(active.siblingAtColumn(0)))

        checkbox = self.tab.get_global_item_by_name('freq', self.tab.GLOBALS_COL_SCAN_ENABLED)
        checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
        self.qapplication.processEvents()
        active, _ = self.value_rect('freq')
        self.assertEqual(active.data(self.tab.GLOBALS_ROLE_EXPRESSION), 'default')
        self.assertEqual(active.data(), '12')

    def test_remote_toggle_preserves_other_globals_uncommitted_editor(self):
        _, rect = self.value_rect('freq')
        send_click(self.view, rect.center())
        editor = self.editor()
        editor.insertPlainText('half typed')

        self.tab.change_global_jit_enabled('power', False, True, interactive=False)
        self.tab.change_global_scan_enabled('power', False, True, interactive=False)
        self.qapplication.processEvents()

        self.assertEqual(self.record('freq')['default'], '10')
        self.assertEqual(self.expression_item('freq', 'default').text(), '10')
        self.assertIs(self.view.indexWidget(self.view.currentIndex()), editor)
        self.assertEqual(editor.toPlainText(), 'half typed')

    def test_ticking_scan_with_no_scan_opens_its_line_for_typing(self):
        checkbox = self.tab.get_global_item_by_name(
            'power', self.tab.GLOBALS_COL_SCAN_ENABLED
        )
        checkbox.setCheckState(QtCore.Qt.CheckState.Checked)
        self.qapplication.processEvents()

        active = self.tab.get_active_value_item('power').index()
        self.assertEqual(active.data(self.tab.GLOBALS_ROLE_EXPRESSION), 'scan')
        self.assertEqual(self.view.currentIndex(), active)
        self.assertIs(self.view.indexWidget(active), self.editor())
        self.commit('[1, 2]')
        self.assertEqual(self.record('power')['scan'], '[1, 2]')

    def test_keyboard_and_copy_follow_selected_expression(self):
        active, _ = self.value_rect('time')
        self.view.expand(active)
        scan = self.expression_item('time', 'scan').index()
        self.view.setCurrentIndex(scan)
        send_key(self.view, QtCore.Qt.Key.Key_7, '7')
        self.commit('7')
        self.assertEqual(self.record('time')['scan'], '7')
        self.view.setCurrentIndex(self.expression_item('time', 'default').index())
        self.tab.on_globals_copy()
        self.assertEqual(self.qapplication.clipboard().text(), '5')

    def test_checkbox_cells_toggle_and_shift_click_preserves_selection(self):
        scan = self.tab.get_global_item_by_name('freq', self.tab.GLOBALS_COL_SCAN_ENABLED)
        rect = self.view.visualRect(scan.index())
        point = QtCore.QPoint(rect.right() - 5, rect.center().y())
        send_click(self.view, point, QtCore.Qt.KeyboardModifier.ShiftModifier)
        self.assertFalse(self.record('freq')['scan_enabled'])
        send_click(self.view, point)
        self.qapplication.processEvents()
        self.assertTrue(self.record('freq')['scan_enabled'])
        jit = self.tab.get_global_item_by_name('power', self.tab.GLOBALS_COL_JIT_ENABLED)
        rect = self.view.visualRect(jit.index())
        send_click(self.view, QtCore.QPoint(rect.right() - 5, rect.center().y()))
        self.qapplication.processEvents()
        self.assertTrue(self.record('power')['jit_enabled'])

    def test_child_selection_targets_the_global_for_bulk_changes(self):
        active, _ = self.value_rect('time')
        self.view.expand(active)
        child = self.expression_item('time', 'default').index()
        self.view.selectionModel().select(
            child,
            QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QtCore.QItemSelectionModel.SelectionFlag.Rows,
        )
        self.view.setCurrentIndex(child)
        self.tab.on_globals_set_selected_bools_triggered('True')
        self.qapplication.processEvents()
        self.assertEqual(self.record('time')['scan'], 'True')

    def test_metadata_refresh_keeps_uncommitted_editor_text(self):
        parent, _ = self.value_rect('freq')
        self.view.expand(parent)
        scan = self.expression_item('freq', 'scan').index()
        send_click(self.view, self.view.visualRect(scan).center())
        editor = self.editor()
        editor.insertPlainText('[0]')
        self.tab.update_expression_backgrounds('freq')
        self.assertEqual(editor.toPlainText(), '[0]')

    def test_expansion_selection_and_sort_survive_scan_toggle(self):
        self.view.sortByColumn(self.tab.GLOBALS_COL_VALUE, QtCore.Qt.SortOrder.DescendingOrder)
        active, _ = self.value_rect('freq')
        root = active.siblingAtColumn(0)
        self.view.expand(root)
        self.view.selectionModel().select(
            root,
            QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QtCore.QItemSelectionModel.SelectionFlag.Rows,
        )
        current_expression = self.tab.get_active_value_item('freq')
        self.view.setCurrentIndex(current_expression.index())
        checkbox = self.tab.get_global_item_by_name('freq', self.tab.GLOBALS_COL_SCAN_ENABLED)
        checkbox.setCheckState(QtCore.Qt.CheckState.Checked)
        self.qapplication.processEvents()

        active, _ = self.value_rect('freq')
        self.assertEqual(active.data(self.tab.GLOBALS_ROLE_EXPRESSION), 'scan')
        self.assertTrue(self.view.isExpanded(active.siblingAtColumn(0)))
        self.assertEqual(self.tab.selected_global_names(), ['freq'])
        self.assertEqual(self.view.currentIndex(), current_expression.index())
        names = [
            self.tab.globals_model.item(row, self.tab.GLOBALS_COL_NAME).text()
            for row in range(self.tab.globals_model.rowCount())
        ]
        self.assertLess(names.index('freq'), names.index('power'))

    def test_arrow_keys_visit_expanded_child(self):
        active, _ = self.value_rect('freq')
        self.view.expand(active.siblingAtColumn(0))
        self.view.setCurrentIndex(active)
        send_key(self.view, QtCore.Qt.Key.Key_Down)
        self.assertEqual(self.view.currentIndex(), self.expression_item('freq', 'scan').index())

    def test_tab_from_active_value_goes_to_units(self):
        active, _ = self.value_rect('freq')
        self.view.setCurrentIndex(active)
        self.view.edit(active)
        send_key(self.editor(), QtCore.Qt.Key.Key_Tab)
        self.assertEqual(self.view.currentIndex().column(), self.tab.GLOBALS_COL_UNITS)

    def test_hidden_unicode_in_inactive_expression_stays_visible(self):
        self.tab.change_global_scan('freq', 'linspace(0, 1, 3)', '1\u200b', interactive=False)
        root = self.tab.get_global_item_by_name('freq', self.tab.GLOBALS_COL_SCAN_ENABLED).index()
        self.assertTrue(self.view.isExpanded(root))
        self.view.collapse(root)
        self.assertTrue(self.view.isExpanded(root))

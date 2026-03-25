#####################################################################
#                                                                   #
# queueing.py                                                       #
#                                                                   #
# Copyright 2026, Monash University                                 #
#                                                                   #
# This file is part of the program runmanager, in the labscript     #
# suite (see http://labscriptsuite.org), and is licensed under the  #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""Runmanager-owned shot queue helpers.

This module keeps queue state and the queue worker thread out of
``runmanager.__main__``. For the initial eager-compile implementation the queue
items are just compiled shot filepaths. BLACS requests the next filepath via
the runmanager remote server, and completed shots are forwarded to lyse by a
separate helper.
"""

import os
import queue
import threading

from qtutils.qt import QtCore, QtGui, QtWidgets
from qtutils.qt.QtCore import pyqtSignal as Signal

from labscript_utils.qtwidgets.shotqueue import FILEPATH_COLUMN, ShotQueueWidget


class RunmanagerQueueWidget(ShotQueueWidget):
    """Shot queue widget configured for runmanager-owned compiled shots."""

    deleteRowsRequested = Signal(list)
    clearQueueRequested = Signal()
    moveRequested = Signal(str, list)

    def __init__(self, parent=None):
        ShotQueueWidget.__init__(
            self,
            parent=parent,
            accepted_extensions=('.h5', '.hdf5'),
            file_dialog_filter='Shot files (*.h5 *.hdf5)',
            allow_duplicates=True,
            column_title='Shot file',
        )
        self.queue_view.setAcceptDrops(False)
        self.queue_view.setDragEnabled(False)
        self.queue_view.setDropIndicatorShown(False)
        self.queue_view.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self._disconnect_default_controls()
        self.add_button.hide()
        self.delete_button.clicked.connect(self._emit_delete)
        self.clear_button.clicked.connect(self.clearQueueRequested.emit)
        self.move_top_button.clicked.connect(lambda: self._emit_move('top'))
        self.move_up_button.clicked.connect(lambda: self._emit_move('up'))
        self.move_down_button.clicked.connect(lambda: self._emit_move('down'))
        self.move_bottom_button.clicked.connect(lambda: self._emit_move('bottom'))
        self.queue_view.deleteRequested.connect(self._emit_delete)

    def _disconnect_default_controls(self):
        for button in (
            self.add_button,
            self.delete_button,
            self.clear_button,
            self.move_top_button,
            self.move_up_button,
            self.move_down_button,
            self.move_bottom_button,
        ):
            try:
                button.clicked.disconnect()
            except TypeError:
                pass
        try:
            self.queue_view.deleteRequested.disconnect()
        except TypeError:
            pass
        try:
            self.queue_view.filesDropped.disconnect()
        except TypeError:
            pass

    def set_queue_paths(self, paths):
        selected_paths = set(self.selected_paths())
        self.queue_model.removeRows(0, self.queue_model.rowCount())
        for path in paths:
            item = QtGui.QStandardItem(os.path.basename(path))
            item.setEditable(False)
            item.setToolTip(path)
            item.setData(path, QtCore.Qt.UserRole)
            self.queue_model.appendRow([item])
        self._restore_selection(selected_paths)

    def selected_paths(self):
        paths = []
        for row in self.selected_rows():
            item = self.queue_model.item(row, FILEPATH_COLUMN)
            paths.append(item.data(QtCore.Qt.UserRole))
        return paths

    def _restore_selection(self, selected_paths):
        if not selected_paths:
            return
        rows = []
        for row in range(self.queue_model.rowCount()):
            item = self.queue_model.item(row, FILEPATH_COLUMN)
            if item.data(QtCore.Qt.UserRole) in selected_paths:
                rows.append(row)
        self._select_rows(rows)

    def _emit_delete(self):
        rows = self.selected_rows()
        if rows:
            self.deleteRowsRequested.emit(rows)

    def _emit_move(self, direction):
        rows = self.selected_rows()
        if rows:
            self.moveRequested.emit(direction, rows)


class QueueController(object):
    """Thread-safe filepath queue."""

    def __init__(self):
        self._items = []
        self._lock = threading.RLock()

    def enqueue(self, paths):
        paths = [os.path.abspath(str(path)) for path in paths]
        with self._lock:
            self._items.extend(paths)

    def delete_rows(self, rows):
        with self._lock:
            for row in sorted(set(rows), reverse=True):
                if 0 <= row < len(self._items):
                    del self._items[row]

    def clear(self):
        with self._lock:
            self._items = []

    def move(self, direction, rows):
        rows = sorted(set(rows))
        if not rows:
            return
        with self._lock:
            items = self._items
            if direction == 'up':
                for row in rows:
                    if row > 0 and row - 1 not in rows:
                        items[row - 1], items[row] = items[row], items[row - 1]
            elif direction == 'down':
                for row in reversed(rows):
                    if row < len(items) - 1 and row + 1 not in rows:
                        items[row + 1], items[row] = items[row], items[row + 1]
            elif direction == 'top':
                selected = [items[row] for row in rows]
                remaining = [item for index, item in enumerate(items) if index not in rows]
                self._items = selected + remaining
            elif direction == 'bottom':
                selected = [items[row] for row in rows]
                remaining = [item for index, item in enumerate(items) if index not in rows]
                self._items = remaining + selected
            else:
                raise ValueError('Invalid move direction: %s' % direction)

    def get_queue_paths(self):
        with self._lock:
            return list(self._items)

    def export_state(self):
        with self._lock:
            return {'items': list(self._items)}

    def restore_state(self, state):
        with self._lock:
            self._items = [os.path.abspath(str(path)) for path in state.get('items', [])]

    def get_queue_state(self):
        with self._lock:
            return {'n_items': len(self._items)}

    def pop_next(self):
        with self._lock:
            if not self._items:
                return None
            return self._items.pop(0)


class QueueManager(QtCore.QObject):
    """Queue worker thread and synchronous wrappers for runmanager."""

    queueChanged = Signal()

    def __init__(self, ack_timeout=30):
        QtCore.QObject.__init__(self)
        self.controller = QueueController()
        self.command_queue = queue.Queue()
        self.thread = threading.Thread(target=self.mainloop)
        self.thread.daemon = True
        self.thread.start()

    def shutdown(self):
        self.command_queue.put(('close', (), None))
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)

    def enqueue(self, paths):
        return self._request('enqueue', list(paths))

    def delete_rows(self, rows):
        return self._request('delete_rows', list(rows))

    def clear(self):
        return self._request('clear')

    def move(self, direction, rows):
        return self._request('move', direction, list(rows))

    def pop_next(self):
        return self._request('pop_next')

    def get_queue_paths(self):
        return self._request('get_queue_paths')

    def get_queue_state(self):
        return self._request('get_queue_state')

    def export_state(self):
        return self._request('export_state')

    def restore_state(self, state):
        self.command_queue.put(('restore_state', (dict(state or {}),), None))

    def _request(self, command, *args):
        response_queue = queue.Queue()
        self.command_queue.put((command, args, response_queue))
        success, data = response_queue.get()
        if success:
            return data
        raise data

    def mainloop(self):
        while True:
            try:
                try:
                    command, args, response_queue = self.command_queue.get(timeout=1)
                except queue.Empty:
                    continue

                if command == 'close':
                    return

                changed = False
                if command == 'enqueue':
                    self.controller.enqueue(*args)
                    changed = True
                    result = None
                elif command == 'delete_rows':
                    self.controller.delete_rows(*args)
                    changed = True
                    result = None
                elif command == 'clear':
                    self.controller.clear()
                    changed = True
                    result = None
                elif command == 'move':
                    self.controller.move(*args)
                    changed = True
                    result = None
                elif command == 'pop_next':
                    result = self.controller.pop_next()
                    changed = result is not None
                elif command == 'get_queue_paths':
                    result = self.controller.get_queue_paths()
                elif command == 'get_queue_state':
                    result = self.controller.get_queue_state()
                elif command == 'export_state':
                    result = self.controller.export_state()
                elif command == 'restore_state':
                    self.controller.restore_state(*args)
                    changed = True
                    result = None
                else:
                    raise ValueError('Invalid queue command: %s' % command)

                if changed:
                    self.queueChanged.emit()
                if response_queue is not None:
                    response_queue.put((True, result))
            except Exception as exc:
                if response_queue is not None:
                    response_queue.put((False, exc))

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
"""Queue controller and queue tab helpers for runmanager."""

import copy
import os
import threading
import uuid
from dataclasses import dataclass

from qtutils.qt import QtCore, QtGui, QtWidgets
from qtutils.qt.QtCore import pyqtSignal as Signal

from labscript_utils.qtwidgets.shotqueue import FILEPATH_COLUMN, ShotQueueWidget


COMPILE_MODE_PRECOMPILE = 'precompile'
COMPILE_MODE_ON_REQUEST = 'compile_on_request'

EMPTY_QUEUE_STOP = 'stop'
EMPTY_QUEUE_REPEAT_LAST = 'repeat_last'
EMPTY_QUEUE_REPEAT_STANDARD = 'repeat_standard'

SOURCE_KIND_QUEUE = 'queue'
SOURCE_KIND_REPEAT_LAST = 'repeat_last'
SOURCE_KIND_REPEAT_STANDARD = 'repeat_standard'

STATUS_QUEUED = 'queued'
STATUS_PRECOMPILING = 'precompiling'
STATUS_READY = 'ready'
STATUS_ERROR = 'error'


@dataclass
class QueueShotDescriptor:
    shot_id: str
    label: str
    labscript_file: str
    output_folder: str
    run_file: str
    active_groups: dict
    sequence_attrs: dict
    run_no: int
    n_runs: int
    sequence_globals_frozen: dict
    shot_globals_frozen: dict
    shot_globals_overrides: dict
    send_to_runviewer: bool = False
    source_kind: str = SOURCE_KIND_QUEUE
    status: str = STATUS_QUEUED
    compiled_path: str = None
    compile_error: str = None

    def clone(self, **overrides):
        data = copy.deepcopy(self.__dict__)
        data.update(overrides)
        if 'shot_id' not in overrides:
            data['shot_id'] = uuid.uuid4().hex
        return QueueShotDescriptor(**data)

    def summary(self):
        return {
            'id': self.shot_id,
            'label': self.label,
            'source_kind': self.source_kind,
            'status': self.status,
            'compiled_path': self.compiled_path,
            'compile_error': self.compile_error,
            'run_file': self.run_file,
        }

class RunmanagerQueueWidget(ShotQueueWidget):
    """Shared queue widget adapted for logical runmanager queue items."""

    deleteRowsRequested = Signal(list)
    clearQueueRequested = Signal()

    def __init__(self, parent=None):
        ShotQueueWidget.__init__(
            self,
            parent=parent,
            accepted_extensions=('.h5', '.hdf5'),
            file_dialog_filter='Shot files (*.h5 *.hdf5)',
            allow_duplicates=True,
            column_title='Queued shot',
        )
        self.queue_view.setAcceptDrops(False)
        self.queue_view.setDragEnabled(False)
        self.queue_view.setDropIndicatorShown(False)
        self.queue_view.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self.queue_view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)

        self._disconnect_default_controls()
        self.queue_view.deleteRequested.connect(self._emit_delete)
        self.queue_view.customContextMenuRequested.connect(self._show_context_menu)

    def _disconnect_default_controls(self):
        for button in (
            self.add_button,
            self.delete_button,
            self.clear_button,
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

    def selected_item_ids(self):
        item_ids = []
        for row in self.selected_rows():
            item = self.queue_model.item(row, FILEPATH_COLUMN)
            item_ids.append(item.data(QtCore.Qt.UserRole))
        return item_ids

    def set_queue_items(self, items):
        selected_ids = set(self.selected_item_ids())
        self.queue_model.removeRows(0, self.queue_model.rowCount())
        for item_data in items:
            item = QtGui.QStandardItem(item_data['display_text'])
            item.setToolTip(item_data['tooltip'])
            item.setEditable(False)
            item.setData(item_data['id'], QtCore.Qt.UserRole)
            self.queue_model.appendRow([item])
        self._restore_selection(selected_ids)

    def _emit_delete(self):
        rows = self.selected_rows()
        if rows:
            self.deleteRowsRequested.emit(rows)

    def _show_context_menu(self, pos):
        index = self.queue_view.indexAt(pos)
        if index.isValid() and not self.queue_view.selectionModel().isSelected(index):
            self._select_rows([index.row()])

        has_selection = bool(self.selected_rows())
        row_count = self.queue_model.rowCount()
        if not has_selection and not row_count:
            return

        menu = QtWidgets.QMenu(self.queue_view)
        delete_action = None
        clear_action = None
        if has_selection:
            delete_action = menu.addAction('Delete selected')
        if row_count:
            clear_action = menu.addAction('Clear queue')

        action = menu.exec_(self.queue_view.viewport().mapToGlobal(pos))
        if action is delete_action:
            self._emit_delete()
        elif action is clear_action:
            self.clearQueueRequested.emit()

    def _restore_selection(self, item_ids):
        if not item_ids:
            return
        rows = []
        for row in range(self.queue_model.rowCount()):
            item = self.queue_model.item(row, FILEPATH_COLUMN)
            if item.data(QtCore.Qt.UserRole) in item_ids:
                rows.append(row)
        self._select_rows(rows)


class QueueController(object):
    def __init__(self, on_items_discarded=None):
        self.on_items_discarded = on_items_discarded
        self.compile_mode = COMPILE_MODE_PRECOMPILE
        self.empty_queue_policy = EMPTY_QUEUE_STOP
        self.standard_labscript_file = ''
        self._items = []
        self._last_queue_descriptor = None
        self._lock = threading.RLock()

    def set_compile_mode(self, value):
        if value not in (COMPILE_MODE_PRECOMPILE, COMPILE_MODE_ON_REQUEST):
            raise ValueError('Invalid compile mode: %s' % value)
        with self._lock:
            self.compile_mode = value

    def set_empty_queue_policy(self, value):
        if value not in (
            EMPTY_QUEUE_STOP,
            EMPTY_QUEUE_REPEAT_LAST,
            EMPTY_QUEUE_REPEAT_STANDARD,
        ):
            raise ValueError('Invalid empty queue policy: %s' % value)
        with self._lock:
            self.empty_queue_policy = value

    def set_standard_labscript_file(self, value):
        with self._lock:
            self.standard_labscript_file = os.path.abspath(value) if value else ''

    def enqueue(self, descriptors):
        descriptors = [copy.deepcopy(descriptor) for descriptor in descriptors]
        with self._lock:
            self._items.extend(descriptors)
            self._refresh_last_queue_descriptor_locked()
            return [descriptor.shot_id for descriptor in descriptors]

    def get_descriptor_for_precompile(self, shot_id):
        with self._lock:
            for descriptor in self._items:
                if descriptor.shot_id == shot_id:
                    descriptor.status = STATUS_PRECOMPILING
                    descriptor.compile_error = None
                    return copy.deepcopy(descriptor)
        return None

    def finish_precompile(self, shot_id, compiled_path=None, error=None):
        with self._lock:
            for descriptor in self._items:
                if descriptor.shot_id == shot_id:
                    if error is None:
                        descriptor.compiled_path = compiled_path
                        descriptor.status = STATUS_READY
                        descriptor.compile_error = None
                    else:
                        descriptor.status = STATUS_ERROR
                        descriptor.compile_error = error
                    return True
        return False

    def pop_next(self, repeat_standard_factory):
        with self._lock:
            if self._items:
                descriptor = self._items.pop(0)
            elif self.empty_queue_policy == EMPTY_QUEUE_STOP:
                return None
            elif self.empty_queue_policy == EMPTY_QUEUE_REPEAT_LAST:
                if self._last_queue_descriptor is None:
                    return None
                descriptor = self._last_queue_descriptor.clone(
                    shot_id=uuid.uuid4().hex,
                    source_kind=SOURCE_KIND_REPEAT_LAST,
                    status=STATUS_QUEUED,
                    compiled_path=None,
                    compile_error=None,
                )
            elif self.empty_queue_policy == EMPTY_QUEUE_REPEAT_STANDARD:
                descriptor = repeat_standard_factory()
            else:
                raise AssertionError('Unhandled empty queue policy: %s' % self.empty_queue_policy)
            self._refresh_last_queue_descriptor_locked(clear_if_empty=False)
            return copy.deepcopy(descriptor)

    def delete_rows(self, rows):
        removed = []
        with self._lock:
            for row in sorted(set(rows), reverse=True):
                if 0 <= row < len(self._items):
                    removed.append(self._items.pop(row))
            self._refresh_last_queue_descriptor_locked(clear_if_empty=True)
        self._discard_items(removed)

    def clear(self):
        removed = []
        with self._lock:
            removed = self._items
            self._items = []
            self._last_queue_descriptor = None
        self._discard_items(removed)

    def get_queue_items(self):
        with self._lock:
            return [descriptor.summary() for descriptor in self._items]

    def get_descriptors(self):
        with self._lock:
            return [copy.deepcopy(descriptor) for descriptor in self._items]

    def export_state(self):
        with self._lock:
            items = []
            for descriptor in self._items:
                data = copy.deepcopy(descriptor.__dict__)
                data['status'] = STATUS_QUEUED
                data.pop('compiled_path', None)
                data.pop('compile_error', None)
                items.append(data)
            return {
                'compile_mode': self.compile_mode,
                'empty_queue_policy': self.empty_queue_policy,
                'standard_labscript_file': self.standard_labscript_file,
                'items': items,
            }

    def restore_state(self, state):
        removed = []
        with self._lock:
            removed = self._items
            self.compile_mode = state.get('compile_mode', COMPILE_MODE_PRECOMPILE)
            self.empty_queue_policy = state.get(
                'empty_queue_policy', EMPTY_QUEUE_STOP
            )
            self.standard_labscript_file = state.get('standard_labscript_file', '')
            self._items = []
            for descriptor_data in state.get('items', []):
                data = copy.deepcopy(descriptor_data)
                data['compiled_path'] = None
                data['compile_error'] = None
                data['status'] = STATUS_QUEUED
                self._items.append(QueueShotDescriptor(**data))
            self._refresh_last_queue_descriptor_locked(clear_if_empty=True)
        self._discard_items(removed)

    def get_queue_state(self):
        with self._lock:
            return {
                'compile_mode': self.compile_mode,
                'empty_queue_policy': self.empty_queue_policy,
                'standard_labscript_file': self.standard_labscript_file,
                'n_items': len(self._items),
                'items': [descriptor.summary() for descriptor in self._items],
            }

    def _refresh_last_queue_descriptor_locked(self, clear_if_empty=False):
        queue_descriptors = [
            descriptor for descriptor in self._items if descriptor.source_kind == SOURCE_KIND_QUEUE
        ]
        if queue_descriptors:
            self._last_queue_descriptor = copy.deepcopy(queue_descriptors[-1])
        elif clear_if_empty:
            self._last_queue_descriptor = None

    def _discard_items(self, descriptors):
        if self.on_items_discarded is None or not descriptors:
            return
        self.on_items_discarded(copy.deepcopy(descriptors))

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
import time
import uuid
from dataclasses import dataclass, field

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
STATUS_OFFERED = 'offered'
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


@dataclass
class QueueOffer:
    offer_id: str
    descriptor: QueueShotDescriptor
    deadline: float
    created_at: float = field(default_factory=time.monotonic)


class RunmanagerQueueWidget(ShotQueueWidget):
    """Shared queue widget adapted for logical runmanager queue items."""

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
            column_title='Queued shot',
        )
        self.queue_view.setAcceptDrops(False)
        self.queue_view.setDragEnabled(False)
        self.queue_view.setDropIndicatorShown(False)
        self.queue_view.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self.add_button.hide()

        self._disconnect_default_controls()
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

    def _emit_move(self, direction):
        rows = self.selected_rows()
        if rows:
            self.moveRequested.emit(direction, rows)

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
    def __init__(self, ack_timeout=30):
        self.ack_timeout = float(ack_timeout)
        self.compile_mode = COMPILE_MODE_PRECOMPILE
        self.empty_queue_policy = EMPTY_QUEUE_STOP
        self.standard_labscript_file = ''
        self._items = []
        self._offers = {}
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

    def reserve_next(self, repeat_standard_factory):
        with self._lock:
            self._requeue_expired_locked()
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
            descriptor.status = STATUS_OFFERED
            offer_id = uuid.uuid4().hex
            self._offers[offer_id] = QueueOffer(
                offer_id=offer_id,
                descriptor=descriptor,
                deadline=time.monotonic() + self.ack_timeout,
            )
            return offer_id, copy.deepcopy(descriptor)

    def ack_received(self, offer_id, valid):
        with self._lock:
            self._requeue_expired_locked()
            offer = self._offers.pop(offer_id, None)
            if offer is None:
                return False
            if valid:
                return True
            self._restore_to_head_locked(offer.descriptor)
            return True

    def update_offer(self, offer_id, compiled_path=None, compile_error=None):
        with self._lock:
            offer = self._offers.get(offer_id)
            if offer is None:
                return False
            if compiled_path is not None:
                offer.descriptor.compiled_path = compiled_path
                offer.descriptor.status = STATUS_READY
            if compile_error is not None:
                offer.descriptor.compile_error = compile_error
                offer.descriptor.status = STATUS_ERROR
            return True

    def delete_rows(self, rows):
        with self._lock:
            for row in sorted(set(rows), reverse=True):
                if 0 <= row < len(self._items):
                    del self._items[row]
            self._refresh_last_queue_descriptor_locked(clear_if_empty=True)

    def clear(self):
        with self._lock:
            self._items = []
            self._offers = {}
            self._last_queue_descriptor = None

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
            self._refresh_last_queue_descriptor_locked(clear_if_empty=True)

    def get_queue_items(self):
        with self._lock:
            return [descriptor.summary() for descriptor in self._items]

    def export_state(self):
        with self._lock:
            items = []
            for descriptor in self._items:
                data = copy.deepcopy(descriptor.__dict__)
                data['compiled_path'] = None
                data['compile_error'] = None
                data['status'] = STATUS_QUEUED
                items.append(data)
            return {
                'compile_mode': self.compile_mode,
                'empty_queue_policy': self.empty_queue_policy,
                'standard_labscript_file': self.standard_labscript_file,
                'items': items,
            }

    def restore_state(self, state):
        with self._lock:
            self.compile_mode = state.get('compile_mode', COMPILE_MODE_PRECOMPILE)
            self.empty_queue_policy = state.get(
                'empty_queue_policy', EMPTY_QUEUE_STOP
            )
            self.standard_labscript_file = state.get('standard_labscript_file', '')
            self._offers = {}
            self._items = []
            for descriptor_data in state.get('items', []):
                data = copy.deepcopy(descriptor_data)
                data['compiled_path'] = None
                data['compile_error'] = None
                data['status'] = STATUS_QUEUED
                self._items.append(QueueShotDescriptor(**data))
            self._refresh_last_queue_descriptor_locked(clear_if_empty=True)

    def get_queue_state(self):
        with self._lock:
            self._requeue_expired_locked()
            return {
                'compile_mode': self.compile_mode,
                'empty_queue_policy': self.empty_queue_policy,
                'standard_labscript_file': self.standard_labscript_file,
                'n_items': len(self._items),
                'n_offers': len(self._offers),
                'items': [descriptor.summary() for descriptor in self._items],
                'offers': {
                    offer_id: {
                        'label': offer.descriptor.label,
                        'source_kind': offer.descriptor.source_kind,
                        'status': offer.descriptor.status,
                    }
                    for offer_id, offer in self._offers.items()
                },
            }

    def _requeue_expired_locked(self):
        now = time.monotonic()
        expired_offer_ids = [
            offer_id
            for offer_id, offer in self._offers.items()
            if offer.deadline <= now
        ]
        expired_offer_ids.sort(key=lambda offer_id: self._offers[offer_id].created_at)
        for offer_id in reversed(expired_offer_ids):
            offer = self._offers.pop(offer_id)
            self._restore_to_head_locked(offer.descriptor)

    def _restore_to_head_locked(self, descriptor):
        descriptor = copy.deepcopy(descriptor)
        descriptor.status = STATUS_READY if descriptor.compiled_path else STATUS_QUEUED
        self._items.insert(0, descriptor)
        self._refresh_last_queue_descriptor_locked(clear_if_empty=False)

    def _refresh_last_queue_descriptor_locked(self, clear_if_empty=False):
        queue_descriptors = [
            descriptor for descriptor in self._items if descriptor.source_kind == SOURCE_KIND_QUEUE
        ]
        if queue_descriptors:
            self._last_queue_descriptor = copy.deepcopy(queue_descriptors[-1])
        elif clear_if_empty:
            self._last_queue_descriptor = None

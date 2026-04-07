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

This module keeps queue state and background queue/compile work out of
``runmanager.__main__``. QueueManager owns the background compile loop used by
Engage, and it also exposes the existing queue state used by BLACS requests.
Queue items are stored internally as shot records, while the queue widget still
shows only their filepaths.
"""

import os
import queue
import threading

from qtutils.qt import QtCore, QtWidgets
from qtutils.qt.QtCore import pyqtSignal as Signal

from labscript_utils.qtwidgets.shotqueue import ShotQueueWidget
from zprocess import raise_exception_in_thread

EMPTY_QUEUE_NOTHING = 'nothing'
EMPTY_QUEUE_DEFAULT_LABSCRIPT = 'default_labscript'
COMPILE_MODE_EAGER = 'eager'
COMPILE_MODE_LAZY = 'lazy'


class RunmanagerQueueWidget(ShotQueueWidget):
    """Shot queue widget configured for runmanager-owned shot records."""

    deleteRowsRequested = Signal(list)

    def __init__(self, parent=None):
        ShotQueueWidget.__init__(
            self,
            parent=parent,
            accepted_extensions=('.h5', '.hdf5'),
            file_dialog_filter='Shot files (*.h5 *.hdf5)',
            allow_duplicates=True,
            column_titles=['Mode', 'Shot file'],
            path_column=1,
            connect_add_button=False,
            connect_delete_requested=False,
            connect_files_dropped=False,
        )
        self.queue_view.setAcceptDrops(False)
        self.queue_view.setDragEnabled(False)
        self.queue_view.setDropIndicatorShown(False)
        self.queue_view.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self.add_button.hide()
        self.queue_view.deleteRequested.connect(self._emit_delete)

    def set_queue_paths(self, paths):
        selected_paths = set(self.selected_files())
        row_infos = []
        for path_info in paths:
            if isinstance(path_info, dict):
                mode = path_info.get('mode', '')
                row_infos.append(
                    {
                        'path': path_info['path'],
                        'label': path_info.get('label', os.path.basename(path_info['path'])),
                        'tooltip': path_info.get('tooltip', path_info['path']),
                        'columns': [
                            {
                                'text': mode,
                                'tooltip': 'Compile mode: %s' % mode if mode else '',
                                'alignment': QtCore.Qt.AlignCenter,
                            }
                        ],
                    }
                )
            else:
                row_infos.append(path_info)
        self.set_row_infos(row_infos)
        self.select_paths(selected_paths)

    def _emit_delete(self):
        rows = self.selected_rows()
        if rows:
            self.deleteRowsRequested.emit(rows)


class QueueController(object):
    """Thread-safe queue of shot records."""

    def __init__(self):
        self.empty_queue_policy = EMPTY_QUEUE_NOTHING
        self.default_labscript_file = ''
        self.compile_mode = COMPILE_MODE_EAGER
        self.last_sent_from_queue = None
        self._items = []
        self._lock = threading.RLock()

    def _normalise_item(self, item):
        if isinstance(item, str):
            item = {'path': item, 'compile_mode': COMPILE_MODE_EAGER, 'compiled': True}
        record = dict(item)
        record['path'] = os.path.abspath(str(record['path']))
        labscript_file = record.get('labscript_file', '')
        record['labscript_file'] = (
            os.path.abspath(str(labscript_file)) if labscript_file else ''
        )
        compile_mode = record.get('compile_mode', COMPILE_MODE_EAGER)
        if compile_mode not in (COMPILE_MODE_EAGER, COMPILE_MODE_LAZY):
            compile_mode = COMPILE_MODE_EAGER
        record['compile_mode'] = compile_mode
        record['compiled'] = bool(record.get('compiled', compile_mode == COMPILE_MODE_EAGER))
        record['frozen_globals'] = {
            str(name): str(expression)
            for name, expression in record.get('frozen_globals', {}).items()
        }
        record['sequence_attrs'] = {
            str(name): value for name, value in record.get('sequence_attrs', {}).items()
        }
        record['active_groups'] = {
            str(name): os.path.abspath(str(path))
            for name, path in record.get('active_groups', {}).items()
        }
        record['run_no'] = int(record.get('run_no', 0))
        record['n_runs'] = int(record.get('n_runs', 1))
        return record

    def set_empty_queue_policy(self, value):
        if value not in (EMPTY_QUEUE_NOTHING, EMPTY_QUEUE_DEFAULT_LABSCRIPT):
            raise ValueError('Invalid empty queue policy: %s' % value)
        with self._lock:
            self.empty_queue_policy = value

    def set_default_labscript_file(self, value):
        with self._lock:
            self.default_labscript_file = os.path.abspath(value) if value else ''

    def set_compile_mode(self, value):
        if value not in (COMPILE_MODE_EAGER, COMPILE_MODE_LAZY):
            raise ValueError('Invalid compile mode: %s' % value)
        with self._lock:
            self.compile_mode = value

    def enqueue(self, items):
        records = [self._normalise_item(item) for item in items]
        with self._lock:
            self._items.extend(records)

    def delete_rows(self, rows):
        with self._lock:
            for row in sorted(set(rows), reverse=True):
                if 0 <= row < len(self._items):
                    self._items.pop(row)

    def clear(self):
        with self._lock:
            removed_paths = [item['path'] for item in self._items]
            self._items = []
            return removed_paths

    def get_queue_paths(self):
        with self._lock:
            return [item['path'] for item in self._items]

    def get_queue_display_items(self):
        with self._lock:
            items = []
            for item in self._items:
                compile_mode = item.get('compile_mode', COMPILE_MODE_EAGER)
                mode_label = 'JIT' if compile_mode == COMPILE_MODE_LAZY else 'compiled'
                path = item['path']
                items.append(
                    {
                        'path': path,
                        'label': os.path.basename(path),
                        'mode': mode_label,
                        'tooltip': path,
                    }
                )
            return items

    def set_last_sent_from_queue(self, value):
        with self._lock:
            self.last_sent_from_queue = str(value) if value else None

    def export_state(self):
        with self._lock:
            return {
                'empty_queue_policy': self.empty_queue_policy,
                'default_labscript_file': self.default_labscript_file,
                'compile_mode': self.compile_mode,
                'items': [dict(item) for item in self._items],
            }

    def restore_state(self, state):
        with self._lock:
            empty_queue_policy = state.get(
                'empty_queue_policy', EMPTY_QUEUE_NOTHING
            )
            if empty_queue_policy not in (
                EMPTY_QUEUE_NOTHING,
                EMPTY_QUEUE_DEFAULT_LABSCRIPT,
            ):
                empty_queue_policy = EMPTY_QUEUE_NOTHING
            self.empty_queue_policy = empty_queue_policy
            default_labscript_file = state.get('default_labscript_file', '')
            self.default_labscript_file = (
                os.path.abspath(default_labscript_file)
                if default_labscript_file
                else ''
            )
            compile_mode = state.get('compile_mode', COMPILE_MODE_EAGER)
            if compile_mode not in (COMPILE_MODE_EAGER, COMPILE_MODE_LAZY):
                compile_mode = COMPILE_MODE_EAGER
            self.compile_mode = compile_mode
            self.last_sent_from_queue = None
            self._items = [self._normalise_item(item) for item in state.get('items', [])]

    def get_queue_state(self):
        with self._lock:
            return {
                'empty_queue_policy': self.empty_queue_policy,
                'default_labscript_file': self.default_labscript_file,
                'compile_mode': self.compile_mode,
                'last_sent_from_queue': self.last_sent_from_queue,
                'n_items': len(self._items),
            }

    def pop_next(self):
        with self._lock:
            if not self._items:
                return None
            return self._items.pop(0)


class QueueManager(QtCore.QObject):
    """Queue worker thread and synchronous wrappers for runmanager."""

    queueChanged = Signal()

    def __init__(
        self,
        prepare_run_file,
        compile_run_file,
        send_to_runviewer,
        output,
        compilation_aborted,
        set_abort_enabled,
    ):
        QtCore.QObject.__init__(self)
        self.controller = QueueController()
        self.command_queue = queue.Queue()
        self.prepare_run_file_callback = prepare_run_file
        self.compile_run_file_callback = compile_run_file
        self.send_to_runviewer_callback = send_to_runviewer
        self.output = output
        self.compilation_aborted = compilation_aborted
        self.set_abort_enabled = set_abort_enabled
        self.thread = threading.Thread(target=self.mainloop)
        self.thread.daemon = True
        self.thread.start()

    def shutdown(self):
        self.command_queue.put(('close', (), None))
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)

    def enqueue(self, items):
        return self._request('enqueue', list(items))

    def compile_shots(self, records, send_to_BLACS, send_to_runviewer):
        self.command_queue.put(
            (
                'compile_shots',
                (list(records), send_to_BLACS, send_to_runviewer),
                None,
            )
        )

    def compile_shot(self, item, send_to_runviewer=False):
        return self._request('compile_shot', item, bool(send_to_runviewer))

    def _compile_shot(self, item, send_to_runviewer=False):
        if 'frozen_globals' in item:
            self.prepare_run_file_callback(item)
        success = self.compile_run_file_callback(item['labscript_file'], item['path'])
        if success and send_to_runviewer:
            self.send_to_runviewer_callback(item['path'])
        item['compiled'] = bool(success)
        return success

    def set_last_sent_from_queue(self, value):
        return self._request('set_last_sent_from_queue', value)
    def _delete_queue_files(self, paths):
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            except Exception as exc:
                self.output(
                    'Could not delete queued shot %s: %s\n'
                    % (os.path.basename(path), str(exc)),
                    red=True,
                )

    def set_empty_queue_policy(self, value):
        return self._request('set_empty_queue_policy', value)

    def set_default_labscript_file(self, value):
        return self._request('set_default_labscript_file', value)

    def set_compile_mode(self, value):
        return self._request('set_compile_mode', value)

    def delete_rows(self, rows):
        return self._request('delete_rows', list(rows))

    def clear(self):
        return self._request('clear')

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
                elif command == 'set_empty_queue_policy':
                    self.controller.set_empty_queue_policy(*args)
                    changed = True
                    result = None
                elif command == 'set_default_labscript_file':
                    self.controller.set_default_labscript_file(*args)
                    changed = True
                    result = None
                elif command == 'set_compile_mode':
                    self.controller.set_compile_mode(*args)
                    changed = True
                    result = None
                elif command == 'set_last_sent_from_queue':
                    self.controller.set_last_sent_from_queue(*args)
                    changed = True
                    result = None
                elif command == 'delete_rows':
                    self.controller.delete_rows(*args)
                    changed = True
                    result = None
                elif command == 'clear':
                    result = self.controller.clear()
                    changed = bool(result)
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
                elif command == 'compile_shot':
                    item, send_to_runviewer = args
                    result = self._compile_shot(
                        item, send_to_runviewer=send_to_runviewer
                    )
                elif command == 'compile_shots':
                    records, send_to_BLACS, send_to_runviewer = args
                    try:
                        for item in records:
                            if self.compilation_aborted.is_set():
                                self.output('Compilation aborted.\n\n', red=True)
                                break
                            compile_now = (
                                not send_to_BLACS
                                or item['compile_mode'] == COMPILE_MODE_EAGER
                            )
                            if compile_now:
                                success = self._compile_shot(
                                    item, send_to_runviewer=send_to_runviewer
                                )
                                if not success:
                                    self.compilation_aborted.set()
                                    continue
                            if send_to_BLACS:
                                self.controller.enqueue([item])
                                self.queueChanged.emit()
                                self.output(
                                    'Queued shot %s in runmanager.\n'
                                    % os.path.basename(item['path'])
                                )
                        else:
                            self.output('Ready.\n\n')
                    finally:
                        self.set_abort_enabled(False)
                        self.compilation_aborted.clear()
                    result = None
                else:
                    raise ValueError('Invalid queue command: %s' % command)

                if command == 'clear' and result:
                    self._delete_queue_files(result)

                if changed:
                    self.queueChanged.emit()
                if response_queue is not None:
                    response_queue.put((True, result))
            except Exception as exc:
                if response_queue is not None:
                    response_queue.put((False, exc))
                else:
                    raise_exception_in_thread((type(exc), exc, exc.__traceback__))

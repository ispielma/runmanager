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
import uuid

from qtutils.qt import QtCore, QtGui, QtWidgets
from qtutils.qt.QtCore import pyqtSignal as Signal

from labscript_utils.qtwidgets.shotqueue import ShotQueueWidget
from zprocess import raise_exception_in_thread

EMPTY_QUEUE_NOTHING = 'nothing'
EMPTY_QUEUE_DEFAULT_LABSCRIPT = 'default_labscript'
COMPILE_MODE_EAGER = 'eager'
COMPILE_MODE_LAZY = 'lazy'
# What an exchange tells BLACS about this runmanager: it offered a shot, its
# queue is paused, or it has nothing to offer right now. Paused is not used
# until runmanager owns a pause control, but the three states are the whole
# protocol vocabulary, so BLACS can tell them apart from the outset:
PROVIDER_SHOT = 'shot'
PROVIDER_PAUSED = 'paused'
PROVIDER_NONE = 'none'
# The shot BLACS is executing keeps its place in the queue, and so does one it
# could not run. Both are coloured rather than given a column of their own, so
# that the queue reads as a list of outstanding work with one row marked as
# under way, or as needing attention:
RUNNING_ROW_BACKGROUND = QtGui.QColor('#ccffcc')
FAILED_ROW_BACKGROUND = QtGui.QColor('#ffcccc')
ROW_BACKGROUNDS = {'running': RUNNING_ROW_BACKGROUND, 'failed': FAILED_ROW_BACKGROUND}
# What a shot record says about this session's attempt at it rather than about
# the shot: assigned by _normalise_item, and left out of a saved queue:
SESSION_ONLY_FIELDS = ('compiling', 'state', 'message')


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
                row_info = {
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
                background = ROW_BACKGROUNDS.get(path_info.get('state'))
                if background is not None:
                    row_info['background'] = background
                row_infos.append(row_info)
            else:
                row_infos.append(path_info)
        self.set_row_infos(row_infos)
        self.select_paths(selected_paths)

    def _emit_delete(self):
        # Emit the selected paths alongside their rows: the queue can shift
        # under the widget when BLACS takes the shot at the front of it, so the
        # controller needs to confirm which shots were actually selected.
        rows = list(zip(self.selected_rows(), self.selected_files()))
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
        # One stable identifier per queue row, assigned when the record is made
        # and kept for the life of the row, including across a save and restore
        # and across every retry of it. It names the row rather than a run of
        # it, so BLACS's outcome finds the row it was offered even when the
        # file it ran is a fresh copy with another name:
        record['shot_id'] = str(record.get('shot_id') or uuid.uuid4().hex)
        labscript_file = record.get('labscript_file', '')
        record['labscript_file'] = (
            os.path.abspath(str(labscript_file)) if labscript_file else ''
        )
        compile_mode = record.get('compile_mode', COMPILE_MODE_EAGER)
        if compile_mode not in (COMPILE_MODE_EAGER, COMPILE_MODE_LAZY):
            compile_mode = COMPILE_MODE_EAGER
        record['compile_mode'] = compile_mode
        record['compiled'] = bool(record.get('compiled', compile_mode == COMPILE_MODE_EAGER))
        # A compile in progress belongs to this session only, so a restored
        # shot never starts out claimed:
        record['compiling'] = False
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
        # 'running' once the row is offered to BLACS, 'failed' once BLACS
        # reports it did not complete; see offer_next() and shot_finished().
        # 'message' is why it did not complete. Like a compile in progress,
        # both belong to this session only: a restored shot never starts out
        # running, and does not carry the reason a previous session gave for
        # it, which nothing has attempted since.
        record['state'] = ''
        record['message'] = ''
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
        """Delete the queued shots given as (row, path) pairs.

        The path identifies the shot the user actually selected. Rows whose
        path no longer matches have shifted since the queue widget was drawn,
        so they are skipped rather than deleting the wrong shot. Returns the
        paths of the shots that were removed."""
        removed_paths = []
        with self._lock:
            for entry in sorted(rows, key=lambda entry: entry[0], reverse=True):
                row, path = entry[0], entry[1]
                if not 0 <= row < len(self._items):
                    continue
                if self._items[row]['path'] != os.path.abspath(str(path)):
                    continue
                removed_paths.append(self._items.pop(row)['path'])
            return removed_paths

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
                # A failed row says why in its tooltip rather than in a column
                # that would be empty on every other row:
                message = item['message']
                items.append(
                    {
                        'path': path,
                        'label': os.path.basename(path),
                        'mode': mode_label,
                        'tooltip': '%s\n%s' % (path, message) if message else path,
                        'state': item['state'],
                    }
                )
            return items

    def set_last_sent_from_queue(self, value):
        """Record the last shot handed out. True if that changed the value."""
        value = str(value) if value else None
        with self._lock:
            if self.last_sent_from_queue == value:
                return False
            self.last_sent_from_queue = value
            return True

    def export_state(self):
        with self._lock:
            return {
                'empty_queue_policy': self.empty_queue_policy,
                'default_labscript_file': self.default_labscript_file,
                'compile_mode': self.compile_mode,
                # Without what this session made of each row: a saved queue is
                # a list of work still to do, so writing out that BLACS was
                # running a row, or the paragraph explaining why it could not,
                # would only put back something restore_state discards -- and
                # would make the configuration differ from the saved one, and
                # so prompt to be saved again, after every offer:
                'items': [
                    {
                        name: value
                        for name, value in item.items()
                        if name not in SESSION_ONLY_FIELDS
                    }
                    for item in self._items
                ],
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

    def offer_next(self):
        """Offer the shot at the head of the queue if it is ready to hand over.

        A lazy shot that has not been compiled yet stays in the queue, so that
        it is neither lost nor overtaken by the empty-queue policy while it is
        being compiled. The offered shot stays in the queue too, marked
        running: the row is what BLACS is executing, so it remains visible. A
        failed row is offered again as well as a waiting one -- that is the
        retry, and it is why a shot stays at the head until it completes or the
        operator deletes it. Only a running row is refused, so the shot BLACS is
        running is not handed out a second time. Returns a copy of the record,
        or None.

        That refusal has no time limit, and only an outcome or a deletion ends
        it, so a row whose offer reply never reached BLACS -- or whose BLACS
        closed while holding it -- stays running for good, and stops the whole
        queue behind it. Deleting the row is the only way out today. Reclaiming
        one belongs with the rest of the protocol hardening, and has to come
        before an active row is protected from deletion."""
        with self._lock:
            if not self._items or not self._items[0]['compiled']:
                return None
            item = self._items[0]
            if item['state'] == 'running':
                return None
            item['state'] = 'running'
            # Whatever went wrong last time is being attempted again, so the
            # row goes back to the running appearance rather than keeping a
            # reason that no longer describes it:
            item['message'] = ''
            return dict(item)

    def shot_finished(self, shot_id, status, message=''):
        """Record how BLACS says the shot it was offered turned out.

        The outcome names the row by its stable id, so it applies to the row
        that was offered whichever file BLACS actually ran. A completed shot is
        finished with and leaves the queue; anything else stays where it is,
        which is the head of the queue, since that is the only row that can be
        offered. It keeps its id and gains the reason it did not run, so that
        the same shot is retried when BLACS asks for work again, and until then
        the operator can see which shot needs attention and why. Deleting the
        row is the only way to discard it. Returns the record the outcome
        belonged to, or None if no row has that id."""
        with self._lock:
            for index, item in enumerate(self._items):
                if item['shot_id'] != shot_id:
                    continue
                if status == 'completed':
                    return self._items.pop(index)
                item['state'] = 'failed'
                item['message'] = str(message)
                return dict(item)
            return None

    def claim_next_for_compile(self):
        """Claim the shot at the head of the queue for compilation.

        Returns ``(item, pending)``. ``item`` is the shot to compile, or None
        if there is nothing to start. ``pending`` is True while a queued shot
        is not yet ready to hand over, whether this call claimed it or another
        compile is already under way."""
        with self._lock:
            if not self._items:
                return None, False
            item = self._items[0]
            if item['compiled']:
                return None, False
            if item['compiling']:
                return None, True
            item['compiling'] = True
            return item, True

    def finish_compile(self, item, success):
        """Record the outcome of a background compile.

        A shot that failed to compile is dropped, as documented for lazy
        compilation. Returns ``(changed, still_queued)``: whether the queue
        changed, and whether the shot was still queued at all — it may have
        been deleted by the operator while it was compiling."""
        with self._lock:
            item['compiling'] = False
            item['compiled'] = bool(success)
            index = None
            for position, queued in enumerate(self._items):
                if queued is item:
                    index = position
                    break
            if index is None:
                return False, False
            if success:
                return True, True
            del self._items[index]
            return True, False


class QueueManager(QtCore.QObject):
    """Queue state access, and the worker thread that compiles shots.

    Queue state lives in the controller, which is safe to use from any thread,
    so state operations act on it directly. Only compilation is handed to the
    worker thread, so that a long Engage batch cannot hold up a queue-tab
    control or a BLACS request for the next shot."""

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
        self.batches_pending = 0
        self.batches_lock = threading.Lock()
        self.thread = threading.Thread(target=self.mainloop)
        self.thread.daemon = True
        self.thread.start()

    def shutdown(self):
        self.command_queue.put(('close', ()))
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)

    def enqueue(self, items):
        self.controller.enqueue(list(items))
        self.queueChanged.emit()

    def compile_shots(self, records, send_to_BLACS, send_to_runviewer):
        with self.batches_lock:
            self.batches_pending += 1
        self.command_queue.put(
            ('compile_shots', (list(records), send_to_BLACS, send_to_runviewer))
        )

    def _compile_shot(self, item, send_to_runviewer=False):
        if 'frozen_globals' in item:
            self.prepare_run_file_callback(item)
        success = self.compile_run_file_callback(item['labscript_file'], item['path'])
        if success and send_to_runviewer:
            self.send_to_runviewer_callback(item['path'])
        item['compiled'] = bool(success)
        return success

    def compile_next_in_background(self, send_to_runviewer):
        """Start compiling the shot at the head of the queue if it is not ready.

        Returns True while a queued shot is pending, so the caller reports that
        there is nothing to hand over yet rather than falling back to the
        empty-queue policy.

        ``send_to_runviewer`` is a callable, evaluated only when a compile is
        actually started, so that a request with nothing to do does not reach
        into the GUI.

        The compile runs on its own thread rather than through the worker's
        command queue, so that it is not held up behind an Engage batch. The
        compiler lock is taken per shot, so it starts at the next shot
        boundary."""
        item, pending = self.controller.claim_next_for_compile()
        if item is not None:
            thread = threading.Thread(
                target=self._background_compile,
                args=(item, bool(send_to_runviewer())),
            )
            thread.daemon = True
            thread.start()
        return pending

    def _background_compile(self, item, send_to_runviewer):
        success = False
        try:
            success = self._compile_shot(item, send_to_runviewer=send_to_runviewer)
        except Exception as exc:
            self.output(
                'Could not compile queued shot %s: %s\n'
                % (os.path.basename(item['path']), str(exc)),
                red=True,
            )
        changed, still_queued = self.controller.finish_compile(item, success)
        if success and not still_queued:
            # The shot was deleted from the queue while it was compiling, which
            # deleted its file. Remove the one this compile just wrote.
            self._delete_queue_files([item['path']])
        elif not success and changed:
            self.output(
                'Dropped queued shot %s.\n' % os.path.basename(item['path']),
                red=True,
            )
        if changed:
            self.queueChanged.emit()

    def set_last_sent_from_queue(self, value):
        if self.controller.set_last_sent_from_queue(value):
            self.queueChanged.emit()

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
        self.controller.set_empty_queue_policy(value)
        self.queueChanged.emit()

    def set_default_labscript_file(self, value):
        self.controller.set_default_labscript_file(value)
        self.queueChanged.emit()

    def set_compile_mode(self, value):
        self.controller.set_compile_mode(value)
        self.queueChanged.emit()

    def delete_rows(self, rows):
        removed_paths = self.controller.delete_rows(list(rows))
        if removed_paths:
            self._delete_queue_files(removed_paths)
            self.queueChanged.emit()
        return removed_paths

    def clear(self):
        removed_paths = self.controller.clear()
        if removed_paths:
            self._delete_queue_files(removed_paths)
            self.queueChanged.emit()
        return removed_paths

    def offer_next(self):
        item = self.controller.offer_next()
        if item is not None:
            self.queueChanged.emit()
        return item

    def shot_finished(self, shot_id, status, message=''):
        record = self.controller.shot_finished(shot_id, status, message)
        if status != 'completed':
            self.output(
                'BLACS reported shot %s as %s%s\n'
                % (
                    os.path.basename(record['path']) if record else shot_id,
                    status,
                    ': %s' % message if message else '',
                ),
                red=True,
            )
        if record is not None:
            self.queueChanged.emit()
        return record

    def get_queue_paths(self):
        return self.controller.get_queue_paths()

    def get_queue_state(self):
        return self.controller.get_queue_state()

    def export_state(self):
        return self.controller.export_state()

    def restore_state(self, state):
        self.controller.restore_state(dict(state or {}))
        self.queueChanged.emit()

    def mainloop(self):
        while True:
            try:
                try:
                    command, args = self.command_queue.get(timeout=1)
                except queue.Empty:
                    continue

                if command == 'close':
                    return

                if command == 'compile_shots':
                    records, send_to_BLACS, send_to_runviewer = args
                    aborted = False
                    try:
                        for item in records:
                            if self.compilation_aborted.is_set():
                                aborted = True
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
                                    aborted = True
                                    break
                            if send_to_BLACS:
                                # Enqueue each shot as it is compiled, so that
                                # BLACS can collect it without waiting for the
                                # rest of the batch:
                                self.enqueue([item])
                                self.output(
                                    'Queued shot %s in runmanager.\n'
                                    % os.path.basename(item['path'])
                                )
                        if aborted:
                            self.output('Compilation aborted.\n\n', red=True)
                        else:
                            self.output('Ready.\n\n')
                    finally:
                        # The abort flag is cleared by the next Engage, not
                        # here, so that aborting also stops batches already
                        # queued behind this one. Abort stays available while
                        # any of them are still pending:
                        with self.batches_lock:
                            self.batches_pending -= 1
                            last_batch = self.batches_pending == 0
                        if last_batch:
                            self.set_abort_enabled(False)
                else:
                    raise ValueError('Invalid queue command: %s' % command)
            except Exception as exc:
                raise_exception_in_thread((type(exc), exc, exc.__traceback__))

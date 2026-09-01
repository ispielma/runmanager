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
# queue is paused, or it has nothing to offer right now. Paused is told apart
# from having nothing so that BLACS can show an operator why no queued work is
# arriving; neither is a reason for BLACS to stop:
PROVIDER_SHOT = 'shot'
PROVIDER_PAUSED = 'paused'
PROVIDER_NONE = 'none'
# How BLACS may say a shot it was offered turned out. Every one but 'completed'
# leaves the row at the head of the queue in red; see shot_finished():
SHOT_OUTCOME_STATUSES = ('completed', 'aborted', 'failed', 'rejected')
# One colour, for the one thing a colour is needed for. The reserved first row
# is above the rule, which is what says BLACS was sent that shot, so running is
# simply what that row looks like and needs no colour of its own. Red marks the
# exception: it came back without running, and is waiting for an operator. The
# rows below the rule are work still waiting and are never tinted.
FAILED_ROW_BACKGROUND = QtGui.QColor('#ffcccc')
ROW_BACKGROUNDS = {
    'failed': FAILED_ROW_BACKGROUND,
    'rejected': FAILED_ROW_BACKGROUND,
    'compile_failed': FAILED_ROW_BACKGROUND,
}
# Set with either of them, and not left to the theme. Both fills are pale, so
# on a dark theme the palette's own near-white text sits on them unreadably;
# naming the text colour alongside the fill is what keeps the pair legible
# whichever theme is in use.
TINTED_ROW_FOREGROUND = QtGui.QColor('#202020')
# What a shot record says about this session's attempt at it rather than about
# the shot: assigned by _normalise_item, and left out of a saved queue:
SESSION_ONLY_FIELDS = ('compiling', 'state', 'message', 'reclaimed')


# The states a row reaches by being given to BLACS. Not every state is one:
# 'compile_failed' is runmanager's own, reached without the row ever leaving
# here, and reading it as a handover put a file BLACS had never seen into the
# row reserved for the shot BLACS was given, kept it through a replacement
# submission, and told the operator BLACS was running it. A new state that
# does mean a handover joins by being named here.
BLACS_STATES = ('running', 'failed', 'rejected')


def sent_to_blacs(row):
    """Whether this row has been handed to BLACS.

    Written once because three rules turn on it -- which row the queue reserves
    its first place for, what Clear leaves alone, and what a saved queue means
    -- and a state added to BLACS_STATES joins all three without being named
    again in each."""
    return row.get('state') in BLACS_STATES


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

    def _row_info(self, path_info):
        mode = path_info.get('mode', '')
        row_info = {
            'path': path_info['path'],
            'label': path_info.get('label', os.path.basename(path_info['path'])),
            'tooltip': path_info.get('tooltip', path_info['path']),
            'row_id': path_info.get('shot_id'),
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
            row_info['foreground'] = TINTED_ROW_FOREGROUND
        return row_info

    def _sent_to_blacs_row(self, path_info):
        """The reserved first row: whichever shot has gone to BLACS.

        Set apart from the queue below it by a rule and by its colour, because
        it is not waiting work -- it is the shot BLACS was given, and what an
        operator wants to know about it is different. It keeps the columns,
        rather than being a label above the table, so that a column added later
        describes it too.

        What "has been sent" means is named once, in BLACS_STATES, so a state
        added there is included here without this having to learn its name. A
        compile failure is deliberately not one of them: that row never left
        runmanager, and drawing it here claimed a handover that never
        happened."""
        if path_info is None:
            return {
                'path': '',
                'label': 'Nothing sent to BLACS',
                'tooltip': 'The shot BLACS was given appears here while it has one.',
                'rule_below': True,
                # Nothing to act on, so nothing to select:
                'selectable': False,
            }
        row_info = self._row_info(path_info)
        row_info['rule_below'] = True
        if path_info['state'] == 'running':
            # Not selectable, so Delete cannot even be aimed at it. The queue
            # refuses to remove it anyway, but saying so afterwards means
            # printing into the output box on another tab, which an operator
            # looking at the queue never sees. Refusing the selection says it
            # where they are, before they try, and the tooltip says why.
            row_info['selectable'] = False
            row_info['tooltip'] = (
                '%s\nBLACS is running this shot. It cannot be deleted until it '
                'is done.' % path_info['path']
            )
        return row_info

    def set_queue_paths(self, paths):
        selected_ids = set(self.selected_ids())
        paths = list(paths)
        # Only the head can have been sent: it is the row that gets offered,
        # and an outcome either retires it or leaves it there with a state.
        sent = paths[0] if paths and sent_to_blacs(paths[0]) else None
        waiting = paths[1:] if sent is not None else paths
        row_infos = [self._sent_to_blacs_row(sent)]
        for path_info in waiting:
            if isinstance(path_info, dict):
                row_infos.append(self._row_info(path_info))
            else:
                row_infos.append(path_info)
        self.set_row_infos(row_infos)
        self.select_ids(selected_ids)

    def _emit_delete(self):
        # By identity, not position. The queue moves on its own -- a shot
        # finishing removes a row while the operator has one selected -- so a
        # row number that meant one shot when the table was drawn can mean
        # another by the time the key is pressed. A shot_id means one shot for
        # as long as that shot exists, and nothing once it is gone.
        shot_ids = self.selected_ids()
        if shot_ids:
            self.deleteRowsRequested.emit(shot_ids)


class QueueController(object):
    """Thread-safe queue of shot records."""

    def __init__(self):
        self.empty_queue_policy = EMPTY_QUEUE_NOTHING
        self.default_labscript_file = ''
        self.compile_mode = COMPILE_MODE_EAGER
        # Whether this runmanager is offering the work it holds. A setting of
        # the queue rather than of any row in it: pausing withholds the next
        # shot without touching the queue, so the row BLACS is running is
        # unaffected and resuming offers the same head again.
        self.paused = False
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
        # A shot runmanager produced itself because the queue was empty, rather
        # than one a user engaged. It is queue work like any other, but it is
        # not part of a sequence and its file lives in the daily default
        # directory, so it must never become the anchor that the next Engage
        # batch is written alongside; see offer_shot().
        record['default_shot'] = bool(record.get('default_shot', False))
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

    def set_paused(self, value):
        with self._lock:
            self.paused = bool(value)

    def enqueue(self, items):
        records = [self._normalise_item(item) for item in items]
        with self._lock:
            self._items.extend(records)

    def delete_rows(self, shot_ids):
        """Delete the queued shots with these stable ids.

        By id and not by position. The queue moves on its own -- a shot
        finishing removes a row while the operator has one selected -- so a row
        number that meant one shot when the table was drawn can mean another by
        the time the key is pressed. An id means one shot for as long as it
        exists, and nothing afterwards, so an id that has gone deletes nothing
        rather than deleting whatever took its place.

        The row BLACS is running is skipped, and its file is kept:
        deleting either would take the shot file out from under hardware that
        is executing it, and editing the queue is not a way to interfere with
        the apparatus. Nothing else is protected -- a red failed row is
        deletable, which is the only way to discard one.

        So a row stuck marked running cannot be deleted. Ordinarily none needs
        to be: the next request from BLACS is offered that row again under the
        same id, so the state clears itself (see offer_next). If BLACS stays
        unavailable, restarting runmanager clears it without losing the queue,
        because export_state never writes 'running' out and restore_state gives
        every row back waiting.

        Returns ``(removed_paths, protected)``: the shots that were
        removed, and the running one that was selected and kept."""
        wanted = set(shot_ids)
        removed_paths = []
        protected = []
        with self._lock:
            keep = []
            for item in self._items:
                if item['shot_id'] not in wanted:
                    keep.append(item)
                elif item['state'] == 'running':
                    protected.append(dict(item))
                    keep.append(item)
                else:
                    removed_paths.append(item['path'])
            self._items = keep
            return removed_paths, protected

    def clear(self):
        """Empty the queue, leaving whatever has been sent to BLACS.

        Clear is reached only by the two replacement submission modes, whose
        offer is to empty the queue and submit a batch in its place. The queue
        is the work still waiting; a shot that has gone to BLACS has left it,
        and sits in the row the display reserves above the rest. Replacing the
        queue therefore replaces what is behind that row and nothing else --
        running, so that a file being written is not pulled away, and failed,
        because a shot that came back needing attention is not what an operator
        meant to discard by submitting different work.

        Discarding a failed row is still possible, and still explicit: select
        it and delete it. That is what delete_rows is for, and it is the only
        thing that does it. Returns ``(removed_paths, protected)``."""
        with self._lock:
            kept = [item for item in self._items if sent_to_blacs(item)]
            removed_paths = [
                item['path'] for item in self._items if not sent_to_blacs(item)
            ]
            self._items = kept
            return removed_paths, [dict(item) for item in kept]

    def get_queue_paths(self, include_default_shots=True):
        """Return the paths of the queued shots.

        Clear ``include_default_shots`` to leave out the shots runmanager
        produced itself, which are not part of any sequence."""
        with self._lock:
            return [
                item['path']
                for item in self._items
                if include_default_shots or not item['default_shot']
            ]

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
                        # Separately from the tooltip it is folded into, because
                        # the reserved row says the reason on its face:
                        'message': message,
                        # What the widget reports back when the operator selects
                        # a row, so that deleting never depends on a position:
                        'shot_id': item['shot_id'],
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
                'paused': self.paused,
                # Without what this session made of each row: a saved queue is
                # a list of work still to do, so writing out that BLACS was
                # running a row, or the paragraph explaining why it could not,
                # would only put back something restore_state discards -- and
                # would make the configuration differ from the saved one, and
                # so prompt to be saved again, after every offer:
                #
                # And without runmanager's own default shots at all. A default
                # shot's globals were read when it was produced, so restoring
                # the row would offer BLACS a shot in a later session as though
                # they were current -- which is the very thing
                # discard_default_shot() exists to prevent within one session.
                # Its file is in the default directory for the day it was made,
                # too, so a restored row would likely name a shot that is no
                # longer there, at the head of the queue, ahead of real work.
                # Nothing is lost by leaving it out: a runmanager that still
                # has the same empty-queue policy produces another as soon as
                # it is asked for a shot.
                'items': [
                    {
                        name: value
                        for name, value in item.items()
                        if name not in SESSION_ONLY_FIELDS
                    }
                    for item in self._items
                    if not item['default_shot']
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
            # A configuration written before there was a pause control opens
            # with the queue running, rather than silently stopped:
            self.paused = bool(state.get('paused', False))
            self.last_sent_from_queue = None
            self._items = [self._normalise_item(item) for item in state.get('items', [])]

    def get_queue_state(self):
        with self._lock:
            return {
                'empty_queue_policy': self.empty_queue_policy,
                'default_labscript_file': self.default_labscript_file,
                'compile_mode': self.compile_mode,
                'paused': self.paused,
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
        operator deletes it. Returns a copy of the record, or None.

        A row already marked running is offered again too, which is what stops
        a lost reply stranding the queue behind it. That rests on an ordering
        rule spanning both applications, recoverable from neither side's source
        alone: BLACS is sequential, and asks for a shot only when it is idle,
        carrying the outcome of the shot it has just finished on the same
        exchange. So a request that arrives without an outcome retiring this
        row proves BLACS is not running it -- either the offer reply never
        arrived, or BLACS restarted while holding it. Both make the row ours to
        hand out again, under the same id. queue_exchange() applies the outcome
        before offering, so a row BLACS has just reported on has already been
        retired or reddened by the time this looks at the head, and is never
        mistaken for a stranded one. Only offer_shot() may call this, because
        only there does a call mean that BLACS asked for work.

        The inference holds for one BLACS per runmanager, which is what this
        protocol is for. A second BLACS asking the same runmanager would be
        handed the row the first is still executing, and would run it again --
        as it would have been under the acknowledgement handshake this
        replaced. Sharing one runmanager between apparatuses is deferred with
        the rest of the multi-provider work.

        Note that running means offered and not yet reported on, not that this
        particular shot is on the hardware: if the head changes while BLACS is
        running the old one -- the operator deletes it, or a configuration is
        loaded -- it is the new head that is offered next.

        The returned copy says in ``reclaimed`` whether it was a row still
        marked running, so that the caller can report a re-offer that the
        operator would otherwise never see."""
        with self._lock:
            if not self._items or not self._items[0]['compiled']:
                return None
            item = self._items[0]
            if item['state'] == 'rejected':
                # BLACS could not read this shot at all -- a file that has gone,
                # or a connection table that does not match the apparatus. That
                # is this queue's problem and not the apparatus's, and offering
                # it again would only be refused again, once per request. It is
                # held here, red, until an operator deletes it or a restart
                # clears the state. Meanwhile BLACS is free: it keeps asking,
                # gets nothing, and runs its own shot.
                return None
            reclaimed = item['state'] == 'running'
            item['state'] = 'running'
            # Whatever went wrong last time is being attempted again, so the
            # row goes back to the running appearance rather than keeping a
            # reason that no longer describes it:
            item['message'] = ''
            record = dict(item)
            record['reclaimed'] = reclaimed
            return record

    def shot_finished(self, shot_id, status, message=''):
        """Record how BLACS says the shot it was offered turned out.

        The outcome names the row by its stable id, so it applies to the row
        that was offered whichever file BLACS actually ran. A completed shot is
        finished with and leaves the queue; anything else stays where it is,
        which is the head of the queue, since that is the only row that can be
        offered. It keeps its id and gains the reason it did not run, so that
        the same shot is retried when BLACS asks for work again, and until then
        the operator can see which shot needs attention and why. Deleting the
        row is the only way to discard it.

        Returns the record if the outcome changed a row, and None if it changed
        nothing. BLACS lets go of an outcome only once runmanager has taken it,
        so a lost reply makes it send the same one again; the repeat finds the
        row gone, or already carrying that same failure, and None is how the
        caller knows there is nothing to report and nothing to analyse."""
        message = str(message)
        with self._lock:
            for index, item in enumerate(self._items):
                if item['shot_id'] != shot_id:
                    continue
                if status == 'completed':
                    return self._items.pop(index)
                state = 'rejected' if status == 'rejected' else 'failed'
                if item['state'] == state and item['message'] == message:
                    return None
                item['state'] = state
                item['message'] = message
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
            if item['state'] == 'compile_failed':
                # Already tried, and it went red. Not claimed again, and not
                # merely to save the work: a compile that fails partway leaves
                # the devices and calibrations groups in the shot file, and
                # labscript refuses to compile into a file that has them. This
                # row can never compile, however often it is asked for.
                # Deleting it -- which takes its file with it -- is the way on.
                return None, False
            if item['compiling']:
                return None, True
            item['compiling'] = True
            return item, True

    def finish_compile(self, item, success, message=''):
        """Record the outcome of a background compile.

        A shot that failed to compile stays where it is and goes red with the
        reason, like a shot that failed to run: only completion or an explicit
        deletion takes a row out of the queue, and a shot that never compiled
        did not complete. It used to be dropped, which was quiet enough to be
        mistaken for the queue draining normally — a queue emptying with no
        shot ever running is exactly what one broken labscript file produced.

        It is not compiled again, though. See claim_next_for_compile: the
        failed compile leaves data in the shot file that stops labscript ever
        compiling into it, so the row is a dead end until it is deleted.

        Returns ``(changed, still_queued)``: whether the queue changed, and
        whether the shot is still queued — it may have been deleted by the
        operator while it was compiling."""
        with self._lock:
            item['compiling'] = False
            item['compiled'] = bool(success)
            for queued in self._items:
                if queued is item:
                    break
            else:
                return False, False
            if success:
                item['state'] = ''
                item['message'] = ''
            else:
                # Its own state, and not the one BLACS's failures use: this row
                # never left runmanager, so nothing that asks whether BLACS has
                # it should say yes. It is still red, still at the head, and
                # still a dead end until deleted.
                item['state'] = 'compile_failed'
                item['message'] = str(message)
            return True, True


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
        # Deliberately does not mark the record compiled. For a row already in
        # the queue that is the controller's to do, under its lock, in
        # finish_compile: marking it here made it offerable before the compile
        # was recorded, and the offer's running state was then wiped by the
        # compile finishing -- so the row was handed to BLACS and offered again
        # afterwards as though it never had been. The eager caller below marks
        # its own record, which is not in the queue yet.
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
        # What the row will say it went red for. A failure in the user's script
        # reaches us only as False, the compiler having written the traceback to
        # the output box, so there is nothing to quote here but a pointer to it.
        # A failure to get even that far arrives as an exception and can say
        # what happened.
        message = 'Could not be compiled. See the output for the reason.'
        try:
            success = self._compile_shot(item, send_to_runviewer=send_to_runviewer)
        except Exception as exc:
            message = 'Could not be compiled: %s' % str(exc)
            self.output(
                'Could not compile queued shot %s: %s\n'
                % (os.path.basename(item['path']), str(exc)),
                red=True,
            )
        changed, still_queued = self.controller.finish_compile(item, success, message)
        if success and not still_queued:
            # The shot was deleted from the queue while it was compiling, which
            # deleted its file. Remove the one this compile just wrote.
            self._delete_queue_files([item['path']])
        elif not success and changed:
            self.output(
                'Queued shot %s could not be compiled. It is held at the head '
                'of the queue; delete it to go on.\n'
                % os.path.basename(item['path']),
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

    def set_paused(self, value):
        self.controller.set_paused(value)
        self.queueChanged.emit()

    def _remove_from_queue(self, removed_paths, protected):
        """Delete the files of the rows that went, and say why one stayed.

        The operator either selected the protected row or asked for a Clear
        that would have taken it, so a line in the output box says why it is
        still there -- a line rather than a dialog, because the rest of what
        they asked for has happened.

        The reason is read off the row, because the two callers keep rows for
        different reasons: Delete keeps only the row BLACS is executing, while
        Clear keeps everything that went to BLACS, which includes rows that came
        back failed or rejected long ago. One message said "BLACS is running it"
        for all of them, which for the second kind is untrue, and it is the
        untruth most likely to send an operator to Abort on idle hardware.
        Returns the paths that were removed."""
        for row in protected:
            if row['state'] == 'running':
                reason = 'BLACS is running it.'
            else:
                reason = 'BLACS reported it as %s%s' % (
                    row['state'],
                    ': %s' % row['message'] if row['message'] else '.',
                )
            self.output(
                'Kept queued shot %s: %s\n' % (os.path.basename(row['path']), reason),
                red=True,
            )
        if removed_paths:
            self._delete_queue_files(removed_paths)
            self.queueChanged.emit()
        return removed_paths

    def delete_rows(self, rows):
        return self._remove_from_queue(*self.controller.delete_rows(list(rows)))

    def clear(self):
        return self._remove_from_queue(*self.controller.clear())

    def offer_next(self):
        item = self.controller.offer_next()
        if item is not None:
            if item['reclaimed']:
                # BLACS asked for work while this row was still marked running,
                # so the offer it was marked running for never got there. Say
                # so: the row looks no different afterwards, and a reply that
                # keeps going missing is worth an operator knowing about.
                self.output(
                    'Shot %s was still marked as running in the queue; '
                    'offering it to BLACS again.\n' % os.path.basename(item['path']),
                    red=True,
                )
            self.queueChanged.emit()
        return item

    def shot_finished(self, shot_id, status, message=''):
        record = self.controller.shot_finished(shot_id, status, message)
        if record is None:
            # The outcome changed nothing: it names a row that has gone, or one
            # already carrying this failure. That is the ordinary shape of an
            # outcome BLACS sent again because our reply went missing, so it is
            # neither reported a second time nor allowed to repaint the queue.
            return None
        if status != 'completed':
            self.output(
                'BLACS reported shot %s as %s%s\n'
                % (
                    os.path.basename(record['path']),
                    status,
                    ': %s' % message if message else '',
                ),
                red=True,
            )
        self.queueChanged.emit()
        return record

    def get_queue_paths(self, include_default_shots=True):
        return self.controller.get_queue_paths(include_default_shots)

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
                                # Safe here, and only here: this record is not
                                # in the queue until enqueue() below, so no
                                # other thread can see it half-marked.
                                item['compiled'] = bool(success)
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

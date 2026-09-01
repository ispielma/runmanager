#####################################################################
#                                                                   #
# blacs_status.py                                                   #
#                                                                   #
# Copyright 2026, Monash University                                 #
#                                                                   #
# This file is part of the program runmanager, in the labscript     #
# suite (see http://labscriptsuite.org), and is licensed under the  #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""What runmanager knows about the BLACS it offers shots to.

Runmanager asks; BLACS never pushes. This holds the small client that asks,
the poller that keeps asking, and the two rules that turn an answer into what
the interface shows: whether the link to BLACS is up, beside the BLACS
destination checkbox, and what BLACS is doing with the queue, beside Pause
queue. They are separate because they are separate questions -- a BLACS that
is up and deliberately not running shots is a healthy link and a stopped
queue, and one glyph cannot say that.

It is monitoring only, and deliberately so: whether BLACS requests shots, the
error that stopped it, and Abort belong to the operator standing at the
apparatus. Nothing here sends BLACS anything but a question.

runmanager does not depend on the blacs package -- the dependency runs the
other way -- so the client is runmanager's own, built on the same ZMQClient
runmanager.remote is. The wire shape is the contract between them.
"""

import os
import sys
import threading

import labscript_utils.shared_drive as shared_drive
from labscript_utils.labconfig import LabConfig
from labscript_utils.ls_zprocess import ZMQClient
from zprocess import raise_exception_in_thread

DEFAULT_PORT = 42517
# A status light, not a data feed: often enough to follow the shot BLACS is
# running, seldom enough to cost nothing. The analysis submission widget polls
# lyse on much the same footing.
POLL_INTERVAL = 2
# Short, as the analysis submission widget's own liveness check is: BLACS
# answers this off its GUI thread, so a BLACS that takes longer than this is
# one runmanager cannot reach. It also keeps a poll shorter than the wait when
# runmanager closes, so that shutting down does not have to abandon one.
POLL_TIMEOUT = 1
# The light beside the BLACS checkbox says one thing: whether BLACS answered.
# It is the lyse light on the row below in every respect -- same three states,
# same icons -- because it means the same thing, and two lights side by side
# that look alike had better not mean different things. What BLACS is doing
# with the queue is a separate question, answered in words beside Pause queue;
# a link is up or down whatever the apparatus is busy with.
LINK_ICONS = {
    'checking': ':/qtutils/fugue/hourglass',
    'online': ':/qtutils/fugue/tick',
    'offline': ':/qtutils/fugue/exclamation',
}


class Client(ZMQClient):
    """A ZMQClient for asking BLACS what it is doing.

    Only questions: BLACS's server offers this runmanager nothing that would
    change it, and this offers no way to ask for anything else."""

    def __init__(self, host=None, port=None, timeout=POLL_TIMEOUT):
        ZMQClient.__init__(self)
        if host is None:
            host = LabConfig().get('servers', 'blacs', fallback='localhost')
        if port is None:
            port = LabConfig().getint('ports', 'blacs', fallback=DEFAULT_PORT)
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, command, *args, **kwargs):
        return self.get(
            self.port, self.host, data=[command, args, kwargs], timeout=self.timeout
        )

    def say_hello(self):
        """Ping the BLACS server for a response"""
        return self.request('hello')

    def get_status(self):
        """Return what BLACS is doing.

        A dict saying whether BLACS is requesting shots, what it is doing, the
        stable id and path of the runmanager shot it is running if there is
        one, and why it stopped requesting shots if it has."""
        return self.request('get_status')


class BlacsStatusMonitor(object):
    """Keep asking BLACS what it is doing, and report every answer.

    Runmanager asks; BLACS never pushes. The asking runs on its own thread,
    independently of the shot exchange and of whether newly engaged shots are
    being queued, so the indicator stays live whatever the destination
    checkbox says, and a BLACS that has stopped answering cannot hold up the
    GUI while runmanager waits for it.

    ``on_status`` is called with each answer, with ``reachable`` saying whether
    it came from BLACS at all."""

    def __init__(self, on_status, client=None, interval=POLL_INTERVAL):
        self.on_status = on_status
        self.client = Client() if client is None else client
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.mainloop)
        self.thread.daemon = True

    def start(self):
        self.thread.start()

    def shutdown(self):
        self.stopped.set()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)

    def poll(self):
        """Ask BLACS once what it is doing, and report the answer."""
        try:
            status = self.client.get_status()
        except Exception as exc:
            status = {'reachable': False, 'reason': str(exc)}
        else:
            if isinstance(status, dict):
                status = dict(status, reachable=True)
            else:
                # A BLACS that answered with something other than a status:
                # an exception its server handed back, or a version that does
                # not know the question. Reached, but nothing to show.
                status = {'reachable': False, 'reason': str(status)}
        if self.stopped.is_set():
            # Runmanager is closing, and the GUI thread this would report to
            # is the one waiting for this thread to finish. An answer that
            # arrived too late is dropped rather than sent into a window that
            # is being taken down.
            return status
        self.on_status(status)
        return status

    def mainloop(self):
        while not self.stopped.is_set():
            try:
                self.poll()
            except Exception:
                raise_exception_in_thread(sys.exc_info())
            self.stopped.wait(self.interval)


def blacs_link_display(status, host=None):
    """Return the ``(state, tooltip)`` for the light beside the BLACS checkbox.

    Whether BLACS answered, and nothing else: ``'checking'`` before it has been
    asked, then ``'online'`` or ``'offline'``. Deliberately says nothing about
    whether BLACS is requesting shots or what it is running -- that is queue
    behaviour, and it is reported in words beside Pause queue. A BLACS sitting
    idle with requests switched off is online.

    ``status`` is the snapshot BLACS answered with, with ``reachable`` added by
    the poller, or None before BLACS has answered at all."""
    if status is None:
        return 'checking', 'Checking BLACS...'
    if status.get('reachable'):
        tooltip = 'BLACS is responding'
        state = 'online'
    else:
        tooltip = 'BLACS is not responding'
        state = 'offline'
    if host:
        tooltip += '\nHost: %s' % host
    reason = status.get('reason')
    if reason and state == 'offline':
        tooltip += '\n' + str(reason)
    return state, tooltip


def blacs_activity_display(status):
    """Return the ``(text, tooltip)`` for the line beside Pause queue.

    What BLACS is doing with the work this runmanager offers it: whether it is
    asking for shots, which one it is running, and what stopped it if anything
    has. In words rather than a glyph, because none of it is a yes or a no, and
    beside Pause queue because that is the control it is about."""
    if status is None:
        return 'BLACS: checking...', 'Waiting for BLACS to answer'
    if not status.get('reachable'):
        tooltip = 'BLACS is not responding'
        reason = status.get('reason')
        if reason:
            tooltip += '\n' + str(reason)
        return 'BLACS: not responding', tooltip
    # BLACS sends the path shared-drive-agnostic, as it sends an outcome's, so
    # put it back into the form this machine uses before showing it:
    shot_path = shared_drive.path_to_local(str(status.get('shot_path') or ''))
    error = str(status.get('error') or '')
    if error:
        # Something at the apparatus stopped BLACS requesting shots. Only an
        # operator there can clear it, so this says what it was and nothing
        # more; runmanager offers no way to acknowledge it from here.
        text = 'BLACS: stopped'
        lines = ['BLACS stopped requesting shots: %s' % error]
    elif shot_path:
        # A shot is under way whether or not BLACS is still asking for more:
        # an operator who has just unticked Request shots is watching this one
        # finish, and that is what runmanager should say it is doing.
        text = 'BLACS: running %s' % os.path.basename(shot_path)
        lines = ['BLACS is running %s' % os.path.basename(shot_path)]
    elif status.get('requesting_shots'):
        text = 'BLACS: requesting shots'
        lines = ['BLACS is requesting shots']
    else:
        text = 'BLACS: not requesting shots'
        lines = ['BLACS is not requesting shots']
    activity = str(status.get('status') or '')
    if activity:
        lines.append('Activity: %s' % activity)
    if shot_path:
        lines.append('Shot: %s' % shot_path)
    shot_id = status.get('shot_id')
    if shot_id:
        lines.append('Shot id: %s' % shot_id)
    elif shot_path:
        # No id means the shot is in no runmanager's queue: BLACS is running
        # its local override shot because this runmanager had nothing to
        # offer, which is worth saying to whoever is wondering why.
        lines.append('Not a queued shot: BLACS local override')
    if error:
        text = 'BLACS: stopped - %s' % error
    return text, '\n'.join(lines)

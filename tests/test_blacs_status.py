"""What runmanager shows about a remote BLACS.

The link light and the activity line are worked out from the status BLACS sent,
so they are tested against snapshots rather than a running apparatus. The
poller runs with a real BlacsClient and no BLACS behind it; BLACS's own tests
run it against a real BlacsServer.
"""
import os
import socket
import threading
import time
import types
import unittest

from blacs.client import BlacsClient
from qtutils.qt.QtWidgets import QApplication, QLabel

# fixtures stubs the splash and does the guarded import of the application,
# once, for every test module. Importing runmanager.__main__ here instead would
# show the startup banner.
from fixtures import RunManager
from runmanager.blacs_status import (
    BlacsStatusMonitor,
    blacs_activity_display,
    blacs_link_display,
)


def snapshot(**fields):
    """A status of the shape BLACS's get_status command answers with."""
    status = {
        'requesting_shots': False,
        'status': 'Idle',
        'shot_id': None,
        'shot_path': None,
        'error': None,
    }
    status.update(fields)
    return status


def answered(**fields):
    """The same, as the poller passes it on: BLACS was reached."""
    return dict(snapshot(**fields), reachable=True)


class LinkIndicatorTests(unittest.TestCase):
    """The light beside the BLACS checkbox: is BLACS answering?

    It means what the lyse light on the row below means, and no more. What BLACS
    is doing with the queue is reported in words beside Pause queue instead.
    """

    def test_the_light_says_whether_blacs_has_answered(self):
        unreachable = {'reachable': False, 'reason': 'Timed out waiting for BLACS'}
        for description, status, state in (
            ('nothing heard yet', None, 'checking'),
            ('answered', answered(requesting_shots=True), 'online'),
            ('did not answer', unreachable, 'offline'),
        ):
            with self.subTest(blacs=description):
                self.assertEqual(blacs_link_display(status)[0], state)

        self.assertIn(
            'Timed out waiting for BLACS',
            blacs_link_display(unreachable)[1],
            'why runmanager could not reach BLACS is worth reading',
        )

    def test_a_blacs_that_is_up_but_not_running_shots_is_still_online(self):
        # The distinction this light exists to keep: an apparatus deliberately
        # not taking work is a healthy link, not a broken one. Every one of
        # these is a BLACS that answered.
        for description, status in (
            ('not requesting shots', answered(requesting_shots=False)),
            ('stopped by an error', answered(error='Device(s) in error state')),
            (
                'running a shot',
                answered(requesting_shots=True, shot_path='/data/shot_a.h5'),
            ),
        ):
            with self.subTest(blacs=description):
                self.assertEqual(blacs_link_display(status)[0], 'online')


class ActivityLineTests(unittest.TestCase):
    """The line beside Pause queue: what is BLACS doing with the queue?"""

    def test_the_line_says_whether_blacs_is_asking_for_work(self):
        for status, text in (
            (None, 'BLACS: checking...'),
            (
                answered(requesting_shots=True, status='Requesting shots'),
                'BLACS: requesting shots',
            ),
            (
                answered(requesting_shots=False, status='Not requesting shots'),
                'BLACS: not requesting shots',
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(blacs_activity_display(status)[0], text)

    def test_a_blacs_running_a_shot_names_the_shot(self):
        text, _ = blacs_activity_display(
            answered(
                requesting_shots=True,
                status='Running (program time: 0.100s)...',
                shot_id='shot-1',
                shot_path='/data/2026/shot_a.h5',
            )
        )
        self.assertEqual(text, 'BLACS: running shot_a.h5')

    def test_a_shot_blacs_ran_on_its_own_is_told_apart_from_queue_work(self):
        # BLACS runs its local override shot when this runmanager has nothing
        # for it. That shot is in nobody's queue and has no id, and saying so
        # is how a user sees why their queue is not moving.
        text, tooltip = blacs_activity_display(
            answered(requesting_shots=True, shot_path='/data/override.h5')
        )
        self.assertEqual(text, 'BLACS: running override.h5')
        self.assertIn('local override', tooltip)

    def test_a_shot_path_from_another_machine_still_names_the_shot(self):
        # BLACS sends the path shared-drive-agnostic, so a BLACS on Windows
        # and a runmanager on anything else still agree which file it is.
        text, tooltip = blacs_activity_display(
            answered(requesting_shots=True, shot_path='Z:\\2026\\shot_a.h5')
        )
        self.assertEqual(text, 'BLACS: running shot_a.h5')
        self.assertIn(
            os.path.join('2026', 'shot_a.h5'),
            tooltip,
            'and shows the path the way this machine writes it',
        )

    def test_the_reason_blacs_stopped_is_on_the_line_itself(self):
        # Not only in the tooltip: a queue that is not moving because the
        # apparatus stopped is the thing a user most needs to see without
        # hunting for it.
        text, _ = blacs_activity_display(
            answered(
                requesting_shots=False,
                status='Device(s) in error state\nRequests stopped',
                error='Device(s) in error state',
            )
        )
        self.assertIn('stopped', text)
        self.assertIn('Device(s) in error state', text)

    def test_a_blacs_that_did_not_answer_says_that_rather_than_guessing(self):
        text, _ = blacs_activity_display({'reachable': False, 'reason': 'refused'})
        self.assertEqual(text, 'BLACS: not responding')


def absent_blacs():
    """A real BlacsClient for a BLACS that is not there: nothing is on its port."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    return BlacsClient(host='localhost', port=port, timeout=0.05)


class PollingTests(unittest.TestCase):
    def test_polling_goes_on_until_it_is_shut_down(self):
        reported = []
        monitor = BlacsStatusMonitor(
            on_status=reported.append, client=absent_blacs(), interval=0
        )
        self.addCleanup(monitor.shutdown)

        monitor.start()
        for _ in range(50):
            if len(reported) > 2:
                break
            time.sleep(0.02)
        polls_while_running = len(reported)
        monitor.shutdown()
        # shutdown() does not wait for the poller, so a poll already in flight
        # can still hand its answer over; let the last one land before counting.
        time.sleep(0.2)
        polls_after_stopping = len(reported)
        time.sleep(0.2)

        self.assertGreater(polls_while_running, 2, 'the loop keeps asking')
        self.assertEqual(
            len(reported), polls_after_stopping, 'and stops when told to'
        )


class MonitorShutdownTests(unittest.TestCase):
    """Closing runmanager while a poll is in flight.

    Reporting a status is a blocking hop to the GUI thread, so closing must not
    wait for the poller, and an answer already queued for the GUI thread must
    not be painted once the window is closing.
    """

    def test_shutdown_does_not_wait_for_a_poll_already_reporting(self):
        reporting = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def on_status(status):
            reporting.set()
            release.wait(5)

        monitor = BlacsStatusMonitor(
            on_status=on_status,
            client=absent_blacs(),
            interval=0.01,
        )
        monitor.start()
        self.assertTrue(reporting.wait(2), 'the poller got as far as reporting')

        started = time.monotonic()
        monitor.shutdown()
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            0.5,
            'closing runmanager waited on a poller that was itself waiting on '
            'the thread doing the closing',
        )

    def test_an_answer_handed_over_before_the_close_is_not_shown_after_it(self):
        # Played out in order rather than raced for: the poll hands its answer
        # over, the window begins closing, and only then does the GUI thread
        # reach the call that was waiting in its queue.
        app = FakeRunManager()
        monitor = app.blacs_status_monitor
        queued_for_the_gui = []
        # Stands in for inmain(), which queues the call for the GUI thread and
        # waits for it to be run.
        monitor.on_status = queued_for_the_gui.append

        poller = threading.Thread(target=monitor.poll)
        poller.start()
        poller.join(2)
        self.assertFalse(poller.is_alive(), 'the poll got as far as handing over')
        self.assertTrue(queued_for_the_gui, 'with an answer for the GUI thread')

        monitor.shutdown()
        for status in queued_for_the_gui:
            app.update_blacs_status(status)

        self.assertTrue(
            app.ui.blacs_status_indicator.pixmap().isNull(),
            'setPixmap on a QLabel already being torn down raises, and the '
            'operator meets it as an error dialog on the way out',
        )
        self.assertEqual(app.queue_blacs_activity_label.text(), '')


_qapplication = None


def hold_qapplication():
    global _qapplication
    if QApplication.instance() is None:
        # Held for the life of the process: a QApplication that is garbage
        # collected takes every widget built under it down with it.
        _qapplication = QApplication([])


class FakeRunManager(object):
    """Runmanager's two status surfaces, on real widgets, without the window."""

    update_blacs_status = RunManager.update_blacs_status

    def __init__(self):
        hold_qapplication()
        self.ui = types.SimpleNamespace(blacs_status_indicator=QLabel())
        self.queue_blacs_activity_label = QLabel()
        # A real monitor, because the update asks it two things: who it is
        # talking to, and whether it has been stopped.
        self.blacs_status_monitor = BlacsStatusMonitor(
            on_status=self.update_blacs_status, client=absent_blacs()
        )


class IndicatorUpdateTests(unittest.TestCase):
    """What runmanager paints on the light and the line when an answer arrives."""

    def test_the_light_reports_the_link_and_the_line_reports_the_queue(self):
        # The split this pair exists for. A BLACS that answered is online even
        # when it is deliberately running nothing, and the reason it is running
        # nothing belongs in the line, not in the light.
        app = FakeRunManager()
        app.update_blacs_status(
            answered(requesting_shots=False, error='Device(s) in error state')
        )

        self.assertFalse(app.ui.blacs_status_indicator.pixmap().isNull())
        self.assertIn('responding', app.ui.blacs_status_indicator.toolTip())
        self.assertNotIn('error state', app.ui.blacs_status_indicator.toolTip())
        self.assertIn('Device(s) in error state', app.queue_blacs_activity_label.text())

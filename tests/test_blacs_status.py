"""Behavioural tests for what runmanager shows about a remote BLACS.

The indicator state and its tooltip are worked out from the status BLACS sent,
so they are tested against snapshots rather than against a running apparatus or
a constructed RunManager.
"""
import os
import threading
import time
import types
import unittest

from qtutils import UiLoader
from qtutils.qt.QtCore import QSize
from qtutils.qt.QtGui import QIcon
from qtutils.qt.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QLayout,
    QPushButton,
)
import runmanager
import runmanager.remote
# FingerTabWidget is runmanager's own, defined in __main__ beside RunManager --
# not the labscript_utils widget of the same name. Loading main.ui with the
# wrong one gives a tab widget whose tab bar the queue tab cannot configure.
from runmanager.__main__ import (
    FingerTabWidget,
    RemoteServer,
    RunManager,
    TreeView,
)
from runmanager.analysis_submission import art_dir
from runmanager.blacs_status import (
    BlacsStatusMonitor,
    Client,
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

    It means what the lyse light on the row below means, and no more. Whether
    BLACS is requesting shots, and what it is running, are queue behaviour and
    are reported in words beside Pause queue instead.
    """

    def test_nothing_heard_from_blacs_yet_is_shown_as_checking(self):
        state, tooltip = blacs_link_display(None)
        self.assertEqual(state, 'checking')
        self.assertIn('Checking', tooltip)

    def test_a_blacs_that_answered_is_shown_as_online(self):
        state, tooltip = blacs_link_display(answered(requesting_shots=True))
        self.assertEqual(state, 'online')
        self.assertIn('responding', tooltip)

    def test_a_blacs_that_did_not_answer_is_shown_as_offline(self):
        state, tooltip = blacs_link_display(
            {'reachable': False, 'reason': 'Timed out waiting for BLACS'}
        )
        self.assertEqual(state, 'offline')
        self.assertIn('not responding', tooltip)
        self.assertIn(
            'Timed out waiting for BLACS',
            tooltip,
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

    def test_the_light_names_the_host_it_is_talking_to(self):
        _, tooltip = blacs_link_display(answered(), host='blacs-pc')
        self.assertIn('blacs-pc', tooltip)


class ActivityLineTests(unittest.TestCase):
    """The line beside Pause queue: what is BLACS doing with the queue?"""

    def test_a_blacs_asking_for_work_says_so(self):
        text, tooltip = blacs_activity_display(
            answered(requesting_shots=True, status='Requesting shots')
        )
        self.assertEqual(text, 'BLACS: requesting shots')
        self.assertIn('requesting shots', tooltip)

    def test_a_blacs_that_is_up_but_not_asking_says_so(self):
        text, tooltip = blacs_activity_display(
            answered(requesting_shots=False, status='Not requesting shots')
        )
        self.assertEqual(text, 'BLACS: not requesting shots')
        self.assertIn('not requesting shots', tooltip)

    def test_a_blacs_running_a_shot_names_the_shot(self):
        text, tooltip = blacs_activity_display(
            answered(
                requesting_shots=True,
                status='Running (program time: 0.100s)...',
                shot_id='shot-1',
                shot_path='/data/2026/shot_a.h5',
            )
        )
        self.assertEqual(text, 'BLACS: running shot_a.h5')
        self.assertIn('/data/2026/shot_a.h5', tooltip, 'the whole path is available')
        self.assertIn('shot-1', tooltip, 'which queued row this is')
        self.assertIn('Running (program time: 0.100s)...', tooltip)

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
        text, tooltip = blacs_activity_display(
            answered(
                requesting_shots=False,
                status='Device(s) in error state\nRequests stopped',
                error='Device(s) in error state',
            )
        )
        self.assertIn('stopped', text)
        self.assertIn('Device(s) in error state', text)
        self.assertIn('Device(s) in error state', tooltip)

    def test_a_blacs_that_did_not_answer_says_that_rather_than_guessing(self):
        text, _ = blacs_activity_display({'reachable': False, 'reason': 'refused'})
        self.assertEqual(text, 'BLACS: not responding')


class FakeBlacs(object):
    """A BLACS server that answers whatever it has been told to answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def request(self, command, *args, **kwargs):
        self.requests.append(command)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get_status(self):
        return self.request('get_status')


class PollingTests(unittest.TestCase):
    def poll_all(self, *answers):
        """Poll a BLACS giving each answer in turn, and collect the states."""
        blacs = FakeBlacs(*answers)
        reported = []
        monitor = BlacsStatusMonitor(on_status=reported.append, client=blacs)
        for _ in answers:
            monitor.poll()
        return blacs, [blacs_activity_display(status)[0] for status in reported]

    def test_a_blacs_that_comes_back_is_shown_as_back(self):
        # Losing BLACS is not final: polling goes on, and the indicator
        # follows it back without anything being restarted or reconnected.
        _, states = self.poll_all(
            TimeoutError('BLACS did not answer'),
            snapshot(requesting_shots=True),
        )
        self.assertEqual(
            states, ['BLACS: not responding', 'BLACS: requesting shots']
        )

    def test_the_indicator_follows_a_shot_from_running_to_failed(self):
        _, states = self.poll_all(
            snapshot(requesting_shots=True),
            snapshot(
                requesting_shots=True,
                status='Running (program time: 0.100s)...',
                shot_id='shot-1',
                shot_path='/data/shot_a.h5',
            ),
            snapshot(
                requesting_shots=False,
                status='Device(s) in error state\nRequests stopped',
                error='Device(s) in error state',
            ),
        )
        self.assertEqual(
            states,
            [
                'BLACS: requesting shots',
                'BLACS: running shot_a.h5',
                'BLACS: stopped - Device(s) in error state',
            ],
        )

    def test_polling_asks_blacs_for_nothing_but_its_status(self):
        # Monitoring only: recovering the apparatus stays with the operator
        # standing at it, so runmanager never sends BLACS anything else.
        blacs, _ = self.poll_all(snapshot(), snapshot(requesting_shots=True))
        self.assertEqual(set(blacs.requests), {'get_status'})

    def test_an_answer_that_is_not_a_status_is_not_shown_as_one(self):
        # A BLACS old enough not to know the question answers its old direct
        # submission refusal instead, and a server that failed hands back the
        # exception. Neither says anything about the apparatus.
        _, states = self.poll_all(
            'Error: BLACS no longer accepts direct shot submissions\n'
        )
        self.assertEqual(states, ['BLACS: not responding'])
        _, states = self.poll_all(RuntimeError('BLACS server returned an exception'))
        self.assertEqual(states, ['BLACS: not responding'])

    def test_an_answer_arriving_after_shutdown_is_not_reported(self):
        # Runmanager closing waits for this thread, and reporting goes to the
        # GUI thread doing the waiting. An answer that arrives once shutdown
        # has begun is dropped rather than sent into a window being torn down.
        blacs = FakeBlacs(snapshot(requesting_shots=True))
        reported = []
        monitor = BlacsStatusMonitor(on_status=reported.append, client=blacs)

        monitor.shutdown()
        monitor.poll()

        self.assertEqual(reported, [])

    def test_polling_goes_on_until_it_is_shut_down(self):
        blacs = FakeBlacs(snapshot(requesting_shots=True))
        reported = []
        monitor = BlacsStatusMonitor(
            on_status=reported.append, client=blacs, interval=0
        )
        self.addCleanup(monitor.shutdown)

        monitor.start()
        for _ in range(50):
            if len(reported) > 2:
                break
            time.sleep(0.02)
        polls_while_running = len(reported)
        monitor.shutdown()
        # shutdown() deliberately does not wait for the poller -- joining it
        # from the GUI thread is what used to deadlock the quit -- so a poll
        # already past its own stopped check can still report once. Let that
        # land before asking whether the loop stopped.
        time.sleep(0.05)
        polls_after_stopping = len(reported)
        time.sleep(0.05)

        self.assertGreater(polls_while_running, 2, 'the loop keeps asking')
        self.assertEqual(
            len(reported), polls_after_stopping, 'and stops when told to'
        )


class ClientTests(unittest.TestCase):
    def test_the_client_asks_the_configured_blacs_in_the_shape_it_answers(self):
        # BLACS dispatches a [command, args, kwargs] request the same way
        # runmanager's own server does. That shape, and the configured host
        # and port, are the whole contract between the two.
        client = Client(host='blacs-pc', port=4242, timeout=3)
        sent = []
        client.get = lambda port, host, data=None, timeout=None: sent.append(
            (port, host, data, timeout)
        )

        client.get_status()
        client.say_hello()

        self.assertEqual(
            sent,
            [
                (4242, 'blacs-pc', ['get_status', (), {}], 3),
                (4242, 'blacs-pc', ['hello', (), {}], 3),
            ],
        )


class MonitorShutdownTests(unittest.TestCase):
    """Closing runmanager while a poll is reporting.

    Reporting a status is a blocking hop to the GUI thread. Joining the poller
    from that same thread meant each waited for the other: the poller parked in
    the GUI queue, the GUI thread parked in join(), and neither moved until the
    join timed out. The operator saw the window freeze on quit, and the stale
    update then landed on widgets already torn down.

    There is nothing to wait for here. The poller holds no state worth
    flushing, it is a daemon, and it has been told to stop.
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
            client=FakeBlacs({'requesting_shots': True}),
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


class FakeLabel(object):
    def __init__(self):
        self.tooltip = ''
        self.text = ''
        self.pixmaps = []

    def setPixmap(self, pixmap):
        self.pixmaps.append(pixmap)

    def setText(self, text):
        self.text = str(text)

    def setToolTip(self, tooltip):
        self.tooltip = str(tooltip)


class FakeCheckBox(object):
    def __init__(self, checked):
        self.checked = checked

    def isChecked(self):
        return self.checked


class FakeUi(object):
    def __init__(self, run_shots_checked):
        self.blacs_status_indicator = FakeLabel()
        self.checkBox_run_shots = FakeCheckBox(run_shots_checked)


class FakeRunManager(object):
    """Runmanager's two status surfaces, over only what they use."""

    update_blacs_status = RunManager.update_blacs_status

    def __init__(self, run_shots_checked=True, host='localhost'):
        self.ui = FakeUi(run_shots_checked)
        self.queue_blacs_activity_label = FakeLabel()
        self.blacs_status_monitor = types.SimpleNamespace(
            client=types.SimpleNamespace(host=host)
        )


_qapplication = None


def load_main_ui():
    """Load main.ui the way RunManager.__init__ does."""
    global _qapplication
    if QApplication.instance() is None:
        # Held for the life of the process: a QApplication that is garbage
        # collected takes every widget built under it down with it.
        _qapplication = QApplication([])
    loader = UiLoader()
    loader.registerCustomWidget(FingerTabWidget)
    loader.registerCustomWidget(TreeView)
    return loader.load(os.path.join(os.path.dirname(runmanager.__file__), 'main.ui'))


class DestinationControlTests(unittest.TestCase):
    """The BLACS destination checkbox, and the status light beside it.

    Asserted against the loaded interface rather than against main.ui read as
    XML: what a user meets is the window runmanager builds, and a main.ui that
    will not load is a runmanager that will not start -- which reading the file
    as text cannot tell us. Loading it here also leaves the .ui free to be
    rearranged, so long as the widgets are still there and still say this.
    """

    @classmethod
    def setUpClass(cls):
        cls.ui = load_main_ui()

    def checkbox(self):
        checkbox = self.ui.findChild(QCheckBox, 'checkBox_run_shots')
        self.assertIsNotNone(checkbox, 'checkBox_run_shots is the object name')
        return checkbox

    def test_the_interface_still_loads_with_the_widgets_this_feature_needs(self):
        # The widgets both halves of this feature reach for by name:
        # AnalysisSubmission is given verticalLayout_2, and Engage reads the
        # two checkboxes.
        self.assertIsNotNone(self.ui.findChild(QLayout, 'verticalLayout_2'))
        self.assertIsNotNone(self.ui.findChild(QCheckBox, 'checkBox_view_shots'))
        indicator = self.ui.findChild(QLabel, 'blacs_status_indicator')
        self.assertIsNotNone(indicator)
        self.assertFalse(
            indicator.pixmap().isNull(),
            'the light shows something before the first poll',
        )

    def test_the_destination_control_is_labelled_blacs(self):
        # The word is a label beside the box rather than the box's own text, so
        # the BLACS logo can sit between them the way lyse's does on the
        # Analyse row below. What the operator reads is the same either way.
        label = self.ui.findChild(QLabel, 'checkBox_run_shots_text')
        self.assertIsNotNone(label)
        self.assertEqual(label.text(), 'BLACS')
        self.assertTrue(
            self.checkbox().isChecked(), 'engaged shots are queued by default'
        )

    def test_the_destination_control_wears_the_blacs_logo(self):
        self.assertIsNotNone(
            self.ui.findChild(QLabel, 'checkBox_run_shots_icon'),
            'a label for RunManager.__init__ to set the logo into',
        )
        self.assertTrue(
            (art_dir / 'blacs_22x22.png').is_file(),
            'the logo it sets into that label has to be there to set',
        )

    def test_the_destination_control_keeps_the_name_other_programs_use(self):
        # Only the label changed. What it means, what it is called, and the
        # remote methods that read and set it are a public interface.
        self.checkbox()
        self.assertTrue(hasattr(runmanager.remote.Client, 'get_run_shots'))
        self.assertTrue(hasattr(runmanager.remote.Client, 'set_run_shots'))
        self.assertTrue(hasattr(RemoteServer, 'handle_get_run_shots'))
        self.assertTrue(hasattr(RemoteServer, 'handle_set_run_shots'))

    def test_the_tooltip_says_what_the_checkbox_is_not(self):
        tooltip = self.checkbox().toolTip()
        self.assertIn('queue', tooltip, 'what ticking it does')
        for not_this in ['Pause queue', 'Request shots', 'Abort']:
            self.assertIn(
                not_this, tooltip, 'the controls it is not must be named'
            )

    def test_the_status_light_sits_beside_the_destination_control(self):
        # Whichever layout holds them, the two are laid out together: BLACS's
        # connectivity belongs next to the control that sends it work, not in
        # a dock or a status bar of its own.
        checkbox = self.checkbox()
        indicator = self.ui.findChild(QLabel, 'blacs_status_indicator')
        self.assertTrue(
            any(
                layout.indexOf(checkbox) != -1 and layout.indexOf(indicator) != -1
                for layout in self.ui.findChildren(QLayout)
            ),
            'the indicator belongs beside the checkbox, not in a new dock',
        )


class IndicatorUpdateTests(unittest.TestCase):
    """What runmanager puts on the status light when an answer arrives.

    Against the indicator's own surface rather than the loaded window: this is
    the update runmanager makes, and it should still be reported here when
    main.ui is what has gone wrong.
    """

    def test_both_surfaces_say_the_same_whatever_the_checkbox_says(self):
        # Watching BLACS is not a consequence of sending it shots: an operator
        # who has unticked the destination still needs to see what the
        # apparatus is doing with the work already queued.
        status = answered(
            requesting_shots=True,
            status='Running (program time: 0.100s)...',
            shot_id='shot-1',
            shot_path='/data/shot_a.h5',
        )
        shown = {}
        for checked in [True, False]:
            app = FakeRunManager(run_shots_checked=checked)
            app.update_blacs_status(status)
            self.assertTrue(
                app.ui.blacs_status_indicator.pixmaps, 'the light is always set'
            )
            shown[checked] = (
                app.ui.blacs_status_indicator.tooltip,
                app.queue_blacs_activity_label.text,
            )

        self.assertIn('shot_a.h5', shown[False][1])
        self.assertEqual(shown[True], shown[False])

    def test_the_light_reports_the_link_and_the_line_reports_the_queue(self):
        # The split this pair exists for. A BLACS that answered is online even
        # when it is deliberately running nothing, and the reason it is running
        # nothing belongs in the line, not in the light.
        app = FakeRunManager()
        app.update_blacs_status(
            answered(requesting_shots=False, error='Device(s) in error state')
        )

        self.assertIn('responding', app.ui.blacs_status_indicator.tooltip)
        self.assertNotIn('error state', app.ui.blacs_status_indicator.tooltip)
        self.assertIn('Device(s) in error state', app.queue_blacs_activity_label.text)

    def test_both_surfaces_say_they_are_checking_before_blacs_answers(self):
        app = FakeRunManager()
        app.update_blacs_status(None)
        self.assertIn('Checking', app.ui.blacs_status_indicator.tooltip)
        self.assertIn('checking', app.queue_blacs_activity_label.text)


class PauseQueueControlTests(unittest.TestCase):
    """The queue's pause control, built the way RunManager builds it.

    A button rather than a checkbox, so that it reads like BLACS's Request
    shots: those two together are what decide whether shots run, and a filled
    button says which way each is set from across the room.

    Building the real tab is also the only thing that catches a mistyped Qt
    enum here -- nothing else would, until runmanager was launched.
    """

    @classmethod
    def setUpClass(cls):
        # The real main.ui, because setup_queue_tab pins its tab in place with
        # tabBar().setMovable(False, index=...), and only runmanager's own
        # FingerTabBarWidget takes that index keyword. labscript_utils' bar of
        # the same name inherits QTabBar.setMovable(bool), which takes no
        # keywords at all, so building the tab against it raises TypeError.
        cls.ui = load_main_ui()

    def build_queue_tab(self):
        app = types.SimpleNamespace(ui=self.ui, refresh_queue_tab=lambda: None)
        RunManager.setup_queue_tab(app)
        return app

    def test_pausing_the_queue_is_a_two_state_button(self):
        app = self.build_queue_tab()

        button = app.queue_pause_button
        self.assertIsInstance(button, QPushButton)
        self.assertTrue(button.isCheckable())
        self.assertFalse(button.isChecked(), 'a queue starts unpaused')
        self.assertEqual(button.text(), 'Pause queue')

    def test_the_button_shows_a_different_icon_once_the_queue_is_paused(self):
        icon = self.build_queue_tab().queue_pause_button.icon()

        running = icon.pixmap(QSize(16, 16), QIcon.Mode.Normal, QIcon.State.Off)
        paused = icon.pixmap(QSize(16, 16), QIcon.Mode.Normal, QIcon.State.On)

        self.assertFalse(running.isNull())
        self.assertFalse(paused.isNull())
        self.assertNotEqual(
            running.toImage(),
            paused.toImage(),
            'the icon has to say which state the button is in',
        )

"""What runmanager answers to a remote caller that is not BLACS.

BLACS's half of the protocol is guarded in ``test_architecture.py``. This is
the rest of what runmanager's server offers: the commands a plugin or an
optimizer sends.

Each is exercised through ``RemoteServer.handler`` under the command name it
travels as, rather than by calling the handler method directly. The name is
what crosses the wire -- a handler reachable only under a name no client sends
is not reachable at all -- and the dispatch is runmanager's own.
"""
import threading
import types
import unittest
from unittest import mock

import runmanager.remote
# fixtures stubs the splash and does the guarded import of the
# application, once, for every test module. Importing
# runmanager.__main__ here instead would show the startup banner.
from fixtures import RemoteServer, main_module
from runmanager.__main__ import SUBMISSION_MODE_CONTINUE_SEQUENCE
from runmanager.queueing import (
    BLACS_STATES,
    BLOCKED_SHOT_STATE,
    EMPTY_QUEUE_DEFAULT_LABSCRIPT,
    EMPTY_QUEUE_NOTHING,
    QueueManager,
)


class LoopbackRemoteServer(RemoteServer):
    """Runmanager's own server, without binding a port.

    The real one reads a port out of the labconfig and binds a socket in its
    constructor, which is the only part a test has no use for. Subclassing
    rather than listing the handlers means this carries the whole handler set
    the running server has, so a command cannot be reachable here and missing
    there.
    """

    def __init__(self):
        pass


class FakeApp(object):
    """The application over only the surface these commands reach.

    The queue is a real QueueManager, because an accessor that answers what a
    stand-in was told to answer tests nothing. Only the callbacks it would
    reach into the GUI through are stood in for.
    """

    def __init__(self):
        self.n_shots = 1
        self.error_in_globals = False
        self.previous_expansions = {}
        # What compile_and_queue_shots was asked to do, and with which globals.
        self.modes = []
        self.submissions = []
        self.current_globals = {}
        self.ui = types.SimpleNamespace(
            checkBox_view_shots=types.SimpleNamespace(isChecked=lambda: False)
        )
        self.queue_manager = QueueManager(
            lambda item: None,
            lambda labscript_file, path: True,
            lambda path: None,
            lambda *args, **kwargs: None,
            threading.Event(),
            lambda enabled: None,
        )

    def wait_until_preparse_complete(self):
        pass

    def compile_and_queue_shots(self, submission_mode, send_to_BLACS, send_to_runviewer):
        """Stand in for compiling a batch, answering as the real one does.

        The real one reads the window, parses globals, writes shot files and
        hands them to the queue. What submit_shots does with what comes back is
        what is under test, so this answers with one record of the shape the
        queue records have.
        """
        self.modes.append((submission_mode, send_to_BLACS, send_to_runviewer))
        self.submissions.append(dict(self.current_globals))
        run_no = len(self.submissions) - 1
        return [
            {
                'shot_id': 'id-%d' % run_no,
                'path': '/tmp/experiment_%02d.h5' % run_no,
                'run_no': run_no,
                'n_runs': run_no + 1,
                'sequence_attrs': {
                    'sequence_id': '20260918T101112_experiment',
                    'sequence_index': 11,
                },
            }
        ]


class RemoteCommandTestCase(unittest.TestCase):
    """A server with an application behind it, for the length of one test."""

    def setUp(self):
        self.app = FakeApp()
        self.addCleanup(self.app.queue_manager.shutdown)
        # RemoteServer's handlers reach the application through this module
        # global, which the application assigns to itself on startup. Nothing
        # starts here, so the test puts it there and takes it away again.
        patcher = mock.patch.object(main_module, 'app', self.app, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = LoopbackRemoteServer()

    def request(self, command, *args, **kwargs):
        answer = self.server.handler([command, args, kwargs])
        if isinstance(answer, Exception):
            raise answer
        return answer


class EmptyQueuePolicyTests(RemoteCommandTestCase):
    """What runmanager does when the queue runs out, asked from outside.

    A caller that submits shots and waits for their results needs this before
    it starts: under 'nothing' an empty queue produces nothing at all, so a
    caller waiting on a result that only a shot could produce waits forever.
    """

    def test_the_policy_in_force_is_what_is_answered(self):
        for policy in (EMPTY_QUEUE_NOTHING, EMPTY_QUEUE_DEFAULT_LABSCRIPT):
            with self.subTest(policy=policy):
                self.app.queue_manager.set_empty_queue_policy(policy)
                self.assertEqual(self.request('get_empty_queue_policy'), policy)

    def test_the_client_asks_under_that_name(self):
        sent = []
        with mock.patch.object(
            runmanager.remote.Client,
            'request',
            lambda self, command, *args, **kwargs: sent.append(command),
        ):
            runmanager.remote.Client.get_empty_queue_policy(
                runmanager.remote.Client.__new__(runmanager.remote.Client)
            )

        self.assertEqual(sent, ['get_empty_queue_policy'])


class SubmitShotsTests(RemoteCommandTestCase):
    """Submitting shots by naming the globals that differ between them.

    One entry is one shot. That promise is what lets a caller match a result
    back to the entry it asked for, so an entry the session's globals would
    expand into several shots is refused rather than quietly answered with
    more descriptors than there were entries.

    Nothing is submitted until every entry has been checked. A call that
    refuses therefore leaves the queue exactly as it found it, which is what
    makes refusing safe: there is no half-submitted batch to tell the caller
    about, and the error can simply be raised.
    """

    def setUp(self):
        super().setUp()
        self.app.n_shots_for = {}
        self.server.handle_set_globals = self.set_globals
        self.server.handle_error_in_globals = lambda: self.app.error_in_globals
        self.sets = []

    def set_globals(self, globals, raw=False):
        """Stand in for the globals machinery, and for what it leads to.

        Setting a global is a globals file on disk, a group tab and a reparse,
        all of which the set-globals command already owns. What matters here is
        that it happened, in what order, and what the session then says the
        globals would produce.
        """
        self.sets.append(dict(globals))
        self.app.current_globals = dict(globals)
        self.app.n_shots = self.app.n_shots_for.get(globals['x'], 1)

    def submit(self, *names):
        return self.request('submit_shots', [{'x': name} for name in names])

    def test_each_entry_becomes_one_shot_and_is_described(self):
        descriptors = self.submit('a', 'b')

        self.assertEqual(
            descriptors,
            [
                {
                    'shot_id': 'id-0',
                    'sequence_id': '20260918T101112_experiment',
                    'run_number': 0,
                    'path': '/tmp/experiment_00.h5',
                },
                {
                    'shot_id': 'id-1',
                    'sequence_id': '20260918T101112_experiment',
                    'run_number': 1,
                    'path': '/tmp/experiment_01.h5',
                },
            ],
        )

    def test_the_globals_are_set_before_each_shot_is_submitted(self):
        self.submit('a', 'b')

        self.assertEqual(
            [entry['x'] for entry in self.app.submissions],
            ['a', 'b'],
            'each shot is submitted with the globals of its own entry, and the '
            'last entry\'s values are what the window is left showing',
        )

    def test_nothing_is_submitted_when_a_later_entry_would_expand(self):
        self.app.n_shots_for = {'c': 3}
        self.app.previous_expansions = {'width': 'outer', 'depth': '', 'height': 'x'}

        with self.assertRaises(Exception) as raised:
            self.submit('a', 'b', 'c')

        self.assertEqual(
            self.app.submissions,
            [],
            'the first two entries were fine, and are still not submitted: a '
            'call that refuses leaves nothing behind to tell the caller about',
        )
        self.assertIn('3', str(raised.exception))
        for name in ('width', 'height'):
            self.assertIn(
                name,
                str(raised.exception),
                'the globals that expanded it are named, because a scan left '
                'enabled on one of them is what the caller has to go and fix',
            )
        self.assertNotIn(
            'depth',
            str(raised.exception),
            'a global that expands into nothing did not cause this',
        )

    def test_an_error_in_the_globals_is_refused_before_anything_is_submitted(self):
        self.app.error_in_globals = True

        with self.assertRaises(Exception):
            self.submit('a')

        self.assertEqual(self.app.submissions, [])

    def test_shots_are_added_to_the_sequence_rather_than_starting_one(self):
        self.submit('a')

        self.assertEqual(
            [mode for mode, _, _ in self.app.modes],
            [SUBMISSION_MODE_CONTINUE_SEQUENCE],
            'a remote submission continues the sequence already running and '
            'never clears the queue',
        )

    def test_the_client_asks_under_that_name(self):
        sent = []
        with mock.patch.object(
            runmanager.remote.Client,
            'request',
            lambda self, command, *args, **kwargs: sent.append((command, args)),
        ):
            runmanager.remote.Client.submit_shots(
                runmanager.remote.Client.__new__(runmanager.remote.Client),
                [{'x': 1}],
            )

        self.assertEqual(sent, [('submit_shots', ([{'x': 1}],))])


class ShotStatusTests(RemoteCommandTestCase):
    """Whether a shot that was submitted can still produce a result.

    A caller waiting on the results of shots it submitted needs to know when
    to stop waiting for one. ``pending`` answers exactly that, and answers it
    as the queue itself would: it is true while the queue would still hand the
    row over, and false once the row is waiting on an operator instead.

    A row is not only its own state. Only the head of the queue is ever
    offered, so a row the queue will not hand over holds up every row behind
    it, and a shot that will never run has to say so wherever it is sitting.

    Nothing is consumed by asking. The same question can be asked as often as
    the caller likes, about shots that finished long ago, and the queue is no
    different afterwards.
    """

    # Every state a queue row can be in: whether the queue would still hand
    # that row over, and whether a row in it holds up the rows behind it.
    #
    # offer_next() hands over a waiting row, a row already marked running --
    # which is the reclaim -- and a failed one, which is the retry. It refuses
    # a rejected row, because offering it again would only be refused again,
    # and a cancelled one, which the operator has said is not to be sent;
    # claim_next_for_compile() refuses a compile_failed row, which can never
    # compile however often it is asked for.
    #
    # Of the three refusals only the cancelled row clears itself: the queue
    # drops it at the next request from BLACS, so the shots behind it are
    # waiting their turn rather than waiting on somebody. The other two stay
    # where they are until an operator deletes them.
    EXPECTED = {
        # state: (would be handed over, holds up the rows behind it)
        '': (True, False),
        'running': (True, False),
        'failed': (True, False),
        'rejected': (False, True),
        'cancelled': (False, False),
        'compile_failed': (False, True),
    }

    def enqueue(self, shot_id, state=''):
        self.app.queue_manager.enqueue(
            [{'path': '/tmp/%s.h5' % shot_id, 'shot_id': shot_id, 'compiled': True}]
        )
        for item in self.app.queue_manager.controller._items:
            if item['shot_id'] == shot_id:
                item['state'] = state

    def queue(self, *rows):
        """A queue holding exactly these ``(shot_id, state)`` rows, in order.

        Replacing what is there rather than adding to it, because what is in
        front of a row is half of its answer: the same row at the head of one
        queue and behind a held one in another is being asked two different
        questions.
        """
        self.app.queue_manager.restore_state({})
        for shot_id, state in rows:
            self.enqueue(shot_id, state=state)

    def test_every_state_a_row_can_be_in_has_an_answer(self):
        # Derived rather than listed, so that a state added to BLACS_STATES
        # arrives here without an answer instead of quietly defaulting to one.
        self.assertEqual(
            set(self.EXPECTED),
            set(BLACS_STATES) | {'', 'compile_failed'},
            'a new row state needs deciding here: can a shot in it still '
            'produce a result, or is it waiting on an operator?',
        )

    def test_pending_is_whether_the_queue_would_still_offer_the_row(self):
        for state, (pending, _) in sorted(self.EXPECTED.items()):
            with self.subTest(state=state):
                self.queue((state or 'waiting', state))
                answer = self.request('shot_status', [state or 'waiting'])
                self.assertEqual(answer[state or 'waiting']['pending'], pending)

    def test_a_row_behind_a_held_one_says_it_is_blocked(self):
        # The row in front is the whole of the reason, so the answer is the
        # same whatever the waiting row itself is doing: nothing behind a shot
        # the queue will not hand over can be handed over either, and a caller
        # polling for its result would otherwise wait for ever on a queue that
        # is not moving.
        for state, (_, holds_up) in sorted(self.EXPECTED.items()):
            with self.subTest(state=state):
                self.queue(('head', state), ('behind', ''))

                answer = self.request('shot_status', ['behind'])

                self.assertEqual(
                    answer['behind'],
                    {'pending': False, 'state': BLOCKED_SHOT_STATE}
                    if holds_up
                    else {'pending': True, 'state': ''},
                )

    def test_a_held_row_still_says_what_it_is(self):
        # Blocked is what a row behind one of these is; the row itself has a
        # reason, and that is what an operator has to act on.
        self.queue(('head', 'rejected'), ('behind', ''))

        answer = self.request('shot_status', ['head', 'behind'])

        self.assertEqual(answer['head']['state'], 'rejected')
        self.assertEqual(answer['behind']['state'], BLOCKED_SHOT_STATE)

    def test_deleting_the_row_in_front_lets_the_ones_behind_run_again(self):
        # Blocked says the queue is not moving, not that the shot is spoiled:
        # the operator clears the row that stopped it and the work behind it
        # is waiting its turn again.
        self.queue(('head', 'rejected'), ('behind', ''))
        self.assertEqual(
            self.request('shot_status', ['behind'])['behind']['state'],
            BLOCKED_SHOT_STATE,
            'which is what the caller polling it is told meanwhile',
        )

        self.app.queue_manager.delete_rows(['head'])

        self.assertEqual(
            self.request('shot_status', ['behind'])['behind'],
            {'pending': True, 'state': ''},
            'the answer is read off the queue as it stands, so a row asked '
            'about while it was stuck is not left carrying that',
        )

    def test_a_state_no_refusal_names_is_still_pending(self):
        # What the queue does with a state it has no refusal for is offer the
        # row, so that is what is answered about it. Listing the states that
        # are pending instead would make every state added later read as a
        # shot that will never run, which is the answer that makes a caller
        # give up on a shot the apparatus is about to take.
        self.queue(('novel', 'some-state-added-later'), ('behind', ''))

        answer = self.request('shot_status', ['novel', 'behind'])

        self.assertTrue(answer['novel']['pending'])
        self.assertTrue(answer['behind']['pending'])

    def test_the_state_is_passed_through_for_somebody_reading_it(self):
        self.enqueue('one', state='failed')

        self.assertEqual(self.request('shot_status', ['one'])['one']['state'], 'failed')

    def test_a_shot_the_queue_no_longer_has_is_not_pending(self):
        answer = self.request('shot_status', ['never-heard-of-it'])

        self.assertEqual(
            answer['never-heard-of-it'],
            {'pending': False, 'state': 'unknown'},
            'nothing more will happen to a shot with no row, and no row state '
            'describes it -- least of all the empty one, which means waiting',
        )

    def test_every_id_asked_about_is_answered(self):
        self.enqueue('here')

        answer = self.request('shot_status', ['here', 'gone', 'here'])

        self.assertEqual(sorted(answer), ['gone', 'here'])

    def test_asking_leaves_the_queue_as_it_was(self):
        self.enqueue('one')
        self.enqueue('two')
        before = list(self.app.queue_manager.controller._items)

        self.request('shot_status', ['one', 'two', 'three'])

        self.assertEqual(self.app.queue_manager.controller._items, before)

    def test_the_client_asks_under_that_name(self):
        sent = []
        with mock.patch.object(
            runmanager.remote.Client,
            'request',
            lambda self, command, *args, **kwargs: sent.append((command, args)),
        ):
            runmanager.remote.Client.shot_status(
                runmanager.remote.Client.__new__(runmanager.remote.Client), ['one']
            )

        self.assertEqual(sent, [('shot_status', (['one'],))])


if __name__ == '__main__':
    unittest.main()

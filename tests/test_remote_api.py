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
import unittest
from unittest import mock

import runmanager.remote
# fixtures stubs the splash and does the guarded import of the
# application, once, for every test module. Importing
# runmanager.__main__ here instead would show the startup banner.
from fixtures import RemoteServer, main_module
from runmanager.queueing import (
    BLACS_STATES,
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
        self.queue_manager = QueueManager(
            lambda item: None,
            lambda labscript_file, path: True,
            lambda path: None,
            lambda *args, **kwargs: None,
            threading.Event(),
            lambda enabled: None,
        )


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


class ShotStatusTests(RemoteCommandTestCase):
    """Whether a shot that was submitted can still produce a result.

    A caller waiting on the results of shots it submitted needs to know when
    to stop waiting for one. ``pending`` answers exactly that, and answers it
    as the queue itself would: it is true for the states the queue would still
    hand the row over in, and false for the states it holds a row in until an
    operator does something about it.

    Nothing is consumed by asking. The same question can be asked as often as
    the caller likes, about shots that finished long ago, and the queue is no
    different afterwards.
    """

    # Every state a queue row can be in, and whether the queue would still
    # offer it. offer_next() hands over a waiting row, a row already marked
    # running -- which is the reclaim -- and a failed one, which is the retry.
    # It refuses a rejected row, because offering it again would only be
    # refused again; a cancelled row is never resent at all; and a
    # compile_failed row can never compile however often it is asked for.
    EXPECTED = {
        '': True,
        'running': True,
        'failed': True,
        'rejected': False,
        'cancelled': False,
        'compile_failed': False,
    }

    def enqueue(self, shot_id, state=''):
        self.app.queue_manager.enqueue(
            [{'path': '/tmp/%s.h5' % shot_id, 'shot_id': shot_id, 'compiled': True}]
        )
        for item in self.app.queue_manager.controller._items:
            if item['shot_id'] == shot_id:
                item['state'] = state

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
        for state, pending in sorted(self.EXPECTED.items()):
            with self.subTest(state=state):
                self.enqueue(state or 'waiting', state=state)
                answer = self.request('shot_status', [state or 'waiting'])
                self.assertEqual(answer[state or 'waiting']['pending'], pending)

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

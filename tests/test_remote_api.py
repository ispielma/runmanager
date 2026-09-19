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


if __name__ == '__main__':
    unittest.main()

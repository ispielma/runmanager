"""Sending shots to runviewer, and starting it when it is not already running.

The shot goes through the real RunviewerClient to a real RunviewerServer; only
the process start is faked, by serving that server where the started runviewer
would.
"""
import functools
import os
import queue
import socket
import subprocess
import types
import unittest
from unittest import mock

from runviewer.client import RunviewerClient

# fixtures stubs the splash and does the guarded imports of runmanager's and
# runviewer's applications, once, for every test module. Importing either
# __main__ here instead would show a startup banner.
from fixtures import RunManager, main_module, runviewer_main


class RefusingQueue:
    def put(self, path):
        raise RuntimeError('runviewer cannot take shots')


class SendToRunviewerTests(unittest.TestCase):
    """The send and the launch, with only the process start faked."""

    def setUp(self):
        self.launched = []
        # runmanager's own name for the module, so that the launch reaches the
        # fake and nothing else in the process, such as a real server, does:
        patcher = mock.patch.object(
            main_module,
            'subprocess',
            types.SimpleNamespace(Popen=self.popen, DEVNULL=subprocess.DEVNULL),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # runviewer's server puts shots on this, which its application makes
        # only when it starts:
        self.shots = queue.Queue()
        patcher = mock.patch.object(
            runviewer_main, 'shots_to_process_queue', self.shots, create=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        # send_to_runviewer builds its own client; this one finds the test's server:
        patcher = mock.patch.object(
            main_module, 'RunviewerClient', functools.partial(RunviewerClient, port=self.port)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said = []
        self.app = RunManager.__new__(RunManager)
        self.app.output_box = types.SimpleNamespace(
            output=lambda text, red=False: self.said.append(text)
        )

    def serve(self):
        server = runviewer_main.RunviewerServer(
            port=self.port, bind_address='tcp://127.0.0.1'
        )
        self.addCleanup(server.shutdown)

    def popen(self, command, **kwargs):
        self.launched.append(list(command))
        # What the started runviewer does first:
        self.serve()
        return types.SimpleNamespace(pid=1234)

    def send(self):
        self.app.send_to_runviewer('/tmp/a_shot.h5')

    def test_a_shot_reaches_a_running_runviewer_as_its_local_path(self):
        self.serve()
        self.send()

        self.assertEqual(self.launched, [], 'no second runviewer is started')
        self.assertEqual(self.shots.get_nowait(), '/tmp/a_shot.h5')

    @unittest.skipIf(os.name == 'nt', 'the POSIX launch path')
    def test_runviewer_is_started_when_nothing_is_listening(self):
        self.send()

        self.assertEqual(len(self.launched), 1, 'exactly one runviewer started')
        self.assertIn('runviewer', self.launched[0], 'and it is runviewer')
        self.assertEqual(self.shots.get_nowait(), '/tmp/a_shot.h5', 'and it has the shot')

    def test_the_operator_is_told_when_runviewer_cannot_take_the_shot(self):
        runviewer_main.shots_to_process_queue = RefusingQueue()
        self.serve()
        self.send()

        self.assertTrue(
            any('cannot take shots' in line for line in self.said),
            "runviewer's own reason reaches the operator",
        )


if __name__ == '__main__':
    unittest.main()

"""Starting runviewer when it is not already running.

Ticking *View shot(s)* with no runviewer up is meant to start one. It did not:
the launch forked, and runmanager is heavily threaded, so the child inherited
one thread and every lock the others happened to hold -- the allocator's among
them, which subprocess needs. It deadlocked before starting anything and said
nothing about it.

What is pinned here is that the child is started detached and without a fork,
because a child that is not detached dies with runmanager and a fork in this
process may never get as far as starting one.
"""
import logging
import os
import subprocess
import types
import unittest

from runmanager.__main__ import RunManager


class FakeOutputBox(object):
    def __init__(self):
        self.lines = []

    def output(self, text, red=False):
        self.lines.append(text)

    def said(self, *words):
        return [l for l in self.lines if all(word in l for word in words)]


class NoRunviewerListening(Exception):
    """What zmq_get raises when nothing is on runviewer's port."""


class SendToRunviewerTests(unittest.TestCase):
    """The launch, over its two boundaries: the port and the process."""

    def setUp(self):
        import runmanager.__main__ as main_module

        self.main_module = main_module
        self.saved = {
            'zmq_get': main_module.zmq_get,
            'Popen': main_module.subprocess.Popen,
            'fork': os.fork,
            'logger': getattr(main_module, 'logger', None),
        }
        # Assigned only inside the module's __main__ block, so it does not
        # exist when the module is merely imported:
        main_module.logger = logging.getLogger('test_send_to_runviewer')
        self.launched = []
        self.probes = []
        # Nothing is listening, so every probe fails and the launch is reached.
        main_module.zmq_get = self.zmq_get
        main_module.subprocess.Popen = self.popen
        # A fork here is the bug. Fail loudly rather than actually forking the
        # test runner, which is what made this hard to see in the first place.
        os.fork = self.forbidden_fork
        self.addCleanup(self.restore)

        self.app = types.SimpleNamespace(
            exp_config=types.SimpleNamespace(get=lambda section, option: '42521'),
            output_box=FakeOutputBox(),
        )

    def restore(self):
        self.main_module.zmq_get = self.saved['zmq_get']
        self.main_module.subprocess.Popen = self.saved['Popen']
        os.fork = self.saved['fork']
        if self.saved['logger'] is None:
            del self.main_module.logger
        else:
            self.main_module.logger = self.saved['logger']

    def zmq_get(self, port, host, data=None, timeout=None):
        self.probes.append(data)
        raise NoRunviewerListening('nothing on port %s' % port)

    def popen(self, command, **kwargs):
        self.launched.append((list(command), kwargs))
        return types.SimpleNamespace(pid=1234)

    def forbidden_fork(self):
        raise AssertionError(
            'the runviewer launch forked. runmanager is multi-threaded, so a '
            'forked child inherits locks no thread will ever release; use '
            'subprocess with start_new_session instead.'
        )

    def send(self):
        RunManager.send_to_runviewer(self.app, '/tmp/a_shot.h5')

    @unittest.skipIf(os.name == 'nt', 'the POSIX launch path')
    def test_runviewer_is_started_when_nothing_is_listening(self):
        self.send()

        self.assertEqual(len(self.launched), 1, 'exactly one runviewer started')
        command, kwargs = self.launched[0]
        self.assertIn('runviewer', command, 'and it is runviewer that is started')

    @unittest.skipIf(os.name == 'nt', 'the POSIX launch path')
    def test_it_is_started_detached_so_it_outlives_runmanager(self):
        self.send()

        _, kwargs = self.launched[0]
        self.assertTrue(
            kwargs.get('start_new_session'),
            'a session of its own, or it dies with the runmanager that spawned it',
        )

    @unittest.skipIf(os.name == 'nt', 'the POSIX launch path')
    def test_its_streams_go_nowhere_rather_than_into_a_leaked_file(self):
        self.send()

        _, kwargs = self.launched[0]
        for stream in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(kwargs.get(stream), subprocess.DEVNULL, stream)

    def test_the_operator_is_told_when_it_could_not_be_reached(self):
        # Started or not, runviewer never answers here, and that has to be said
        # rather than swallowed.
        self.send()

        self.assertTrue(self.app.output_box.said("Couldn't submit shot"))


if __name__ == '__main__':
    unittest.main()

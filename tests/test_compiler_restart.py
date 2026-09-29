#####################################################################
#                                                                   #
# /tests/test_compiler_restart.py                                   #
#                                                                   #
# Copyright 2026, JQI                                               #
# Author: Ian Spielman                                              #
#                                                                   #
# This file is part of runmanager, in the labscript suite           #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""Restarting the compiler subprocess while a shot is compiling.

The subprocess is stood in for by a thread answering on queues, as
process_tree.subprocess hands them over. The rest is runmanager's own.
"""
import queue
import threading
import types
import unittest
from unittest import mock

from qtutils.qt.QtWidgets import QApplication, QPushButton

# fixtures stubs the splash and does the guarded import of the application.
from fixtures import RunManager, main_module, wait_for

_qapplication = None


class FakeChild:
    """A compiler subprocess that finishes the compile in hand before it reads on."""

    def __init__(self, release=None):
        self.to_child, self.from_child = queue.Queue(), queue.Queue()
        self.release = release
        self.returncode = None
        self.compiled = []
        threading.Thread(target=self.mainloop, daemon=True).start()

    def mainloop(self):
        while True:
            signal, data = self.to_child.get()
            if signal == 'quit':
                self.returncode = 0
                return
            self.compiled.append(data[1])
            if self.release:
                self.release.wait(10)
            self.from_child.put(['done', True])

    def poll(self):
        return self.returncode


class RestartDuringCompileTests(unittest.TestCase):
    def test_a_restart_during_a_compile_does_not_wedge_later_compiles(self):
        global _qapplication
        if QApplication.instance() is None:
            _qapplication = QApplication([])
        release = threading.Event()
        old, new = FakeChild(release), FakeChild()
        app = RunManager.__new__(RunManager)
        app.compiler_lock = threading.Lock()
        app.child_ready = threading.Event()
        app.child_ready.set()
        app.to_child, app.from_child, app.child = old.to_child, old.from_child, old
        app.output_box = types.SimpleNamespace(output=lambda *args, **kwargs: None, port=0)
        app.ui = types.SimpleNamespace(pushButton_restart_subprocess=QPushButton())
        spawn = mock.patch.object(
            main_module.process_tree,
            'subprocess',
            return_value=(new.to_child, new.from_child, new),
        )
        spawn.start()
        self.addCleanup(spawn.stop)

        threading.Thread(
            target=app.compile_run_file, args=('e.py', 'first.h5'), daemon=True
        ).start()
        wait_for(lambda: old.compiled)
        # Waiting on compiler_lock when the restart frees the first compile, so
        # it is the next thing sent to a child:
        threading.Thread(
            target=app.compile_run_file, args=('e.py', 'later.h5'), daemon=True
        ).start()
        app.on_restart_subprocess_clicked()
        release.set()

        wait_for(lambda: new.compiled, timeout=5)
        self.assertEqual(old.compiled, ['first.h5'])
        self.assertEqual(new.compiled, ['later.h5'])


if __name__ == '__main__':
    unittest.main()

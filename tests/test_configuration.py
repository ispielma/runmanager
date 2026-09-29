#####################################################################
#                                                                   #
# /tests/test_configuration.py                                      #
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
"""The shots waiting in the queue, in a saved configuration."""
import os
import tempfile
import types
import unittest

from qtutils import UiLoader
from qtutils.qt.QtGui import QStandardItemModel
from qtutils.qt.QtWidgets import QApplication

import runmanager
# fixtures stubs the splash and does the guarded import of the application.
from fixtures import FingerTabWidget, RunManager, TreeView
from runmanager.queueing import QueueController, QueueManager

_qapplication = None


class QueueInConfigurationTests(unittest.TestCase):
    def setUp(self):
        global _qapplication
        if QApplication.instance() is None:
            # Held for the life of the process: a QApplication that is garbage
            # collected takes every widget built under it down with it.
            _qapplication = QApplication([])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def session(self, *shots):
        """Runmanager without its startup, with these shots in its queue."""
        loader = UiLoader()
        loader.registerCustomWidget(FingerTabWidget)
        loader.registerCustomWidget(TreeView)
        app = RunManager.__new__(RunManager)
        app.ui = loader.load(
            os.path.join(os.path.dirname(runmanager.__file__), 'main.ui')
        )
        app.init_config_window_title()
        app.groups_model = QStandardItemModel()
        app.axes_model = QStandardItemModel()
        app.currently_open_groups = {}
        app.analysis_submission = types.SimpleNamespace(
            get_configuration_data=dict, restore_configuration_data=lambda data: None
        )
        app.queue_controller = QueueController()
        app.queue_manager = QueueManager(
            app.queue_controller,
            lambda item, default_globals: None,
            lambda labscript_file, path: True,
            lambda path: None,
            lambda *args, **kwargs: None,
        )
        self.addCleanup(app.queue_manager.shutdown)
        app.setup_queue_tab()
        app.queue_manager.enqueue([{'path': shot, 'compiled': True} for shot in shots])
        return app

    def test_queued_shots_come_back_at_startup_and_a_later_load_leaves_them(self):
        a, b, c = (os.path.join(self.directory, f'{name}.h5') for name in 'abc')
        path = os.path.join(self.directory, 'runmanager.toml')
        closing = self.session(a, b)
        closing.queue_manager.set_paused(True)
        closing.save_configuration(path)

        starting = self.session()
        starting.load_configuration(path, at_startup=True)
        self.assertEqual(starting.queue_controller.get_queue_paths(), [a, b])

        running = self.session(c)
        running.load_configuration(path)
        self.assertEqual(running.queue_controller.get_queue_paths(), [c])
        self.assertTrue(
            running.queue_controller.get_queue_state()['paused'],
            'the queue settings in the configuration are still applied',
        )

"""The sequence a default shot belongs to."""
import datetime
import os
import shutil
import tempfile
import threading
import types
import unittest
from unittest import mock

# h5_lock must be imported before h5py is, by anything in the process, and it
# is what runmanager imports h5py through. Naming it here rather than relying
# on runmanager below, so that this file can be run on its own.
import labscript_utils.h5_lock  # noqa: F401
import h5py
import runmanager
import runmanager.globals_file as globals_file
from fixtures import RunManager, labconfig
from runmanager.queueing import QueueController, QueueManager


class DefaultSequenceTests(unittest.TestCase):
    """All of a day's default shots are one sequence, claiming no index."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.labscript_file = os.path.join(self.directory, 'experiment.py')
        self.globals_file = os.path.join(self.directory, 'globals.toml')
        globals_file.new_globals_file(self.globals_file)
        runmanager.new_group(self.globals_file, 'group')

    def at(self, hour):
        """Runmanager's clock, reading this hour of 23 September 2026."""
        clock = types.SimpleNamespace(
            now=lambda: datetime.datetime(2026, 9, 23, hour, 1, 2)
        )
        return mock.patch.object(
            runmanager, 'datetime', types.SimpleNamespace(datetime=clock)
        )

    def test_every_default_shot_of_a_day_is_in_one_sequence(self):
        with self.at(8):
            morning, _, _ = runmanager.new_sequence_details(
                self.labscript_file, config=labconfig(self.directory), default=True
            )
        with self.at(21):
            evening, _, _ = runmanager.new_sequence_details(
                self.labscript_file, config=labconfig(self.directory), default=True
            )

        self.assertEqual(morning['sequence_id'], evening['sequence_id'])
        self.assertEqual(morning['sequence_index'], -1)
        next_index = runmanager.next_sequence_index(
            os.path.join(self.directory, 'experiment'),
            datetime.datetime(2026, 9, 23),
            increment=False,
        )
        self.assertEqual(next_index, 0, 'and no sequence index was claimed')

    def default_shot(self, app, hour=8):
        """The queue row of one default shot this app produces at this hour."""
        app._default_shot_ready = None
        app._default_shot_preparing = True
        with self.at(hour):
            app.prepare_default_shot(self.labscript_file, False)
        self.assertIsNotNone(app._default_shot_ready, app.said)
        return app._default_shot_ready

    def default_shot_app(self, **runmanager_settings):
        """A runmanager just started, over the globals file in the directory."""
        app = RunManager.__new__(RunManager)
        app.exp_config = labconfig(self.directory, **runmanager_settings)
        app.sequences = {}
        app.said = []
        app._default_shot_lock = threading.Lock()
        app.get_active_groups = lambda interactive=True: {'group': self.globals_file}
        app.output_box = types.SimpleNamespace(
            output=lambda text, red=False: app.said.append(text)
        )
        app.queue_manager = QueueManager(
            QueueController(),
            app.prepare_queue_shot,
            lambda labscript_file, run_file: (True, ''),
            lambda run_file: None,
            app.output_box.output,
        )
        self.addCleanup(app.queue_manager.shutdown)
        return app

    def test_default_shots_named_by_a_global_are_numbered_through_a_restart(self):
        # Named after a global, the day's default files differ in more than
        # their number, and a restart forgets the count; the run number is
        # still the one sequence's, and the queue row carries it.
        runmanager.new_global(self.globals_file, 'group', 'x')
        prefix = '{globals[x]}_{script_basename}'
        rows = []
        app = self.default_shot_app(filename_prefix_format=prefix)
        for x, restart in (('1', False), ('2', False), ('1', True)):
            globals_file.set_field(self.globals_file, 'group', 'x', 'default', x)
            if restart:
                app = self.default_shot_app(filename_prefix_format=prefix)
            rows.append(self.default_shot(app))

        self.assertEqual([row['run_no'] for row in rows], [0, 1, 2])
        for row in rows:
            with h5py.File(row['path'], 'r') as shot:
                self.assertEqual(row['run_no'], shot.attrs['run number'])
                for name, value in row['sequence_attrs'].items():
                    self.assertEqual(value, shot.attrs[name])

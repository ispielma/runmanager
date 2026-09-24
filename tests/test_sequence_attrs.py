"""What identifies the sequence a shot belongs to.

Every shot file carries the attributes saying which sequence it is part of,
and runmanager reads them back out of one when a later batch is added to that
sequence. The two ends have to agree on which attributes those are: a name
written but not read is dropped from the added shots, and a name read but not
written raises the first time a sequence is extended.
"""
import datetime
import os
import shutil
import tempfile
import threading
import types
import unittest
from unittest import mock

from labscript_utils.labconfig import LabConfig
# h5_lock must be imported before h5py is, by anything in the process, and it
# is what runmanager imports h5py through. Naming it here rather than relying
# on runmanager below, so that this file can be run on its own.
import labscript_utils.h5_lock  # noqa: F401
import h5py
import runmanager
from fixtures import RunManager


class FakeConfig(object):
    """A labconfig carrying only the setting new_sequence_details must have.

    Everything else it asks for has a built-in fallback, reached by raising
    what a real LabConfig raises for a setting that is not there.
    """

    def __init__(self, shot_storage, filename_prefix_format=None):
        self.shot_storage = shot_storage
        self.filename_prefix_format = filename_prefix_format

    def get(self, section, option, *args, **kwargs):
        if (section, option) == ('default', 'experiment_shot_storage'):
            return self.shot_storage
        if (section, option) == ('runmanager', 'filename_prefix_format'):
            if self.filename_prefix_format is not None:
                return self.filename_prefix_format
        raise LabConfig.NoOptionError(option, section)


def sequence_attrs(**overrides):
    attrs = {
        'script_basename': 'experiment',
        'sequence_date': '2026-09-18',
        'sequence_index': 7,
        'sequence_id': '20260918T101112_experiment',
    }
    attrs.update(overrides)
    return attrs


class SequenceAttrsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_a_new_sequence_is_described_by_exactly_the_named_attributes(self):
        # next_sequence_index takes a zlock and keeps a counter on disk. Which
        # index it hands out does not matter here; that it is one of the
        # attributes describing the sequence does.
        with mock.patch.object(runmanager, 'next_sequence_index', lambda *a, **k: 7):
            attrs, _, _ = runmanager.new_sequence_details(
                os.path.join(self.directory, 'experiment.py'),
                config=FakeConfig(self.directory),
            )

        self.assertEqual(
            set(attrs),
            set(runmanager.SEQUENCE_ATTRS),
            'SEQUENCE_ATTRS is what a shot is asked for when a sequence is '
            'extended, so it has to be what new_sequence_details produces',
        )

    def test_a_shot_file_answers_with_the_sequence_it_was_written_with(self):
        attrs = sequence_attrs()
        path = os.path.join(self.directory, 'experiment_00.h5')
        runmanager.make_single_run_file(path, None, {}, attrs, 0, 1)

        self.assertEqual(
            runmanager.get_sequence_attrs(path),
            attrs,
            'a shot added to this sequence is written with what is read here, '
            'so anything lost on the way through lands in the added shots',
        )

    def test_a_shot_file_answers_with_the_values_it_was_written_with(self):
        # Equal values are not the same values. h5py answers with numpy
        # scalars, and np.int64(7) == 7 while being something a TOML app
        # config cannot hold -- so a queue holding a sequence read back from a
        # shot file is a queue that cannot be saved. Types are asserted here
        # because equality cannot see the difference.
        attrs = sequence_attrs()
        path = os.path.join(self.directory, 'experiment_01.h5')
        runmanager.make_single_run_file(path, None, {}, attrs, 0, 1)

        read = runmanager.get_sequence_attrs(path)

        self.assertEqual(
            {name: type(value) for name, value in read.items()},
            {name: type(value) for name, value in attrs.items()},
            'what is read here is written into the shots added to this '
            'sequence and kept in their queue records, so it has to be the '
            'plain values that were written and not stand-ins for them',
        )


class DefaultSequenceTests(unittest.TestCase):
    """All of a day's default shots are one sequence, claiming no index."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.labscript_file = os.path.join(self.directory, 'experiment.py')
        self.globals_file = os.path.join(self.directory, 'globals.toml')
        runmanager.new_globals_file(self.globals_file)
        runmanager.new_group(self.globals_file, 'group')
        self.claims = []
        patcher = mock.patch.object(
            runmanager,
            'next_sequence_index',
            lambda *args, **kwargs: self.claims.append(args) or 7,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
                self.labscript_file, config=FakeConfig(self.directory), default=True
            )
        with self.at(21):
            evening, _, _ = runmanager.new_sequence_details(
                self.labscript_file, config=FakeConfig(self.directory), default=True
            )

        self.assertEqual(morning['sequence_id'], evening['sequence_id'])
        self.assertEqual(morning['sequence_index'], -1)
        self.assertEqual(self.claims, [], 'and no sequence index was claimed')

    def default_shot(self, app, hour=8):
        """The queue row of one default shot this app produces at this hour."""
        app._default_shot_ready = None
        app._default_shot_preparing = True
        with self.at(hour):
            RunManager.prepare_default_shot(app, self.labscript_file, False)
        self.assertIsNotNone(app._default_shot_ready, app.said)
        return app._default_shot_ready

    def default_shot_app(self, filename_prefix_format=None):
        """A runmanager just started, over the globals file in the directory."""
        said = []
        return types.SimpleNamespace(
            exp_config=FakeConfig(self.directory, filename_prefix_format),
            sequences={},
            said=said,
            _default_shot_lock=threading.Lock(),
            get_active_groups=lambda interactive=True: {'group': self.globals_file},
            compile_run_file=lambda labscript_file, run_file: True,
            send_to_runviewer=lambda run_file: None,
            output_box=types.SimpleNamespace(
                output=lambda text, red=False: said.append(text)
            ),
        )

    def test_each_default_shot_takes_the_next_run_number_of_that_sequence(self):
        # Every one of them run 0 would be one run number for many shots of
        # one sequence.
        app = self.default_shot_app()
        runs = []
        for hour in (8, 21):
            with h5py.File(self.default_shot(app, hour)['path'], 'r') as shot:
                runs.append((shot.attrs['run number'], shot.attrs['n_runs']))

        self.assertEqual(runs, [(0, 1), (1, 2)])

    def test_default_shots_named_by_a_global_are_numbered_through_a_restart(self):
        # Named after a global, the day's default files differ in more than
        # their number, and a restart forgets the count; the run number is
        # still the one sequence's, and the queue row carries it.
        runmanager.new_global(self.globals_file, 'group', 'x')
        rows = []
        app = self.default_shot_app('{globals[x]}_{script_basename}')
        for x, restart in (('1', False), ('2', False), ('1', True)):
            runmanager.set_value(self.globals_file, 'group', 'x', x)
            if restart:
                app = self.default_shot_app('{globals[x]}_{script_basename}')
            rows.append(self.default_shot(app))

        self.assertEqual([row['run_no'] for row in rows], [0, 1, 2])
        for row in rows:
            with h5py.File(row['path'], 'r') as shot:
                self.assertEqual(row['run_no'], shot.attrs['run number'])
            self.assertEqual(
                row['sequence_attrs'], runmanager.get_sequence_attrs(row['path'])
            )

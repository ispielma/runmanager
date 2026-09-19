"""What identifies the sequence a shot belongs to.

Every shot file carries the attributes saying which sequence it is part of,
and runmanager reads them back out of one when a later batch is added to that
sequence. The two ends have to agree on which attributes those are: a name
written but not read is dropped from the added shots, and a name read but not
written raises the first time a sequence is extended.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from labscript_utils.labconfig import LabConfig
# h5_lock must be imported before h5py is, by anything in the process, and it
# is what runmanager imports h5py through. Naming it here rather than relying
# on runmanager below, so that this file can be run on its own.
import labscript_utils.h5_lock  # noqa: F401
import runmanager


class FakeConfig(object):
    """A labconfig carrying only the setting new_sequence_details must have.

    Everything else it asks for has a built-in fallback, reached by raising
    what a real LabConfig raises for a setting that is not there.
    """

    def __init__(self, shot_storage):
        self.shot_storage = shot_storage

    def get(self, section, option, *args, **kwargs):
        if (section, option) == ('default', 'experiment_shot_storage'):
            return self.shot_storage
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

#####################################################################
#                                                                   #
# /tests/test_legacy_globals_file.py                                #
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
import os
import shutil
import tempfile
import types
import unittest

import labscript_utils.h5_lock, h5py

import runmanager
# fixtures stubs the splash and does the guarded import of the application.
from fixtures import RunManager


class LegacyGlobalsFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.path = os.path.join(directory, 'globals.h5')

    def test_opening_a_legacy_file_with_a_list_global_stores_nothing(self):
        # Preparsing guesses an expansion for every list-valued global, and a
        # legacy HDF5 file is read-only until the user converts it, so opening
        # one must not try to store a guess in it.
        with h5py.File(self.path, 'w') as f:
            group = f.create_group('globals/scans')
            group.attrs['x'] = '[1, 2, 3]'
            group.create_group('units').attrs['x'] = ''
        preparse_state = types.SimpleNamespace(
            previous_evaled_globals={},
            previous_global_hierarchy={},
            previous_expansion_types={},
            previous_expansions={},
        )

        changed = RunManager.guess_expansion_modes(
            preparse_state,
            {'scans': self.path},
            {'scans': {'x': [1, 2, 3]}},
            {},
            {'x': ''},
        )

        self.assertFalse(changed)

    def test_a_global_the_file_expands_arrives_as_a_scan(self):
        # An expansion is what made a global a scan when the file was written,
        # so the file keeps its scans and their shot count. A group written
        # before expansions were stored has no scans.
        with h5py.File(self.path, 'w') as f:
            group = f.create_group('globals/scans')
            units = group.create_group('units')
            expansions = group.create_group('expansion')
            for name, value, expansion in [
                ('x', '[1, 2, 3]', 'outer'),
                ('y', '[4, 5]', 'pair'),
                ('w', '6', ''),
            ]:
                group.attrs[name] = value
                units.attrs[name] = ''
                expansions.attrs[name] = expansion
            f.create_group('globals/older').attrs['v'] = '[7, 8]'

        groups = {'scans': self.path, 'older': self.path}
        self.assertEqual(
            runmanager.get_globals(groups),
            {
                'scans': {
                    'x': ('[1, 2, 3]', '', 'outer'),
                    'y': ('[4, 5]', '', 'pair'),
                    'w': ('6', '', ''),
                },
                'older': {'v': ('[7, 8]', '', '')},
            },
        )

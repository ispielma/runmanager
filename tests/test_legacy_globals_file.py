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

# fixtures stubs the splash and does the guarded import of the application.
from fixtures import RunManager


class LegacyGlobalsFileTests(unittest.TestCase):
    def test_opening_a_legacy_file_with_a_list_global_stores_nothing(self):
        # Preparsing guesses an expansion for every list-valued global, and a
        # legacy HDF5 file is read-only until the user converts it, so opening
        # one must not try to store a guess in it.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, 'globals.h5')
        with h5py.File(path, 'w') as f:
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
            preparse_state, {'scans': path}, {'scans': {'x': [1, 2, 3]}}, {}, {'x': ''}
        )

        self.assertFalse(changed)

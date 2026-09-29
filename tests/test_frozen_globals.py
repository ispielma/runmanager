#####################################################################
#                                                                   #
# /tests/test_frozen_globals.py                                     #
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
import unittest

import numpy as np

import runmanager


class FrozenGlobalsTests(unittest.TestCase):
    def test_a_scanned_value_compiles_back_to_exactly_what_was_frozen(self):
        # A queued shot is compiled from the text its scanned values were
        # frozen as, but named for the values themselves, so the text has to
        # give back every digit and every element.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, 'globals.toml')
        runmanager.new_globals_file(path)
        runmanager.new_group(path, 'group')
        runmanager.new_global(path, 'group', 'x')
        runmanager.set_scan(
            path, 'group', 'x', '[array([1.23456789012345, 2.]), arange(2000) * 0.1]'
        )
        runmanager.set_scan_enabled(path, 'group', 'x', True)
        groups = {'group': path}
        details = runmanager.get_globals_details(groups)
        sequence_globals = runmanager.get_globals(groups)
        evaled, _, _ = runmanager.evaluate_globals(sequence_globals)

        for shot in runmanager.expand_globals(sequence_globals, evaled):
            frozen = runmanager.get_frozen_globals(details, shot)
            _, compiled = runmanager.get_queue_compile_globals(groups, frozen)
            np.testing.assert_array_equal(compiled['x'], shot['x'])

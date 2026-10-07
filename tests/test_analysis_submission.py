#####################################################################
#                                                                   #
# /tests/test_analysis_submission.py                                #
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
"""Shots runmanager sends to lyse, through the real client to a real lyse server.

lyse's application is a stand-in holding the queue its server puts shots on,
because lyse's startup builds the whole window.
"""
import queue
import socket
import tempfile
import types
import unittest
from pathlib import Path

from qtutils.qt import QtWidgets

from fixtures import serve_lyse, submit_to_lyse, wait_for


class RefusingQueue:
    def __init__(self):
        self.offered = []

    def put(self, path):
        self.offered.append(path)
        raise RuntimeError('lyse cannot take shots')


_qapplication = None


class AnalysisSubmissionTests(unittest.TestCase):
    def setUp(self):
        global _qapplication
        if QtWidgets.QApplication.instance() is None:
            # Held for the life of the process: a QApplication that is garbage
            # collected takes every widget built under it down with it.
            _qapplication = QtWidgets.QApplication([])
        self.path = str(Path(tempfile.gettempdir(), 'shot.h5'))
        self.lyse_app = types.SimpleNamespace(filebox=types.SimpleNamespace())

    def serve(self, incoming_queue, port=None):
        self.lyse_app.filebox.incoming_queue = incoming_queue
        return serve_lyse(self, self.lyse_app, port)

    def test_a_submitted_shot_reaches_lyse_as_its_local_path(self):
        incoming = queue.Queue()
        submission = submit_to_lyse(self, self.serve(incoming).port)
        submission.notify_shot_complete(self.path)
        wait_for(lambda: not incoming.empty())

        self.assertEqual(incoming.get(), self.path)

    def test_a_shot_lyse_refuses_is_dropped_not_retried(self):
        refusing = RefusingQueue()
        submission = submit_to_lyse(self, self.serve(refusing).port)
        submission.notify_shot_complete(self.path)
        wait_for(lambda: refusing.offered)
        # Once lyse takes shots again, one still waiting would be sent first:
        incoming = queue.Queue()
        self.lyse_app.filebox.incoming_queue = incoming
        later = str(Path(tempfile.gettempdir(), 'later.h5'))
        submission.notify_shot_complete(later)
        wait_for(lambda: not incoming.empty())

        self.assertEqual(incoming.get(), later)
        self.assertEqual(refusing.offered, [self.path])

    def submit_recording(self, port):
        """A submission to this port, and the outcomes it reports, in order."""
        outcomes = []
        submission = submit_to_lyse(self, port, lambda *outcome: outcomes.append(outcome))
        return submission, outcomes

    def test_each_shot_file_is_reported_as_sent_rejected_or_not_sent(self):
        with self.subTest('lyse takes it'):
            submission, outcomes = self.submit_recording(self.serve(queue.Queue()).port)
            submission.notify_shot_complete(self.path)
            wait_for(lambda: outcomes)

            self.assertEqual(outcomes, [(self.path, 'sent', '')])

        with self.subTest('lyse refuses it'):
            submission, outcomes = self.submit_recording(self.serve(RefusingQueue()).port)
            submission.notify_shot_complete(self.path)
            wait_for(lambda: outcomes)

            self.assertEqual([outcome[:2] for outcome in outcomes], [(self.path, 'rejected')])
            self.assertIn('lyse cannot take shots', outcomes[0][2])

        with self.subTest('Analyse is off'):
            submission, outcomes = self.submit_recording(self.serve(queue.Queue()).port)
            submission.send_to_server = False
            submission.notify_shot_complete(self.path)
            wait_for(lambda: outcomes)

            self.assertEqual(outcomes, [(self.path, 'not sent', 'Analysis is off.')])

        with self.subTest('cleared while lyse is away'):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            submission, outcomes = self.submit_recording(port)
            submission.notify_shot_complete(self.path)
            wait_for(lambda: submission.server_online == 'offline')
            self.assertEqual(outcomes, [], 'a connection failure is retried, not reported')
            submission.clear_waiting_files()
            wait_for(lambda: outcomes)

            self.assertEqual(
                outcomes,
                [(self.path, 'not sent', 'Cleared before it was sent to lyse.')],
            )

    def test_a_shot_waits_while_lyse_is_away_and_goes_once_it_is_back(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        submission = submit_to_lyse(self, port)
        submission.notify_shot_complete(self.path)
        wait_for(lambda: submission.server_online == 'offline')
        incoming = queue.Queue()
        self.serve(incoming, port)
        submission.check_retry()
        wait_for(lambda: not incoming.empty())

        self.assertEqual(incoming.get(), self.path)

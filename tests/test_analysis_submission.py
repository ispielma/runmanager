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
import threading
import time
import types
import unittest
from pathlib import Path

from lyse.client import LyseClient
from lyse.communication import LyseServer
from qtutils.qt import QtWidgets

from runmanager.analysis_submission import AnalysisSubmission


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
        server = LyseServer(self.lyse_app, port=port, bind_address='tcp://127.0.0.1')
        self.addCleanup(server.shutdown)
        return server

    def submission(self, port):
        submission = AnalysisSubmission()
        self.addCleanup(self.stop, submission)
        submission.lyse = LyseClient(host='127.0.0.1', port=port, timeout=1)
        submission.send_to_server = True
        return submission

    def stop(self, submission):
        # shutdown joins the submission thread, which may be waiting on the main
        # thread, so it is joined from another while events are processed:
        stopping = threading.Thread(target=submission.shutdown)
        stopping.start()
        self.wait_for(lambda: not stopping.is_alive())

    def wait_for(self, condition):
        # The submission thread sets its status in the main thread, so events
        # are processed while waiting:
        deadline = time.monotonic() + 10
        while not condition():
            self.assertLess(time.monotonic(), deadline)
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)

    def test_a_submitted_shot_reaches_lyse_as_its_local_path(self):
        incoming = queue.Queue()
        submission = self.submission(self.serve(incoming).port)
        submission.notify_shot_complete(self.path)
        self.wait_for(lambda: not incoming.empty())

        self.assertEqual(incoming.get(), self.path)

    def test_a_shot_lyse_refuses_is_dropped_not_retried(self):
        refusing = RefusingQueue()
        submission = self.submission(self.serve(refusing).port)
        submission.notify_shot_complete(self.path)
        self.wait_for(lambda: refusing.offered)
        # Once lyse takes shots again, one still waiting would be sent first:
        incoming = queue.Queue()
        self.lyse_app.filebox.incoming_queue = incoming
        later = str(Path(tempfile.gettempdir(), 'later.h5'))
        submission.notify_shot_complete(later)
        self.wait_for(lambda: not incoming.empty())

        self.assertEqual(incoming.get(), later)
        self.assertEqual(refusing.offered, [self.path])

    def test_a_shot_waits_while_lyse_is_away_and_goes_once_it_is_back(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        submission = self.submission(port)
        submission.notify_shot_complete(self.path)
        self.wait_for(lambda: submission.server_online == 'offline')
        incoming = queue.Queue()
        self.serve(incoming, port)
        submission.check_retry()
        self.wait_for(lambda: not incoming.empty())

        self.assertEqual(incoming.get(), self.path)

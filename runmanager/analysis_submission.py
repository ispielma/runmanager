#####################################################################
#                                                                   #
# analysis_submission.py                                            #
#                                                                   #
# Copyright 2026, Monash University                                 #
#                                                                   #
# This file is part of the program runmanager, in the labscript     #
# suite (see http://labscriptsuite.org), and is licensed under the  #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""Lyse submission widget and retry loop for runmanager."""

import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

from qtutils import UiLoader, inmain_decorator
from qtutils.qt import QtGui
from qtutils.qt.QtCore import Qt, QSize
from qtutils.qt.QtWidgets import QSizePolicy
from zprocess import TimeoutError, raise_exception_in_thread
from zprocess.security import AuthenticationFailure

from labscript_utils.ls_zprocess import zmq_get
import labscript_utils.shared_drive
from labscript_utils.qtwidgets.elide_label import elide_label


runmanager_dir = Path(__file__).absolute().parent


class AnalysisSubmission(object):
    def __init__(self, runmanager, parent_layout=None):
        self.inqueue = queue.Queue()
        self.runmanager = runmanager
        self.port = self.runmanager.exp_config.getint('ports', 'lyse')

        self.widget = UiLoader().load(
            os.path.join(runmanager_dir, 'analysis_submission.ui')
        )
        if parent_layout is not None:
            try:
                parent_layout.insertWidget(parent_layout.count(), self.widget)
            except AttributeError:
                parent_layout.addWidget(self.widget)
        self.widget.frame.setSizePolicy(
            QSizePolicy.MinimumExpanding, QSizePolicy.Preferred
        )
        elide_label(
            self.widget.resend_shots_label,
            self.widget.failed_to_send_frame.layout(),
            Qt.ElideRight,
        )

        self.widget.send_to_server.toggled.connect(self._set_send_to_server)
        self.widget.server.editingFinished.connect(
            lambda: self._set_server(self.widget.server.text())
        )
        self.widget.clear_unsent_shots_button.clicked.connect(
            lambda _=False: self.clear_waiting_files()
        )
        self.widget.retry_button.clicked.connect(lambda _=False: self.check_retry())

        self._waiting_for_submission = []
        self.failure_reason = None
        self.time_of_last_connectivity_check = 0
        self._shutdown = False
        self.server = ''
        self.send_to_server = False
        self.server_online = ''

        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def get_configuration_data(self):
        return {'server': self.server, 'send_to_server': self.send_to_server}

    def restore_configuration_data(self, data):
        data = data or {}
        self.server = data.get('server', '')
        self._apply_send_to_server(
            data.get('send_to_server', False), clear_waiting=False
        )

    def _set_send_to_server(self, value):
        self.send_to_server = value

    def _set_server(self, server):
        self.server = server
        self.check_retry()

    @property
    @inmain_decorator(True)
    def send_to_server(self):
        return self._send_to_server

    @send_to_server.setter
    def send_to_server(self, value):
        self._apply_send_to_server(value, clear_waiting=True)

    @inmain_decorator(True)
    def _apply_send_to_server(self, value, clear_waiting):
        self._send_to_server = bool(value)
        self.widget.send_to_server.setChecked(self.send_to_server)
        if self.send_to_server:
            self.widget.server.setEnabled(True)
            self.widget.server_online.show()
            self.check_retry()
        else:
            if clear_waiting:
                self.clear_waiting_files()
            else:
                self.widget.failed_to_send_frame.hide()
            self.widget.server.setEnabled(False)
            self.widget.server_online.hide()

    @property
    @inmain_decorator(True)
    def server(self):
        return str(self._server)

    @server.setter
    @inmain_decorator(True)
    def server(self, value):
        self._server = value
        self.widget.server.setText(self.server)

    @property
    @inmain_decorator(True)
    def server_online(self):
        return self._server_online

    @server_online.setter
    @inmain_decorator(True)
    def server_online(self, value):
        self._server_online = str(value)

        icon_names = {
            'checking': ':/qtutils/fugue/hourglass',
            'online': ':/qtutils/fugue/tick',
            'offline': ':/qtutils/fugue/exclamation',
            '': ':/qtutils/fugue/status-offline',
        }
        tooltips = {
            'checking': 'Checking...',
            'online': 'Server is responding',
            'offline': 'Server not responding',
            '': 'Disabled',
        }

        icon = QtGui.QIcon(icon_names.get(self._server_online, ':/qtutils/fugue/exclamation-red'))
        pixmap = icon.pixmap(QSize(16, 16))
        tooltip = tooltips.get(
            self._server_online,
            'Invalid server status: %s' % self._server_online,
        )
        if self.failure_reason is not None:
            tooltip += '\n' + self.failure_reason

        self.widget.server_online.setPixmap(pixmap)
        self.widget.server_online.setToolTip(tooltip)
        self.update_waiting_files_message()

    @inmain_decorator(True)
    def update_waiting_files_message(self):
        if (
            self.server_online == 'checking'
            and len(self._waiting_for_submission) == 1
            and not self.widget.failed_to_send_frame.isVisible()
        ):
            return
        if self._waiting_for_submission:
            self.widget.failed_to_send_frame.show()
            if self.server_online == 'checking':
                self.widget.retry_button.hide()
                text = 'Sending %s shot(s)...' % len(self._waiting_for_submission)
            else:
                self.widget.retry_button.show()
                text = '%s shot(s) to send' % len(self._waiting_for_submission)
            self.widget.resend_shots_label.setText(text)
        else:
            self.widget.failed_to_send_frame.hide()

    @inmain_decorator(True)
    def clear_waiting_files(self):
        self._waiting_for_submission = []
        self.update_waiting_files_message()

    @inmain_decorator(True)
    def check_retry(self):
        self.inqueue.put(['check/retry', None])

    def notify_shot_complete(self, filepath):
        if not filepath:
            return
        filepath = labscript_utils.shared_drive.path_to_local(str(filepath))
        self.inqueue.put(['file', filepath])
        return 'queued'

    def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self.inqueue.put(['close', None])
        if (
            self.mainloop_thread.is_alive()
            and threading.current_thread() is not self.mainloop_thread
        ):
            self.mainloop_thread.join(timeout=1)

    def mainloop(self):
        self._mainloop_logger = logging.getLogger('runmanager.AnalysisSubmission.mainloop')
        timeout = 10
        while True:
            try:
                try:
                    signal, data = self.inqueue.get(timeout=timeout)
                except queue.Empty:
                    timeout = 10
                    if (time.time() - self.time_of_last_connectivity_check) > 1:
                        signal = 'check/retry'
                    else:
                        continue
                if signal == 'check/retry':
                    self.check_connectivity()
                    if self.server_online == 'online':
                        self.submit_waiting_files()
                elif signal == 'file':
                    if self.send_to_server:
                        self._waiting_for_submission.append(data)
                        if self.server_online != 'online':
                            if (time.time() - self.time_of_last_connectivity_check) > 1:
                                self.check_connectivity()
                            else:
                                timeout = 1
                        if self.server_online == 'online':
                            self.submit_waiting_files()
                elif signal == 'close':
                    break
                else:
                    raise ValueError('Invalid signal: %s' % str(signal))

                self._mainloop_logger.info('Processed signal: %s' % str(signal))
            except Exception:
                raise_exception_in_thread(sys.exc_info())
                self._mainloop_logger.exception('Exception in mainloop, continuing')

    def check_connectivity(self):
        host = self.server
        send_to_server = self.send_to_server
        if host and send_to_server:
            self.server_online = 'checking'
            try:
                response = zmq_get(self.port, host, 'hello', timeout=1)
                self.failure_reason = None
            except (TimeoutError, OSError, AuthenticationFailure) as e:
                success = False
                self.failure_reason = str(e)
            else:
                success = response == 'hello'
                if not success:
                    self.failure_reason = 'unexpected response: %s' % str(response)

            self.server_online = 'online' if success else 'offline'
        else:
            self.server_online = ''

        self.time_of_last_connectivity_check = time.time()

    def submit_waiting_files(self):
        success = True
        while self._waiting_for_submission and success:
            path = self._waiting_for_submission[0]
            self._mainloop_logger.info('Submitting run file %s.\n' % os.path.basename(path))
            data = {'filepath': labscript_utils.shared_drive.path_to_agnostic(path)}
            self.server_online = 'checking'
            try:
                response = zmq_get(self.port, self.server, data, timeout=1)
                self.failure_reason = None
            except (TimeoutError, OSError, AuthenticationFailure) as e:
                success = False
                self.failure_reason = str(e)
            else:
                success = response == 'added successfully'
                if not success:
                    self.failure_reason = 'unexpected response: %s' % str(response)
                try:
                    self._waiting_for_submission.pop(0)
                except IndexError:
                    pass
            if not success:
                break

        self.server_online = 'online' if success else 'offline'
        self.time_of_last_connectivity_check = time.time()

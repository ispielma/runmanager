#####################################################################
#                                                                   #
# /runmanager/analysis_submission.py                                #
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
"""Send completed shots to lyse from runmanager's lyse row, retrying any it cannot send."""

import importlib.resources
import logging
import os
import queue
import sys
import threading
import time

import labscript_utils.shared_drive
from labscript_utils.qtwidgets.elide_label import elide_label
from labscript_utils.qtwidgets.link_indicator import LinkIndicator
from lyse.client import LyseClient
from qtutils import inmain_decorator
from qtutils.qt import QtGui
from qtutils.qt.QtCore import Qt
from zprocess import TimeoutError, raise_exception_in_thread
from zprocess.security import AuthenticationFailure


def set_icon_label_pixmap(label, icon_path, size=16):
    icon = QtGui.QIcon(str(icon_path))
    if icon.isNull():
        label.clear()
        return
    label.setPixmap(icon.pixmap(size, size))
    label.setMinimumSize(size, size)
    label.setMaximumSize(size + 4, size + 4)


class AnalysisSubmission(object):
    def __init__(self, ui):
        self.inqueue = queue.Queue()
        self.lyse = LyseClient(timeout=1)

        self.ui = ui
        set_icon_label_pixmap(
            self.ui.send_to_server_icon, importlib.resources.files('lyse') / 'lyse.svg'
        )
        self.lyse_link = LinkIndicator(
            'lyse', lambda: self.lyse.say_hello(timeout=1), host=self.lyse.host
        )
        self.ui.lyse_link_layout.addWidget(self.lyse_link)
        if self.lyse.host:
            self.lyse_link.start()
        else:
            self.lyse_link.show_disabled('No lyse host is configured')

        elide_label(
            self.ui.resend_shots_label,
            self.ui.failed_to_send_frame.layout(),
            Qt.ElideRight,
        )

        self.ui.send_to_server.toggled.connect(self._set_send_to_server)
        self.ui.clear_unsent_shots_button.clicked.connect(
            lambda _=False: self.clear_waiting_files()
        )
        self.ui.retry_button.clicked.connect(lambda _=False: self.check_retry())

        self._waiting_for_submission = []
        self.time_of_last_attempt = 0
        self._shutdown = False
        self._send_to_server = False
        self.server_online = ''
        self.send_to_server = False

        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def get_configuration_data(self):
        return {'send_to_server': self.send_to_server}

    def restore_configuration_data(self, data):
        data = data or {}
        self._apply_send_to_server(data.get('send_to_server', False), clear_waiting=False)

    def _set_send_to_server(self, value):
        self.send_to_server = value

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
        self.ui.send_to_server.setChecked(self.send_to_server)
        if self.send_to_server:
            self.check_retry()
        else:
            if clear_waiting:
                self.clear_waiting_files()
            else:
                self.ui.failed_to_send_frame.hide()

    @inmain_decorator(True)
    def update_waiting_files_message(self):
        if (
            self.server_online == 'checking'
            and len(self._waiting_for_submission) == 1
            and not self.ui.failed_to_send_frame.isVisible()
        ):
            return
        if self._waiting_for_submission:
            self.ui.failed_to_send_frame.show()
            if self.server_online == 'checking':
                self.ui.retry_button.hide()
                text = 'Sending %s shot(s)...' % len(self._waiting_for_submission)
            else:
                self.ui.retry_button.show()
                text = '%s shot(s) to send' % len(self._waiting_for_submission)
            self.ui.resend_shots_label.setText(text)
        else:
            self.ui.failed_to_send_frame.hide()

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
        self.lyse_link.shutdown()
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
                    if (time.time() - self.time_of_last_attempt) > 1:
                        signal = 'check/retry'
                    else:
                        continue

                if signal == 'check/retry':
                    if self.send_to_server:
                        self.submit_waiting_files()
                elif signal == 'file':
                    if self.send_to_server:
                        self._waiting_for_submission.append(data)
                        if (
                            self.server_online == 'offline'
                            and time.time() - self.time_of_last_attempt <= 1
                        ):
                            timeout = 1
                        else:
                            self.submit_waiting_files()
                elif signal == 'close':
                    break
                else:
                    raise ValueError('Invalid signal: %s' % str(signal))

                self._mainloop_logger.debug('Processed signal: %s' % str(signal))
            except Exception:
                raise_exception_in_thread(sys.exc_info())
                self._mainloop_logger.exception('Exception in mainloop, continuing')

    def submit_waiting_files(self):
        success = True
        while self._waiting_for_submission and success:
            path = self._waiting_for_submission[0]
            self._mainloop_logger.debug('Submitting run file %s.\n' % os.path.basename(path))
            self.server_online = 'checking'
            self.update_waiting_files_message()
            try:
                self.lyse.add_shot(labscript_utils.shared_drive.path_to_agnostic(path))
                self.lyse_link.show_state(None)
            except (TimeoutError, OSError, AuthenticationFailure):
                success = False
            except Exception as e:
                # lyse answered and refused the shot, which a retry would not change:
                self._mainloop_logger.exception('lyse refused %s', path)
                self.lyse_link.show_state(None, [str(e)])
            if not success:
                break
            try:
                self._waiting_for_submission.pop(0)
            except IndexError:
                pass

        self.server_online = 'online' if success else 'offline'
        self.update_waiting_files_message()
        self.time_of_last_attempt = time.time()

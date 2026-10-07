"""What runmanager's server answers a remote caller.

These are the commands a plugin or an optimizer sends. BLACS's side of the
handover, queue_exchange, is tested in blacs.

Each is sent by the real RunmanagerClient to a real RunmanagerServer on a free
port, so a command reaches its handler under the name the client sends it by,
and an exception the handler returns is raised by the client, as for any caller.
"""
import datetime
import os
import queue
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import labscript_utils.h5_lock, h5py
from labscript_utils.ls_zprocess import ZMQServer
from qtutils.qt.QtCore import Qt
from qtutils.qt.QtGui import QStandardItemModel
from qtutils.qt.QtWidgets import QApplication
import runmanager
import runmanager.globals_file as globals_file
from runmanager.client import (
    SUBMISSION_MODE_LAST_SEQUENCE_CLEAR_QUEUE,
    RunmanagerClient,
    SequenceRefused,
)
# fixtures stubs the splash and does the guarded import of the
# application, once, for every test module. Importing
# runmanager.__main__ here instead would show the startup banner.
from fixtures import (
    RunManager,
    RunmanagerServer,
    labconfig,
    main_module,
    wait_for,
)
from runmanager.queueing import (
    BLOCKED_SHOT_STATE,
    COMPILE_MODE_EAGER,
    QUEUED_SHOT_STATE,
    QueueController,
    QueueManager,
)


class FakeApp(object):
    """The application over a real QueueManager, which is all these commands read."""

    def __init__(self):
        self.queue_controller = QueueController()
        self.queue_manager = QueueManager(
            self.queue_controller,
            lambda item, default_globals: None,
            lambda labscript_file, path: (True, ''),
            lambda path: None,
            lambda *args, **kwargs: None,
        )


class RemoteCommandTestCase(unittest.TestCase):
    """A server with an application behind it, for the length of one test."""

    def make_app(self):
        """The application the server under test reaches through."""
        return FakeApp()

    def setUp(self):
        global _qapplication
        if QApplication.instance() is None:
            # Held for the life of the process: a QApplication that is garbage
            # collected takes every widget built under it down with it.
            _qapplication = QApplication([])
        self.app = self.make_app()
        self.addCleanup(self.app.queue_manager.shutdown)
        # RunmanagerServer's handlers reach the application through this module
        # global, which the application assigns to itself on startup. Nothing
        # starts here, so the test puts it there and takes it away again.
        patcher = mock.patch.object(main_module, 'app', self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Only the port RunmanagerServer's __init__ reads from the labconfig is
        # skipped: the real server binds a free one, on loopback.
        server = RunmanagerServer.__new__(RunmanagerServer)
        ZMQServer.__init__(server, bind_address='tcp://127.0.0.1')
        self.addCleanup(server.shutdown)
        self.client = RunmanagerClient(host='127.0.0.1', port=server.port, timeout=10)

    def request(self, method, *args, **kwargs):
        # Most handlers hop to the main thread, as the running server's do, so
        # the client asks from another thread while this one processes events:
        answer = {}

        def ask():
            try:
                answer['value'] = method(*args, **kwargs)
            except Exception as exc:
                answer['error'] = exc

        asker = threading.Thread(target=ask, daemon=True)
        asker.start()
        while asker.is_alive():
            QApplication.processEvents()
            asker.join(0.005)
        if 'error' in answer:
            raise answer['error']
        return answer['value']


_qapplication = None


class SubmittingApp(object):
    """The application over the whole path a submission takes.

    Globals files, shot naming and numbering, the sequence a batch joins and the
    queue are runmanager's own. The window is stood in for: the group tabs, and
    the preparse thread and what it writes. The compile callback blocks until
    the test releases it, which is where a shot waits while it compiles.
    """

    AXES_COL_NAME = RunManager.AXES_COL_NAME
    AXES_COL_SHUFFLE = RunManager.AXES_COL_SHUFFLE
    AXES_ROLE_NAME = RunManager.AXES_ROLE_NAME

    get_queue_append_filepath = RunManager.get_queue_append_filepath
    get_last_sent_from_queue_filepath = RunManager.get_last_sent_from_queue_filepath
    get_submission_anchor = RunManager.get_submission_anchor
    get_sequence_attrs_to_extend = RunManager.get_sequence_attrs_to_extend
    compile_and_queue_shots = RunManager.compile_and_queue_shots
    reindex_run_file_infos = RunManager.reindex_run_file_infos
    make_h5_files = RunManager.make_h5_files
    prepare_queue_shot = RunManager.prepare_queue_shot
    on_abort_clicked = RunManager.on_abort_clicked
    on_engage_clicked = RunManager.on_engage_clicked
    engage = RunManager.engage
    expand_pending_shots = RunManager.expand_pending_shots
    test_compile = RunManager.test_compile
    parse_globals = RunManager.parse_globals
    add_item_to_axes_model = RunManager.add_item_to_axes_model
    update_axes_indentation = RunManager.update_axes_indentation
    update_axes_tab = RunManager.update_axes_tab

    def __init__(self, directory):
        self.directory = directory
        self.globals_file = os.path.join(directory, 'globals.toml')
        globals_file.new_globals_file(self.globals_file)
        runmanager.new_group(self.globals_file, 'group')
        self.labscript_file = os.path.join(directory, 'experiment.py')
        self.exp_config = labconfig(directory)
        self.previous_default_output_folder = ''
        self.currently_open_groups = {}
        self.n_shots = None
        self.compiling = threading.Event()
        # What a compile answers: whether it worked and, if not, why.
        self.compile_result = (True, '')
        self.test_compile_dir = tempfile.TemporaryDirectory(
            prefix='runmanager_test_compile_'
        )
        self.axes_model = QStandardItemModel()
        self.queue_compile_mode_combo = types.SimpleNamespace(
            currentData=lambda: COMPILE_MODE_EAGER
        )
        # What Engage puts in front of the operator instead of raising.
        self.said = []
        self.output_box = types.SimpleNamespace(
            output=lambda text, red=False: self.said.append(text)
        )
        self.ui = types.SimpleNamespace(
            checkBox_run_shots=types.SimpleNamespace(isChecked=lambda: True),
            checkBox_view_shots=types.SimpleNamespace(isChecked=lambda: False),
            lineEdit_labscript_file=types.SimpleNamespace(
                text=lambda: self.labscript_file
            ),
            lineEdit_shot_output_folder=types.SimpleNamespace(
                text=lambda: self.directory
            ),
            pushButton_shuffle=types.SimpleNamespace(
                checkState=lambda: Qt.CheckState.Unchecked
            ),
        )
        # The records of each batch that reached the queue.
        self.batches = []
        # The files compiled, and the files sent to runviewer:
        self.compiled = []
        self.sent_to_runviewer = []
        self.sequences = {}
        self.queue_controller = QueueController()
        self.queue_manager = QueueManager(
            self.queue_controller,
            self.prepare_queue_shot,
            self.compile_run_file,
            self.sent_to_runviewer.append,
            lambda *args, **kwargs: None,
        )
        submit_batch = self.queue_manager.compile_shots

        def compile_shots(records, send_to_BLACS, send_to_runviewer):
            self.batches.append(records)
            return submit_batch(records, send_to_BLACS, send_to_runviewer)

        self.queue_manager.compile_shots = compile_shots

    def compile_run_file(self, labscript_file, path):
        self.compiling.wait()
        self.compiled.append(path)
        return self.compile_result

    def get_active_groups(self, interactive=True):
        return {'group': self.globals_file}

    def check_output_folder_update(self):
        pass

    def globals_changed(self):
        # Only the preparse updates the axes, so one the globals no longer
        # produce stays until it runs, and expanding along it is what breaks.
        self.add_item_to_axes_model('outer no_longer_produced', False)

    def wait_until_preparse_complete(self):
        """Do what the preparse does to what a submission reads afterwards."""
        _, shots, _, _, expansions = self.parse_globals(
            self.get_active_groups(), raise_exceptions=False
        )
        self.n_shots = len(shots)
        self.update_axes_tab(expansions, {})


class SubmitShotsTests(RemoteCommandTestCase):
    """Submitting shots by naming the globals that differ between them.

    One entry is one shot, and the batch is made and submitted at once, so its
    shots claim distinct filenames and run numbers. The window is left holding
    the last entry. Nothing is queued until every entry has been evaluated, so
    a refused call leaves the queue as it found it.
    """

    #: The shot already in the queue that every submission here is added to.
    SEQUENCE = {
        'script_basename': 'experiment',
        'sequence_date': '2026-09-18',
        'sequence_index': 7,
        'sequence_id': '20260918T101112_experiment',
    }

    def make_app(self):
        return SubmittingApp(self.directory)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        super().setUp()
        # Cleanups run in reverse, so this releases the blocked compile before
        # the queue is asked to shut down.
        self.addCleanup(self.app.compiling.set)
        self.define(x='0 # metres', y='2*3', depth='4')
        self.anchor = os.path.join(self.directory, 'experiment_007.h5')
        self.app.queue_manager.enqueue(
            [
                {
                    'path': self.anchor,
                    'shot_id': 'already-queued',
                    'compiled': True,
                    'run_no': 7,
                    'n_runs': 8,
                    'sequence_attrs': dict(self.SEQUENCE),
                }
            ]
        )

    def define(self, **expressions):
        """Give the operator's globals these expressions."""
        for name, expression in expressions.items():
            runmanager.new_global(self.app.globals_file, 'group', name)
            globals_file.set_field(
                self.app.globals_file, 'group', name, 'default', expression
            )

    def scan(self, name, expression):
        """Turn on a scan of this global, as an operator would have left it."""
        globals_file.set_field(self.app.globals_file, 'group', name, 'scan', expression)
        globals_file.set_field(self.app.globals_file, 'group', name, 'scan_enabled', True)
        globals_file.set_field(self.app.globals_file, 'group', name, 'expansion', 'outer')

    def submit(self, *entries, **kwargs):
        return self.request(
            self.client.submit_shots, [dict(entry) for entry in entries], **kwargs
        )

    def expressions(self):
        """The expressions the window is left holding."""
        return self.request(self.client.get_values, raw=True)

    def test_a_batch_is_distinct_shots_of_one_new_sequence_added_to_the_queue(self):
        # A submitted shot has no file and no queue row until the worker
        # reaches it, so the batch is numbered in one go, or its entries would
        # all name one file and overwrite each other.
        descriptors = self.submit({'x': 1}, {'x': 2}, {'x': 3})

        self.assertEqual(
            [descriptor['run_number'] for descriptor in descriptors], [0, 1, 2]
        )
        self.assertEqual(len({descriptor['path'] for descriptor in descriptors}), 3)
        self.assertEqual(
            len({descriptor['shot_id'] for descriptor in descriptors}), 3
        )
        self.assertEqual(
            len({descriptor['sequence_id'] for descriptor in descriptors}),
            1,
            'and they are one sequence',
        )
        self.assertNotEqual(
            descriptors[0]['sequence_id'],
            self.SEQUENCE['sequence_id'],
            "of its own: a first submission does not join the queue's sequence",
        )
        self.assertIn(
            self.anchor,
            self.app.queue_controller.get_queue_paths(),
            'and the shot queued when the batch arrived is queued still: a '
            'remote submission adds to the queue, never replaces it',
        )

    def test_a_batch_of_no_entries_submits_nothing_and_says_so(self):
        # A caller that generated no entries this round has asked for
        # nothing, which is not the same as asking wrongly. Nothing is made,
        # so nothing claims a filename or a run number.
        self.assertEqual(self.submit(), [])
        self.assertEqual(self.app.batches, [])

    def test_a_later_submission_joins_the_sequence_it_names(self):
        # The first batch is still being compiled, so its shots have neither
        # rows nor files: the join is numbered after them all the same, in the
        # sequence's own folder.
        first = self.submit({'x': 1}, {'x': 2})

        second = self.submit({'x': 3}, sequence=first[0]['sequence_id'])

        self.assertEqual(second[0]['sequence_id'], first[0]['sequence_id'])
        self.assertEqual(second[0]['run_number'], 2)
        self.assertEqual(
            os.path.dirname(second[0]['path']), os.path.dirname(first[0]['path'])
        )
        self.assertNotIn(second[0]['path'], {d['path'] for d in first})

    def test_a_session_joins_its_own_sequence_when_another_shares_its_id(self):
        # A sequence_id is a timestamp to the second, so an operator's Engage
        # in the same second as a session's first batch has the same one.
        clock = types.SimpleNamespace(
            datetime=types.SimpleNamespace(now=lambda: datetime.datetime(2026, 9, 24, 12))
        )
        with mock.patch.object(runmanager, 'datetime', clock):
            first = self.submit({'x': 1})
            # Engage waits on the preparse the submission's window change starts.
            self.app.wait_until_preparse_complete()
            self.app.compile_and_queue_shots(
                main_module.SUBMISSION_MODE_NEW_SEQUENCE,
                True,
                False,
                self.app.expand_pending_shots(),
            )
            second = self.submit(
                {'x': 2},
                sequence=first[0]['sequence_id'],
                sequence_index=first[0]['sequence_index'],
            )

        self.assertEqual(second[0]['sequence_index'], first[0]['sequence_index'])
        self.assertEqual(second[0]['run_number'], 1)

    def test_a_joined_shot_is_named_from_its_own_globals(self):
        # The filename prefix and folder can be written in terms of a global.
        # A joined shot is named from the sequence's formats and its own
        # globals, not after the shot before it.
        self.app.exp_config = labconfig(
            self.directory,
            output_folder_format='{globals[x]}',
            filename_prefix_format='{globals[x]}_{script_basename}',
        )
        # The window shows the default folder, the one the folder format makes.
        self.app.previous_default_output_folder = self.directory
        first = self.submit({'x': 1})

        second = self.submit(
            {'x': 5},
            sequence=first[0]['sequence_id'],
            sequence_index=first[0]['sequence_index'],
        )

        folder = os.path.join(self.directory, 'experiment')
        self.assertEqual(
            [first[0]['path'], second[0]['path']],
            [
                os.path.join(folder, '1', '1_experiment_0.h5'),
                os.path.join(folder, '5', '5_experiment_1.h5'),
            ],
        )

    def test_a_join_is_refused_once_the_labscript_file_has_changed(self):
        # A sequence is one labscript file's shots; another file's would land
        # in its folder under its name.
        first = self.submit({'x': 1})
        self.app.labscript_file = os.path.join(self.directory, 'other.py')

        with self.assertRaises(SequenceRefused):
            self.submit(
                {'x': 2},
                sequence=first[0]['sequence_id'],
                sequence_index=first[0]['sequence_index'],
            )

        self.assertEqual(len(self.app.batches), 1, 'the refused batch was not queued')

    def test_a_sequence_runmanager_has_no_record_of_is_refused(self):
        # Refused rather than started afresh, which would split the session
        # quietly in two; the caller stops on this refusal.
        with self.assertRaises(SequenceRefused):
            self.submit({'x': 1}, sequence='20260923T101112_experiment')

        self.assertEqual(self.app.batches, [], 'nothing reached the queue')

    def test_a_submission_that_raises_has_queued_nothing_at_all(self):
        # The window is the operator's throughout, and the labscript file can
        # be cleared from it at any time. A caller told the submission failed
        # must not have shots running under identifiers it was never given.
        self.app.labscript_file = ''

        with self.assertRaises(Exception):
            self.submit({'x': 1}, {'x': 2}, {'x': 3})

        self.assertEqual(self.app.batches, [])
        self.assertEqual(self.app.queue_controller.get_queue_paths(), [self.anchor])

    def test_the_window_is_left_holding_the_last_shot_submitted(self):
        # Whoever is watching has to be able to see what is running, so the
        # globals really are set and really are left set. What is left is one
        # shot's worth of them and not every entry's at once.
        self.submit({'x': 1, 'y': 8}, {'x': 2, 'y': 9})

        self.assertEqual(
            self.expressions(), {'x': '2 # metres', 'y': '9', 'depth': '4'}
        )

    def test_nothing_is_submitted_when_a_later_entry_would_expand(self):
        # A scan left on a global the entries do not name can make an entry
        # more than one shot. The entries before it were fine and are still
        # not submitted, because a refused call leaves nothing behind.
        self.scan('y', '[1] * x')

        with self.assertRaises(Exception) as raised:
            self.submit({'x': 1}, {'x': 1}, {'x': 3})

        self.assertEqual(self.app.batches, [])
        self.assertIn('3', str(raised.exception))
        self.assertIn(
            'y',
            str(raised.exception),
            'the global that expanded it is named, because the scan left on '
            'it is what the caller has to go and turn off',
        )
        self.assertNotIn(
            'depth',
            str(raised.exception),
            'a global that expands into nothing did not cause this',
        )

    def test_an_error_in_the_globals_is_refused_before_anything_is_submitted(self):
        self.define(broken='1/0')

        with self.assertRaises(Exception) as raised:
            self.submit({'x': 1})

        self.assertIn('broken', str(raised.exception), 'the refusal names the global')
        self.assertEqual(self.app.batches, [])

    def test_a_global_no_active_group_has_is_refused_before_anything_is_set(self):
        with self.assertRaises(Exception) as raised:
            self.submit({'x': 1, 'not_a_global': 2}, {'x': 3, 'not_a_global': 4})

        self.assertIn(
            'Global not_a_global not found in any active group',
            str(raised.exception),
            'said the way the command that sets one global says it, because '
            'that is what tells the caller the name is the problem',
        )
        self.assertEqual(self.app.batches, [])
        self.assertEqual(
            self.expressions(), {'x': '0 # metres', 'y': '2*3', 'depth': '4'}
        )


class ShotStatusTests(RemoteCommandTestCase):
    """Whether a shot that was submitted can still produce a result.

    ``pending`` says whether a caller should go on waiting for a shot: true
    while the queue would still hand its row over, or BLACS can still complete
    it, and false once the row waits on an operator. Only the head is ever
    offered, so a row the queue will not hand over holds up every row behind it.
    """

    # Every state a queue row can be in: whether a shot in it can still produce
    # a result, and whether it holds up the rows behind it. A cancelled row
    # clears itself at BLACS's next request; rejected and compile_failed wait.
    EXPECTED = {
        # state: (pending, holds up the rows behind it)
        '': (True, False),
        'running': (True, False),
        'failed': (True, False),
        'rejected': (False, True),
        'cancelled': (True, False),
        'compile_failed': (False, True),
    }

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def enqueue(self, shot_id, state=''):
        path = os.path.join(self.directory, '%s.h5' % shot_id)
        self.app.queue_manager.enqueue(
            [{'path': path, 'shot_id': shot_id, 'compiled': True}]
        )
        for item in self.app.queue_controller._items:
            if item['shot_id'] == shot_id:
                item['state'] = state

    def queue(self, *rows):
        """A queue holding exactly these ``(shot_id, state)`` rows, in order.

        Replacing what is there rather than adding to it, because what is in front
        of a row is half of its answer.
        """
        self.app.queue_manager.restore_state({})
        for shot_id, state in rows:
            self.enqueue(shot_id, state=state)

    def test_pending_is_whether_the_shot_can_still_produce_a_result(self):
        for state, (pending, _) in sorted(self.EXPECTED.items()):
            with self.subTest(state=state):
                self.queue((state or 'waiting', state))
                answer = self.request(self.client.shot_status, [state or 'waiting'])
                self.assertEqual(answer[state or 'waiting']['pending'], pending)

    def test_a_row_behind_a_held_one_says_it_is_blocked(self):
        # The row in front is the whole of the reason: nothing behind a shot the
        # queue will not hand over can be handed over either, and a caller
        # polling for its result would otherwise wait for ever.
        for state, (_, holds_up) in sorted(self.EXPECTED.items()):
            with self.subTest(state=state):
                self.queue(('head', state), ('behind', ''))

                answer = self.request(self.client.shot_status, ['behind'])

                self.assertEqual(
                    answer['behind'],
                    {'pending': False, 'state': BLOCKED_SHOT_STATE}
                    if holds_up
                    else {'pending': True, 'state': ''},
                )

    def test_the_queue_lists_each_row_in_the_state_its_status_gives_it(self):
        # A caller reading the queue and one polling shot_status are told the
        # same thing: a row behind a held one is not listed as queued.
        for state in sorted(self.EXPECTED):
            with self.subTest(state=state):
                self.queue(('head', state), ('behind', ''))
                shot_ids = ['head', 'behind']

                listed = self.request(self.client.get_queue)
                answer = self.request(self.client.shot_status, shot_ids)

                self.assertEqual(
                    [row['state'] for row in listed],
                    [
                        answer[shot_id]['state'] or QUEUED_SHOT_STATE
                        for shot_id in shot_ids
                    ],
                )

    def test_a_held_row_keeps_its_state_and_deleting_it_frees_the_rows_behind(self):
        # Blocked says the queue is not moving, not that the shot behind is
        # spoiled: the held row keeps the reason an operator has to act on, and
        # clearing it puts the work behind back to waiting its turn.
        self.queue(('head', 'rejected'), ('behind', ''))
        answer = self.request(self.client.shot_status, ['head', 'behind'])
        self.assertEqual(answer['head']['state'], 'rejected')
        self.assertEqual(
            answer['behind']['state'],
            BLOCKED_SHOT_STATE,
            'which is what the caller polling it is told meanwhile',
        )

        self.app.queue_manager.delete_rows(['head'])

        self.assertEqual(
            self.request(self.client.shot_status, ['behind'])['behind'],
            {'pending': True, 'state': ''},
            'the answer is read off the queue as it stands, so a row asked '
            'about while it was stuck is not left carrying that',
        )

    def test_a_shot_the_queue_no_longer_has_is_not_pending(self):
        # Told apart by how each left the queue, because a result is coming
        # only from the shot BLACS completed.
        self.queue(('ran', 'running'), ('deleted', ''), ('emptied', ''))
        self.app.queue_manager.shot_finished('ran', 'completed')
        self.app.queue_manager.delete_rows(['deleted'])
        self.app.queue_manager.clear()

        answer = self.request(
            self.client.shot_status,
            ['ran', 'deleted', 'emptied', 'never-heard-of-it'],
        )

        self.assertEqual(
            answer,
            {
                'ran': {'pending': False, 'state': 'completed'},
                'deleted': {'pending': False, 'state': 'removed'},
                'emptied': {'pending': False, 'state': 'removed'},
                'never-heard-of-it': {'pending': False, 'state': 'unknown'},
            },
            'nothing more will happen to a shot with no row, and no row state '
            'describes it -- least of all the empty one, which means waiting',
        )


class EmptyQueueTests(RemoteCommandTestCase):
    """Empty queue backs out of the work that is waiting.

    A batch is queued as rows the moment it is submitted, so emptying the queue
    takes all of it, including a shot still compiling, whose file goes when its
    compile finishes.
    """

    def make_app(self):
        return SubmittingApp(self.directory)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        super().setUp()
        # Cleanups run in reverse, so this releases the blocked compile before
        # the queue is asked to shut down.
        self.addCleanup(self.app.compiling.set)
        runmanager.new_global(self.app.globals_file, 'group', 'x')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'default', '0')

    def test_a_batch_still_compiling_goes_with_its_files(self):
        started, written, compiled = threading.Event(), threading.Event(), []
        compile_run_file = self.app.queue_manager.compile_run_file_callback

        def compile_writing_its_file(labscript_file, path):
            compiled.append(path)
            started.set()
            result = compile_run_file(labscript_file, path)
            open(path, 'w').close()
            written.set()
            return result

        self.app.queue_manager.compile_run_file_callback = compile_writing_its_file
        descriptors = self.request(self.client.submit_shots, [{'x': 1}, {'x': 2}])
        self.assertTrue(started.wait(5), 'the first shot is compiling')

        self.request(self.client.abort)
        self.app.compiling.set()

        self.assertTrue(written.wait(5), 'and its compile finished')
        for _ in range(500):
            if not os.path.exists(descriptors[0]['path']):
                break
            time.sleep(0.01)
        self.assertEqual(self.app.queue_controller.get_queue_paths(), [])
        self.assertEqual(compiled, [descriptors[0]['path']], 'the second never compiled')
        self.assertFalse(os.path.exists(descriptors[0]['path']), 'and no file is left')


class SubmissionAnchorTests(RemoteCommandTestCase):
    """The shot each submission mode numbers its batch after.

    A mode that adds to a sequence looks for one in the queue and among the
    shots sent to BLACS, in the order the mode calls for, and starts a sequence
    of its own when neither has one. The queue empties on its own, so the last
    shot sent is what "add shots to last sequence" falls back to.
    """

    SEQUENCE = {
        'script_basename': 'experiment',
        'sequence_date': '2026-09-18',
        'sequence_index': 7,
        'sequence_id': '20260918T101112_experiment',
    }

    def make_app(self):
        return SubmittingApp(self.directory)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        super().setUp()
        self.addCleanup(self.app.compiling.set)
        runmanager.new_global(self.app.globals_file, 'group', 'x')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'default', '0')

    def enqueue(self, name, **overrides):
        path = os.path.join(self.directory, name)
        record = {
            'path': path,
            'compiled': True,
            'run_no': 7,
            'n_runs': 8,
            'sequence_attrs': dict(self.SEQUENCE),
        }
        record.update(overrides)
        self.app.queue_manager.enqueue([record])
        return path

    def wait_until(self, predicate):
        for _ in range(500):
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_a_replacement_takes_no_name_of_a_shot_still_compiling(self):
        # That shot writes its file after the replacement is named, and then
        # deletes it, its row having gone with the Clear.
        self.enqueue('experiment_000.h5', run_no=0, n_runs=1)
        [compiling] = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)
        self.assertTrue(self.wait_until(self.app.queue_controller.get_compiling_paths))
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan', '[1, 2]')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan_enabled', True)
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'expansion', 'outer')

        replacement = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE_CLEAR_QUEUE)

        self.assertEqual([record['run_no'] for record in replacement], [0, 2])
        self.assertNotIn(compiling['path'], [record['path'] for record in replacement])

    def test_a_join_after_a_replacement_numbers_after_both_batches(self):
        self.enqueue('experiment_000.h5', run_no=0, n_runs=1)
        self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)
        self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE_CLEAR_QUEUE)
        self.app.compiling.set()
        self.assertTrue(
            self.wait_until(lambda: not self.app.queue_controller.get_compiling_paths())
        )

        later = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)

        self.assertEqual([record['run_no'] for record in later], [2])

    def test_adding_twice_before_the_first_compiles_does_not_reuse_its_numbers(self):
        # The first batch's shot is still compiling, with no row and no file,
        # so the queue's last row is still the shot it was added to. The second
        # batch is numbered after the first all the same.
        self.enqueue('experiment_007.h5')

        first = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)
        second = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)

        self.assertEqual([record['run_no'] for record in first], [8])
        self.assertEqual([record['run_no'] for record in second], [9])
        self.assertNotEqual(first[0]['path'], second[0]['path'])

    def test_adding_to_the_last_sequence_carries_on_from_the_shot_blacs_has(self):
        # BLACS can take the last queued shot between the menu being drawn and
        # the item being clicked; the shot it was sent is the sequence the
        # operator was looking at, so the batch joins that.
        sent = os.path.join(self.directory, 'experiment_007.h5')
        runmanager.make_single_run_file(sent, None, {}, self.SEQUENCE, 7, 8)
        self.app.queue_manager.set_last_sent_from_queue(sent)

        records = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)

        self.assertEqual(
            [record['sequence_attrs']['sequence_id'] for record in records],
            [self.SEQUENCE['sequence_id']],
        )

    def test_adding_to_the_last_sequence_reads_no_file_when_the_queue_knows_it(self):
        # A file read would be on the GUI thread, and lyse can hold the file of
        # the shot BLACS has just run for as long as it likes.
        sent = os.path.join(self.directory, 'experiment_007.h5')
        self.app.queue_manager.set_last_sent_from_queue(sent, dict(self.SEQUENCE))

        records = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)

        self.assertFalse(os.path.exists(sent), 'there was no file to read')
        self.assertEqual(
            [record['sequence_attrs']['sequence_id'] for record in records],
            [self.SEQUENCE['sequence_id']],
        )

    def test_adding_to_the_last_sequence_joins_one_still_compiling(self):
        # "Last sequence" is the one submitted last, compiled or not: its rows
        # are in the queue from the moment it is submitted.
        self.enqueue('experiment_007.h5')
        engaged = self.app.engage(main_module.SUBMISSION_MODE_NEW_SEQUENCE)

        added = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)

        self.assertEqual(
            added[0]['sequence_attrs']['sequence_id'],
            engaged[0]['sequence_attrs']['sequence_id'],
        )

    def test_adding_to_nothing_at_all_starts_a_sequence(self):
        records = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)

        self.assertEqual(
            [record['sequence_attrs']['sequence_index'] for record in records],
            [0],
            'a runmanager with nothing queued and nothing ever sent has no '
            'last sequence, so this batch is the start of one',
        )

    def test_replacing_nothing_at_all_starts_a_sequence(self):
        records = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE_CLEAR_QUEUE)

        self.assertEqual(
            [record['sequence_attrs']['sequence_index'] for record in records],
            [0],
        )

    def test_the_queue_is_read_once_for_the_sequence_being_added_to(self):
        # BLACS empties the queue on the server thread while a batch is made,
        # so a second read could say it is empty and turn "add shots to last
        # sequence" into a new sequence, silently. The batch acts on one read.
        queued = self.enqueue('experiment_007.h5')
        answers = [queued]
        self.app.get_queue_append_filepath = (
            lambda: answers.pop(0) if answers else None
        )

        records = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE)

        self.assertEqual(
            [record['sequence_attrs']['sequence_id'] for record in records],
            [self.SEQUENCE['sequence_id']],
            'the batch joined the sequence the queue named when the mode was '
            'chosen',
        )

    def test_a_replacement_batch_joins_the_sequence_blacs_is_running(self):
        # "Empty queue, then add shots to last sequence" replaces the work
        # that is waiting, so the sequence it joins is the one BLACS is
        # running -- not the one the shots about to be deleted belong to.
        sent = self.enqueue('experiment_007.h5')
        self.app.queue_manager.set_last_sent_from_queue(sent)
        self.enqueue(
            'later_000.h5',
            sequence_attrs=dict(self.SEQUENCE, sequence_id='20260918T140000_experiment'),
        )

        records = self.app.engage(main_module.SUBMISSION_MODE_LAST_SEQUENCE_CLEAR_QUEUE)

        self.assertEqual(
            [record['sequence_attrs']['sequence_id'] for record in records],
            [self.SEQUENCE['sequence_id']],
        )
        self.assertEqual(
            self.app.queue_controller.get_queue_paths(),
            [record['path'] for record in records],
            'and the work that was waiting was thrown away, which is the '
            'other half of what the mode offers',
        )
        self.assertEqual(
            [record['run_no'] for record in records],
            [0],
            'the replacement takes back the run numbers the deleted shots '
            'gave up, rather than carrying on past them',
        )

    def test_a_remote_engage_carries_out_the_mode_and_receipts_each_shot(self):
        self.enqueue('experiment_007.h5')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan', '[1, 2]')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan_enabled', True)
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'expansion', 'outer')

        receipts = self.request(
            self.client.engage, SUBMISSION_MODE_LAST_SEQUENCE_CLEAR_QUEUE
        )

        sequence_id = self.SEQUENCE['sequence_id']
        self.assertEqual(
            [(receipt['sequence_id'], receipt['run_number']) for receipt in receipts],
            [(sequence_id, 0), (sequence_id, 1)],
            'the replacement joined the sequence that was queued, from 0 again',
        )
        self.assertEqual(
            [row['shot_id'] for row in self.request(self.client.get_queue)],
            [receipt['shot_id'] for receipt in receipts],
            'and the queue lists those shots and nothing it replaced',
        )


class ShuffledEngageTests(RemoteCommandTestCase):
    """Engaging a scan with the shuffle button down, or an axis's box checked.

    The batch is queued in the order the shuffle chose, and each shot's globals
    travel with it, so a file is never named for one set of parameters and run
    with another.
    """

    def make_app(self):
        return SubmittingApp(self.directory)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        # A shuffle this test can say the answer to. Reversing is a
        # permutation of three shots like any other, and the only one whose
        # result can be written down.
        patcher = mock.patch.object(
            runmanager.random, 'shuffle', lambda sequence: sequence.reverse()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.addCleanup(self.app.compiling.set)
        runmanager.new_global(self.app.globals_file, 'group', 'x')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'default', '0')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan', '[1, 2, 3]')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan_enabled', True)
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'expansion', 'outer')
        self.app.exp_config = labconfig(
            self.directory, filename_prefix_format='{globals[x]}_{script_basename}'
        )

    def engage(self):
        """Press Engage, and hand back the records that reached the queue."""
        self.app.wait_until_preparse_complete()
        # Down once the axes are listed: a new axis takes its box from the
        # button, and here only the batch is to be shuffled.
        self.app.ui.pushButton_shuffle.checkState = lambda: Qt.CheckState.Checked
        self.app.on_engage_clicked()
        self.assertEqual(self.app.said, [], 'Engage put nothing in the output box')
        self.assertEqual(len(self.app.batches), 1)
        return self.app.batches[0]

    def test_a_shuffled_batch_keeps_each_shot_named_for_the_globals_it_carries(self):
        records = self.engage()

        self.assertEqual(
            [
                (
                    os.path.basename(record['path']).split('_')[0],
                    record['frozen_globals']['x'],
                )
                for record in records
            ],
            [('3', '3'), ('2', '2'), ('1', '1')],
            'in the order the shuffle chose, each file named for the globals '
            'compiled into it',
        )

    def test_an_axis_is_shuffled_only_when_its_shuffle_box_is_checked(self):
        self.app.wait_until_preparse_complete()
        box = self.app.axes_model.item(0, self.app.AXES_COL_SHUFFLE)

        def scan():
            return [shot['x'] for shot, _ in self.app.expand_pending_shots()]

        self.assertEqual(scan(), [1, 2, 3], 'an unchecked axis keeps its order')
        box.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(scan(), [3, 2, 1], 'and a checked one is shuffled')


class CompileOnlyEngageTests(RemoteCommandTestCase):
    """Engage with neither destination ticked compiles the batch and leaves it alone."""

    def make_app(self):
        return SubmittingApp(self.directory)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        super().setUp()
        runmanager.new_global(self.app.globals_file, 'group', 'x')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan', '[1, 2]')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan_enabled', True)
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'expansion', 'outer')
        self.app.ui.checkBox_run_shots.isChecked = lambda: False

    def test_the_shots_are_compiled_and_go_nowhere(self):
        self.app.compiling.set()
        self.app.wait_until_preparse_complete()

        self.app.on_engage_clicked()
        # The worker compiles the whole batch before it closes:
        self.app.queue_manager.shutdown()

        self.assertEqual(self.app.said, [])
        self.assertEqual(len(self.app.compiled), 2)
        self.assertTrue(all(os.path.isfile(path) for path in self.app.compiled))
        self.assertEqual(self.app.queue_controller.get_queue_paths(), [])
        self.assertEqual(self.app.sent_to_runviewer, [])


class CompileErrorTests(RemoteCommandTestCase):
    """What a remote caller is told when a compile fails.

    A queued shot's row carries the error, and test_compile, which queues
    nothing, answers with it.
    """

    ERROR = 'NameError: name "z" is not defined'

    def make_app(self):
        return SubmittingApp(self.directory)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        super().setUp()
        runmanager.new_global(self.app.globals_file, 'group', 'x')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'default', '0')
        self.app.compile_result = (False, self.ERROR)
        self.app.compiling.set()

    def test_a_shot_that_fails_to_compile_lists_its_error_in_the_queue(self):
        [receipt] = self.request(self.client.engage)
        wait_for(
            lambda: self.app.queue_controller.get_queue()[0]['state'] == 'compile_failed'
        )

        [row] = self.request(self.client.get_queue)

        self.assertEqual(row['shot_id'], receipt['shot_id'])
        self.assertEqual(row['state'], 'compile_failed')
        self.assertIn(self.ERROR, row['message'])

    def test_a_test_compile_answers_with_the_error_and_queues_nothing(self):
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan', '[1, 2]')
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'scan_enabled', True)
        globals_file.set_field(self.app.globals_file, 'group', 'x', 'expansion', 'outer')
        stored = sorted(os.listdir(self.directory))

        answer = self.request(self.client.test_compile)

        self.assertEqual(answer['success'], False)
        self.assertEqual(answer['error'], self.ERROR)
        self.assertEqual(self.app.compiled, [answer['path']])
        with h5py.File(answer['path'], 'r') as f:
            self.assertEqual(f['globals'].attrs['x'], 1, 'it is the first shot')
        self.assertEqual(self.request(self.client.get_queue), [])
        self.assertEqual(
            sorted(os.listdir(self.directory)),
            stored,
            'and no sequence was claimed and no output folder touched',
        )


class PreparsingApp(FakeApp):
    """The application's preparse thread, over a globals file that is not there."""

    preparse_globals_loop = RunManager.preparse_globals_loop
    wait_until_preparse_complete = RunManager.wait_until_preparse_complete
    preparse_globals = RunManager.preparse_globals
    parse_globals = RunManager.parse_globals

    def __init__(self, directory):
        super().__init__()
        self.preparse_globals_required = queue.Queue()
        self.n_shots = 3
        self.missing_globals_file = os.path.join(directory, 'moved.toml')

    def get_active_groups(self, interactive=True):
        return {'group': self.missing_globals_file}


class PreparseFailureTests(RemoteCommandTestCase):
    def make_app(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        return PreparsingApp(directory)

    def test_a_command_waiting_on_a_preparse_is_answered_after_one_fails(self):
        # BLACS is answered on the thread such a command waits on, so a wait
        # that never ends leaves BLACS unanswered too.
        answers = []
        reported = threading.Event()
        with mock.patch.object(main_module, 'qtlock', threading.Lock()), mock.patch.object(
            main_module, 'raise_exception_in_thread', lambda exc_info: reported.set()
        ):
            threading.Thread(target=self.app.preparse_globals_loop, daemon=True).start()
            self.app.preparse_globals_required.put(None)
            asker = threading.Thread(
                target=lambda: answers.append(self.request(self.client.n_shots)),
                daemon=True,
            )
            asker.start()
            asker.join(5)
            self.assertTrue(reported.wait(5), 'the failed preparse is still reported')

        self.assertEqual(answers, [3])


if __name__ == '__main__':
    unittest.main()

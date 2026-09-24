"""What runmanager answers to a remote caller that is not BLACS.

BLACS's half of the protocol is guarded in ``test_architecture.py``. This is
the rest of what runmanager's server offers: the commands a plugin or an
optimizer sends.

Each is exercised through ``RemoteServer.handler`` under the command name it
travels as, rather than by calling the handler method directly. The name is
what crosses the wire -- a handler reachable only under a name no client sends
is not reachable at all -- and the dispatch is runmanager's own.
"""
import copy
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

from labscript_utils.labconfig import LabConfig
import runmanager
import runmanager.remote
# fixtures stubs the splash and does the guarded import of the
# application, once, for every test module. Importing
# runmanager.__main__ here instead would show the startup banner.
from fixtures import RemoteServer, RunManager, main_module
from runmanager.queueing import (
    BLACS_STATES,
    BLOCKED_SHOT_STATE,
    COMPILE_MODE_EAGER,
    QueueManager,
    UNKNOWN_SHOT_STATE,
)


class LoopbackRemoteServer(RemoteServer):
    """Runmanager's own server, without binding a port.

    The real one reads a port out of the labconfig and binds a socket in its
    constructor, which is the only part a test has no use for. Subclassing
    rather than listing the handlers means this carries the whole handler set
    the running server has, so a command cannot be reachable here and missing
    there.
    """

    def __init__(self):
        pass


class FakeApp(object):
    """The application over only the surface these commands reach.

    The queue is a real QueueManager, because an accessor that answers what a
    stand-in was told to answer tests nothing. Nothing else of the window is
    here, because the commands guarded against this one read nothing else.
    """

    def __init__(self):
        self.queue_manager = QueueManager(
            lambda item: None,
            lambda labscript_file, path: True,
            lambda path: None,
            lambda *args, **kwargs: None,
        )


class RemoteCommandTestCase(unittest.TestCase):
    """A server with an application behind it, for the length of one test."""

    def make_app(self):
        """The application the server under test reaches through."""
        return FakeApp()

    def setUp(self):
        self.app = self.make_app()
        self.addCleanup(self.app.queue_manager.shutdown)
        # RemoteServer's handlers reach the application through this module
        # global, which the application assigns to itself on startup. Nothing
        # starts here, so the test puts it there and takes it away again.
        patcher = mock.patch.object(main_module, 'app', self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = LoopbackRemoteServer()

    def request(self, command, *args, **kwargs):
        answer = self.server.handler([command, args, kwargs])
        if isinstance(answer, Exception):
            raise answer
        return answer


class ClientCommandNameTests(unittest.TestCase):
    """Each client method asks under the name the server answers to.

    The name is the whole of what crosses the wire, so a method sending
    anything else reaches a handler that is not there -- or, worse, one that
    is. What travels with the name is the caller's own arguments and is not
    fixed here: that is the shape of a request rather than the protocol, and
    the handlers are exercised through ``handler`` above.
    """

    #: One call per command, carrying arguments only where the method takes
    #: them. A command added to the client belongs here.
    CALLS = {
        'shot_status': (['one'],),
        'submit_shots': ([{'x': 1}],),
    }

    def test_each_command_is_asked_for_under_its_own_name(self):
        for command, args in sorted(self.CALLS.items()):
            with self.subTest(command=command):
                sent = []
                with mock.patch.object(
                    runmanager.remote.Client,
                    'request',
                    lambda self, name, *a, **kw: sent.append(name),
                ):
                    getattr(runmanager.remote.Client, command)(
                        runmanager.remote.Client.__new__(runmanager.remote.Client),
                        *args,
                    )

                self.assertEqual(sent, [command])


class LabConfigWithShotStorage(object):
    """A labconfig carrying the settings a shot's filename is built from.

    Everything else it asks for has a built-in fallback, reached by raising
    what a real LabConfig raises for a setting that is not there. The filename
    prefix is settable because an installation may write one in terms of a
    global, which is what makes it matter which shot a file is named after.
    """

    def __init__(self, shot_storage):
        self.shot_storage = shot_storage
        self.filename_prefix_format = None

    def get(self, section, option, *args, **kwargs):
        if (section, option) == ('default', 'experiment_shot_storage'):
            return self.shot_storage
        if (section, option) == ('runmanager', 'filename_prefix_format'):
            if self.filename_prefix_format is not None:
                return self.filename_prefix_format
        raise LabConfig.NoOptionError(option, section)


class AxesModel(object):
    """The axes the shots are expanded along, as the window holds them.

    Only the preparse rebuilds this, and a submission reads it to decide the
    order to expand the globals in. Changing a global leaves behind an axis
    the globals may no longer produce, and expanding along an axis that is not
    there is what breaks -- so the rows here go stale on a change and come
    back from the preparse, exactly as the window's do.
    """

    STALE_AXIS = 'outer no_longer_produced'

    def __init__(self):
        self.names = []

    def go_stale(self):
        if self.STALE_AXIS not in self.names:
            self.names.append(self.STALE_AXIS)

    def rebuild(self, expansions):
        self.names = sorted(
            ('outer ' + name) if expansion == 'outer' else ('zip ' + expansion)
            for name, expansion in expansions.items()
            if expansion
        )

    def rowCount(self):
        return len(self.names)

    def item(self, row, column):
        name = self.names[row]
        return types.SimpleNamespace(
            data=lambda role: name, checkState=lambda: 0
        )


class SubmittingApp(object):
    """The application over the whole path a submission takes.

    Everything a submitted shot's identity comes from is runmanager's own
    here: real globals files, the methods that choose each shot's filename and
    run number, the sequence a batch is added to, and the queue it is handed
    to. A stand-in that invents a path per call cannot collide with itself,
    and not colliding with itself is most of what submitting a batch has to
    get right.

    What is stood in for is the window around that: the group tabs a global
    would be written through, and the preparse thread and everything it writes
    for a submission to read afterwards.

    The compile callback blocks until the test releases it, which is where a
    shot waits while it compiles.
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
    on_abort_clicked = RunManager.on_abort_clicked
    on_engage_clicked = RunManager.on_engage_clicked
    expand_pending_shots = RunManager.expand_pending_shots
    parse_globals = RunManager.parse_globals

    def __init__(self, directory):
        self.directory = directory
        self.globals_file = os.path.join(directory, 'globals.toml')
        runmanager.new_globals_file(self.globals_file)
        runmanager.new_group(self.globals_file, 'group')
        self.labscript_file = os.path.join(directory, 'experiment.py')
        self.exp_config = LabConfigWithShotStorage(directory)
        self.previous_default_output_folder = ''
        self.currently_open_groups = {}
        self.n_shots = None
        self.compiling = threading.Event()
        self.axes_model = AxesModel()
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
            pushButton_shuffle=types.SimpleNamespace(checkState=lambda: 0),
        )
        # Where each submission asked for its shots to be sent, and the
        # records of each batch that reached the queue.
        self.destinations = []
        self.batches = []
        self.sequences = {}
        self.queue_manager = QueueManager(
            lambda item: None,
            self.compile_run_file,
            lambda path: None,
            lambda *args, **kwargs: None,
        )
        submit_batch = self.queue_manager.compile_shots

        def compile_shots(records, send_to_BLACS, send_to_runviewer):
            self.batches.append(records)
            self.destinations.append((send_to_BLACS, send_to_runviewer))
            return submit_batch(records, send_to_BLACS, send_to_runviewer)

        self.queue_manager.compile_shots = compile_shots

    def compile_run_file(self, labscript_file, path):
        self.compiling.wait()
        return True

    def get_active_groups(self, interactive=True):
        return {'group': self.globals_file}

    def check_output_folder_update(self):
        pass

    def globals_changed(self):
        self.axes_model.go_stale()

    def ensure_editable_globals_file(self, globals_file, parent=None):
        return globals_file

    def wait_until_preparse_complete(self):
        """Do what the preparse does to what a submission reads afterwards."""
        _, shots, _, _, expansions = self.parse_globals(
            self.get_active_groups(), raise_exceptions=False
        )
        self.n_shots = len(shots)
        self.axes_model.rebuild(expansions)


class SubmitShotsTests(RemoteCommandTestCase):
    """Submitting shots by naming the globals that differ between them.

    One entry is one shot, and the whole batch is made and submitted at once.
    Making it at once is what tells its shots apart: a run number and a
    filename are claimed by the batch being made, so entries submitted one at
    a time each find the same number free and write over each other.

    Each entry's shot is evaluated from one read of the globals, as the
    window would hold them with that entry's values set, and the window is
    set once, to the last entry.

    Nothing reaches the queue until every entry has been evaluated, so a call
    that refuses leaves the queue exactly as it found it and the error can
    simply be raised.
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
        # next_sequence_index takes a zlock and keeps a counter on disk.
        # Which index it hands out does not matter here.
        patcher = mock.patch.object(
            runmanager, 'next_sequence_index', lambda *a, **k: 7
        )
        patcher.start()
        self.addCleanup(patcher.stop)
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
            runmanager.set_value(self.app.globals_file, 'group', name, expression)

    def scan(self, name, expression):
        """Turn on a scan of this global, as an operator would have left it."""
        runmanager.set_scan(self.app.globals_file, 'group', name, expression)
        runmanager.set_scan_enabled(self.app.globals_file, 'group', name, True)
        runmanager.set_expansion(self.app.globals_file, 'group', name, 'outer')

    def submit(self, *entries, **kwargs):
        return self.request(
            'submit_shots', [dict(entry) for entry in entries], **kwargs
        )

    def expressions(self):
        """The expressions the window is left holding."""
        return self.request('get_default_globals', raw=True)

    def queued(self):
        """The records of the one batch that reached the queue."""
        self.assertEqual(
            len(self.app.batches),
            1,
            'the whole batch is handed over at once, and once only',
        )
        return self.app.batches[0]

    def test_the_shots_of_a_batch_do_not_share_a_file_or_a_run_number(self):
        # A submitted shot has no file on disk and no queue row until the
        # worker reaches it, so an entry asking what number is free gets the
        # same answer as the entry before it unless the whole batch is
        # numbered in one go. Three rows naming one file is three shots
        # overwriting each other and three descriptors that cannot be told
        # apart by anything the caller was given.
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

    def test_a_submission_sends_its_shots_where_the_window_says(self):
        # "View shot(s)" is the operator's, and a submission reads it the same
        # way an Engage does -- the same read the remote protocol offers as
        # get_view_shots. Shots go to BLACS either way: a submission is work
        # asked for, not a look at what it would be.
        self.app.ui.checkBox_view_shots.isChecked = lambda: True

        self.submit({'x': 1})

        self.assertEqual(self.app.destinations, [(True, True)])

    def test_a_batch_of_no_entries_submits_nothing_and_says_so(self):
        # A caller that generated no entries this round has asked for
        # nothing, which is not the same as asking wrongly. Nothing is made,
        # so nothing claims a filename or a run number.
        self.assertEqual(self.submit(), [])
        self.assertEqual(self.app.batches, [])

    def test_the_batch_reaches_the_queue_as_a_single_submission(self):
        # One submission is what makes refusing safe. Until the last entry has
        # been evaluated there is nothing in the queue to take back, so every
        # way the batch can fail -- and most of them are not things an entry
        # can be checked for -- leaves the queue as it was found.
        self.submit({'x': 1}, {'x': 2})

        self.assertEqual(len(self.queued()), 2)
        sequence_ids = {
            record['sequence_attrs']['sequence_id'] for record in self.queued()
        }
        self.assertEqual(len(sequence_ids), 1, 'as one sequence')
        self.assertNotIn(
            self.SEQUENCE['sequence_id'],
            sequence_ids,
            'of its own: a first submission does not join the queue\'s sequence',
        )
        self.assertIn(
            self.anchor,
            self.app.queue_manager.get_queue_paths(),
            'and the shot that was queued when the batch arrived is queued '
            'still: a remote submission adds to the queue, never replaces it',
        )

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

    def test_an_operator_s_engage_in_between_does_not_take_the_session_s_shots(self):
        # The queue's last shot is the operator's, as an Engage between two
        # submissions leaves it; the session names its own sequence instead.
        first = self.submit({'x': 1})
        self.app.wait_until_preparse_complete()
        [engaged] = self.app.compile_and_queue_shots(
            main_module.SUBMISSION_MODE_NEW_FOLDER,
            True,
            False,
            self.app.expand_pending_shots(),
        )
        self.assertEqual(self.app.get_queue_append_filepath(), engaged['path'])

        second = self.submit({'x': 2}, sequence=first[0]['sequence_id'])

        self.assertNotEqual(first[0]['sequence_id'], self.SEQUENCE['sequence_id'])
        self.assertEqual(second[0]['sequence_id'], first[0]['sequence_id'])

    def test_a_session_joins_its_own_sequence_when_another_shares_its_id(self):
        # A sequence_id is a timestamp to the second, so an operator's Engage
        # in the same second as a session's first batch has the same one.
        clock = types.SimpleNamespace(
            datetime=types.SimpleNamespace(now=lambda: datetime.datetime(2026, 9, 24, 12))
        )
        indexes = iter([7, 8])
        with mock.patch.object(runmanager, 'datetime', clock), mock.patch.object(
            runmanager, 'next_sequence_index', lambda *a, **k: next(indexes)
        ):
            first = self.submit({'x': 1})
            # Engage waits on the preparse the submission's window change starts.
            self.app.wait_until_preparse_complete()
            self.app.compile_and_queue_shots(
                main_module.SUBMISSION_MODE_NEW_FOLDER,
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

    def test_a_join_is_refused_once_the_labscript_file_has_changed(self):
        # A sequence is one labscript file's shots; another file's would land
        # in its folder under its name.
        first = self.submit({'x': 1})
        self.app.labscript_file = os.path.join(self.directory, 'other.py')

        with self.assertRaises(Exception) as raised:
            self.submit(
                {'x': 2},
                sequence=first[0]['sequence_id'],
                sequence_index=first[0]['sequence_index'],
            )

        self.assertIn(
            'Cannot add shots to sequence %s: ' % first[0]['sequence_id'],
            str(raised.exception),
        )
        self.assertEqual(len(self.app.batches), 1, 'the refused batch was not queued')

    def test_a_sequence_runmanager_has_no_record_of_is_refused(self):
        # Refused rather than started afresh, which would split the session
        # quietly in two; the caller stops on this message.
        with self.assertRaises(Exception) as raised:
            self.submit({'x': 1}, sequence='20260923T101112_experiment')

        self.assertIn(
            'Cannot add shots to sequence 20260923T101112_experiment: ',
            str(raised.exception),
        )
        self.assertEqual(self.app.batches, [], 'nothing reached the queue')

    def test_a_submission_that_raises_has_queued_nothing_at_all(self):
        # The window is the operator's throughout, and the labscript file can
        # be cleared from it at any time. A caller told the submission failed
        # must not have shots running under identifiers it was never given.
        self.app.labscript_file = ''

        with self.assertRaises(Exception):
            self.submit({'x': 1}, {'x': 2}, {'x': 3})

        self.assertEqual(self.app.batches, [])
        self.assertEqual(self.app.queue_manager.get_queue_paths(), [self.anchor])

    def test_the_window_is_left_holding_the_last_shot_submitted(self):
        # Whoever is watching has to be able to see what is running, so the
        # globals really are set and really are left set. What is left is one
        # shot's worth of them and not every entry's at once.
        self.submit({'x': 1, 'y': 8}, {'x': 2, 'y': 9})

        self.assertEqual(
            self.expressions(), {'x': '2 # metres', 'y': '9', 'depth': '4'}
        )

    def test_a_batch_is_evaluated_without_touching_the_window_per_entry(self):
        # BLACS's shot exchange is answered on the thread this command runs
        # on, and each change to the window starts a preparse. A batch changes
        # the window once and waits on at most one preparse, whatever its size.
        with mock.patch.object(
            self.app, 'globals_changed', wraps=self.app.globals_changed
        ) as changed, mock.patch.object(
            self.app,
            'wait_until_preparse_complete',
            wraps=self.app.wait_until_preparse_complete,
        ) as waited:
            self.submit({'x': 1}, {'x': 2}, {'x': 3})

        self.assertEqual(changed.call_count, 1)
        self.assertLessEqual(waited.call_count, 1)

    def test_entries_that_name_different_globals_are_refused(self):
        with self.assertRaises(Exception) as raised:
            self.submit({'x': 1}, {'y': 9})

        self.assertIn('name different globals', str(raised.exception))
        self.assertEqual(self.app.batches, [], 'nothing reached the queue')
        self.assertEqual(
            self.expressions(),
            {'x': '0 # metres', 'y': '2*3', 'depth': '4'},
            'and no global was set',
        )

    def test_nothing_is_submitted_when_a_later_entry_would_expand(self):
        # A scan left on a global the entries do not name can make an entry
        # more than one shot. The entries before it were fine and are still
        # not submitted, because a call that refuses leaves nothing behind for
        # the caller to hear about later.
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

    def test_a_shot_is_named_after_the_globals_written_into_it(self):
        # A filename prefix can be written in terms of a global, and the
        # operator's shuffle randomises the order a scan runmanager expanded
        # itself is made in. A batch was named shot by shot and is answered by
        # position, so shuffling it would name each file after one entry and
        # write another entry's globals into it.
        self.app.exp_config.filename_prefix_format = '{globals[x]}_{script_basename}'
        self.app.ui.pushButton_shuffle.checkState = (
            lambda: main_module.QtCore.Qt.Checked
        )
        # With nothing queued there is no sequence to join, so the filenames
        # are the ones the batch makes rather than ones numbered after a shot.
        self.app.queue_manager.delete_rows(['already-queued'])
        patcher = mock.patch.object(
            runmanager.random, 'shuffle', lambda sequence: sequence.reverse()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        descriptors = self.submit({'x': 1}, {'x': 2}, {'x': 3})

        self.assertEqual(
            [
                os.path.basename(descriptor['path']).split('_')[0]
                for descriptor in descriptors
            ],
            ['1', '2', '3'],
        )
        self.assertEqual(
            [record['frozen_globals']['x'] for record in self.queued()],
            ['1 # metres', '2 # metres', '3 # metres'],
        )

class ShotStatusTests(RemoteCommandTestCase):
    """Whether a shot that was submitted can still produce a result.

    A caller waiting on the results of shots it submitted needs to know when
    to stop waiting for one. ``pending`` answers exactly that: it is true
    while the queue would still hand the row over, or BLACS has a cancelled
    row it can still complete, and false once the row is waiting on an
    operator instead.

    A row is not only its own state. Only the head of the queue is ever
    offered, so a row the queue will not hand over holds up every row behind
    it, and a shot that will never run has to say so wherever it is sitting.

    Nothing is consumed by asking. The same question can be asked as often as
    the caller likes, about shots that finished long ago, and the queue is no
    different afterwards.
    """

    # Every state a queue row can be in: whether a shot in it can still
    # produce a result, and whether a row in it holds up the rows behind it.
    #
    # offer_next() hands over a waiting row, a row already marked running --
    # which is the reclaim -- and a failed one, which is the retry. It refuses
    # a rejected row, because offering it again would only be refused again,
    # and a cancelled one, which the operator has said is not to be sent;
    # claim_next_for_compile() refuses a compile_failed row, which can never
    # compile however often it is asked for.
    #
    # Of the three refusals only the cancelled row clears itself: the queue
    # drops it at the next request from BLACS, so the shots behind it are
    # waiting their turn rather than waiting on somebody. It was deleted while
    # BLACS had it, and BLACS can still complete it, so it is pending. The
    # other two stay where they are until an operator deletes them.
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
        for item in self.app.queue_manager.controller._items:
            if item['shot_id'] == shot_id:
                item['state'] = state

    def queue(self, *rows):
        """A queue holding exactly these ``(shot_id, state)`` rows, in order.

        Replacing what is there rather than adding to it, because what is in
        front of a row is half of its answer: the same row at the head of one
        queue and behind a held one in another is being asked two different
        questions.
        """
        self.app.queue_manager.restore_state({})
        for shot_id, state in rows:
            self.enqueue(shot_id, state=state)

    def test_every_state_a_row_can_be_in_has_an_answer(self):
        # Derived rather than listed, so that a state added to BLACS_STATES
        # arrives here without an answer instead of quietly defaulting to one.
        self.assertEqual(
            set(self.EXPECTED),
            set(BLACS_STATES) | {'', 'compile_failed'},
            'a new row state needs deciding here: can a shot in it still '
            'produce a result, or is it waiting on an operator?',
        )

    def test_pending_is_whether_the_shot_can_still_produce_a_result(self):
        for state, (pending, _) in sorted(self.EXPECTED.items()):
            with self.subTest(state=state):
                self.queue((state or 'waiting', state))
                answer = self.request('shot_status', [state or 'waiting'])
                self.assertEqual(answer[state or 'waiting']['pending'], pending)

    def test_a_row_behind_a_held_one_says_it_is_blocked(self):
        # The row in front is the whole of the reason, so the answer is the
        # same whatever the waiting row itself is doing: nothing behind a shot
        # the queue will not hand over can be handed over either, and a caller
        # polling for its result would otherwise wait for ever on a queue that
        # is not moving.
        for state, (_, holds_up) in sorted(self.EXPECTED.items()):
            with self.subTest(state=state):
                self.queue(('head', state), ('behind', ''))

                answer = self.request('shot_status', ['behind'])

                self.assertEqual(
                    answer['behind'],
                    {'pending': False, 'state': BLOCKED_SHOT_STATE}
                    if holds_up
                    else {'pending': True, 'state': ''},
                )

    def test_a_held_row_still_says_what_it_is(self):
        # Blocked is what a row behind one of these is; the row itself has a
        # reason, and that is what an operator has to act on.
        self.queue(('head', 'rejected'), ('behind', ''))

        answer = self.request('shot_status', ['head', 'behind'])

        self.assertEqual(answer['head']['state'], 'rejected')
        self.assertEqual(answer['behind']['state'], BLOCKED_SHOT_STATE)

    def test_deleting_the_row_in_front_lets_the_ones_behind_run_again(self):
        # Blocked says the queue is not moving, not that the shot is spoiled:
        # the operator clears the row that stopped it and the work behind it
        # is waiting its turn again.
        self.queue(('head', 'rejected'), ('behind', ''))
        self.assertEqual(
            self.request('shot_status', ['behind'])['behind']['state'],
            BLOCKED_SHOT_STATE,
            'which is what the caller polling it is told meanwhile',
        )

        self.app.queue_manager.delete_rows(['head'])

        self.assertEqual(
            self.request('shot_status', ['behind'])['behind'],
            {'pending': True, 'state': ''},
            'the answer is read off the queue as it stands, so a row asked '
            'about while it was stuck is not left carrying that',
        )

    def test_a_state_no_refusal_names_is_still_pending(self):
        # What the queue does with a state it has no refusal for is offer the
        # row, so that is what is answered about it. Listing the states that
        # are pending instead would make every state added later read as a
        # shot that will never run, which is the answer that makes a caller
        # give up on a shot the apparatus is about to take.
        self.queue(('novel', 'some-state-added-later'), ('behind', ''))

        answer = self.request('shot_status', ['novel', 'behind'])

        self.assertTrue(answer['novel']['pending'])
        self.assertTrue(answer['behind']['pending'])

    def test_the_state_is_passed_through_for_somebody_reading_it(self):
        self.enqueue('one', state='failed')

        self.assertEqual(self.request('shot_status', ['one'])['one']['state'], 'failed')

    def test_a_shot_the_queue_no_longer_has_is_not_pending(self):
        answer = self.request('shot_status', ['never-heard-of-it'])

        self.assertEqual(
            answer['never-heard-of-it'],
            {'pending': False, 'state': 'unknown'},
            'nothing more will happen to a shot with no row, and no row state '
            'describes it -- least of all the empty one, which means waiting',
        )

    def test_every_id_asked_about_is_answered(self):
        self.enqueue('here')

        answer = self.request('shot_status', ['here', 'gone', 'here'])

        self.assertEqual(sorted(answer), ['gone', 'here'])

    def test_asking_leaves_the_queue_as_it_was(self):
        # Deep, because the rows themselves are half of the claim: a list of
        # the same dicts compares each row to itself and would pass with the
        # answer written back into every row it was read from.
        self.enqueue('one')
        self.enqueue('two')
        before = copy.deepcopy(self.app.queue_manager.controller._items)

        self.request('shot_status', ['one', 'two', 'three'])

        self.assertEqual(self.app.queue_manager.controller._items, before)

    def test_every_answer_the_state_can_carry_is_named_to_the_caller(self):
        # Derived rather than listed, and the state's own name rather than any
        # of the words around it: the client docstring is where a caller reads
        # what an answer can say, so a state runmanager answers with and the
        # docstring does not name is one the caller has to guess at --
        # including whether a shot in it is still coming.
        docstring = runmanager.remote.Client.shot_status.__doc__
        for state in (BLOCKED_SHOT_STATE, UNKNOWN_SHOT_STATE):
            with self.subTest(state=state):
                self.assertIn(state, docstring)


class EmptyQueueTests(RemoteCommandTestCase):
    """Empty queue, in Abort's place, backs out of the work that is waiting.

    A batch is queued as rows the moment it is submitted, so emptying the
    queue takes all of it, including a shot still compiling, whose file goes
    when its compile finishes.
    """

    def make_app(self):
        return SubmittingApp(self.directory)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        # next_sequence_index takes a zlock and keeps a counter on disk. Which
        # index it hands out does not matter here.
        patcher = mock.patch.object(
            runmanager, 'next_sequence_index', lambda *a, **k: 7
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        # Cleanups run in reverse, so this releases the blocked compile before
        # the queue is asked to shut down.
        self.addCleanup(self.app.compiling.set)
        runmanager.new_global(self.app.globals_file, 'group', 'x')
        runmanager.set_value(self.app.globals_file, 'group', 'x', '0')

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
        descriptors = self.request('submit_shots', [{'x': 1}, {'x': 2}])
        self.assertTrue(started.wait(5), 'the first shot is compiling')

        self.request('abort')
        self.app.compiling.set()

        self.assertTrue(written.wait(5), 'and its compile finished')
        for _ in range(500):
            if not os.path.exists(descriptors[0]['path']):
                break
            time.sleep(0.01)
        self.assertEqual(self.app.queue_manager.get_queue_paths(), [])
        self.assertEqual(compiled, [descriptors[0]['path']], 'the second never compiled')
        self.assertFalse(os.path.exists(descriptors[0]['path']), 'and no file is left')


class SubmissionAnchorTests(RemoteCommandTestCase):
    """The shot each submission mode numbers its batch after.

    A mode that adds to a sequence looks in two places for one, in the order
    that mode calls for, and starts a sequence of its own when neither has
    anything. Nothing is refused: a runmanager that has queued nothing and
    sent nothing has no last sequence at all, and starting one is the only
    thing "add shots to the last sequence" can mean there.

    The queue empties on its own -- BLACS takes the last shot on a thread of
    its own -- so the shot a mode was chosen against can be gone by the time
    the batch is made. The shot last sent to BLACS is then the one the
    operator was looking at in the queue, which is why it is what "add shots
    to last sequence" falls back to.
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
        patcher = mock.patch.object(
            runmanager, 'next_sequence_index', lambda *a, **k: 12
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.addCleanup(self.app.compiling.set)
        runmanager.new_global(self.app.globals_file, 'group', 'x')
        runmanager.set_value(self.app.globals_file, 'group', 'x', '0')

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

    def engage(self, submission_mode):
        return self.app.compile_and_queue_shots(
            submission_mode, True, False, self.app.expand_pending_shots()
        )

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
        [compiling] = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)
        self.assertTrue(self.wait_until(self.app.queue_manager.get_compiling_paths))
        runmanager.set_scan(self.app.globals_file, 'group', 'x', '[1, 2]')
        runmanager.set_scan_enabled(self.app.globals_file, 'group', 'x', True)
        runmanager.set_expansion(self.app.globals_file, 'group', 'x', 'outer')

        replacement = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS_CLEAR_QUEUE)

        self.assertEqual([record['run_no'] for record in replacement], [0, 2])
        self.assertNotIn(compiling['path'], [record['path'] for record in replacement])

    def test_a_join_after_a_replacement_numbers_after_both_batches(self):
        self.enqueue('experiment_000.h5', run_no=0, n_runs=1)
        self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)
        self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS_CLEAR_QUEUE)
        self.app.compiling.set()
        self.assertTrue(
            self.wait_until(lambda: not self.app.queue_manager.get_compiling_paths())
        )

        later = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)

        self.assertEqual([record['run_no'] for record in later], [2])

    def test_adding_twice_before_the_first_compiles_does_not_reuse_its_numbers(self):
        # The first batch's shot is still compiling, with no row and no file,
        # so the queue's last row is still the shot it was added to. The second
        # batch is numbered after the first all the same.
        self.enqueue('experiment_007.h5')

        first = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)
        second = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)

        self.assertEqual([record['run_no'] for record in first], [8])
        self.assertEqual([record['run_no'] for record in second], [9])
        self.assertNotEqual(first[0]['path'], second[0]['path'])

    def test_adding_to_the_last_sequence_carries_on_from_the_shot_blacs_has(self):
        # BLACS can take the last queued shot between the menu being drawn and
        # the item being clicked. The shot it was sent is the one the operator
        # was looking at, so that is the sequence the batch joins -- rather
        # than a new sequence written where an extension was asked for.
        sent = os.path.join(self.directory, 'experiment_007.h5')
        runmanager.make_single_run_file(sent, None, {}, self.SEQUENCE, 7, 8)
        self.app.queue_manager.set_last_sent_from_queue(sent)

        records = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)

        self.assertEqual(
            [record['sequence_attrs']['sequence_id'] for record in records],
            [self.SEQUENCE['sequence_id']],
        )

    def test_adding_to_the_last_sequence_reads_no_file_and_takes_no_lock(self):
        # Both would be on the GUI thread, and lyse can hold the file of the
        # shot BLACS has just run for as long as it likes.
        sent = os.path.join(self.directory, 'experiment_007.h5')
        self.app.queue_manager.set_last_sent_from_queue(sent, dict(self.SEQUENCE))

        with mock.patch.object(
            self.app, 'check_output_folder_update', side_effect=AssertionError
        ):
            records = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)

        self.assertFalse(os.path.exists(sent), 'there was no file to read')
        self.assertEqual(
            [record['sequence_attrs']['sequence_id'] for record in records],
            [self.SEQUENCE['sequence_id']],
        )

    def test_adding_to_the_last_sequence_joins_one_still_compiling(self):
        # "Last sequence" is the one submitted last, compiled or not: its rows
        # are in the queue from the moment it is submitted.
        self.enqueue('experiment_007.h5')
        engaged = self.engage(main_module.SUBMISSION_MODE_NEW_FOLDER)

        added = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)

        self.assertEqual(
            added[0]['sequence_attrs']['sequence_id'],
            engaged[0]['sequence_attrs']['sequence_id'],
        )

    def test_adding_to_nothing_at_all_starts_a_sequence(self):
        records = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)

        self.assertEqual(
            [record['sequence_attrs']['sequence_index'] for record in records],
            [12],
            'a runmanager with nothing queued and nothing ever sent has no '
            'last sequence, so this batch is the start of one',
        )

    def test_replacing_nothing_at_all_starts_a_sequence(self):
        records = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS_CLEAR_QUEUE)

        self.assertEqual(
            [record['sequence_attrs']['sequence_index'] for record in records],
            [12],
        )

    def test_the_queue_is_read_once_for_the_sequence_being_added_to(self):
        # The queue empties on its own between two reads of it: BLACS asks for
        # the last shot on the server thread while the batch is being made.
        # Whichever answer a submission acts on has to be the one it was
        # checked against -- a second read saying the queue is empty turned
        # "add shots to last sequence" into a new sequence in the default
        # folder, with nothing said about it.
        queued = self.enqueue('experiment_007.h5')
        answers = [queued]
        self.app.get_queue_append_filepath = (
            lambda: answers.pop(0) if answers else None
        )

        records = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS)

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

        records = self.engage(main_module.SUBMISSION_MODE_ADD_SHOTS_CLEAR_QUEUE)

        self.assertEqual(
            [record['sequence_attrs']['sequence_id'] for record in records],
            [self.SEQUENCE['sequence_id']],
        )
        self.assertEqual(
            self.app.queue_manager.get_queue_paths(),
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


class ShuffledEngageTests(RemoteCommandTestCase):
    """Engaging a scan with the shuffle button down.

    Shuffle decorrelates the order the shots run in from the order the scan
    was written in, so that whatever the apparatus does slowly does not line
    up with the parameter being scanned. A batch queued in the order it was
    expanded leaves the scan correlated with exactly the drift the button was
    pressed to separate it from, and the window says it was shuffled.

    The globals each shot is compiled from travel with it as far as the
    queue, and a filename prefix may be written in terms of a global. So a
    batch whose files and globals come apart is a shot named for one set of
    parameters and run with another, which is a result recorded against
    parameters that never ran.
    """

    def make_app(self):
        return SubmittingApp(self.directory)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        patcher = mock.patch.object(
            runmanager, 'next_sequence_index', lambda *a, **k: 3
        )
        patcher.start()
        self.addCleanup(patcher.stop)
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
        runmanager.set_value(self.app.globals_file, 'group', 'x', '0')
        runmanager.set_scan(self.app.globals_file, 'group', 'x', '[1, 2, 3]')
        runmanager.set_scan_enabled(self.app.globals_file, 'group', 'x', True)
        runmanager.set_expansion(self.app.globals_file, 'group', 'x', 'outer')
        self.app.exp_config.filename_prefix_format = '{globals[x]}_{script_basename}'
        self.app.ui.pushButton_shuffle.checkState = (
            lambda: main_module.QtCore.Qt.Checked
        )

    def engage(self):
        """Press Engage, and hand back the records that reached the queue."""
        self.app.wait_until_preparse_complete()
        self.app.on_engage_clicked()
        self.assertEqual(self.app.said, [], 'Engage put nothing in the output box')
        self.assertEqual(len(self.app.batches), 1)
        return self.app.batches[0]

    def test_a_shuffled_batch_reaches_the_queue_in_the_shuffled_order(self):
        records = self.engage()

        self.assertEqual(
            [record['frozen_globals']['x'] for record in records],
            ['3', '2', '1'],
            'the queue was built in the order the shuffle chose, not the '
            'order the scan was written in',
        )

    def test_a_shuffled_shot_is_named_after_the_globals_it_carries(self):
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
            'each file is named for the globals that are compiled into it',
        )


class PreparsingApp(FakeApp):
    """The application's preparse thread, with a preparse that fails."""

    preparse_globals_loop = RunManager.preparse_globals_loop
    wait_until_preparse_complete = RunManager.wait_until_preparse_complete

    def __init__(self):
        super().__init__()
        self.preparse_globals_required = queue.Queue()
        self.n_shots = 3

    def preparse_globals(self):
        raise RuntimeError('a globals file could not be read')


class PreparseFailureTests(RemoteCommandTestCase):
    def make_app(self):
        return PreparsingApp()

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
                target=lambda: answers.append(self.request('n_shots')), daemon=True
            )
            asker.start()
            asker.join(5)
            self.assertTrue(reported.wait(5), 'the failed preparse is still reported')

        self.assertEqual(answers, [3])


if __name__ == '__main__':
    unittest.main()

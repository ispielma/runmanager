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
import os
import shutil
import tempfile
import threading
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
    EMPTY_QUEUE_DEFAULT_LABSCRIPT,
    EMPTY_QUEUE_NOTHING,
    QueueManager,
    SUBMITTED_SHOT_STATE,
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
            lambda enabled: None,
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


class EmptyQueuePolicyTests(RemoteCommandTestCase):
    """What runmanager does when the queue runs out, asked from outside.

    A caller that submits shots and waits for their results needs this before
    it starts: under 'nothing' an empty queue produces nothing at all, so a
    caller waiting on a result that only a shot could produce waits forever.
    """

    def test_the_policy_in_force_is_what_is_answered(self):
        for policy in (EMPTY_QUEUE_NOTHING, EMPTY_QUEUE_DEFAULT_LABSCRIPT):
            with self.subTest(policy=policy):
                self.app.queue_manager.set_empty_queue_policy(policy)
                self.assertEqual(self.request('get_empty_queue_policy'), policy)


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
        'get_empty_queue_policy': (),
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


class ExpansionsBeingRewritten(dict):
    """The preparse thread's expansions, read while that thread is writing.

    ``previous_expansions`` belongs to the preparse thread, which assigns a
    guess into it per global as it works. Iterating it from the server thread
    is the read that breaks when a key arrives partway through, so this one
    grows by a key after its first item is handed over -- which is what makes
    the next step of a live iteration raise.
    """

    def items(self):
        iterator = iter(super().items())
        first = next(iterator)
        self['guessed_while_reading'] = 'outer'
        yield first
        yield from iterator


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
    would be written through, the preparse thread and everything it writes for
    a submission to read afterwards, and the abort button.

    The compile callback blocks until the test releases it, which is where a
    shot waits under eager compilation -- so no record reaches the queue while
    a submission is being made, and each entry sees the queue the next one
    would really see.
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
        self.previous_expansions = {}
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
            pushButton_abort=types.SimpleNamespace(setEnabled=lambda enabled: None),
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
            lambda enabled: None,
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
        self.previous_expansions = expansions
        self.axes_model.rebuild(expansions)


class SubmitShotsTests(RemoteCommandTestCase):
    """Submitting shots by naming the globals that differ between them.

    One entry is one shot, and the whole batch is made and submitted at once.
    Making it at once is what tells its shots apart: a run number and a
    filename are claimed by the batch being made, so entries submitted one at
    a time each find the same number free and write over each other.

    Each entry's shot is evaluated while the window holds that entry's
    globals and nothing else's. A global some other entry names is put back to
    the operator's own expression first, because a shot compiled with a value
    an earlier entry asked for is a result attributed to parameters it never
    ran with.

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

    def without_preparse(self, expansions):
        """Leave the window as a preparse that never completed leaves it.

        preparse_globals returns without touching either of these when it
        cannot read the active groups, so this is the state a submission can
        genuinely find the window in.
        """
        self.app.wait_until_preparse_complete = lambda: None
        self.app.previous_expansions = expansions

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
        self.assertEqual(self.app.get_queue_append_filepath(), self.anchor)

        second = self.submit({'x': 2}, sequence=first[0]['sequence_id'])

        self.assertNotEqual(first[0]['sequence_id'], self.SEQUENCE['sequence_id'])
        self.assertEqual(second[0]['sequence_id'], first[0]['sequence_id'])

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
        # The window is the operator's throughout, and clearing the labscript
        # file is one of the things they can do to it while a batch is being
        # submitted. Whatever the batch then fails on, a caller that was told
        # the submission failed must not have shots running under identifiers
        # it was never given and can neither poll nor cancel.
        reads = []

        def labscript_file():
            reads.append(None)
            return '' if len(reads) > 1 else self.app.labscript_file

        self.app.ui.lineEdit_labscript_file.text = labscript_file

        try:
            descriptors = self.submit({'x': 1}, {'x': 2}, {'x': 3})
        except Exception:
            self.assertEqual(self.app.batches, [])
        else:
            self.assertEqual(len(descriptors), 3)

    def test_a_global_an_entry_does_not_name_keeps_the_operators_value(self):
        # The globals are set in the window and left set, so setting one entry
        # on top of the last leaves the window holding the union of them all.
        # A shot compiled that way used a value some earlier entry asked for,
        # and a result recorded against parameters that never ran is worse
        # than no result.
        self.submit({'x': 1}, {'y': 9})

        self.assertEqual(
            [record['frozen_globals']['x'] for record in self.queued()],
            ['1 # metres', '0 # metres'],
            'the second entry names only y, so x runs at the value the '
            'operator gave it',
        )
        self.assertEqual(
            [record['frozen_globals']['y'] for record in self.queued()],
            ['2*3', '9'],
        )

    def test_the_window_is_left_holding_the_last_shot_submitted(self):
        # Whoever is watching has to be able to see what is running, so the
        # globals really are set and really are left set. What is left is one
        # shot's worth of them and not every entry's at once.
        self.submit({'x': 1}, {'y': 9})

        self.assertEqual(
            self.expressions(), {'x': '0 # metres', 'y': '9', 'depth': '4'}
        )

    def test_a_restored_global_keeps_its_expression_and_its_comment(self):
        # An expression is what the operator gave a global, and a number
        # frozen out of it stops following whatever it was written in terms
        # of. The comment beside it is theirs as well, and the window puts it
        # back on every expression written, so handing one back with its own
        # comment still attached returns it carrying two.
        self.define(width='depth * 2  # doubled')
        self.submit({'width': 1}, {'x': 5})

        self.assertEqual(
            self.expressions()['width'],
            'depth * 2  # doubled',
            'the entry that does not name it hands back what the operator '
            'wrote, not a number and not a second copy of the comment',
        )
        self.assertEqual(
            [record['frozen_globals']['width'] for record in self.queued()],
            ['1  # doubled', 'depth * 2  # doubled'],
        )

    def test_nothing_is_submitted_when_a_later_entry_would_expand(self):
        # A global with a scan enabled ignores the value the entry gave it, so
        # the shot would not use the parameters it was asked for. The entries
        # before it were fine and are still not submitted, because a call that
        # refuses leaves nothing behind for the caller to hear about later.
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

    def test_the_refusal_counts_the_shots_itself(self):
        # n_shots is None until a preparse sets it, and preparse_globals
        # returns without setting it when it cannot read the active groups. A
        # caller handed a formatting error instead of the refusal has nothing
        # to go and turn off.
        self.scan('y', '[1] * x')
        self.without_preparse({'x': '', 'y': 'outer', 'depth': ''})
        self.assertIsNone(self.app.n_shots)

        with self.assertRaises(Exception) as raised:
            self.submit({'x': 3})

        self.assertIn('produce 3', str(raised.exception))
        self.assertIn('y', str(raised.exception))

    def test_the_refusal_survives_the_expansions_being_written_to(self):
        # The refusal names the globals that expanded the entry, and the
        # preparse thread is free to be guessing another one meanwhile. A
        # caller told its scan is still on can turn it off; a caller told the
        # dictionary changed size cannot do anything with that at all.
        self.scan('y', '[1] * x')
        self.without_preparse(
            ExpansionsBeingRewritten({'width': 'outer', 'depth': '', 'y': 'outer'})
        )

        with self.assertRaises(Exception) as raised:
            self.submit({'x': 3})

        self.assertIn('Cannot submit', str(raised.exception))
        self.assertIn('width', str(raised.exception))

    def test_an_error_in_the_globals_is_refused_before_anything_is_submitted(self):
        self.define(broken='1/0')

        with self.assertRaises(Exception):
            self.submit({'x': 1})

        self.assertEqual(self.app.batches, [])

    def test_a_global_no_active_group_has_is_refused_before_anything_is_set(self):
        # The globals of a refused batch are left set, so a name that was
        # never going to work is worth finding before the first one is
        # written rather than after.
        with self.assertRaises(Exception) as raised:
            self.submit({'x': 1}, {'not_a_global': 2})

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

    def test_the_axes_are_read_after_the_preparse_has_rebuilt_them(self):
        # Setting a global queues a reparse on another thread, and only that
        # reparse rebuilds the axes the globals are expanded along. An axis
        # left over from before names something the globals no longer produce,
        # and expanding along it is an error raised partway through a batch.
        descriptors = self.submit({'x': 1}, {'x': 2})

        self.assertEqual(len(descriptors), 2)
        self.assertNotIn(AxesModel.STALE_AXIS, self.app.axes_model.names)

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
    to stop waiting for one. ``pending`` answers exactly that, and answers it
    as the queue itself would: it is true while the queue would still hand the
    row over, and false once the row is waiting on an operator instead.

    A row is not only its own state. Only the head of the queue is ever
    offered, so a row the queue will not hand over holds up every row behind
    it, and a shot that will never run has to say so wherever it is sitting.

    Nothing is consumed by asking. The same question can be asked as often as
    the caller likes, about shots that finished long ago, and the queue is no
    different afterwards.
    """

    # Every state a queue row can be in: whether the queue would still hand
    # that row over, and whether a row in it holds up the rows behind it.
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
    # waiting their turn rather than waiting on somebody. The other two stay
    # where they are until an operator deletes them.
    EXPECTED = {
        # state: (would be handed over, holds up the rows behind it)
        '': (True, False),
        'running': (True, False),
        'failed': (True, False),
        'rejected': (False, True),
        'cancelled': (False, False),
        'compile_failed': (False, True),
    }

    def enqueue(self, shot_id, state=''):
        self.app.queue_manager.enqueue(
            [{'path': '/tmp/%s.h5' % shot_id, 'shot_id': shot_id, 'compiled': True}]
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

    def test_pending_is_whether_the_queue_would_still_offer_the_row(self):
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
        for state in (
            BLOCKED_SHOT_STATE,
            SUBMITTED_SHOT_STATE,
            UNKNOWN_SHOT_STATE,
        ):
            with self.subTest(state=state):
                self.assertIn(state, docstring)


class AbortDuringSubmissionTests(RemoteCommandTestCase):
    """An abort the operator has pressed is not called off by a submission.

    Abort stops the batch being compiled and every batch already queued behind
    it. A submission landing while those are draining is work the operator has
    just said they do not want, and a remote caller is in no position to decide
    otherwise: it cannot see the window, and the operator cannot see it.

    It has to end by itself, though. Nothing holds an abort open once the
    batches it stopped have been let go of, so the next submission compiles
    without anybody having to press anything -- there is no Engage button a
    remote caller could press.
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
        self.app.queue_manager.enqueue(
            [
                {
                    'path': os.path.join(self.directory, 'experiment_007.h5'),
                    'compiled': True,
                    'run_no': 7,
                    'n_runs': 8,
                    'sequence_attrs': {
                        'script_basename': 'experiment',
                        'sequence_date': '2026-09-18',
                        'sequence_index': 7,
                        'sequence_id': '20260918T101112_experiment',
                    },
                }
            ]
        )
        # Where a batch is when Abort reaches it: the worker has it and is in
        # the middle of compiling its first shot. Waited on rather than slept
        # through, so that what the operator interrupts is settled.
        self.compiling_started = threading.Event()
        compile_run_file = self.app.queue_manager.compile_run_file_callback

        def note_compile_started(labscript_file, path):
            self.compiling_started.set()
            return compile_run_file(labscript_file, path)

        self.app.queue_manager.compile_run_file_callback = note_compile_started
        # The queue turns the Abort button off as it lets go of the last batch
        # it was holding, which is the moment to ask what it left behind.
        self.batches_finished = threading.Event()
        self.app.queue_manager.set_abort_enabled = self.note_abort_enabled

    def note_abort_enabled(self, enabled):
        if not enabled:
            self.batches_finished.set()

    def submit(self, *entries):
        return self.request('submit_shots', [dict(entry) for entry in entries])

    def status(self, descriptors):
        shot_ids = [descriptor['shot_id'] for descriptor in descriptors]
        return self.app.queue_manager.get_shot_statuses(shot_ids)

    def test_a_submission_does_not_call_off_an_abort_that_is_in_force(self):
        first = self.submit({'x': 1})
        self.assertTrue(
            self.compiling_started.wait(5), 'the worker has the first batch'
        )
        self.app.on_abort_clicked()
        stopped = self.submit({'x': 2})
        self.app.compiling.set()

        self.assertTrue(self.batches_finished.wait(5), 'the batches were let go')
        self.assertEqual(
            [status['state'] for status in self.status(stopped).values()],
            [UNKNOWN_SHOT_STATE],
            'the batch submitted during the abort was stopped by it: no row '
            'was ever made for its shot, and nothing further will happen to it',
        )
        self.assertEqual(
            [status['pending'] for status in self.status(first).values()],
            [True],
            'and the shot that was already compiling when Abort was pressed '
            'is not taken back out of the queue',
        )

    def test_an_abort_with_nothing_to_stop_stops_nothing(self):
        # Abort is reachable from a remote caller at any time, including while
        # runmanager is idle and the operator's own Abort button is greyed
        # out. An abort kept over work that has not been submitted yet would
        # stop the next batch to arrive, whoever sent it and however long
        # afterwards.
        self.app.compiling.set()
        self.app.on_abort_clicked()

        carries_on = self.submit({'x': 1})

        self.assertTrue(self.batches_finished.wait(5))
        self.assertEqual(
            [status['pending'] for status in self.status(carries_on).values()],
            [True],
            'there was nothing to stop, so nothing was stopped',
        )

    def test_the_abort_ends_with_the_batches_it_stopped(self):
        # An abort that outlived the work it stopped would refuse every later
        # submission, with nothing in the window saying why.
        self.submit({'x': 1})
        self.assertTrue(self.compiling_started.wait(5))
        self.app.on_abort_clicked()
        self.submit({'x': 2})
        self.app.compiling.set()
        self.assertTrue(self.batches_finished.wait(5))
        self.batches_finished.clear()

        carries_on = self.submit({'x': 3})

        self.assertTrue(self.batches_finished.wait(5))
        self.assertFalse(
            self.app.queue_manager.compilation_aborted.is_set(),
            'the abort is over',
        )
        self.assertEqual(
            [status['pending'] for status in self.status(carries_on).values()],
            [True],
            'and the next batch compiles and queues as though nothing had '
            'happened',
        )


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
            [],
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


if __name__ == '__main__':
    unittest.main()

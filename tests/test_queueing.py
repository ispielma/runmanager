"""Behavioural tests for the runmanager-owned shot queue.

These exercise QueueController and the queue widget directly, and the exchange
protocol in runmanager.__main__ through its methods, called against a stand-in
for the application rather than a running one.
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

# h5_lock must be imported before h5py is, by anything in the process, and
# it is what runmanager imports h5py through. Naming it here rather than
# relying on runmanager below, so that this file can be run on its own.
import labscript_utils.h5_lock  # noqa: F401
import h5py
import runmanager
from qtutils.qt.QtCore import Qt
from qtutils.qt.QtWidgets import QApplication

from labscript_utils.labconfig import load_appconfig, save_appconfig
# fixtures stubs the splash and does the guarded import of the
# application, once, for every test module. Importing
# runmanager.__main__ here instead would show the startup banner.
from fixtures import (
    RunManager,
    labconfig,
    main_module,
    serve_lyse,
    submit_to_lyse,
    wait_for,
)
from runmanager.client import PROVIDER_PAUSED, PROVIDER_PENDING
from runmanager.queueing import (
    COMPILE_MODE_EAGER,
    COMPILE_MODE_LAZY,
    EMPTY_QUEUE_DEFAULT_LABSCRIPT,
    EMPTY_QUEUE_NOTHING,
    PROVIDER_NONE,
    PROVIDER_SHOT,
    ROW_BACKGROUNDS,
    TINTED_ROW_FOREGROUND,
    UNKNOWN_SHOT_STATE,
    QueueController,
    QueueManager,
    RunmanagerQueueWidget,
)


def queued_shot(path, **kwargs):
    item = {'path': path, 'compiled': True}
    item.update(kwargs)
    return item


class QueueIdentityTests(unittest.TestCase):
    def test_queued_shots_have_distinct_ids_that_survive_save_and_restore(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')])
        state = controller.export_state()
        shot_ids = [item['shot_id'] for item in state['items']]
        self.assertTrue(all(shot_ids), 'every queued shot needs an id')
        self.assertEqual(len(set(shot_ids)), 2, 'ids must distinguish rows')

        restored = QueueController()
        restored.restore_state(state)
        self.assertEqual(
            [item['shot_id'] for item in restored.export_state()['items']], shot_ids
        )

    def test_running_and_failing_a_shot_does_not_change_the_saved_queue(self):
        # What this session made of a row is not part of the queue: a saved
        # queue is the work still to do. Were it included, runmanager would
        # offer to save the configuration again after every single shot.
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        before = controller.export_state()
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'failed', 'Device error')
        self.assertEqual(controller.export_state(), before)

    def test_a_restored_record_without_an_id_is_given_one_and_keeps_the_rest(self):
        # A queue saved before shot ids existed, or with a setting since
        # retired, still has to open and its shots still have to be offerable:
        # an id is minted on the way in and saved with the row from then on.
        legacy = {
            'path': '/tmp/shot_a.h5',
            'labscript_file': '/tmp/experiment.py',
            'compile_mode': 'lazy',
            'compiled': True,
            'frozen_globals': {'x': '1', 'y': 'linspace(0, 1, 3)'},
            'run_no': 2,
            'n_runs': 5,
        }
        controller = QueueController()
        controller.restore_state({'failure_policy': 'drop', 'items': [legacy]})

        offered = controller.offer_next()

        self.assertTrue(offered['shot_id'], 'a restored shot can be offered')
        self.assertEqual(offered['path'], os.path.abspath('/tmp/shot_a.h5'))
        self.assertEqual(
            offered['labscript_file'], os.path.abspath('/tmp/experiment.py')
        )
        self.assertEqual(offered['compile_mode'], 'lazy')
        self.assertEqual(offered['frozen_globals'], legacy['frozen_globals'])
        self.assertEqual((offered['run_no'], offered['n_runs']), (2, 5))
        self.assertEqual(
            controller.export_state()['items'][0]['shot_id'],
            offered['shot_id'],
            'and the id it was given is stable from then on',
        )


class SavedQueueValueTests(unittest.TestCase):
    """A queue can be saved whatever its shots' values came from."""

    def test_a_queue_whose_sequence_came_off_a_shot_file_can_be_saved(self):
        # The ordinary path between one submission and the next: the queue is
        # empty, so the sequence the batch is added to is read back out of the
        # shot file. The app config holds only strings, numbers and booleans.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        attrs = {
            'script_basename': 'experiment',
            'sequence_date': '2026-09-18',
            'sequence_index': 11,
            'sequence_id': '20260918T101112_experiment',
        }
        shot = os.path.join(directory, 'experiment_00.h5')
        runmanager.make_single_run_file(shot, None, {}, attrs, 0, 1)
        controller = QueueController()
        controller.enqueue(
            [queued_shot(shot, sequence_attrs=runmanager.get_sequence_attrs(shot))]
        )

        path = os.path.join(directory, 'runmanager.toml')
        save_appconfig(
            path, {'runmanager_state': {'queue_state': controller.export_state()}}
        )
        saved = load_appconfig(path)['runmanager_state']['queue_state']

        self.assertEqual(saved['items'][0]['sequence_attrs'], attrs)


class QueuePauseTests(unittest.TestCase):
    """Pause is this runmanager's policy on its own queue."""

    def test_a_saved_queue_with_no_pause_state_loads_unpaused(self):
        # A configuration carrying no pause state -- one saved by a runmanager
        # without the pause control -- must not open with the queue silently
        # stopped.
        controller = QueueController()
        controller.set_paused(True)
        controller.restore_state({'items': [queued_shot('/tmp/shot_a.h5')]})
        self.assertFalse(controller.get_queue_state()['paused'])

    def test_a_paused_queue_offers_no_shot_until_it_is_resumed(self):
        # Pause says runmanager has nothing to offer, not that BLACS should
        # stop, and it leaves the row waiting rather than marking it running.
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        app.queue_manager.set_paused(True)

        self.assertEqual(app.offer_shot()['state'], PROVIDER_PAUSED)
        self.assertEqual(
            [row['state'] for row in app.queue_controller.get_queue_display_items()],
            [''],
        )

        app.queue_manager.set_paused(False)
        offered = app.offer_shot()

        self.assertEqual(offered['state'], PROVIDER_SHOT)
        self.assertEqual(offered['path'], os.path.abspath('/tmp/shot_a.h5'))


class QueueOfferTests(unittest.TestCase):
    def test_offered_shot_stays_at_the_head_of_the_queue(self):
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        offered = controller.offer_next()
        self.assertEqual(offered['path'], os.path.abspath('/tmp/shot_a.h5'))
        self.assertTrue(offered['shot_id'])
        rows = controller.get_queue_display_items()
        self.assertEqual(
            [row['path'] for row in rows],
            [os.path.abspath('/tmp/shot_a.h5'), os.path.abspath('/tmp/shot_b.h5')],
        )
        self.assertEqual(rows[0]['state'], 'running')
        self.assertEqual(rows[1]['state'], '')

    def test_failed_row_is_offered_again_under_the_same_id(self):
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'failed', 'Device error')

        retried = controller.offer_next()

        self.assertEqual(retried['shot_id'], offered['shot_id'])
        self.assertEqual(retried['path'], os.path.abspath('/tmp/shot_a.h5'))
        rows = controller.get_queue_display_items()
        self.assertEqual(rows[0]['state'], 'running', 'the retry runs like any shot')
        self.assertNotIn(
            'Device error', rows[0]['tooltip'], 'the old reason is not still shown'
        )

        controller.shot_finished(retried['shot_id'], 'failed', 'Device error again')

        rows = controller.get_queue_display_items()
        self.assertEqual(
            [row['path'] for row in rows],
            [os.path.abspath('/tmp/shot_a.h5'), os.path.abspath('/tmp/shot_b.h5')],
            'a retry that fails again keeps its row',
        )
        self.assertEqual(rows[0]['state'], 'failed')
        self.assertIn('Device error again', rows[0]['tooltip'])

    def test_head_that_is_not_compiled_yet_is_not_offered(self):
        controller = QueueController()
        controller.enqueue(
            [
                queued_shot('/tmp/shot_a.h5', compiled=False, compile_mode='lazy'),
                queued_shot('/tmp/shot_b.h5'),
            ]
        )
        self.assertIsNone(controller.offer_next())


class QueueOutcomeTests(unittest.TestCase):
    def test_completed_shot_leaves_the_queue_and_the_next_one_is_offered(self):
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'completed')
        self.assertEqual(
            [row['path'] for row in controller.get_queue_display_items()],
            [os.path.abspath('/tmp/shot_b.h5')],
        )
        self.assertEqual(
            controller.offer_next()['path'], os.path.abspath('/tmp/shot_b.h5')
        )

    def test_shot_that_did_not_complete_stays_queued_with_its_reason(self):
        for status in ('failed', 'aborted', 'rejected'):
            with self.subTest(status=status):
                controller = QueueController()
                controller.enqueue(
                    [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
                )
                offered = controller.offer_next()
                controller.shot_finished(offered['shot_id'], status, 'Device error')
                rows = controller.get_queue_display_items()
                self.assertEqual(
                    [row['path'] for row in rows],
                    [
                        os.path.abspath('/tmp/shot_a.h5'),
                        os.path.abspath('/tmp/shot_b.h5'),
                    ],
                    'the shot that did not run stays at the head of the queue',
                )
                self.assertEqual(
                    rows[0]['state'],
                    'rejected' if status == 'rejected' else 'failed',
                    'a rejected shot is held apart: it is the queue that has to '
                    'be put right, not the apparatus',
                )
                self.assertIn('Device error', rows[0]['tooltip'])
                self.assertEqual(
                    [item['shot_id'] for item in controller.export_state()['items']][0],
                    offered['shot_id'],
                    'the row keeps the id it was offered under',
                )


class RejectedShotTests(unittest.TestCase):
    """A shot BLACS could not read is the queue's problem, not the apparatus's.

    Nothing about the apparatus will change by asking again, so the row is held
    red at the head and is not offered again until an operator deletes it. BLACS
    keeps asking, gets nothing, and runs its own shot meanwhile.
    """

    def rejected_queue(self):
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        offered = controller.offer_next()
        controller.shot_finished(
            offered['shot_id'], 'rejected', 'H5 file not accessible to Control PC'
        )
        return controller, offered

    def test_a_rejected_shot_is_not_offered_again(self):
        controller, _ = self.rejected_queue()

        self.assertIsNone(
            controller.offer_next(),
            'offering it again would only be refused again, once per request',
        )
        self.assertEqual(
            [row['path'] for row in controller.get_queue_display_items()],
            [os.path.abspath('/tmp/shot_a.h5'), os.path.abspath('/tmp/shot_b.h5')],
            'and it holds its place rather than letting the queue past it',
        )

    def test_deleting_it_lets_the_queue_go_on(self):
        controller, _ = self.rejected_queue()
        rows = controller.get_queue_display_items()

        controller.delete_rows([rows[0]['shot_id']])

        offered = controller.offer_next()
        self.assertEqual(offered['path'], os.path.abspath('/tmp/shot_b.h5'))


ANALYSED_MARKER = '/runmanager-tests/marker.h5'


class FakeRunManager(object):
    """Runmanager's exchange over only the surface of it that it uses.

    RunManager.__init__ builds the whole application, so the methods here are
    RunManager's own over a real QueueManager, with compiling and default-shot
    production stood in. A completed shot goes to a real lyse.
    """

    queue_exchange = RunManager.queue_exchange
    apply_shot_outcome = RunManager.apply_shot_outcome
    offer_shot = RunManager.offer_shot
    get_queue_append_filepath = RunManager.get_queue_append_filepath
    get_last_sent_from_queue_filepath = RunManager.get_last_sent_from_queue_filepath
    get_submission_anchor = RunManager.get_submission_anchor
    reindex_run_file_infos = RunManager.reindex_run_file_infos
    make_h5_files = RunManager.make_h5_files
    prepare_queue_shot = RunManager.prepare_queue_shot
    get_sequence_attrs_to_extend = RunManager.get_sequence_attrs_to_extend

    # make_h5_files reads these. There is no line edit to keep the output
    # folder up to date, and a batch added to a sequence is written to the
    # folder of the shot it is added to anyway.
    exp_config = None
    previous_default_output_folder = None

    def check_output_folder_update(self):
        pass

    def __init__(self, testcase, default_shot_file=None, compiles=True):
        hold_qapplication()
        self.said = []
        self.output_box = types.SimpleNamespace(
            output=lambda text, red=False: self.said.append(text)
        )
        # What the compiler does: True as though the shot compiled, False as it
        # reports a bad labscript file, or an exception for a failure to get
        # that far. Recorded so a test can say how many times it was asked.
        self.compiles = compiles
        self.compiled = []
        self.queue_controller = QueueController()
        self.queue_manager = QueueManager(
            self.queue_controller,
            lambda item, default_globals: None,
            self.compile_run_file,
            lambda path: None,
            self.output_box.output,
        )
        self.analysed = queue.Queue()
        lyse_app = types.SimpleNamespace(
            filebox=types.SimpleNamespace(incoming_queue=self.analysed)
        )
        lyse = serve_lyse(testcase, lyse_app)
        self.analysis_submission = submit_to_lyse(testcase, lyse.port)
        self.default_shot_file = default_shot_file
        self.default_shots_taken = 0

    def compile_run_file(self, labscript_file, path):
        self.compiled.append(path)
        if isinstance(self.compiles, Exception):
            raise self.compiles
        return self.compiles

    def analysed_paths(self):
        """The paths lyse has received, once all handed over so far have gone."""
        # Handed over last, so it reaches lyse after everything before it:
        self.analysis_submission.notify_shot_complete(ANALYSED_MARKER)
        paths = []

        def marker_arrived():
            while not self.analysed.empty():
                path = self.analysed.get()
                if path == ANALYSED_MARKER:
                    return True
                paths.append(path)
            return False

        wait_for(marker_arrived)
        return paths

    def take_default_shot(self, labscript_file):
        self.default_shots_taken += 1
        if self.default_shot_file is None:
            return None
        return {'path': self.default_shot_file, 'compiled': True, 'default_shot': True}

    def discard_default_shot(self):
        self.default_shot_file = None


class DefaultShotTests(unittest.TestCase):
    """A shot runmanager produced itself is an ordinary queue row.

    It is visible while it runs, retried when it fails and analysed when it
    completes, by the rules the queue already has.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.labscript_file = os.path.join(self.directory, 'default.py')
        open(self.labscript_file, 'w').close()
        self.default_shot = os.path.join(self.directory, 'default_shot_0.h5')

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def make_runmanager(self, default_shot_file=None):
        app = FakeRunManager(self, default_shot_file=default_shot_file)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.set_empty_queue_policy(EMPTY_QUEUE_DEFAULT_LABSCRIPT)
        app.queue_manager.set_default_labscript_file(self.labscript_file)
        return app

    def rows(self, app):
        return app.queue_controller.get_queue_display_items()

    def test_a_default_shot_is_offered_as_a_row_of_the_queue(self):
        app = self.make_runmanager(default_shot_file=self.default_shot)

        response = app.offer_shot()

        self.assertEqual(response['state'], PROVIDER_SHOT)
        self.assertTrue(response['shot_id'])
        self.assertTrue(response['path'].endswith('default_shot_0.h5'))
        rows = self.rows(app)
        self.assertEqual(
            [row['path'] for row in rows],
            [self.default_shot],
            'the shot runmanager is running is visible in its queue',
        )
        self.assertEqual(rows[0]['state'], 'running')

    def test_a_completed_default_shot_leaves_the_queue_and_goes_for_analysis(self):
        app = self.make_runmanager(default_shot_file=self.default_shot)
        offered = app.offer_shot()

        response = app.queue_exchange(
            outcome={
                'shot_id': offered['shot_id'],
                'status': 'completed',
                'path': offered['path'],
            },
            request_shot=False,
        )

        self.assertEqual(response['state'], PROVIDER_NONE)
        self.assertEqual(self.rows(app), [], 'finished work leaves the queue')
        self.assertEqual(
            app.analysed_paths(),
            [offered['path']],
            'work runmanager ran on its own behalf is still analysed',
        )

    def test_a_default_shot_that_failed_stays_red_and_holds_back_the_next_one(self):
        app = self.make_runmanager(default_shot_file=self.default_shot)
        offered = app.offer_shot()

        app.queue_exchange(
            outcome={
                'shot_id': offered['shot_id'],
                'status': 'failed',
                'path': offered['path'],
            },
            request_shot=False,
        )

        rows = self.rows(app)
        self.assertEqual([row['path'] for row in rows], [self.default_shot])
        self.assertEqual(rows[0]['state'], 'failed')
        self.assertEqual(
            app.analysed_paths(), [], 'a shot that did not run is not analysed'
        )

        taken_before = app.default_shots_taken
        retried = app.offer_shot()

        self.assertEqual(
            retried['shot_id'],
            offered['shot_id'],
            'the same shot is offered again, not a fresh default',
        )
        self.assertEqual(
            app.default_shots_taken,
            taken_before,
            'no new default shot is produced while one still needs attention',
        )

    def test_no_default_shot_is_produced_while_the_queue_holds_work(self):
        # The empty-queue policy is for an empty queue. A head BLACS never
        # confirmed is work to hand out again, so no default shot is made to
        # stack up behind it.
        app = self.make_runmanager(default_shot_file=self.default_shot)
        app.queue_manager.enqueue([queued_shot(os.path.join(self.directory, 'shot_a.h5'))])
        offered = app.offer_shot()

        response = app.offer_shot()

        self.assertEqual(response['state'], PROVIDER_SHOT)
        self.assertEqual(response['shot_id'], offered['shot_id'])
        self.assertEqual(app.default_shots_taken, 0)
        self.assertEqual(
            [row['path'] for row in self.rows(app)],
            [os.path.join(self.directory, 'shot_a.h5')],
        )

    def test_a_default_shot_is_not_saved_with_the_queue(self):
        # Its globals were read when it was produced, so it must not be handed
        # to BLACS in a later session as though they were current -- which is
        # exactly what restoring the row would do.
        app = self.make_runmanager(default_shot_file=self.default_shot)
        engaged = os.path.join(self.directory, 'shot_a.h5')
        app.offer_shot()
        app.queue_manager.enqueue([queued_shot(engaged)])

        saved = app.queue_controller.export_state()

        self.assertEqual(
            [item['path'] for item in saved['items']],
            [engaged],
            'the queue is saved as the work still to do',
        )
        restored = QueueController()
        restored.restore_state(saved)
        self.assertEqual(
            [row['path'] for row in restored.get_queue_display_items()], [engaged]
        )


class LazyCompileFailureTests(unittest.TestCase):
    """A queued shot that cannot be compiled stays, red, with the reason.

    Dropping it would look like the queue draining normally. It is not compiled
    again by itself, or a shot that fails every time would be recompiled for ever.
    """

    def make_runmanager(self, compiles):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        app = FakeRunManager(self, compiles=compiles)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue(
            [
                {'path': os.path.join(self.directory, 'lazy_a.h5'),
                 'labscript_file': os.path.join(self.directory, 'e.py'),
                 'compile_mode': COMPILE_MODE_LAZY, 'compiled': False},
                queued_shot(os.path.join(self.directory, 'shot_b.h5')),
            ]
        )
        return app

    def ask_until_settled(self, app, requests=3):
        """Ask for a shot a few times, letting the compile thread finish."""
        responses = []
        for _ in range(requests):
            responses.append(app.offer_shot())
            for _ in range(100):
                if not app.queue_controller.get_compiling_paths():
                    break
                time.sleep(0.01)
            time.sleep(0.01)
        return responses

    def rows(self, app):
        return app.queue_controller.get_queue_display_items()

    def test_a_shot_that_cannot_be_compiled_stays_red_at_the_head_with_its_reason(self):
        for compiles, reason in (
            (False, 'Could not be compiled'),
            (RuntimeError('no such labscript file'), 'no such labscript file'),
        ):
            with self.subTest(compiles=repr(compiles)):
                app = self.make_runmanager(compiles=compiles)

                self.ask_until_settled(app)

                rows = self.rows(app)
                self.assertEqual(
                    [os.path.basename(row['path']) for row in rows],
                    ['lazy_a.h5', 'shot_b.h5'],
                    'it is still there, and still first',
                )
                self.assertIn(
                    rows[0]['state'],
                    ROW_BACKGROUNDS,
                    'and coloured, whichever kind of failure it was',
                )
                self.assertIn(reason, rows[0]['tooltip'])

    def test_it_is_not_compiled_over_and_over(self):
        app = self.make_runmanager(compiles=False)

        self.ask_until_settled(app, requests=4)

        self.assertEqual(
            app.compiled,
            [os.path.join(self.directory, 'lazy_a.h5')],
            'a failed compile is not retried by itself, so it is only tried once',
        )

    def test_nothing_is_offered_while_it_is_held(self):
        app = self.make_runmanager(compiles=False)

        responses = self.ask_until_settled(app, requests=3)

        self.assertEqual(
            [response['state'] for response in responses],
            [PROVIDER_PENDING, PROVIDER_NONE, PROVIDER_NONE],
            'pending while it compiles, then nothing: the shot behind it waits '
            'rather than overtaking it',
        )

    def test_a_shot_that_compiles_is_offered_with_no_reason_on_it(self):
        app = self.make_runmanager(compiles=True)

        responses = self.ask_until_settled(app, requests=2)

        self.assertEqual(responses[-1]['state'], PROVIDER_SHOT)
        self.assertTrue(responses[-1]['path'].endswith('lazy_a.h5'))
        row = self.rows(app)[0]
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['tooltip'], row['path'], 'nothing to explain')


class CompileFailureIsNotAHandoverTests(unittest.TestCase):
    """A shot that never compiled has not been given to BLACS.

    The row is red and at the head, and holds the queue until it is deleted or
    compiled again, but it makes no claim that BLACS has it.
    """

    def test_a_replacement_submission_clears_it(self):
        controller = QueueController()
        controller.enqueue(
            [
                {
                    'path': '/tmp/lazy_a.h5',
                    'labscript_file': '/tmp/e.py',
                    'compile_mode': COMPILE_MODE_LAZY,
                    'compiled': False,
                },
                queued_shot('/tmp/shot_b.h5'),
            ]
        )
        item, _pending = controller.claim_next_for_compile()
        controller.finish_compile(item, False, 'Could not be compiled. See the output.')

        removed_paths, protected = controller.clear()

        self.assertEqual(
            sorted(os.path.basename(path) for path in removed_paths),
            ['lazy_a.h5', 'shot_b.h5'],
            'nothing here went to BLACS, so replacing the queue replaces all of '
            'it rather than stranding the batch behind a row BLACS never had',
        )
        self.assertEqual(protected, [])


class CompiledFlagOwnershipTests(unittest.TestCase):
    """The controller owns ``compiled`` for a row that is already in the queue.

    A compile that wrote it itself, outside the lock, would leave a gap in which
    an exchange could take the row and mark it running, and the finishing
    compile would then erase that state: the same file runs twice on hardware.
    These drive the two steps by hand, because a test that has to win a race to
    see the state between them reports nothing when it loses.
    """

    def test_a_row_is_not_offerable_until_the_controller_records_the_compile(self):
        controller = QueueController()
        manager = QueueManager(
            controller,
            lambda item, default_globals: None,
            lambda labscript_file, path: True,
            lambda path: None,
            lambda *args, **kwargs: None,
        )
        self.addCleanup(manager.shutdown)
        manager.enqueue(
            [
                {
                    'path': '/tmp/lazy.h5',
                    'labscript_file': '/tmp/e.py',
                    'compile_mode': COMPILE_MODE_LAZY,
                    'compiled': False,
                }
            ]
        )
        item, _pending = controller.claim_next_for_compile()
        self.assertIsNotNone(item, 'the row is there to be compiled')

        manager.compile_shot(item)

        self.assertIsNone(
            controller.offer_next(),
            'finish_compile has not taken the lock yet, so the row is not ready '
            'to hand over: offering it here is what lets the finishing compile '
            'erase the running state the offer set',
        )


class SubmittedShotTests(unittest.TestCase):
    """A batch bound for the queue has its rows from the moment it is submitted.

    A caller that submits and asks straight away is told about those rows, so it
    does not submit the same work again. The worker compiles the eager rows in
    order, and a row that will not compile holds the queue only once it is the
    head.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.compiles = True
        self.compiled = []
        # A compile the test can stop partway, to ask what is true while the
        # worker is in the middle of a batch. Held compiles are released
        # before the worker is shut down, so nothing is left waiting.
        self.compiling = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.hold_from = 1
        self.controller = QueueController()
        self.manager = QueueManager(
            self.controller,
            lambda item, default_globals: None,
            self.compile_run_file,
            lambda path: None,
            lambda *args, **kwargs: None,
        )
        self.addCleanup(self.manager.shutdown)
        self.addCleanup(self.release.set)

    def compile_run_file(self, labscript_file, path):
        self.compiled.append(path)
        self.compiling.set()
        if len(self.compiled) >= self.hold_from:
            self.release.wait(5)
        if isinstance(self.compiles, Exception):
            raise self.compiles
        return self.compiles

    def submit(self, count=1, send_to_BLACS=True):
        records = self.manager.compile_shots(
            [
                {
                    'path': os.path.join(self.directory, 'shot_%d.h5' % n),
                    'labscript_file': os.path.join(self.directory, 'e.py'),
                    'compile_mode': COMPILE_MODE_EAGER,
                    'compiled': False,
                }
                for n in range(count)
            ],
            send_to_BLACS,
            False,
        )
        return [record['shot_id'] for record in records]

    def status(self, shot_ids):
        return self.controller.get_shot_statuses(shot_ids)

    def wait_until(self, predicate):
        for _ in range(500):
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_a_shot_just_submitted_has_its_row_at_once(self):
        self.release.clear()
        [shot_id] = self.submit()

        self.assertEqual(len(self.controller.get_queue_paths()), 1)
        self.assertEqual(
            self.status([shot_id])[shot_id], {'pending': True, 'state': ''}
        )

    def test_a_shot_that_completed_before_its_batch_did_is_finished_with(self):
        # The shots ahead of a long batch are compiled, offered and completed
        # while the rest of it is still compiling. A shot that has produced
        # its result is finished with, whatever its batch is still doing.
        self.hold_from = 2
        self.release.clear()
        first, _second = self.submit(count=2)
        self.assertTrue(
            self.wait_until(lambda: len(self.compiled) == 2)
        )
        offered = self.manager.offer_next()
        self.manager.shot_finished(offered['shot_id'], 'completed')

        self.assertEqual(
            self.status([first])[first],
            {'pending': False, 'state': UNKNOWN_SHOT_STATE},
        )

    def test_a_shot_that_will_not_compile_is_marked_and_the_rest_still_compile(self):
        # A red row holds the queue only once it is the head, so the worker
        # goes on to the rows behind it rather than stopping there.
        self.compiles = False
        self.submit(count=3)

        self.assertTrue(self.wait_until(lambda: len(self.compiled) == 3))
        self.assertTrue(
            self.wait_until(
                lambda: [row['state'] for row in self.controller.get_queue_display_items()]
                == ['compile_failed'] * 3
            )
        )
        self.assertIsNone(self.manager.offer_next(), 'and the red head holds the queue')

    def test_a_batch_that_is_not_going_to_the_queue_is_not_taken_on(self):
        # Compiling a batch for a look at it in runviewer queues nothing, so
        # there is no queued shot for the queue to answer about.
        self.release.clear()
        [shot_id] = self.submit(send_to_BLACS=False)
        self.assertTrue(
            self.wait_until(self.compiling.is_set), 'the worker has the batch'
        )

        self.assertEqual(
            self.status([shot_id])[shot_id],
            {'pending': False, 'state': UNKNOWN_SHOT_STATE},
        )


class ContinuingSequenceAnchorTests(unittest.TestCase):
    """What "add shots to last sequence" carries on from.

    BLACS finds the queue empty within seconds of any batch finishing, so an
    empty queue cannot mean a new sequence: the shot last sent stays the anchor.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.app = FakeRunManager(self)
        self.addCleanup(self.app.queue_manager.shutdown)

    def enqueue(self, name):
        path = os.path.join(self.directory, name)
        open(path, 'w').close()
        self.app.queue_manager.enqueue([queued_shot(path)])
        return path

    def test_a_queue_with_work_in_it_is_what_is_continued(self):
        # BLACS running the first shot while the rest wait: the shot it was
        # sent is not the end of the sequence, and numbering the next batch
        # after that one would write it over the shots still waiting.
        sent = self.enqueue('experiment_00.h5')
        waiting = self.enqueue('experiment_01.h5')
        self.app.offer_shot()

        self.assertEqual(
            self.app.get_last_sent_from_queue_filepath(),
            sent,
            'BLACS has the first shot',
        )
        self.assertEqual(
            self.app.get_submission_anchor(main_module.SUBMISSION_MODE_ADD_SHOTS),
            waiting,
            'and the queue still ends where it ends',
        )

    def run_a_shot_and_let_blacs_ask_again(self, app, default_shot=None):
        """Submit one shot, run it to completion, and let BLACS ask for more.

        ``default_shot`` is the file the next default shot is made from: it stands
        for the one prepared off-thread once the queue empties, and without it the
        request finds nothing to offer.
        """
        path = os.path.join(self.directory, 'experiment_00.h5')
        open(path, 'w').close()
        app.queue_manager.enqueue([queued_shot(path)])
        offered = app.offer_shot()
        app.queue_exchange(
            outcome={
                'shot_id': offered['shot_id'],
                'status': 'completed',
                'path': offered['path'],
            },
            request_shot=False,
        )
        app.default_shot_file = default_shot
        return app.offer_shot()

    def test_a_default_shot_in_the_gap_does_not_become_what_is_continued(self):
        # Under the default-shot policy the gaps are filled by shots that belong
        # to no sequence and live in the daily default folder; continuing from
        # one would take the next submission with it.
        labscript_file = os.path.join(self.directory, 'default.py')
        open(labscript_file, 'w').close()
        default_shot = os.path.join(self.directory, 'default_shot_0.h5')
        open(default_shot, 'w').close()
        app = FakeRunManager(self, default_shot_file=default_shot)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.set_empty_queue_policy(EMPTY_QUEUE_DEFAULT_LABSCRIPT)
        app.queue_manager.set_default_labscript_file(labscript_file)

        submitted = os.path.join(self.directory, 'experiment_00.h5')
        filler = self.run_a_shot_and_let_blacs_ask_again(app, default_shot=default_shot)

        self.assertEqual(
            filler['path'],
            default_shot,
            'the gap was filled by a shot runmanager made itself, which is '
            'what BLACS is running while the caller works out what to send',
        )
        self.assertEqual(
            app.get_submission_anchor(main_module.SUBMISSION_MODE_ADD_SHOTS),
            submitted,
            'and the sequence still carries on from the submitted shot',
        )

    def test_the_anchor_survives_blacs_finding_the_queue_empty(self):
        # BLACS finds the queue empty within seconds of any batch finishing. If
        # that let go of the shot last sent, "add shots to last sequence" would
        # start a new sequence every time; under either policy it must not.
        submitted = os.path.join(self.directory, 'experiment_00.h5')
        for policy in (EMPTY_QUEUE_NOTHING, EMPTY_QUEUE_DEFAULT_LABSCRIPT):
            with self.subTest(policy=policy):
                app = FakeRunManager(self)
                self.addCleanup(app.queue_manager.shutdown)
                app.queue_manager.set_empty_queue_policy(policy)

                filler = self.run_a_shot_and_let_blacs_ask_again(app)

                self.assertEqual(
                    filler['state'], PROVIDER_NONE, 'nothing filled the gap'
                )
                self.assertEqual(
                    app.get_submission_anchor(main_module.SUBMISSION_MODE_ADD_SHOTS),
                    submitted,
                    'and the shot that ran is what the next submission '
                    'carries on from',
                )
                self.assertTrue(os.path.exists(submitted))


class ShotIdBeforeCompileTests(unittest.TestCase):
    """A shot has its identifier before anything writes its file.

    compile_shots settles it before the record is queued or compiled, so the id
    a file carries is the id of its row.
    """

    def test_a_record_is_compiled_with_the_id_its_row_will_have(self):
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        prepared = []
        app.queue_manager.prepare_run_file_callback = lambda item, default_globals: prepared.append(
            dict(item)
        )

        app.queue_manager.compile_shots(
            [
                {
                    'path': '/tmp/eager.h5',
                    'labscript_file': '/tmp/e.py',
                    'compile_mode': COMPILE_MODE_EAGER,
                    'compiled': False,
                    'frozen_globals': {},
                }
            ],
            True,
            False,
        )
        for _ in range(200):
            if prepared:
                break
            time.sleep(0.01)

        self.assertTrue(prepared, 'the record reached the step that writes its file')
        self.assertTrue(
            prepared[0].get('shot_id'),
            'and had its identifier by then, which is what a file can carry',
        )
        self.assertEqual(
            prepared[0]['shot_id'],
            app.queue_controller.get_queue_display_items()[0]['shot_id'],
            'the id written into the file is the id of the row in the queue',
        )

    def test_a_queued_shot_is_written_with_its_id(self):
        # What the id is for: a shot carries the id of the queue row it was
        # written for, so that a result coming back can be matched to the
        # shot that was submitted.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        path = os.path.join(directory, 'experiment_00.h5')
        globals_file = os.path.join(directory, 'globals.toml')
        runmanager.globals_file.new_globals_file(globals_file)
        runmanager.new_group(globals_file, 'group')
        item = {
            'path': path,
            'active_groups': {'group': globals_file},
            'frozen_globals': {},
            'sequence_attrs': {
                'script_basename': 'experiment',
                'sequence_date': '2026-09-18',
                'sequence_index': 11,
                'sequence_id': '20260918T101112_experiment',
            },
            'run_no': 0,
            'n_runs': 1,
            'shot_id': 'the-id',
        }

        app.prepare_queue_shot(item)

        with h5py.File(path, 'r') as f:
            self.assertEqual(f.attrs['shot_id'], 'the-id')


class QueueEditingTests(unittest.TestCase):
    """Delete and Clear around the shot BLACS is running.

    Either would take the file out from under a shot on the hardware, so that
    one row is kept and everything else the operation asked for still goes.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.app = FakeRunManager(self)
        self.addCleanup(self.app.queue_manager.shutdown)

    def enqueue(self, name):
        path = os.path.join(self.directory, name)
        open(path, 'w').close()
        self.app.queue_manager.enqueue([queued_shot(path)])
        return path

    def rows(self):
        return self.app.queue_controller.get_queue_display_items()

    def selection(self, *paths):
        """The shot ids the queue widget emits when these shots are selected."""
        by_path = {row['path']: row['shot_id'] for row in self.rows()}
        return [by_path[path] for path in paths]

    def test_delete_removes_the_rest_of_the_selection_around_it(self):
        # Selecting the whole queue and pressing Delete is the ordinary way to
        # discard the work behind the shot in progress: all of that goes, and
        # the row BLACS is running is what is left.
        running = self.enqueue('shot_a.h5')
        waiting = self.enqueue('shot_b.h5')
        also_waiting = self.enqueue('shot_c.h5')
        self.app.offer_shot()

        removed = self.app.queue_manager.delete_rows(
            self.selection(running, waiting, also_waiting)
        )

        self.assertEqual(sorted(removed), sorted([waiting, also_waiting]))
        self.assertEqual(
            [row['path'] for row in self.rows()],
            [running],
            'the shot BLACS has is cancelled rather than removed, so it is '
            'still the row that is left',
        )
        self.assertEqual(self.rows()[0]['state'], 'cancelled')
        self.assertFalse(os.path.exists(waiting), 'a waiting row takes its file')
        self.assertFalse(os.path.exists(also_waiting))
        self.assertTrue(os.path.exists(running))

    def test_clear_keeps_the_shot_blacs_is_running_and_removes_the_rest(self):
        # Clear is what both replacement submission modes do before the batch
        # is compiled in, so protecting the running row keeps an Engage from
        # emptying the queue out from under a running shot.
        running = self.enqueue('shot_a.h5')
        waiting = self.enqueue('shot_b.h5')
        self.app.offer_shot()

        removed = self.app.queue_manager.clear()

        self.assertEqual(removed, [waiting])
        rows = self.rows()
        self.assertEqual([row['path'] for row in rows], [running])
        self.assertEqual(rows[0]['state'], 'running')
        self.assertTrue(os.path.exists(running), 'with the file it is running')
        self.assertFalse(os.path.exists(waiting))

    def test_clear_leaves_a_failed_row_alone_with_the_running_one(self):
        # A shot that came back needing attention is not what an operator
        # meant to discard by submitting different work: Clear replaces what
        # is behind it, and deleting it stays explicit.
        failed = self.enqueue('shot_a.h5')
        waiting = self.enqueue('shot_b.h5')
        offered = self.app.offer_shot()
        self.app.queue_exchange(
            outcome={'shot_id': offered['shot_id'], 'status': 'aborted'},
            request_shot=False,
        )

        removed = self.app.queue_manager.clear()

        self.assertEqual(removed, [waiting], 'only the waiting work is replaced')
        rows = self.rows()
        self.assertEqual([row['path'] for row in rows], [failed])
        self.assertEqual(rows[0]['state'], 'failed', 'and it is still red')
        self.assertTrue(os.path.exists(failed), 'its file survives with it')
        self.assertFalse(os.path.exists(waiting))

    def test_deleting_a_failed_row_uncovers_the_next_waiting_shot(self):
        # Deleting the red row is the only way to discard a shot BLACS could
        # not run, and it is an edit of the queue and nothing more: what
        # runmanager has to offer is then simply the next shot.
        failed = self.enqueue('shot_a.h5')
        waiting = self.enqueue('shot_b.h5')
        offered = self.app.offer_shot()
        self.app.queue_exchange(
            outcome={
                'shot_id': offered['shot_id'],
                'status': 'failed',
                'message': 'Device(s) in error state',
            },
            request_shot=False,
        )

        removed = self.app.queue_manager.delete_rows(self.selection(failed))

        self.assertEqual(removed, [failed])
        self.assertFalse(os.path.exists(failed), 'a red row takes its file with it')
        response = self.app.queue_exchange(request_shot=True)
        self.assertEqual(response['state'], PROVIDER_SHOT)
        rows = self.rows()
        self.assertEqual([row['path'] for row in rows], [waiting])
        self.assertEqual(rows[0]['state'], 'running')

    def test_a_row_still_marked_running_can_be_deleted_after_a_restart(self):
        # A row stuck marked running cannot be deleted. BLACS's next request is
        # normally offered it again; if BLACS stays away, a restart clears the
        # marking and costs nothing else.
        running = self.enqueue('shot_a.h5')
        self.app.offer_shot()

        restarted = QueueController()
        restarted.restore_state(self.app.queue_controller.export_state())

        rows = restarted.get_queue_display_items()
        self.assertEqual([row['path'] for row in rows], [running])
        self.assertEqual([row['state'] for row in rows], [''])
        self.assertEqual(
            restarted.delete_rows(
                [restarted.get_queue_display_items()[0]['shot_id']]
            ),
            ([running], []),
            'and the row a previous session left running is deletable again',
        )


class ReplayTests(unittest.TestCase):
    """A message that goes missing costs a poll, not a shot or the queue.

    Neither side can tell a reply that was never sent from one never received,
    so BLACS resends: an outcome rides on the next exchange, and a request whose
    offer never arrived is simply made again. Runmanager takes either twice.
    """

    def setUp(self):
        self.app = FakeRunManager(self)
        self.addCleanup(self.app.queue_manager.shutdown)
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def enqueue(self, name):
        path = os.path.join(self.directory, name)
        self.app.queue_manager.enqueue([queued_shot(path)])
        return path

    def rows(self):
        return self.app.queue_controller.get_queue_display_items()

    def test_a_lost_offer_reply_leaves_the_same_row_available(self):
        # BLACS never saw the reply, so it asks again -- with no outcome,
        # because it never ran anything. It is idle and asking, so it cannot be
        # running the row, whatever the row still says.
        self.enqueue('shot_a.h5')
        offered = self.app.queue_exchange(request_shot=True)

        reoffered = self.app.queue_exchange(request_shot=True)

        self.assertEqual(reoffered['state'], PROVIDER_SHOT)
        self.assertEqual(reoffered['shot_id'], offered['shot_id'])
        self.assertEqual(reoffered['path'], offered['path'])
        self.assertEqual(
            [row['state'] for row in self.rows()],
            ['running'],
            'one row, still the one BLACS is being asked to run',
        )

    def test_a_completed_outcome_that_arrives_twice_retires_the_row_once(self):
        # A lost reply makes BLACS send the same completed outcome again. The
        # queue changes once, but the completion is passed on both times: a
        # resend cannot be told from a shot whose row went while BLACS ran it.
        self.enqueue('shot_a.h5')
        offered = self.app.queue_exchange(request_shot=True)
        outcome = {
            'shot_id': offered['shot_id'],
            'status': 'completed',
            'path': offered['path'],
        }

        self.app.queue_exchange(outcome=outcome, request_shot=False)
        self.app.queue_exchange(outcome=outcome, request_shot=False)

        self.assertEqual(self.rows(), [], 'the shot is finished with, once')
        self.assertEqual(
            self.app.analysed_paths(),
            [offered['path'], offered['path']],
            'reported twice, because it completed twice as far as this side '
            'can tell, and reporting it is what this side does',
        )

    def test_a_repeated_outcome_cannot_reach_a_later_shot_of_the_same_file(self):
        # The same filepath queued again is a different row with an id of its
        # own -- the id names the row, not the file -- so a completed outcome
        # resent for the first cannot retire the second in its place.
        path = self.enqueue('shot_a.h5')
        first = self.app.queue_exchange(request_shot=True)
        outcome = {
            'shot_id': first['shot_id'],
            'status': 'completed',
            'path': first['path'],
        }
        self.app.queue_exchange(outcome=outcome, request_shot=False)
        self.enqueue('shot_a.h5')
        second = self.app.queue_exchange(request_shot=True)
        self.assertNotEqual(second['shot_id'], first['shot_id'])

        self.app.queue_exchange(outcome=outcome, request_shot=False)

        rows = self.rows()
        self.assertEqual([row['path'] for row in rows], [path])
        self.assertEqual(
            rows[0]['state'], 'running', 'the shot BLACS is running is untouched'
        )
        self.assertEqual(
            self.app.analysed_paths(),
            [first['path'], first['path']],
            'the resend reports the first shot again, and the second row is '
            'not retired on the strength of it',
        )

    def test_a_repeated_failed_outcome_keeps_one_red_row_and_one_reason(self):
        # A failure is resent for the same reason a completion is. Recording it
        # twice must not double the row, its reason, or what the operator is
        # told: nothing about the shot has changed since the first time.
        self.enqueue('shot_a.h5')
        offered = self.app.queue_exchange(request_shot=True)
        outcome = {
            'shot_id': offered['shot_id'],
            'status': 'failed',
            'message': 'Device(s) in error state',
        }

        self.app.queue_exchange(outcome=outcome, request_shot=False)
        self.app.queue_exchange(outcome=outcome, request_shot=False)

        rows = self.rows()
        self.assertEqual([row['state'] for row in rows], ['failed'])
        self.assertEqual(rows[0]['tooltip'].count('Device(s) in error state'), 1)
        failures_reported = [line for line in self.app.said if 'failed' in line]
        self.assertEqual(len(failures_reported), 1, 'one failure is reported once')

    def test_a_retry_that_fails_the_same_way_is_still_a_second_failure(self):
        # The other side of the same rule. An outcome sent twice for one
        # attempt changes nothing, but a retry that fails again for the same
        # reason is a new event: the row going back to running tells them apart.
        self.enqueue('shot_a.h5')
        outcome = {'status': 'failed', 'message': 'Device(s) in error state'}

        for _ in range(2):
            offered = self.app.queue_exchange(request_shot=True)
            self.app.queue_exchange(
                outcome=dict(outcome, shot_id=offered['shot_id']),
                request_shot=False,
            )

        failures_reported = [line for line in self.app.said if 'failed' in line]
        self.assertEqual(
            len(failures_reported), 2, 'each attempt that failed is reported'
        )


class OutcomeAppliedOnceTests(unittest.TestCase):
    """A failure while applying an outcome costs a re-run, not the data.

    The completion is reported to lyse before the row is retired, so a
    submission that fails leaves the row queued for BLACS's next request to
    reclaim. BLACS is answered normally either way, so it drops the outcome
    instead of resending it.
    """

    def app_with_shot(self):
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = app.queue_manager.offer_next()
        return app, offered['shot_id']

    def outcome(self, shot_id, status, message=''):
        return {
            'shot_id': shot_id,
            'status': status,
            'message': message,
            'path': '/tmp/shot_a.h5',
        }

    def rows(self, app):
        return app.queue_controller.get_queue_display_items()

    def test_a_submission_that_falls_over_leaves_the_shot_to_be_run_again(self):
        app, shot_id = self.app_with_shot()

        # An injected fault: the real hand-off only queues the path.
        def explode(path):
            raise RuntimeError('lyse submission fell over')

        app.analysis_submission.notify_shot_complete = explode

        response = app.queue_exchange(self.outcome(shot_id, 'completed'), False)

        self.assertEqual(
            response['state'],
            PROVIDER_NONE,
            'BLACS gets an answer, so it lets go of an outcome runmanager has '
            'already applied rather than resending it once a second forever',
        )
        self.assertEqual(
            [os.path.basename(row['path']) for row in self.rows(app)],
            ['shot_a.h5'],
            'the row is the only thing left that names this shot, and the '
            'answer BLACS was given means nothing will report it again',
        )
        reoffered = app.queue_exchange(request_shot=True)
        self.assertEqual(
            reoffered['shot_id'],
            shot_id,
            'so the next request reclaims it and the shot is run again',
        )

    def test_a_completion_that_named_no_file_is_reported_under_the_rows_own(self):
        # The path comes from BLACS, which names the file it ran. When it names
        # none the row's own path is all there is, and it has to be read while
        # the row is still in the queue.
        app, shot_id = self.app_with_shot()
        outcome = self.outcome(shot_id, 'completed')
        del outcome['path']

        app.queue_exchange(outcome, False)

        self.assertEqual(
            app.analysed_paths(),
            [os.path.abspath('/tmp/shot_a.h5')],
        )
        self.assertEqual(self.rows(app), [], 'and the row is retired as usual')


class CancelledShotTests(unittest.TestCase):
    """Deleting the shot BLACS was given.

    Delete marks the row, struck through and never offered again, because its
    file may be under the hardware. BLACS's next request carrying no outcome for
    it proves the file is free and clears it; an outcome arriving first clears it
    whatever the outcome was, and a completed one is still reported onward.
    """

    def queue_with_a_shot_at_blacs(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue(
            [
                queued_shot(os.path.join(directory, 'X.h5')),
                queued_shot(os.path.join(directory, 'Y.h5')),
            ]
        )
        offered = app.queue_manager.offer_next()
        return app, offered['shot_id']

    def rows(self, app):
        return app.queue_controller.get_queue_display_items()

    def test_it_is_never_offered_again(self):
        app, shot_id = self.queue_with_a_shot_at_blacs()
        app.queue_manager.delete_rows([shot_id])

        offered = app.queue_manager.offer_next()

        self.assertTrue(
            offered['path'].endswith('Y.h5'),
            'the queue goes on to the next shot rather than stalling',
        )
        self.assertEqual(
            [os.path.basename(row['path']) for row in self.rows(app)],
            ['Y.h5'],
            'and the cancelled row is gone: a request carrying no outcome for '
            'it is proof nobody was running it, which is when its file is free',
        )

    def test_an_outcome_for_it_clears_it_and_only_a_completed_one_is_analysed(self):
        for status, analysed in (('completed', ['/tmp/X.h5']), ('failed', [])):
            with self.subTest(status=status):
                app, shot_id = self.queue_with_a_shot_at_blacs()
                app.queue_manager.delete_rows([shot_id])

                app.apply_shot_outcome(
                    {
                        'shot_id': shot_id,
                        'status': status,
                        'message': 'a device would not arm',
                        'path': '/tmp/X.h5',
                    }
                )

                self.assertEqual(
                    app.analysed_paths(),
                    analysed,
                    'the cancel is about the queue, not about physics that '
                    'already happened',
                )
                self.assertEqual(
                    [os.path.basename(row['path']) for row in self.rows(app)],
                    ['Y.h5'],
                    'the operator has said they do not want this shot; a '
                    'failure is not an invitation to try it again',
                )


class QueueBookkeepingUnderSubmissionTests(unittest.TestCase):
    """A default shot made because the queue looked empty does not outlive that.

    It is made on a different thread from the one that fills the queue, so a
    batch can land in between and leave it parked behind real work with globals
    frozen earlier.
    """

    def test_a_default_shot_behind_real_work_is_discarded_with_its_file(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        default_path = os.path.join(directory, 'default.h5')
        with open(default_path, 'w') as f:
            f.write('')
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue([queued_shot('/tmp/seq_00.h5')])
        app.queue_manager.enqueue(
            [{'path': default_path, 'compiled': True, 'default_shot': True}]
        )

        offered = app.queue_manager.offer_next()

        self.assertTrue(
            offered['path'].endswith('seq_00.h5'), 'the real work is offered'
        )
        self.assertEqual(
            [
                row['path']
                for row in app.queue_controller.get_queue_display_items()
                if row['path'] == default_path
            ],
            [],
            'and the default shot, made only because the queue looked empty, '
            'does not sit behind it with globals frozen before that work arrived',
        )
        self.assertFalse(
            os.path.exists(default_path), 'its file goes with it rather than leaking'
        )


class MalformedOutcomeTests(unittest.TestCase):
    """An outcome runmanager cannot read is refused, and the exchange goes on.

    A refusal returns normally, so the same reply still offers BLACS a shot; a
    raise would end the exchange with nothing offered.
    """

    def make_runmanager(self):
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        return app

    def test_an_outcome_runmanager_cannot_read_is_refused_and_answered(self):
        for description, outcome in (
            ('not a shot outcome at all', 'any-id'),
            ('an empty one', {}),
            ('one that names no shot', {'status': 'completed'}),
            ('one with no status', {'shot_id': 'any-id'}),
            (
                'one with a status runmanager does not know',
                {'shot_id': 'any-id', 'status': 'partly'},
            ),
        ):
            with self.subTest(outcome=description):
                app = self.make_runmanager()
                offered = app.queue_exchange(request_shot=True)
                # The one that could do real damage names a shot that exists:
                if isinstance(outcome, dict) and 'shot_id' in outcome:
                    outcome = dict(outcome, shot_id=offered['shot_id'])

                app.queue_exchange(outcome=outcome, request_shot=False)

                rows = app.queue_controller.get_queue_display_items()
                self.assertEqual([row['state'] for row in rows], ['running'])
                self.assertEqual(rows[0]['tooltip'], rows[0]['path'])
                response = app.queue_exchange(outcome=outcome, request_shot=True)
                self.assertEqual(
                    response['state'],
                    PROVIDER_SHOT,
                    'a normal reply, so BLACS moves on rather than retrying it',
                )
                self.assertEqual(response['shot_id'], offered['shot_id'])


_qapplication = None


def hold_qapplication():
    global _qapplication
    if QApplication.instance() is None:
        # Held for the life of the process: a QApplication that is garbage
        # collected takes every widget built under it down with it.
        _qapplication = QApplication([])


def make_queue_widget():
    hold_qapplication()
    return RunmanagerQueueWidget()


class SentToBlacsRowTests(unittest.TestCase):
    """The shot BLACS was given is the widget's reserved first row."""

    def test_the_shot_that_was_sent_moves_into_the_reserved_row(self):
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        controller.offer_next()
        widget = make_queue_widget()
        widget.set_queue_paths(controller.get_queue_display_items())

        model = widget.queue_model
        self.assertEqual(
            [
                model.item(row, widget.path_column).text()
                for row in range(model.rowCount())
            ],
            ['shot_a.h5', 'shot_b.h5'],
            'the sent shot is the reserved row, and is not listed twice',
        )


class QueueDisplayTests(unittest.TestCase):
    """A failed row is tinted, and its text stands off the tint on any theme."""

    def test_a_tinted_row_names_its_text_colour_too(self):
        # A background alone leaves the theme's own text colour on it, and the
        # fill is pale: near-white text on near-white on a dark theme.
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        offered = controller.offer_next()
        controller.shot_finished(
            offered['shot_id'], 'failed', 'Device(s) in error state'
        )
        widget = make_queue_widget()
        widget.set_queue_paths(controller.get_queue_display_items())

        model = widget.queue_model
        for column in range(model.columnCount()):
            item = model.item(0, column)
            self.assertEqual(item.foreground().color(), TINTED_ROW_FOREGROUND)
            self.assertGreater(
                abs(
                    item.foreground().color().lightness()
                    - item.background().color().lightness()
                ),
                100,
                'the text has to stand off the fill it sits on',
            )
        self.assertIsNone(
            model.item(1, 0).data(Qt.ForegroundRole),
            'an untinted row is left to the theme',
        )


class ExchangeFailureTests(unittest.TestCase):
    """A fault on runmanager's side must not read to BLACS as an outage.

    The outcome is applied before a shot is chosen, and BLACS holds an outcome
    until it knows runmanager took it, so a raise while choosing would make it
    resend the same outcome every second while showing runmanager unavailable.
    """

    def make_runmanager(self):
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        return app

    def test_a_failure_choosing_a_shot_is_reported_and_answered(self):
        app = self.make_runmanager()
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = app.queue_exchange(request_shot=True)

        # An injected fault: no input makes choosing a shot raise.
        def raise_instead(*args, **kwargs):
            raise RuntimeError('the default labscript file has moved')

        app.queue_manager.offer_next = raise_instead

        response = app.queue_exchange(
            outcome={'shot_id': offered['shot_id'], 'status': 'completed'},
            request_shot=True,
        )

        self.assertEqual(
            response['state'],
            PROVIDER_NONE,
            'a normal reply, so BLACS knows its outcome landed',
        )
        self.assertEqual(
            app.queue_controller.get_queue_paths(),
            [],
            'the outcome that came with the request was still applied',
        )


class LostRowTests(unittest.TestCase):
    """A completed shot that matches no row must not vanish quietly.

    Usually it is a lost reply sent again, which changes nothing, but a row can
    also go while BLACS runs the shot, and then nothing else would analyse it.
    """

    def test_a_completed_shot_with_no_row_is_still_analysed(self):
        app = FakeRunManager(self)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = app.queue_exchange(request_shot=True)
        # Neither Delete nor Clear can take the running row. Only restoring
        # the queue at startup replaces the whole of it, and the shot BLACS is
        # running can still go that way.
        app.queue_manager.restore_state({})

        app.queue_exchange(
            outcome={
                'shot_id': offered['shot_id'],
                'status': 'completed',
                'path': '/tmp/shot_a.h5',
            },
            request_shot=False,
        )

        self.assertEqual(
            app.analysed_paths(),
            ['/tmp/shot_a.h5'],
            'the queue lost the row, but the shot ran and wrote data, and a '
            'completion withheld here is one nothing downstream can ask for',
        )


class SequenceContinuityTests(unittest.TestCase):
    """Shots added to the last sequence are part of that sequence.

    A sequence is identified by the attributes its shots carry, not by their
    folder, and a run number is unique within it, so an added batch has to keep
    the attributes and continue the run numbers.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.app = FakeRunManager(self)
        self.app.exp_config = labconfig(self.directory)
        self.addCleanup(self.app.queue_manager.shutdown)
        self.existing = {
            'script_basename': 'experiment',
            'sequence_date': '2026-09-18',
            'sequence_index': 11,
            'sequence_id': '20260918T101112_experiment',
        }

    def next_free_index(self):
        """The index a new sequence of experiment.py would be given today."""
        return runmanager.next_sequence_index(
            os.path.join(self.directory, 'experiment'),
            datetime.datetime.now(),
            increment=False,
        )

    def path(self, name):
        return os.path.join(self.directory, name)

    def add_shots(self, count, anchor, index_start=None, sequence_attrs=None):
        """Compile a batch onto the sequence the anchor shot belongs to."""
        _, run_files = self.app.make_h5_files(
            self.path('experiment.py'),
            self.directory,
            [{'x': n} for n in range(count)],
            with_metadata=True,
            indexed_path_base=anchor,
            index_start=index_start,
            sequence_attrs=sequence_attrs,
        )
        return list(run_files)

    def test_an_added_shot_is_numbered_by_the_filename_it_is_given(self):
        # A sequence compiled in one go names each file after the run number
        # written into it, so renumbering the files of an added batch without
        # its runs would restart run numbers inside a sequence with a shot 0.
        anchor = self.path('experiment_03.h5')
        self.app.queue_manager.enqueue(
            [queued_shot(anchor, sequence_attrs=self.existing)]
        )

        added = self.add_shots(2, anchor)

        self.assertEqual(
            [(os.path.basename(info['path']), info['run_no']) for info in added],
            [('experiment_04.h5', 4), ('experiment_05.h5', 5)],
            'the run number and the filename index are the same number',
        )
        self.assertEqual(
            [info['n_runs'] for info in added],
            [6, 6],
            'runs 0 to 5 of this sequence exist once these are written',
        )
        self.assertEqual(
            [info['sequence_attrs'] for info in added],
            [self.existing, self.existing],
            'the sequence added to is the sequence the added shots are in',
        )

    def test_a_shot_written_earlier_keeps_the_extent_it_was_written_with(self):
        # n_runs is how far the sequence reached as of the shot it is written
        # into, so the shots written before this batch, which may already have
        # run, are not rewritten to agree with it.
        anchor = self.path('experiment_00.h5')
        runmanager.make_single_run_file(anchor, None, {}, self.existing, 0, 1)

        added = self.add_shots(2, anchor)

        self.assertEqual(
            [info['n_runs'] for info in added],
            [3, 3],
            'runs 0 to 2 of this sequence exist once these are written',
        )
        with h5py.File(anchor, 'r') as f:
            self.assertEqual(
                f.attrs['n_runs'],
                1,
                'the shot that was already there still says what its sequence '
                'was when it was written',
            )

    def test_adding_shots_to_a_sequence_claims_no_new_sequence_index(self):
        anchor = self.path('experiment_03.h5')
        self.app.queue_manager.enqueue(
            [queued_shot(anchor, sequence_attrs=self.existing)]
        )

        self.add_shots(2, anchor)

        self.assertEqual(
            self.next_free_index(),
            0,
            'a sequence index claimed for a sequence that was never started '
            'is one no sequence will ever carry, and minting one to throw it '
            'away costs a lock on shot storage that every submission waits in',
        )

    def test_a_batch_of_its_own_does_claim_a_sequence_index(self):
        # The other half of the same rule: a batch that is not being added to
        # anything is a new sequence, and has to take a number for it.
        self.app.make_h5_files(
            self.path('experiment.py'),
            self.directory,
            [{'x': 0}],
            with_metadata=True,
        )

        self.assertEqual(self.next_free_index(), 1)

    def test_no_row_and_no_file_is_no_sequence_to_add_to(self):
        # Reported rather than quietly compiled onto a sequence of its own:
        # a batch added to a sequence that cannot be found is not a batch that
        # should go anywhere. on_engage_clicked puts this in the output box.
        missing = self.path('experiment_00.h5')
        with self.assertRaises(Exception) as raised:
            self.add_shots(1, missing)

        self.assertIn(missing, str(raised.exception))

    def test_a_cleared_queue_still_knows_the_sequence_it_was_adding_to(self):
        # With nothing yet sent to BLACS the shot being added to is a queued
        # one, and Clear removes its row and deletes its file, so what says
        # which sequence this is has to be read before that happens.
        anchor = self.path('experiment_00.h5')
        open(anchor, 'w').close()
        self.app.queue_manager.enqueue(
            [queued_shot(anchor, sequence_attrs=self.existing)]
        )

        sequence = self.app.get_sequence_attrs_to_extend(anchor)
        self.app.queue_manager.clear()
        added = self.add_shots(1, anchor, index_start=0, sequence_attrs=sequence)

        self.assertFalse(os.path.exists(anchor), 'the Clear deleted its file')
        self.assertEqual(
            [info['sequence_attrs'] for info in added],
            [self.existing],
            'the batch replacing the queue is in the sequence it replaced',
        )

    def test_a_replacement_batch_resumes_after_the_shots_blacs_has(self):
        # The shots BLACS has been given keep their files, so the numbering
        # picks up after them; the shots it has not keep nothing, so their
        # numbers are free for the replacement batch to take back.
        sent = self.path('experiment_00.h5')
        open(sent, 'w').close()
        waiting = self.path('experiment_01.h5')
        open(waiting, 'w').close()
        self.app.queue_manager.enqueue(
            [
                queued_shot(sent, sequence_attrs=self.existing),
                queued_shot(waiting, sequence_attrs=self.existing),
            ]
        )
        self.app.offer_shot()

        sequence = self.app.get_sequence_attrs_to_extend(sent)
        self.app.queue_manager.clear()
        added = self.add_shots(2, sent, index_start=0, sequence_attrs=sequence)

        self.assertTrue(os.path.exists(sent), 'BLACS has this one; it stays')
        self.assertFalse(os.path.exists(waiting), 'this one was only waiting')
        self.assertEqual(
            [(os.path.basename(info['path']), info['run_no']) for info in added],
            [('experiment_01.h5', 1), ('experiment_02.h5', 2)],
            'numbering resumes at the first run whose file has gone',
        )

    def test_the_sequence_is_read_off_the_shot_when_the_queue_has_lost_it(self):
        # "Empty queue, then add shots to last sequence" empties the queue
        # first, so the shot being added to is the one last sent to BLACS,
        # whose row may be gone; its file carries what the queue no longer holds.
        anchor = self.path('experiment_00.h5')
        runmanager.make_single_run_file(anchor, None, {}, self.existing, 0, 1)

        added = self.add_shots(1, anchor, index_start=0)

        self.assertEqual(
            [info['sequence_attrs'] for info in added],
            [self.existing],
            'the shot file answers for the sequence when no row does',
        )


class DeletedAnchorTests(unittest.TestCase):
    """What a sequence carries on from when that shot has been deleted.

    The shot last sent to BLACS is named by its file. A file that is gone is not
    a sequence to add to, and would make every later submission refuse, so the
    anchor is let go of with its file and the next submission starts a sequence.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.app = FakeRunManager(self)
        self.addCleanup(self.app.queue_manager.shutdown)

    def enqueue(self, app, name):
        path = os.path.join(self.directory, name)
        open(path, 'w').close()
        app.queue_manager.enqueue([queued_shot(path)])
        return path

    def test_deleting_the_failed_shot_it_named_lets_go_of_the_anchor(self):
        sent = self.enqueue(self.app, 'experiment_00.h5')
        offered = self.app.offer_shot()
        self.app.queue_manager.shot_finished(
            offered['shot_id'], 'failed', 'Device error'
        )

        self.app.queue_manager.delete_rows([offered['shot_id']])

        self.assertFalse(os.path.exists(sent), 'the row took its file with it')
        self.assertIsNone(
            self.app.get_submission_anchor(main_module.SUBMISSION_MODE_ADD_SHOTS),
            'and a deleted shot is not a sequence for the next batch to join',
        )

    def test_deleting_another_shot_leaves_the_anchor_alone(self):
        # Only the shot whose file is being deleted. An operator clearing the
        # work waiting behind the one BLACS is running has not said anything
        # about the sequence it belongs to.
        sent = self.enqueue(self.app, 'experiment_00.h5')
        self.enqueue(self.app, 'experiment_01.h5')
        self.app.offer_shot()
        waiting_id = self.app.queue_controller.get_queue_display_items()[1][
            'shot_id'
        ]

        self.app.queue_manager.delete_rows([waiting_id])

        self.assertEqual(
            self.app.get_last_sent_from_queue_filepath(),
            sent,
            'the shot BLACS was given is still what the sequence carries on '
            'from',
        )

    def test_a_cancelled_shots_file_going_takes_the_anchor_with_it(self):
        # The other way a shot BLACS was given loses its file: the operator
        # deletes the row while BLACS has it, and the file goes at the next
        # request, once nobody can be running it.
        sent = self.enqueue(self.app, 'experiment_00.h5')
        offered = self.app.offer_shot()
        self.app.queue_manager.delete_rows([offered['shot_id']])

        self.app.offer_shot()

        self.assertFalse(os.path.exists(sent), 'and the cancelled row went')
        self.assertIsNone(
            self.app.get_submission_anchor(main_module.SUBMISSION_MODE_ADD_SHOTS),
            'a shot the operator cancelled and whose file has gone is not '
            'what the next submission carries on from',
        )


class CallerChosenShotIdTests(unittest.TestCase):
    """An id the caller chose names the same row every other id does.

    compile_shots keeps the id a record arrives with, and the row made from it
    takes it as text, so the caller polls for the shot it was handed the id of.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.written = []
        self.controller = QueueController()
        self.manager = QueueManager(
            self.controller,
            lambda item, default_globals: self.written.append(item['shot_id']),
            lambda labscript_file, path: True,
            lambda path: None,
            lambda *args, **kwargs: None,
        )
        self.addCleanup(self.manager.shutdown)

    def test_the_queue_answers_about_the_id_the_caller_was_given(self):
        records = self.manager.compile_shots(
            [
                {
                    'path': os.path.join(self.directory, 'shot.h5'),
                    'labscript_file': os.path.join(self.directory, 'e.py'),
                    'compile_mode': COMPILE_MODE_EAGER,
                    'compiled': False,
                    'frozen_globals': {},
                    'shot_id': 7,
                }
            ],
            True,
            False,
        )
        shot_id = records[0]['shot_id']
        for _ in range(500):
            if self.written:
                break
            time.sleep(0.01)

        self.assertTrue(
            self.controller.get_shot_statuses([shot_id])[shot_id]['pending'],
            'the queue holds the shot under the id its submitter was handed',
        )
        self.assertEqual(
            self.written,
            [self.controller.get_queue_display_items()[0]['shot_id']],
            'and the id written into the shot file is the one its row has',
        )


if __name__ == '__main__':
    unittest.main()

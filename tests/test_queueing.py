"""Behavioural tests for the runmanager-owned shot queue.

These exercise QueueController and the queue widget directly, and the exchange
protocol in runmanager.__main__ through its methods, called against a stand-in
for the application rather than a running one.
"""
import os
import shutil
import tempfile
import threading
import unittest

from qtutils.qt.QtCore import Qt
from qtutils.qt.QtWidgets import QApplication

from runmanager.__main__ import RunManager
from runmanager.queueing import (
    EMPTY_QUEUE_DEFAULT_LABSCRIPT,
    FAILED_ROW_BACKGROUND,
    PROVIDER_NONE,
    PROVIDER_SHOT,
    RUNNING_ROW_BACKGROUND,
    QueueController,
    QueueManager,
    RunmanagerQueueWidget,
)


def queued_shot(path, **kwargs):
    item = {'path': path, 'compiled': True}
    item.update(kwargs)
    return item


class QueueIdentityTests(unittest.TestCase):
    def test_queued_shot_has_a_stable_id(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')])
        shot_ids = [item['shot_id'] for item in controller.export_state()['items']]
        self.assertTrue(all(shot_ids), 'every queued shot needs an id')
        self.assertEqual(len(set(shot_ids)), 2, 'ids must distinguish rows')

    def test_configuration_saved_with_a_failure_policy_still_loads(self):
        # Retry/Drop is gone: a shot now stays at the head of the queue until
        # it completes or the operator deletes it. An older saved queue still
        # has to open, with the setting simply ignored.
        controller = QueueController()
        controller.restore_state(
            {'failure_policy': 'drop', 'items': [queued_shot('/tmp/shot_a.h5')]}
        )
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'failed', 'Device error')
        self.assertEqual(
            controller.offer_next()['shot_id'],
            offered['shot_id'],
            'the failed shot is retried whatever the old setting said',
        )
        self.assertNotIn('failure_policy', controller.get_queue_state())

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
        # A queue saved before shot ids existed still has to open, and its
        # shots still have to be offerable: an id is minted on the way in, is
        # saved with the row from then on, and nothing else is lost with it.
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
        controller.restore_state({'items': [legacy]})

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

    def test_shot_id_survives_save_and_restore(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        state = controller.export_state()
        restored = QueueController()
        restored.restore_state(state)
        self.assertEqual(
            [item['shot_id'] for item in restored.export_state()['items']],
            [item['shot_id'] for item in state['items']],
        )


class QueuePauseTests(unittest.TestCase):
    """Pause is this runmanager's policy on its own queue.

    Whether a paused runmanager withholds the shot is decided in offer_shot().
    What is checked here is the queue's half of it: that pause is a saved queue
    setting, and that pausing does nothing to the queue itself.
    """

    def test_a_new_queue_is_not_paused(self):
        self.assertFalse(QueueController().get_queue_state()['paused'])

    def test_pause_is_saved_and_restored_with_the_queue(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        controller.set_paused(True)
        restored = QueueController()
        restored.restore_state(controller.export_state())
        self.assertTrue(restored.get_queue_state()['paused'])

    def test_a_saved_queue_with_no_pause_state_loads_unpaused(self):
        # An older configuration was written before there was a pause control,
        # and must not open with the queue silently stopped.
        controller = QueueController()
        controller.set_paused(True)
        controller.restore_state({'items': [queued_shot('/tmp/shot_a.h5')]})
        self.assertFalse(controller.get_queue_state()['paused'])

    def test_pausing_does_not_disturb_the_shot_that_is_running(self):
        # Pause withholds the next shot; it does not stop the one in hand. The
        # row BLACS is running stays running, and its outcome still retires it.
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        offered = controller.offer_next()
        controller.set_paused(True)
        self.assertEqual(controller.get_queue_display_items()[0]['state'], 'running')
        controller.shot_finished(offered['shot_id'], 'completed')
        self.assertEqual(
            [row['path'] for row in controller.get_queue_display_items()],
            [os.path.abspath('/tmp/shot_b.h5')],
            'the shot that was under way completed normally',
        )

    def test_resuming_leaves_the_head_of_the_queue_where_it_was(self):
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        before = controller.get_queue_display_items()
        controller.set_paused(True)
        self.assertEqual(controller.get_queue_display_items(), before)
        controller.set_paused(False)

        offered = controller.offer_next()
        self.assertEqual(offered['path'], os.path.abspath('/tmp/shot_a.h5'))
        self.assertEqual(
            [item['shot_id'] for item in controller.export_state()['items']][0],
            offered['shot_id'],
            'the head is the same shot it was before the pause',
        )


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


    def test_row_still_marked_running_is_offered_again_under_the_same_id(self):
        # BLACS asks for a shot only when it is idle, so a request that has no
        # outcome for the running row proves the offer never reached it. The
        # row is handed out again rather than stranding the queue behind it.
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = controller.offer_next()

        reoffered = controller.offer_next()

        self.assertEqual(reoffered['shot_id'], offered['shot_id'])
        self.assertEqual(reoffered['path'], offered['path'])
        self.assertTrue(reoffered['reclaimed'], 'and it says that it did so')
        self.assertFalse(offered['reclaimed'], 'the first offer reclaimed nothing')

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

    def test_a_retry_that_fails_again_keeps_the_same_row(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'failed', 'Device error')
        retried = controller.offer_next()
        controller.shot_finished(retried['shot_id'], 'failed', 'Device error again')

        rows = controller.get_queue_display_items()
        self.assertEqual([row['path'] for row in rows], [os.path.abspath('/tmp/shot_a.h5')])
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

    def test_an_outcome_that_arrives_twice_cannot_reach_another_row(self):
        # A reply BLACS never received is sent again. The outcome names its row
        # by id, so the repeat finds the row it was always about -- or, once
        # that row has gone, nothing at all.
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'completed')

        self.assertIsNone(controller.shot_finished(offered['shot_id'], 'completed'))
        self.assertEqual(
            [row['path'] for row in controller.get_queue_display_items()],
            [os.path.abspath('/tmp/shot_b.h5')],
            'the shot behind it is not completed in its place',
        )

    def test_outcome_for_an_unknown_shot_changes_nothing(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        self.assertIsNone(controller.shot_finished('not-a-shot-id', 'completed'))
        self.assertEqual(len(controller.get_queue_display_items()), 1)

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
                self.assertEqual(rows[0]['state'], 'failed')
                self.assertIn('Device error', rows[0]['tooltip'])
                self.assertEqual(
                    [item['shot_id'] for item in controller.export_state()['items']][0],
                    offered['shot_id'],
                    'the row keeps the id it was offered under',
                )


class FakeAnalysisSubmission(object):
    def __init__(self):
        self.submitted = []

    def notify_shot_complete(self, path):
        self.submitted.append(path)


class FakeOutputBox(object):
    """What runmanager shows its user, which is where protocol trouble shows."""

    def __init__(self):
        self.lines = []

    def output(self, text, red=False):
        self.lines.append(text)

    def said(self, *words):
        return [line for line in self.lines if all(word in line for word in words)]


class FakeRunManager(object):
    """Runmanager's own exchange, over only the surface of it that it uses.

    RunManager.__init__ builds the whole application, which does not belong in
    a unit test, so this holds the few things the exchange reaches for. The
    methods below are RunManager's own and the queue underneath is a real
    QueueManager, so these exercise the rules runmanager applies rather than a
    description of them. Only the two boundaries are stood in for: producing a
    default shot, which evaluates globals and compiles a labscript file, and
    submitting to lyse.
    """

    queue_exchange = RunManager.queue_exchange
    apply_shot_outcome = RunManager.apply_shot_outcome
    offer_shot = RunManager.offer_shot
    get_queue_append_filepath = RunManager.get_queue_append_filepath

    def __init__(self, default_shot_file=None):
        self.output_box = FakeOutputBox()
        self.queue_manager = QueueManager(
            lambda item: None,
            lambda labscript_file, path: True,
            lambda path: None,
            self.output_box.output,
            threading.Event(),
            lambda enabled: None,
        )
        self.analysis_submission = FakeAnalysisSubmission()
        self.default_shot_file = default_shot_file
        self.default_shots_taken = 0

    def take_default_shot(self, labscript_file):
        self.default_shots_taken += 1
        return self.default_shot_file

    def discard_default_shot(self):
        self.default_shot_file = None


class DefaultShotTests(unittest.TestCase):
    """A shot runmanager produced itself is queue work like any other.

    The empty-queue policy exists to keep the apparatus busy, but what it
    produces is still a shot a runmanager user is running: it has to be
    visible, retryable, deletable and analysed. So it is materialised as an
    ordinary queue row and follows every rule the queue already has.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.labscript_file = os.path.join(self.directory, 'default.py')
        open(self.labscript_file, 'w').close()
        self.default_shot = os.path.join(self.directory, 'default_shot_0.h5')

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def make_runmanager(self, default_shot_file=None):
        app = FakeRunManager(default_shot_file=default_shot_file)
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.set_empty_queue_policy(EMPTY_QUEUE_DEFAULT_LABSCRIPT)
        app.queue_manager.set_default_labscript_file(self.labscript_file)
        return app

    def rows(self, app):
        return app.queue_manager.controller.get_queue_display_items()

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
            app.analysis_submission.submitted,
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
            app.analysis_submission.submitted, [], 'a shot that did not run is not analysed'
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

    def test_a_default_shot_being_produced_offers_nothing_meanwhile(self):
        # Producing one evaluates globals and compiles, off the request
        # thread, so the first request that asks for one comes back empty.
        app = self.make_runmanager(default_shot_file=None)

        response = app.offer_shot()

        self.assertEqual(response['state'], PROVIDER_NONE)
        self.assertEqual(self.rows(app), [], 'nothing is queued until there is a shot')

    def test_deleting_the_default_shot_row_discards_it_like_any_other(self):
        app = self.make_runmanager(default_shot_file=self.default_shot)
        open(self.default_shot, 'w').close()
        app.offer_shot()

        removed = app.queue_manager.delete_rows([(0, self.default_shot)])

        self.assertEqual(removed, [self.default_shot])
        self.assertEqual(self.rows(app), [])
        self.assertFalse(
            os.path.exists(self.default_shot), 'its file goes with the row'
        )

    def test_a_default_shot_is_not_the_sequence_anchor(self):
        # A default shot is not part of a sequence and lives in the daily
        # default directory, so neither anchor an Engage batch can be written
        # alongside may be a default shot's file.
        app = self.make_runmanager(default_shot_file=self.default_shot)
        app.offer_shot()

        self.assertIsNone(
            app.queue_manager.get_queue_state()['last_sent_from_queue'],
            'a default shot is not the last shot sent from the queue',
        )
        self.assertIsNone(
            app.get_queue_append_filepath(),
            'and it is not what "add shots to last sequence" appends to',
        )

    def test_an_engaged_shot_is_still_the_sequence_anchor(self):
        app = self.make_runmanager()
        app.queue_manager.enqueue([queued_shot(os.path.join(self.directory, 'shot_a.h5'))])

        offered = app.offer_shot()

        self.assertEqual(
            app.queue_manager.get_queue_state()['last_sent_from_queue'],
            offered['path'],
        )
        self.assertEqual(
            app.get_queue_append_filepath(), os.path.join(self.directory, 'shot_a.h5')
        )

    def test_an_engaged_shot_goes_for_analysis_the_same_way(self):
        # The counterpart of the completed default shot above: a default shot
        # reaches lyse because it is now an ordinary row, not by a path of its
        # own. BLACS's own local fallback shots reach neither, which is tested
        # where they are run, in blacs/tests/test_shot_execution.py.
        app = self.make_runmanager()
        app.queue_manager.enqueue([queued_shot(os.path.join(self.directory, 'shot_a.h5'))])
        offered = app.offer_shot()

        app.queue_exchange(
            outcome={
                'shot_id': offered['shot_id'],
                'status': 'completed',
                'path': offered['path'],
            },
            request_shot=False,
        )

        self.assertEqual(self.rows(app), [])
        self.assertEqual(app.analysis_submission.submitted, [offered['path']])

    def test_no_default_shot_is_produced_while_the_queue_holds_work(self):
        # The empty-queue policy is for a queue that is empty. A queue whose
        # head BLACS never confirmed has work to hand out rather than nothing
        # to offer, so that row is offered again and no default shot is made to
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

        saved = app.queue_manager.export_state()

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


class ReplayTests(unittest.TestCase):
    """A message that goes missing must cost a poll, not a shot or the queue.

    Neither side of the exchange can tell a reply that was never sent from one
    that was never received, so BLACS resends: an outcome runmanager has not
    taken rides on the next exchange, and a request whose offer never arrived
    is simply made again. Runmanager has to be able to take either twice.
    """

    def setUp(self):
        self.app = FakeRunManager()
        self.addCleanup(self.app.queue_manager.shutdown)
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def enqueue(self, name):
        path = os.path.join(self.directory, name)
        self.app.queue_manager.enqueue([queued_shot(path)])
        return path

    def rows(self):
        return self.app.queue_manager.controller.get_queue_display_items()

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
        self.assertTrue(
            self.app.output_box.said('shot_a.h5', 'again'),
            'a re-offer the operator would otherwise never see is reported',
        )

    def test_a_completed_outcome_that_arrives_twice_is_analysed_once(self):
        # BLACS lets go of an outcome only once runmanager has taken it, so a
        # lost reply makes it send the same completed outcome again. Analysis
        # follows the row that was retired, so the repeat finds nothing to do.
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
            self.app.analysis_submission.submitted,
            [offered['path']],
            'lyse is not given the same shot to analyse twice',
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
            self.app.analysis_submission.submitted,
            [first['path']],
            'and it is not analysed on the strength of the first shot finishing',
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
        self.assertEqual(
            len(self.app.output_box.said('shot_a.h5', 'failed')),
            1,
            'one failure is reported once',
        )

    def test_a_retry_that_fails_the_same_way_is_still_a_second_failure(self):
        # The other side of the same rule. An outcome sent twice for one
        # attempt changes nothing, but an operator's retry that fails again for
        # the very same reason is a new event, and must be reported: the row
        # went back to running in between, which is what tells them apart.
        self.enqueue('shot_a.h5')
        outcome = {'status': 'failed', 'message': 'Device(s) in error state'}

        for _ in range(2):
            offered = self.app.queue_exchange(request_shot=True)
            self.app.queue_exchange(
                outcome=dict(outcome, shot_id=offered['shot_id']),
                request_shot=False,
            )

        self.assertEqual(
            len(self.app.output_box.said('shot_a.h5', 'failed')),
            2,
            'each attempt that failed is reported',
        )


def malformed_outcomes(shot_id):
    return (
        ('not a shot outcome at all', shot_id),
        ('an empty one', {}),
        ('one that names no shot', {'status': 'completed'}),
        ('one with no status', {'shot_id': shot_id}),
        (
            'one with a status runmanager does not know',
            {'shot_id': shot_id, 'status': 'partly'},
        ),
    )


class MalformedOutcomeTests(unittest.TestCase):
    """An outcome runmanager cannot read must not look like an outage.

    An exception raised here reaches BLACS as an error it cannot tell apart
    from never having reached runmanager, and BLACS holds an outcome until it
    knows runmanager took it -- so it would send the same unreadable message
    for ever. Refusing it, saying so, and answering the exchange normally is
    what lets BLACS move on.
    """

    def make_runmanager(self):
        app = FakeRunManager()
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        return app

    def test_an_outcome_runmanager_cannot_read_is_refused_and_answered(self):
        for description, outcome in malformed_outcomes('any-id'):
            with self.subTest(outcome=description):
                app = self.make_runmanager()
                offered = app.queue_exchange(request_shot=True)

                response = app.queue_exchange(outcome=outcome, request_shot=True)

                self.assertEqual(
                    response['state'],
                    PROVIDER_SHOT,
                    'a normal reply, so BLACS moves on rather than retrying it',
                )
                self.assertEqual(response['shot_id'], offered['shot_id'])
                self.assertTrue(
                    app.output_box.said('could not read'),
                    'and the operator is told the protocol went wrong',
                )

    def test_an_outcome_runmanager_cannot_read_leaves_the_queue_alone(self):
        for description, outcome in malformed_outcomes('any-id'):
            with self.subTest(outcome=description):
                app = self.make_runmanager()
                offered = app.queue_exchange(request_shot=True)
                # The one that could do real damage names a shot that exists:
                if isinstance(outcome, dict) and 'shot_id' in outcome:
                    outcome = dict(outcome, shot_id=offered['shot_id'])

                app.queue_exchange(outcome=outcome, request_shot=False)

                rows = app.queue_manager.controller.get_queue_display_items()
                self.assertEqual([row['state'] for row in rows], ['running'])
                self.assertEqual(rows[0]['tooltip'], rows[0]['path'])


_qapplication = None


def make_queue_widget():
    global _qapplication
    if QApplication.instance() is None:
        # Held for the life of the process: a QApplication that is garbage
        # collected takes every widget built under it down with it.
        _qapplication = QApplication([])
    return RunmanagerQueueWidget()


def row_backgrounds(widget, row):
    model = widget.queue_model
    return [
        model.item(row, column).data(Qt.BackgroundRole)
        for column in range(model.columnCount())
    ]


class QueueDisplayTests(unittest.TestCase):
    def test_running_row_is_shown_green_and_waiting_rows_are_not(self):
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        controller.offer_next()
        widget = make_queue_widget()
        widget.set_queue_paths(controller.get_queue_display_items())
        running = row_backgrounds(widget, 0)
        self.assertTrue(all(brush is not None for brush in running))
        for brush in running:
            colour = brush.color()
            self.assertEqual(colour, RUNNING_ROW_BACKGROUND)
            self.assertGreater(colour.green(), max(colour.red(), colour.blue()))
        self.assertTrue(all(brush is None for brush in row_backgrounds(widget, 1)))

    def test_failed_row_is_shown_red_with_its_reason_in_the_tooltip(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'failed', 'Device(s) in error state')
        widget = make_queue_widget()
        widget.set_queue_paths(controller.get_queue_display_items())

        failed = row_backgrounds(widget, 0)
        self.assertTrue(all(brush is not None for brush in failed))
        for brush in failed:
            colour = brush.color()
            self.assertEqual(colour, FAILED_ROW_BACKGROUND)
            self.assertGreater(colour.red(), max(colour.green(), colour.blue()))
        self.assertIn(
            'Device(s) in error state',
            widget.queue_model.item(0, 1).toolTip(),
            'the reason a shot needs attention is on the row',
        )

    def test_retried_row_goes_back_to_green(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'failed', 'Device(s) in error state')
        controller.offer_next()
        widget = make_queue_widget()
        widget.set_queue_paths(controller.get_queue_display_items())

        for brush in row_backgrounds(widget, 0):
            self.assertEqual(brush.color(), RUNNING_ROW_BACKGROUND)
        self.assertNotIn(
            'Device(s) in error state', widget.queue_model.item(0, 1).toolTip()
        )

    def test_row_stops_being_green_once_the_shot_completes(self):
        controller = QueueController()
        controller.enqueue(
            [queued_shot('/tmp/shot_a.h5'), queued_shot('/tmp/shot_b.h5')]
        )
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'completed')
        widget = make_queue_widget()
        widget.set_queue_paths(controller.get_queue_display_items())
        self.assertEqual(widget.queue_model.rowCount(), 1)
        self.assertTrue(all(brush is None for brush in row_backgrounds(widget, 0)))


class ExchangeFailureTests(unittest.TestCase):
    """A fault on runmanager's side must not read to BLACS as an outage.

    The outcome is applied before a shot is chosen, so an exchange that raises
    while choosing has already taken BLACS's outcome. BLACS cannot tell a
    raised error from never having reached runmanager, and holds an outcome
    until it knows runmanager took it, so it would send that same outcome again
    once a second, indefinitely, while showing runmanager as unavailable.
    """

    def make_runmanager(self):
        app = FakeRunManager()
        self.addCleanup(app.queue_manager.shutdown)
        return app

    def test_a_failure_choosing_a_shot_is_reported_and_answered(self):
        app = self.make_runmanager()
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = app.queue_exchange(request_shot=True)

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
        self.assertTrue(
            app.output_box.said('could not offer a shot', 'moved'),
            'and the operator is told what went wrong here',
        )
        self.assertEqual(
            app.queue_manager.get_queue_state()['n_items'],
            0,
            'the outcome that came with the request was still applied',
        )


class LostRowTests(unittest.TestCase):
    """A completed shot that matches no row must not vanish quietly.

    Usually it is a lost reply being sent again, which should change nothing.
    But a row can also go while BLACS is running it, and then a shot really did
    run and nothing will analyse it. Runmanager cannot tell the two apart, so
    it says what happened either way.
    """

    def test_a_completed_shot_with_no_row_is_reported_and_not_analysed(self):
        app = FakeRunManager()
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = app.queue_exchange(request_shot=True)
        app.queue_manager.clear()

        app.queue_exchange(
            outcome={
                'shot_id': offered['shot_id'],
                'status': 'completed',
                'path': '/tmp/shot_a.h5',
            },
            request_shot=False,
        )

        self.assertEqual(app.analysis_submission.submitted, [])
        self.assertTrue(app.output_box.said(offered['shot_id'], 'not been sent'))

    def test_a_path_runmanager_cannot_use_does_not_reach_analysis_as_it_came(self):
        app = FakeRunManager()
        self.addCleanup(app.queue_manager.shutdown)
        app.queue_manager.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = app.queue_exchange(request_shot=True)

        app.queue_exchange(
            outcome={
                'shot_id': offered['shot_id'],
                'status': 'completed',
                'path': ['/tmp/shot_a.h5'],
            },
            request_shot=False,
        )

        self.assertTrue(
            all(isinstance(path, str) for path in app.analysis_submission.submitted),
            'lyse is given a path, whatever shape BLACS sent',
        )


if __name__ == '__main__':
    unittest.main()

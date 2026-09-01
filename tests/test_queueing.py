"""Behavioural tests for the runmanager-owned shot queue.

These exercise QueueController and the queue widget directly. The exchange
protocol itself lives in runmanager.__main__, which cannot be imported without
starting the application, so the behaviour it delegates is tested here.
"""
import os
import unittest

from qtutils.qt.QtCore import Qt
from qtutils.qt.QtWidgets import QApplication

from runmanager.queueing import (
    FAILED_ROW_BACKGROUND,
    RUNNING_ROW_BACKGROUND,
    QueueController,
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

    Whether a paused runmanager withholds the shot is decided in offer_shot(),
    which lives in runmanager.__main__ and cannot be imported here. What is
    checked here is the half that can be: that pause is a saved queue setting,
    and that pausing does nothing to the queue itself.
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


    def test_running_row_is_not_offered_again(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        self.assertIsNotNone(controller.offer_next())
        self.assertIsNone(controller.offer_next())

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


if __name__ == '__main__':
    unittest.main()

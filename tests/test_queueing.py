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

    def test_outcome_for_an_unknown_shot_changes_nothing(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        self.assertIsNone(controller.shot_finished('not-a-shot-id', 'completed'))
        self.assertEqual(len(controller.get_queue_display_items()), 1)

    def test_shot_that_did_not_complete_stays_queued(self):
        controller = QueueController()
        controller.enqueue([queued_shot('/tmp/shot_a.h5')])
        offered = controller.offer_next()
        controller.shot_finished(offered['shot_id'], 'failed', 'Device error')
        rows = controller.get_queue_display_items()
        self.assertEqual([row['path'] for row in rows], [os.path.abspath('/tmp/shot_a.h5')])
        self.assertEqual(rows[0]['state'], '')


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

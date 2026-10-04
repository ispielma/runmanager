"""What runmanager says BLACS is doing, worked out from the status BLACS answers with."""
import os
import unittest

from runmanager.blacs_status import blacs_state


def snapshot(**fields):
    """A status of the shape BLACS's get_status command answers with."""
    status = {
        'requesting_shots': False,
        'status': 'Idle',
        'shot_id': None,
        'shot_path': None,
        'error': None,
    }
    status.update(fields)
    return status


class BlacsStateTests(unittest.TestCase):
    def test_the_state_says_whether_blacs_is_asking_for_work(self):
        for status, state in (
            (
                snapshot(requesting_shots=True, status='Requesting shots'),
                'requesting shots',
            ),
            (
                snapshot(requesting_shots=False, status='Not requesting shots'),
                'not requesting shots',
            ),
        ):
            with self.subTest(state=state):
                self.assertEqual(blacs_state(status)[0], state)

    def test_a_blacs_running_a_shot_names_the_shot(self):
        state, _ = blacs_state(
            snapshot(
                requesting_shots=True,
                status='Running (program time: 0.100s)...',
                shot_id='shot-1',
                shot_path='/data/2026/shot_a.h5',
            )
        )
        self.assertEqual(state, 'running shot_a.h5')

    def test_a_shot_blacs_ran_on_its_own_is_told_apart_from_queue_work(self):
        # BLACS runs its local override shot when this runmanager has nothing
        # for it. That shot is in nobody's queue and has no id, and saying so
        # is how a user sees why their queue is not moving.
        state, details = blacs_state(
            snapshot(requesting_shots=True, shot_path='/data/override.h5')
        )
        self.assertEqual(state, 'running override.h5')
        self.assertIn('local override', '\n'.join(details))

    def test_a_shot_path_from_another_machine_still_names_the_shot(self):
        # BLACS sends the path shared-drive-agnostic, so a BLACS on Windows
        # and a runmanager on anything else still agree which file it is.
        state, details = blacs_state(
            snapshot(requesting_shots=True, shot_path='Z:\\2026\\shot_a.h5')
        )
        self.assertEqual(state, 'running shot_a.h5')
        self.assertIn(
            os.path.join('2026', 'shot_a.h5'),
            '\n'.join(details),
            'and shows the path the way this machine writes it',
        )

    def test_the_reason_blacs_stopped_is_in_the_state(self):
        # Not only in the tooltip: a queue that is not moving because the
        # apparatus stopped is the thing a user most needs to see without
        # hunting for it.
        state, _ = blacs_state(
            snapshot(
                requesting_shots=False,
                status='Device(s) in error state\nRequests stopped',
                error='Device(s) in error state',
            )
        )
        self.assertIn('stopped', state)
        self.assertIn('Device(s) in error state', state)

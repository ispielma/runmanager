"""Runmanager's half of the guard on the runmanager/BLACS boundary.

Two rules are enforced here, and only these two. They are about what the two
applications may say to each other, not about how either is put together
inside: a helper may be renamed, a class split, a module moved, and none of
that should fail a test in this file.

  1. The superseded request/accept/reject/report RPC surface stays gone. One
     exchange replaced it, and a compatibility shim was deliberately not kept,
     so a method quietly reappearing would be a second protocol rather than an
     addition to this one.

  2. Runmanager may ask BLACS what it is doing and nothing else. Whether BLACS
     requests shots, the error that stopped it, restarting a device and Abort
     belong to the operator standing at the apparatus, and no message
     runmanager can send may reach any of them.

BLACS's half -- what BLACS actually calls, and what its own server offers --
is in ``blacs/tests/test_architecture.py``, because only a test in blacs can
import both. Each repository's guard has to fail on its own repository's
regression, so neither half can stand in for the other.
"""
import ast
import os
import unittest

import runmanager.blacs_status
import runmanager.remote
from runmanager.__main__ import RemoteServer
from runmanager.analysis_submission import AnalysisSubmission

# The commands that carried the old handoff: BLACS asked for a shot, said it
# had taken it, said it would not, and reported the result on a channel of its
# own. queue_exchange does all four now. See ISSUES_PRD.md.
SUPERSEDED_COMMANDS = (
    'queue_request_next',
    'shot_accepted',
    'shot_rejected',
    'notify_shot_complete',
)


def requested_commands(module):
    """The command names a client module can put on the wire.

    Read from the source rather than from the method names, because the command
    is the thing that crosses the boundary: a client method is free to be
    called anything.
    """
    source = open(module.__file__).read()
    commands = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != 'request':
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            commands.add(node.args[0].value)
    return commands


def fail(rule, detail):
    raise AssertionError(
        '%s\n\nThe architecture guard in %s enforces this. See the module '
        'docstring there, and the "Synchronising with runmanager" section of '
        'blacs/docs/source/shot-management.rst.\n\n%s'
        % (rule, os.path.basename(__file__), detail)
    )


class SupersededProtocolTests(unittest.TestCase):
    """One exchange, and no way back to the four calls it replaced."""

    def test_runmanagers_client_offers_no_superseded_command(self):
        offered = requested_commands(runmanager.remote)
        back = sorted(offered & set(SUPERSEDED_COMMANDS))
        if back:
            fail(
                'runmanager.remote.Client can send the superseded shot-handoff '
                'command(s) %s again.' % ', '.join(back),
                'A shot is offered, reported on and requested through '
                'queue_exchange, and only through it: the outcome is applied '
                'before the next shot is chosen, which is what makes the '
                'offer, the reclaim and the retry sound. A second route round '
                'that ordering reintroduces the failures it closed.',
            )

    def test_runmanagers_server_answers_no_superseded_command(self):
        # RemoteServer.handler dispatches whatever handle_<command> it finds,
        # so a handler is a command whether or not any client calls it.
        back = sorted(
            command
            for command in SUPERSEDED_COMMANDS
            if hasattr(RemoteServer, 'handle_' + command)
        )
        if back:
            fail(
                'runmanager\'s server answers the superseded shot-handoff '
                'command(s) %s again.' % ', '.join(back),
                'RemoteServer.handler dispatches any handle_<command> method, '
                'so adding one puts that command back on the wire even with '
                'no client for it.',
            )

    def test_the_lyse_submission_of_the_same_name_is_untouched(self):
        # notify_shot_complete is a superseded BLACS *command*. The method of
        # that name on AnalysisSubmission is runmanager's own, unrelated, and
        # is how a completed shot reaches lyse: the guard above is written
        # against the command surfaces precisely so that it cannot be
        # satisfied by deleting this.
        self.assertTrue(hasattr(AnalysisSubmission, 'notify_shot_complete'))


class StatusIsReadOnlyTests(unittest.TestCase):
    """Runmanager may ask BLACS a question. It may not tell it anything."""

    def test_runmanager_can_ask_blacs_for_nothing_but_its_status(self):
        asked = requested_commands(runmanager.blacs_status)
        allowed = {'hello', 'get_status'}
        extra = sorted(asked - allowed)
        if extra:
            fail(
                'runmanager can now send BLACS the command(s) %s.'
                % ', '.join(extra),
                'The status surface is monitoring only. Enabling Request '
                'shots, clearing the error that stopped it, restarting a '
                'device and aborting a shot are the operator\'s, at the '
                'apparatus; a runmanager that could do any of them from a '
                'distance would take back the ownership boundary this branch '
                'drew. Polling BLACS for what it is doing stays allowed -- '
                'that is what get_status is.',
            )
        self.assertEqual(
            asked,
            allowed,
            'runmanager must still be able to reach BLACS and read its status',
        )


if __name__ == '__main__':
    unittest.main()

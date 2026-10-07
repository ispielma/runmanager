DEFAULT_PORT = 42523

from labscript_utils.ls_zprocess import ZMQClient

# What an exchange tells BLACS about this runmanager: it offered a shot, its
# queue is paused, the shot it will offer next is still compiling, or it has
# nothing to offer right now. Paused and pending are told apart from having
# nothing so that BLACS can say why no queued work is arriving, or wait for it;
# neither is a reason for BLACS to stop. Only a shot comes with a path:
PROVIDER_SHOT = 'shot'
PROVIDER_PAUSED = 'paused'
PROVIDER_PENDING = 'pending'
PROVIDER_NONE = 'none'
# How Engage adds its batch to the queue: as a sequence of its own or to the
# last one, with the queue emptied first or not. See engage():
SUBMISSION_MODE_NEW_SEQUENCE = 'new_sequence'
SUBMISSION_MODE_LAST_SEQUENCE = 'last_sequence'
SUBMISSION_MODE_NEW_SEQUENCE_CLEAR_QUEUE = 'new_sequence_clear_queue'
SUBMISSION_MODE_LAST_SEQUENCE_CLEAR_QUEUE = 'last_sequence_clear_queue'
# How BLACS may say a shot it was offered turned out. Every one but 'completed'
# leaves the row at the head of the queue in red; see shot_finished() in
# queueing.py:
SHOT_OUTCOME_STATUSES = ('completed', 'aborted', 'failed', 'rejected')


class SequenceRefused(ValueError):
    """Runmanager will not add shots to the sequence asked for."""


class RunmanagerClient(ZMQClient):
    """A ZMQClient for communication with runmanager"""

    server = 'runmanager'
    default_port = DEFAULT_PORT

    def get_version(self):
        """Return the version of runmanager the server is running in"""
        return self.request('get_version')

    def get_values(self, raw=False):
        """Return all active globals' Default values.

        If raw=True, return the stored Default expression strings. Otherwise return
        the evaluated Python values."""
        return self.request('get_values', raw=raw)

    def get_globals(self, raw=False):
        """Return all active globals' effective values.

        If raw=True, return the stored effective expression strings. Otherwise return
        the evaluated Python values."""
        return self.request('get_globals', raw=raw)

    def set_values(self, globals, raw=False):
        """Set Default expressions for active globals."""
        return self.request('set_values', globals, raw=raw)

    def get_scans(self, raw=False):
        """Return all active globals' Scan values.

        If raw=True, return the stored Scan expression strings. Otherwise return the
        evaluated Python values."""
        return self.request('get_scans', raw=raw)

    def set_scans(self, globals, raw=False):
        """Set Scan expressions for active globals."""
        return self.request('set_scans', globals, raw=raw)

    def get_scan_enabled(self):
        """Return all active globals' Scan? state."""
        return self.request('get_scan_enabled')

    def set_scan_enabled(self, globals):
        """Set Scan? state for active globals."""
        return self.request('set_scan_enabled', globals)

    def get_jit_enabled(self):
        """Return all active globals' JIT? state."""
        return self.request('get_jit_enabled')

    def set_jit_enabled(self, globals):
        """Set JIT? state for active globals."""
        return self.request('set_jit_enabled', globals)

    def engage(self, submission_mode=SUBMISSION_MODE_NEW_SEQUENCE):
        """Compile the shots the window's globals expand into, and queue them.

        Does what the window's Engage button does with its globals, scans and
        shuffle as they stand, sending the shots where its BLACS and runviewer
        checkboxes say; ``n_shots`` says how many there will be. Answers once
        the batch is made, before its shots compile, so each ``path`` is where
        a file will be written and not a file that is there yet. With 'BLACS'
        unticked the shots are not queued, so ``get_queue`` does not list them,
        ``shot_status`` answers ``None`` for them and a compile error
        reaches only the output box. ``test_compile`` is how to check that a
        script compiles.

        Parameters
        ----------
        submission_mode : str
            How the batch joins the queue, one of the ``SUBMISSION_MODE_``
            constants of this module, each named after an entry of the window's
            Engage menu:

            ``SUBMISSION_MODE_NEW_SEQUENCE``
                "Add shots to new sequence": the batch is a sequence of its own.
            ``SUBMISSION_MODE_LAST_SEQUENCE``
                "Add shots to last sequence": the batch is added to the sequence
                of the last shot queued or, with none queued, of the shot last
                sent to BLACS, and is a sequence of its own if there is neither.
            ``SUBMISSION_MODE_NEW_SEQUENCE_CLEAR_QUEUE``
                "Empty queue, then add shots to new sequence": the queued shots
                not yet sent to BLACS are deleted first. Running, failed,
                rejected and cancelled rows are kept.
            ``SUBMISSION_MODE_LAST_SEQUENCE_CLEAR_QUEUE``
                "Empty queue, then add shots to last sequence": the queue is
                emptied in the same way, and the batch is added to the sequence
                of the shot last sent to BLACS or, if none has been sent, of the
                last shot queued, and is a sequence of its own if there is
                neither.

        Returns
        -------
        list of dict
            One descriptor per shot, in the order made:
            ``{'shot_id', 'sequence_id', 'sequence_index', 'run_number',
            'path'}``, as ``submit_shots`` returns.

        Raises
        ------
        ValueError
            For a ``submission_mode`` that is not one of these, for any but the
            first while 'BLACS' is unticked, since the others are about the
            queue, and when there is no labscript file or output folder, or the
            globals cannot be evaluated or expand into no shots.
        Exception
            Whatever else stops the batch being made.
        """
        return self.request('engage', submission_mode)

    def abort(self):
        """Empty runmanager's queue, as its Empty queue button does.

        The shots not yet sent to BLACS are deleted. Running, failed, rejected
        and cancelled rows are kept."""
        return self.request('abort')

    def get_run_shots(self):
        """Get boolean state of the 'BLACS' checkbox.

        Whether shots compiled by Engage are put into the runmanager queue for
        BLACS to run."""
        return self.request('get_run_shots')

    def set_run_shots(self, value):
        """Set boolean state of the 'BLACS' checkbox.

        This decides only where newly engaged shots go. It is not the queue's
        pause, and it is not BLACS's own gate on running anything."""
        return self.request('set_run_shots', value)

    def get_view_shots(self):
        """Get boolean state of 'runviewer' checkbox"""
        return self.request('get_view_shots')

    def set_view_shots(self, value):
        """Set boolean state of 'runviewer' checkbox"""
        return self.request('set_view_shots', value)

    def get_analyse_shots(self):
        """Get boolean state of the lyse checkbox.

        Whether completed shots are sent to lyse."""
        return self.request('get_analyse_shots')

    def set_analyse_shots(self, value):
        """Set boolean state of the lyse checkbox.

        Setting it False also drops the shots still waiting to be sent, as
        unticking the checkbox does."""
        return self.request('set_analyse_shots', value)

    def get_shuffle(self):
        """Get boolean state of 'Shuffle' checkbox"""
        return self.request('get_shuffle')

    def set_shuffle(self, value):
        """Set boolean state of 'Shuffle' checkbox"""
        return self.request('set_shuffle', value)

    def n_shots(self):
        """Get the number of prospective shots from pressing 'Engage'"""
        return self.request('n_shots')

    def get_labscript_file(self):
        """Get the path of the current experiment script"""
        return self.request('get_labscript_file')

    def set_labscript_file(self, value):
        """Set the current experiment script"""
        return self.request('set_labscript_file', value)

    def get_shot_output_folder(self):
        """Get the current shot output folder"""
        return self.request('get_shot_output_folder')

    def set_shot_output_folder(self, value):
        """Set the shot output folder"""
        return self.request('set_shot_output_folder', value)

    def error_in_globals(self):
        """True if any tab of an active group contains error(s)"""
        return self.request('error_in_globals')

    def is_output_folder_default(self):
        """True if shot output folder is not the default path"""
        return self.request('is_output_folder_default')

    def reset_shot_output_folder(self):
        """Reset the shot output folder to the default path"""
        return self.request('reset_shot_output_folder')

    def submit_shots(self, entries, sequence=None, sequence_index=None):
        """Submit one shot per entry, each with the globals that entry names.

        ``entries`` is a list of ``{global_name: value}`` dicts; one entry is
        one shot, and every entry names the same globals. runmanager's window
        is set to the last entry and left so, so that whoever is watching sees
        what is running; globals no entry names are untouched.

        Returns one descriptor per entry, in the order submitted:
        ``{'shot_id', 'sequence_id', 'sequence_index', 'run_number', 'path'}``.
        The queue is never cleared.

        A remote session is one sequence. With no ``sequence`` the shots start
        a sequence of their own. Pass a ``sequence_id`` from an earlier
        submission as ``sequence`` to add to that sequence instead, numbered
        after every shot of it runmanager has made, whatever ran in between.
        Pass its ``sequence_index`` too: two sequences started in the same
        second share an id, and runmanager refuses to guess between them.
        Runmanager remembers its sequences only until it restarts, so a
        sequence it has no record of is refused.

        Each submitted shot is written with its ``shot_id`` as a root
        attribute of its h5 file, which is where the id handed back here
        reappears: it is how a result produced from that file is matched to
        the entry that asked for it. A shot file carrying no such attribute is
        one nobody submitted -- runmanager writes the shots it makes itself,
        to keep the apparatus busy between submissions, without one.

        Raises, having submitted nothing at all, whatever it is that goes
        wrong. The whole batch is made and handed over in one go, so until
        that succeeds there is nothing queued to take back and no shot running
        under an identifier the caller was never given. A reply that times
        out is the exception: runmanager may still queue the batch, under ids
        the caller never received. A submission is answered in milliseconds,
        so a timeout means runmanager is not answering at all.

        An entry that would produce anything other than exactly one shot is
        refused this way, as a scan left on a global can make it do. So are a
        labscript file or output folder that is not set, globals that cannot
        be evaluated, entries that do not all name the same globals, a name no
        active group has, and a ``sequence`` runmanager has no record of, of
        another labscript file, or sharing its id with another when no
        ``sequence_index`` is given. A batch refused while its entries are
        evaluated sets no global, and one refused as it is queued is left at
        its last entry; either way nothing is queued and nothing runs.

        The Scan? and JIT? boxes of the globals an entry names are the
        caller's to manage, through get_scan_enabled, set_scan_enabled,
        get_jit_enabled and set_jit_enabled."""
        return self.request(
            'submit_shots',
            list(entries),
            sequence=sequence,
            sequence_index=sequence_index,
        )

    def shot_status(self, shot_ids):
        """Say how far each of these shots has got, step by step.

        A shot is compiled, queued, run by BLACS and, once BLACS completes it,
        analysed by lyse. Its record says where it stands in each of those
        steps. Reads only: nothing is consumed by asking, so the same ids can
        be asked about as often as wanted.

        Parameters
        ----------
        shot_ids : list of str
            The ``shot_id`` of each shot to ask about, as ``submit_shots`` and
            ``engage`` return it.

        Returns
        -------
        dict
            ``{shot_id: record}``, one entry per id asked about. ``record`` is
            ``None`` for an id runmanager has not held since it started, or
            held before a restart, because what it remembers of shots is not
            saved. Otherwise it is a dict with these keys:

            ``shot_id``, ``sequence_id``, ``sequence_index``, ``run_number``, ``path``
                As ``submit_shots`` returns them.
            ``compile``
                ``'waiting'``, ``'compiling'``, ``'compiled'`` or ``'failed'``.
                A shot that fails to compile stays in the queue and holds up
                the shots behind it until an operator deletes it or asks for
                another compile. ``None`` for a shot that left the queue
                without having compiled or failed to compile.
            ``queue``
                ``'queued'`` while the shot is in the queue, including a shot
                that itself holds the queue up. ``'blocked'`` when it is in the
                queue behind a shot only an operator can clear, one whose
                ``blacs`` is ``'rejected'`` or whose ``compile`` is
                ``'failed'``, so that it is not offered to BLACS until they do.
                ``'left'`` once it is no longer in the queue.
            ``blacs``
                ``'waiting'`` for a shot not yet offered to BLACS, and
                ``'running'`` once it is offered and BLACS has reported no
                outcome. Then ``'completed'``, or ``'aborted'`` or ``'failed'``
                when BLACS reported that it did not complete, after which it is
                offered again, or ``'rejected'`` when BLACS could not read it,
                which holds the queue up until an operator clears it. A shot
                deleted while BLACS had it is ``'cancelled'``: it is not
                offered again, but BLACS may still complete it. A shot that
                left keeps the last of these it had, and is ``None`` if it was
                never offered.
            ``lyse``
                ``'waiting'`` for every shot in the queue and for a shot BLACS
                completed whose submission to lyse is still to be settled, then
                ``'sent'``, ``'rejected'`` or ``'not sent'``. ``None`` for a
                shot that left without BLACS completing it, which lyse never
                gets.
            ``pending``
                Whether the shot may still reach ``blacs`` ``'completed'``, as
                far as runmanager can tell, and nothing about lyse. True for a
                shot the queue would still hand over to BLACS, and for a
                ``'cancelled'`` one, which BLACS may still complete. False for
                a ``'blocked'`` shot, a shot waiting on an operator and every
                shot that has left.
            ``since``
                ``time.time()`` of the latest change to the shot's ``compile``,
                ``blacs``, ``lyse`` or ``message``, or of its leaving the
                queue. It does not move when another shot's change blocks or
                unblocks this one.
            ``message``
                Why the shot last changed: what BLACS said of its outcome, the
                compile's error, or why it left the queue. ``''`` if none.

        Notes
        -----
        Only some combinations of these values occur. A shot is in the queue
        when ``queue`` is ``'queued'`` or ``'blocked'``, and has left when it
        is ``'left'``:

        ========  ======================  =============================================
        Where     When                    Then
        ========  ======================  =============================================
        in queue  always                  lyse 'waiting'
        in queue  blacs not 'waiting'     compile 'compiled'
        in queue  compile 'failed'        blacs 'waiting', pending False
        in queue  blacs 'rejected'        pending False
        in queue  blacs 'cancelled'       pending True
        in queue  queue 'blocked'         pending False
        left      always                  pending False
        left      blacs 'completed'       compile 'compiled'; lyse 'waiting', 'sent',
                                          'rejected' or 'not sent'
        left      blacs not 'completed'   lyse None
        ========  ======================  =============================================
        """
        return self.request('shot_status', list(shot_ids))

    def get_queue(self):
        """List the shots in runmanager's queue, the front of the queue first.

        Reads only: nothing is consumed by asking, so the queue can be asked
        about as often as wanted. A shot engaged with 'BLACS' unticked is not
        queued, and is not listed.

        Returns
        -------
        list of dict
            The record ``shot_status`` gives for each shot in the queue, in
            queue order.
        """
        return self.request('get_queue')

    def test_compile(self):
        """Compile the first shot of the window's globals, and queue nothing.

        Compiles the shot Engage would make first, from the globals, scans and
        shuffle as they stand, into a scratch .h5 file that each test compile
        overwrites and that is removed when runmanager exits. It queues
        nothing, claims no sequence and touches no output folder, so it can be
        repeated freely to see whether a labscript file compiles. Answers only
        once the compile finishes, so the client's timeout has to outlast it;
        meanwhile runmanager answers no other remote request.

        Returns
        -------
        dict
            ``{'success': bool, 'error': str, 'path': str}``. ``error`` is ''
            on success and the compile's traceback otherwise, and ``path`` is
            the compiled shot file, which stays valid until the next
            ``test_compile``.

        Raises
        ------
        ValueError
            When no labscript file is selected, or the globals cannot be
            evaluated or expand into no shots.
        Exception
            Whatever else stops the globals being expanded.
        """
        return self.request('test_compile')

    def queue_exchange(self, outcome=None, request_shot=True):
        """Report how a shot turned out, and ask for the next one.

        ``outcome`` is None, or a dict describing the shot runmanager last
        offered: its ``shot_id``, its ``status`` (``'completed'``,
        ``'aborted'``, ``'failed'`` or ``'rejected'``), a human-readable
        ``message``, and the ``path`` actually run if that is not the path
        offered. Runmanager applies the outcome before choosing what to offer,
        so one exchange can finish one shot and take the next.

        Repeating an exchange is safe: an outcome for a row that has gone, or
        one already carrying that same failure, changes nothing. An outcome
        runmanager cannot read at all, and a failure on runmanager's side while
        it chooses what to offer, are both reported in runmanager's output
        rather than raised, and the exchange still answers normally. A caller
        that gets an answer has been heard, and must move on rather than
        sending the same outcome again.

        Returns a dict: ``state`` is ``'shot'``, ``'paused'``, ``'pending'``
        (the next shot is still compiling) or ``'none'``, and ``shot_id`` and
        ``path`` name the offered shot when there is one."""
        return self.request('queue_exchange', outcome, request_shot)

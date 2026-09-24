# Runmanager Review Findings — 2026-09-24

Work items from a code review of runmanager's optimisation work on
`RunmanagerControl`: the range `1d35c48..c1a3862`, which is everything from the
parent of the first optimisation commit to the branch tip. It covers the remote
API (`submit_shots`, `shot_status`, the greeting), shot identity, the
accepted-record bookkeeping, abort, the in-memory sequence record that joins a
remote session's batches, the day's default sequence, and where all of these
meet existing code: Engage's anchor sources and replace-queue modes, the saved
queue, BLACS's exchange, lyse, and labscript-optimization's use of the API.
Line references are to `c1a3862`.

Ten review angles produced 79 candidates, which merge into about 42
mechanisms. Every correctness and efficiency mechanism had its own verifier,
and nearly every one was reproduced with a probe against the real code. The
slices below cover the ones that were confirmed, plus one plausible one
(Slice 15). Prose findings were checked by reading the lines.

Four findings were reviewed and **rejected as intended behaviour**. Each is
deliberate and tested; do not "fix" them:

- An abort also stops batches submitted while it is in force, until every
  batch it covers has drained (`QueueManager.abort`, and the comment beside
  `compilation_aborted`). Slice 12 replaces Abort with Empty queue, so this
  holds only until then.
- "Empty queue, then add shots to last sequence" prefers the shot last sent
  to BLACS over the queue's last row. The `SUBMISSION_MODES` comment gives the
  reasoning.
- An operator's "add shots to last sequence" joins a remote session's
  sequence when that session's shots are queued last. That is the menu item's
  documented meaning, and no run numbers collide.
- Deleting the failed last-sent row lets go of the anchor, and the next "add
  shots" starts a sequence (`forget_last_sent`, `DeletedAnchorTests`).

Verification: runmanager has a test suite. Run it from inside the repo with
`~/miniforge3/envs/labscript/bin/python -m pytest tests -q`. Each slice's fix
comes with a test that fails without it, driven through the public surface as
the labscript-style skill describes. The checkout stays on `RunmanagerControl`
until the effort merges.

## Work areas

- **The remote server keeps answering:** Slices 1, 13, 14, 15. BLACS shares
  runmanager's one request thread with `submit_shots`.
- **A join lands in the right sequence, under the right number and name:**
  Slices 2, 3, 4, 5, 9, 12, 16.
- **Each submitted entry runs, and is accounted for, as asked:** Slices 7,
  8.
- **Default shots:** Slice 10.
- **The tests and the prose:** Slices 18, 19.

Cleanup candidates for the excess-code review, not verified one by one, are
listed at the end.

## Checklist

Slices are numbered in the suggested order of work.

Every decision is made. Each slice is built under the labscript-narrow-patch
and labscript-style skills. A box is ticked only where every criterion below
it is met.

- [x] Slice 1: A failed preparse no longer wedges the remote server
- [ ] Slice 2: A replacement batch never takes an in-flight shot's number
- [x] Slice 3: Two sequences started in the same second stay two sequences (HITL)
      — decided: key the record by `(sequence_id, sequence_index)`
- [ ] Slice 4: A join refuses a sequence of another labscript file
- [ ] Slice 5: "Add shots to last sequence" sees a sequence still compiling (HITL)
      — decided: done by Slice 12
- [ ] ~~Slice 6: Each entry runs the values it names (HITL)~~ — moved to
      labscript-optimization; runmanager already exposes both boxes
- [ ] Slice 7: A timed-out submission is not left running unaccounted (HITL)
      — decided: `submit_shots` is made practically instant; no recovery path
- [ ] Slice 8: A cancelled shot that can still complete stays pending
- [ ] Slice 9: Joined shots are named from the template and their own globals
- [ ] Slice 10: Default shots are numbered per sequence
- [ ] ~~Slice 11: The day's default sequence sorts as current in lyse (HITL,
      lyse)~~ — dropped
- [ ] Slice 12: Emptying the queue also stops the batches it discards (HITL)
      — decided: queue each batch's rows at submission; Abort becomes
      Empty queue
- [ ] Slice 13: A refused entry says which global failed and why
- [ ] Slice 14: `submit_shots` checks its global names in one parse
- [ ] Slice 15: A batch no longer holds BLACS's server thread (HITL)
      — decided: evaluate each entry from one read; keep one server
- [ ] Slice 16: A join reads no shot file and takes no lock on the GUI thread
- [ ] ~~Slice 17: Abort stays enabled while a batch is pending~~ — goes with
      Slice 12's Empty queue
- [ ] Slice 18: The tests exercise what they claim, inside temporary directories
- [ ] Slice 19: Comments and docstrings say what the code does now

---

## Slice 1: A failed preparse no longer wedges the remote server

### Type

`AFK`

### What to build

`preparse_globals_loop` calls `preparse_globals_required.task_done()` only
after `preparse_globals()` succeeds. When a preparse raises, for example
because a globals file is briefly unreadable on the share, its `task_done()`
calls are skipped. `unfinished_tasks` then never returns to zero, and every
later `wait_until_preparse_complete()` blocks forever.

`handle_submit_shots` calls that wait once per entry, on zprocess's
single-threaded request server that BLACS's `queue_exchange` and `say_hello`
also use. So one failed preparse leaves `submit_shots` hung and BLACS
unanswered until runmanager restarts. `handle_engage` and `handle_n_shots`
wait the same way.

Mark each request done whether or not the preparse raised, for instance in a
`finally`, while still reporting the exception as the loop does now.

### Acceptance criteria

- [x] After a preparse that raises, `wait_until_preparse_complete()` returns
      once the loop has taken the request.
- [x] A `submit_shots` made after a failed preparse either completes or is
      refused. It never hangs.
- [x] The preparse error still reaches the log and the output, as it does now.
- [x] A test that makes one preparse raise and then waits fails without the
      fix.

### Blocked by

None - can start immediately.

### Review findings covered

- One preparse error hangs `submit_shots` and BLACS for good
  (`__main__.py:5292`, root in the preparse loop). The flaw is older than the
  range; `submit_shots` is a new caller of it.

---

## Slice 2: A replacement batch never takes an in-flight shot's number

### Type

`AFK`

### What to build

The replace-queue modes number the replacement from 0 and skip only files that
already exist (`compile_and_queue_shots`, `index_start = 0`). Under eager
compile, a batch of the same sequence can still be with the worker: its shots
have neither rows nor files, and the Clear does not touch them. The replacement
is then handed those shots' run numbers and paths. When both batches have
compiled, two rows name one file, and the file holds only whichever was written
last (`make_single_run_file` opens with `'w'`).

The record update after the batch also lowers `self.sequences[id]`'s next run
number, from 13 to 10 in the reproduction, so a later join through the record
is numbered onto in-flight paths as well.

Rule to implement: a replacement takes back only the run numbers of the rows
this Clear removed; otherwise it numbers after the record. A sequence's next run
number in the record never goes down.

### Acceptance criteria

- [ ] An add-shots batch still compiling, followed by "Empty queue, then add
      shots to last sequence", leaves no two rows naming one path and no file
      overwritten.
- [ ] The record's next run number never decreases, so a join after the
      replacement numbers after every shot of both batches.
- [ ] A replacement still reuses the numbers of the rows the Clear deleted,
      as the existing tests pin.
- [ ] A test of the in-flight case fails without the fix.

### Blocked by

None - can start immediately.

### Review findings covered

- Replace-queue reuses in-flight shots' numbers and files
  (`__main__.py:2720` and `:2764`).

---

## Slice 3: Two sequences started in the same second stay two sequences

### Type

`HITL`

### What to build

`RunManager.sequences` is keyed by `sequence_id`, which is a timestamp to the
second plus the script name. Two sequences of one script started in the same
second share the key, and the second overwrites the first's record. A remote
session whose first submission falls in the same second as an operator's Engage
then has every later join written into the operator's folder with the
operator's `sequence_index`. The descriptor hands back the same id, so the
client cannot tell. An Engage "add shots" through the record can be misrouted
the same way.

### Decision

Key the record by `(sequence_id, sequence_index)`, which is how lyse already
tells sequences apart. Descriptors then carry `sequence_index`, and
`submit_shots(sequence=...)` takes both. The Engage path compares the anchor's
full attributes. The change is small and additive for the client.

### Acceptance criteria

- [x] With the clock fixed within one second, a remote session and an Engage
      are two sequences, and the session's later joins land in its own folder
      with its own `sequence_index`.
- [x] An Engage "add shots" joins the sequence its anchor belongs to, not
      another that shares the id.
- [x] labscript-optimization is told of any change to the descriptors or to
      `submit_shots`' arguments.

### Blocked by

None - can start immediately.

### Review findings covered

- Same-second sequence_ids collide in the sequence record
  (`__main__.py:2764`).

---

## Slice 4: A join refuses a sequence of another labscript file

### Type

`AFK`

### What to build

A joined batch takes `script_basename` and `sequence_id` from the anchor or the
record, even when the window is now compiling a different labscript file.
Switching the labscript field and then choosing "add shots to last sequence",
or letting the optimiser submit with `sequence=id`, compiles the new file into
the old script's sequence and folder.

Refuse the join when the joined sequence's `script_basename` differs from the
labscript file being compiled. For a remote join the message keeps the prefix
`Cannot add shots to sequence <id>: `, which labscript-optimization uses as its
stop reason. For Engage, the output box says why.

### Acceptance criteria

- [ ] After switching the labscript file, a remote join is refused with the
      prefix and nothing is queued.
- [ ] After switching the labscript file, Engage "add shots" is refused, with
      the reason in the output box.
- [ ] Joins compiled from the sequence's own file are unchanged.

### Blocked by

None - can start immediately.

### Review findings covered

- Join keeps the old script's sequence after a file switch
  (`__main__.py:2725`).

---

## Slice 5: "Add shots to last sequence" sees a sequence still compiling

### Type

`HITL`

### What to build

Under eager compile, a newly engaged sequence has no queue row until its first
shot has compiled. Choosing "add shots to last sequence" in that window finds
nothing in the queue and falls back to the last-sent shot of the previous
sequence, so the batch is written into that older sequence.

### Decision

"Last sequence" means the sequence submitted last, including one still
compiling. Slice 12 gives that: a batch's rows are in the queue from the
moment it is submitted, so the queue's last row belongs to the sequence
submitted last. Nothing is built here beyond Slice 12.

### Acceptance criteria

- [ ] Engaging a new sequence and choosing "add shots to last sequence" before
      its first shot has compiled adds to the new sequence, never to the
      previous one.
- [ ] A test of that interleaving fails without the fix.

### Blocked by

Slice 12.

### Review findings covered

- Add-shots during a first compile joins the old sequence (`__main__.py:175`,
  anchor sources).

---

## Slice 6: Each entry runs the values it names

### Type

`HITL`

### What to build

`_shot_for_entry` refuses an entry only when it expands to other than one shot.
Two cases slip through, and in each the shot runs a value other than the entry's
while the descriptor credits the cost to the entry's value:

- A named global with **scan enabled and a single value** (`[5]`,
  `linspace(a, b, 1)`) is accepted, and the shot runs the scan value. The
  client docstring says such entries are refused.
- A named **JIT-enabled** global is left out of the frozen record
  (`get_frozen_globals` skips JIT globals). Every shot of the batch then
  compiles with the window's value at compile time, which is the last entry's.
  Under lazy compile it can be a later batch's.

### Decision

This is the optimizer's to handle, not runmanager's. runmanager's part is
that the optimizer can read and set both boxes, which the remote API already
provides: `get_scan_enabled`, `set_scan_enabled`, `get_jit_enabled` and
`set_jit_enabled` in `runmanager.remote`.

### Acceptance criteria

- [x] The optimizer can read and set a global's Scan? and JIT? boxes through
      `runmanager.remote`.

### Blocked by

None.

### Review findings covered

- JIT globals named by entries all run the last value (`__main__.py:5348`).
- One-point scans are accepted and override the entry (`__main__.py:5341`).

---

## Slice 7: A timed-out submission is not left running unaccounted

### Type

`HITL`

### What to build

`Client.submit_shots` promises it raises having submitted nothing. zprocess's
client timeout (`communication_timeout`, 60 s) bounds only the wait for the
reply: the server finishes the handler and queues the whole batch under shot
ids the caller never received. labscript-optimization's refill relies on "a
raise here leaves nothing behind" and stops the session, while runmanager runs
the batch with no record on the optimiser's side.

The labscript-style skill rules out retry, backoff or timeout machinery around
ZMQ calls.

### Decision

No recovery path. `submit_shots` only fills the queue, so it should be
practically instant; Slices 1, 14 and 15 remove what makes it slow. A timeout
is then a fault, not a case to design for.

### Acceptance criteria

- [ ] The client docstring states what a timeout means.
- [ ] `submit_shots` for a batch of the optimizer's size returns in
      milliseconds, by measurement.

### Blocked by

Slices 1, 14 and 15.

### Review findings covered

- Timed-out submit still queues shots the client can't see
  (`remote.py:185`).

---

## Slice 8: A cancelled shot that can still complete stays pending

### Type

`AFK`, with a notice to labscript-optimization

### What to build

A cancelled row is a shot the operator deleted while BLACS was running it.
`get_shot_statuses` answers it `{'pending': False, 'state': 'cancelled'}`
(`REFUSED_STATES`), yet BLACS goes on to complete it and runmanager sends the
completion to lyse. The contract in `Client.shot_status` is that pending is
false once nothing further will happen.

labscript-optimization drops a cancelled shot at once, frees its budget slot,
proposes a replacement, and later accepts the late cost. With the real Session
and `max_num_runs=2`, the run ended with three results.

Answer a cancelled row as pending, since it can still produce a result, until it
leaves the queue. After that it is `'unknown'`, as for any shot that has left.
The row still refuses to be offered, and still does not hold up the rows behind
it.

### Acceptance criteria

- [ ] Deleting a running remote shot's row leaves its status pending with
      state `'cancelled'`.
- [ ] Once BLACS reports it, or asks with no outcome for it, its status is not
      pending.
- [ ] Rows behind a cancelled head are still pending and offered as now.
- [ ] labscript-optimization is told that `'cancelled'` can still complete.

### Blocked by

None - can start immediately.

### Review findings covered

- Cancelled running shot reported done, still sends a cost
  (`queueing.py:99`).

---

## Slice 9: Joined shots are named from the template and their own globals

### Type

`AFK`

### What to build

A batch joining a sequence is named by editing the anchor or newest shot's
filename (`reindex_run_file_infos`), not by `make_run_files`' per-shot format.
With `{globals[x]}` in `filename_prefix_format`, every joined shot is named after
the previous batch's last shot: joining with x=5 writes `2_experiment_2.h5`.
labscript-optimization joins on every batch after its first, and Engage "add
shots" misnames the same way. A globals-dependent output folder puts every
joined shot in the anchor's folder.

Keep the sequence's unresolved folder and prefix in the record, as
`new_sequence_details` returns them, with the globals placeholders left in.
Name a joined batch with `make_run_files` starting at the record's next run
number. `reindex_run_file_infos` then serves only sequences with no record.

### Acceptance criteria

- [ ] With `{globals[x]}` in the prefix, each joined shot is named with its own
      x and ends in its run number.
- [ ] With `{globals[x]}` in the folder format, each joined shot goes in its
      own x's folder.
- [ ] Existing numbering tests pass unchanged.

### Blocked by

Slice 2. Both change how a joined batch is numbered; do them in order.

### Review findings covered

- Joined shots are named after the previous shot's globals
  (`__main__.py:2567`).

---

## Slice 10: Default shots are numbered per sequence

### Type

`AFK`

### What to build

All of a day's default shots share one sequence, but each one's run number comes
from `_next_default_shot_index`, keyed by the file base. With a global in the
prefix or folder format the base varies, so run numbers repeat within the one
sequence: x = 1, 2, 1 gives runs 0, 0, 1. That contradicts
`make_single_run_file`'s "unique within its sequence", and under lyse's
`integer_indexing` the rows collide at (-1, 0, 0).

The counter is also held in memory only, and a day's default shots now share one
base. So after a restart the first default shot calls `os.path.exists` on every
default file already written that day: 1501 calls with 1500 files.

The default row is queued as a bare path, with no sequence and run 0.

Number default shots per sequence: keep the day's default sequence in the
record like any other, and take the run number from it. After a restart, seed
the count from one listing of the folder being written into. Queue the row with
the sequence attributes, run number and n_runs it was written with.

### Acceptance criteria

- [ ] With a global in the prefix, a day's default shots have distinct,
      increasing run numbers.
- [ ] After a restart, numbering continues from the day's files with one
      directory listing, not one stat per file.
- [ ] The default row carries the sequence and run number its file has.
- [ ] The existing default-sequence tests pass.

### Blocked by

Slice 3, which changes the record's key.

### Review findings covered

- Default shots repeat run numbers under globals formats
  (`__main__.py:4714`).
- After a restart, the first default shot stats every default file of the day
  (`__main__.py:4714`, below the report's cap).

---

## Slice 11: The day's default sequence sorts as current in lyse

### Type

`HITL`, cross-repo. The lyse half belongs to the lyse session.

### What to build

The day's default sequence is dated 00:00:00, and lyse orders sequences by that
timestamp (`_extract_n_sequences_from_df`). So today's default shots rank older
than any sequence engaged today. With the real lyse code, `n_sequences=1`
returns the 09:00 engaged sequence and drops the default shots running now, and
`n_sequences=2` returns the whole day's default shots at once.

### Decision

Dropped.

### Review findings covered

- Midnight default sequence sorts wrongly in lyse (`__init__.py:741`).

---

## Slice 12: Emptying the queue also stops the batches it discards

### Type

`HITL`

### What to build

Clear removes only queue rows, and under eager compile a shot gets its row only
once it has compiled. So the rest of a discarded batch that is still compiling
is queued after the Clear, ahead of the replacement, and runs.

### Decision

Queue each batch's rows the moment it is submitted, in both compile modes, and
have eager compile work through the queued rows one by one. Lazy compile
already does the rest: it compiles rows off the server thread, and a compile
that finishes after its row was deleted deletes its file (`queueing.py:1055`).
Emptying the queue then removes the whole discarded batch. It also closes
Slice 5, since a batch has rows from the start.

A replacement takes no filename of a shot still compiling (Slice 2).

A failed compile marks its row, which stays in the queue, red with its
reason, while the rows after it go on compiling. It holds the queue only once
it is the head, the next shot BLACS asks for: the rows behind it then wait
until it is deleted, as a failed head row does under lazy compile now.

Abort, a leftover from compiling a batch at once, becomes Empty queue: it
removes every waiting row, as the replace modes' Clear does, so after
submitting shots that do not compile the operator can back out. The remote
`abort` command follows the button.

### Acceptance criteria

- [ ] A batch's rows are in the queue when its submission returns, in both
      compile modes.
- [ ] Emptying the queue while a batch is compiling leaves none of its shots
      to run and no file of a deleted row behind.
- [ ] Empty queue, in Abort's place, removes every waiting row, and a row still
      compiling has its file deleted when its compile finishes.
- [ ] A failed compile marks its row and holds the queue only once that row
      is the head, in both compile modes.
- [ ] A test of emptying the queue during a compile fails without the fix.

### Blocked by

Slice 2.

### Review findings covered

- Empty queue does not stop a batch still compiling (`queueing.py:452`).

---

## Slice 13: A refused entry says which global failed and why

### Type

`AFK`

### What to build

`handle_error_in_globals` catches every exception and returns a bare `True`, so
a refused entry says only "the globals it produces cannot be evaluated". An
unrelated `broken = 1/0`, duplicate active group names and file read errors all
read the same, and labscript-optimization stops the session with that as its
reason.

`_shot_for_entry`'s own parse already raises the specific error. Refuse with
that error's message, and drop the separate pre-check, which evaluates every
global a second time per entry.

### Acceptance criteria

- [ ] A batch refused for a broken global names the global and the error.
- [ ] Nothing is queued, and the refusal comes before the batch is made, as now.
- [ ] A test pinning the named global fails without the fix.

### Blocked by

None. Build it with Slice 15, which replaces the loop it changes.

### Review findings covered

- Globals refusal hides which global failed and why (`__main__.py:5293`).
- The per-entry pre-check duplicates the parse in `_shot_for_entry` (cleanup).

---

## Slice 14: `submit_shots` checks its global names in one parse

### Type

`AFK`

### What to build

The unknown-global check calls `handle_get_default_globals(raw=True)`, which
re-parses the globals TOML once per active global, so its cost is quadratic:
1.47 s at 300 globals and 5.87 s at 600. It runs before any entry, on the thread
BLACS's 5 s `say_hello` probe waits on. `_get_active_global_locations()` gives
the same names from one parse per file.

### Acceptance criteria

- [ ] An unknown global is still refused before anything is set, with the same
      message.
- [ ] The check reads each globals file once per submission.

### Blocked by

None. Build it with Slice 15, which replaces the loop it changes.

### Review findings covered

- Name check re-parses the globals file once per global (`__main__.py:5280`).

---

## Slice 15: A batch no longer holds BLACS's server thread

### Type

`HITL`

### What to build

`handle_submit_shots` runs its whole per-entry loop on zprocess's
single-threaded request server, which BLACS's `say_hello` liveness probe (5 s)
and `queue_exchange` share. For each entry it rewrites the globals file once per
named global, waits for a full preparse and evaluates twice. That measured
0.7-1.8 s for 8 entries in the harness, before GUI round trips. It is plausible,
though not shown end to end, that a larger batch or globals set crosses BLACS's
timeout, so that BLACS runs its local override instead of queued work.

### Decision

Keep one server. Evaluate each entry from the globals read once, with the
entry's values as overrides, the way `get_queue_compile_globals` builds
compile globals, and set the window once, to the last entry. With
`submit_shots` and `queue_exchange` both fast, sharing the thread costs BLACS
milliseconds. `queue_exchange` already keeps its slow work off the thread:
compiles, default shots and lyse submissions run on threads of their own.

### Acceptance criteria

- [ ] A batch of N entries reads the globals once and waits for at most one
      preparse.
- [ ] Each entry is still evaluated with its own values and none carries over.
- [ ] During a large batch, BLACS's `say_hello` is answered within its
      liveness timeout, by measurement.

### Blocked by

Slice 1. Slices 13 and 14 change the loop this replaces, so build the three
together.

### Review findings covered

- A batch holds the request thread past BLACS's liveness timeout (plausible).
- Per-entry globals rewrites and preparse waits (efficiency).

---

## Slice 16: A join reads no shot file and takes no lock on the GUI thread

### Type

`AFK`

### What to build

Two blocking reads sit on the GUI thread in the join path:

- "Add shots to last sequence" on a drained queue opens the last-sent shot's h5
  file under h5_lock's zlock (45 s default) only to learn a `sequence_id` the
  record already holds. The window stalls while lyse holds that file.
- `make_h5_files`' join branch calls `check_output_folder_update()`. That takes
  the `.next_sequence_index` zlock and reads the counter file, for a folder the
  join never uses; `rollover_shot_output_folder` refreshes it every 30 s anyway.

Record the offered row's sequence attributes beside `last_sent_from_queue`, so
the anchor's sequence is known from memory. Drop `check_output_folder_update()`
from the join branch. `forget_last_sent` keeps its current, tested behaviour.

### Acceptance criteria

- [ ] "Add shots to last sequence" joins the last-sent shot's sequence while
      that shot's file is held by another process.
- [ ] A join takes no shot-storage lock on the GUI thread.

### Blocked by

None - can start immediately.

### Review findings covered

- A drained add-shots reads the last-sent file on the GUI thread (below the
  cap).
- A join takes the counter-file lock on the GUI thread (below the cap).

---

## Slice 17: Abort stays enabled while a batch is pending

### Type

`AFK`

### What to build

`compile_and_queue_shots` enables Abort itself, while the queue worker disables
it through `set_abort_enabled(False)` via `inmain`, from a batch count read
before that call reaches the GUI thread. The two can cross. A probe ended with
`batches_pending` at 1 and the button disabled, so the operator cannot abort a
batch that is compiling. The race is older than the range; rapid remote
submissions make it likelier.

Let the queue decide both transitions from `batches_pending` under its lock,
and remove `compile_and_queue_shots`' own enable.

### Decision

Dropped: Slice 12 replaces Abort with Empty queue.

### Review findings covered

- Abort button enable/disable race (below the cap).

---

## Slice 18: The tests exercise what they claim, inside temporary directories

### Type

`AFK`

### What to build

- `test_a_submission_that_raises_has_queued_nothing_at_all` never takes its
  raising branch. Its stand-in clears the labscript file only from the second
  read, and a submission reads it once, so the test asserts only a successful
  submission. Make the submission genuinely fail partway, and assert that
  nothing was queued.
- `ShotStatusTests` queues rows at `/tmp/<shot_id>.h5`, and
  `test_deleting_the_row_in_front_lets_the_ones_behind_run_again` deletes
  `/tmp/head.h5` on the developer's machine. Queue the rows under a temporary
  directory.

### Acceptance criteria

- [ ] The raising test fails if a failed submission leaves shots queued.
- [ ] No test creates or deletes files outside a temporary directory.

### Blocked by

None - can start immediately.

### Review findings covered

- A test that checks nothing (below the cap).
- A test that deletes a real file in `/tmp` (sweep).

---

## Slice 19: Comments and docstrings say what the code does now

### Type

`AFK`

### What to build

Correct the prose that the code no longer matches, and apply the
labscript-style skill's limits on what is touched: brief docstrings on internal
code, and comments of one to three lines that say why, not what changed.

- `SubmitShotsTests`' docstring (`tests/test_remote_api.py:330`) still describes
  restoring the operator's expression between entries. Such batches are now
  refused.
- `handle_shot_status` says "Like the policy above", but no policy handler is
  above it.
- `ContinuingSequenceAnchorTests` is titled "What a remote submission carries on
  from". Remote submissions join by id; the anchor serves Engage only.
- Five places give "a default shot is not part of a sequence" as the reason to
  keep default shots out of the anchors, but default shots now belong to the
  day's default sequence: `__main__.py:4949`, `queueing.py:286` and `:463`, and
  `tests/test_queueing.py:696` and `:1407`. The rule is still right; restate
  its reason.
- `compile_and_queue_shots` describes the no-record path as "as before". Say
  what it does: it numbers after the anchor's file.
- `offer_shot`'s comment says the anchor is let go of only when its file is
  deleted, but `restore_state` also clears it on every configuration load.
- Past-tense accounts of guarded failures, in `DeletedAnchorTests`
  (`tests/test_queueing.py:3094`) and a `SubmissionAnchorTests` comment
  (`tests/test_remote_api.py:1132`), should state the hazard instead.
- `docs/source/usage.rst:431` says each press of Engage claims a new sequence
  index. A press that adds to a sequence claims none.
- `runmanager/remote.py:196` is a 125-character line in `submit_shots`'
  docstring.
- `Client.submit_shots` says an entry naming a global with a scan enabled is
  refused. A one-point scan is accepted, and Slice 6 leaves the boxes to the
  optimizer, so say what is refused: an entry that makes other than one shot.

### Acceptance criteria

- [ ] Each place above states current behaviour.
- [ ] No comment or docstring touched narrates history.

### Blocked by

Do this last, after the code slices, since several of them change what this
prose describes.

### Review findings covered

- The prose findings from all ten review angles and the sweep.

---

## Cleanup candidates for the excess-code review

These came from the reuse, simplification and altitude angles. They were not
verified one by one, and the master session's reviewers are studying runmanager
for exactly this. Several overlap slices above.

- Record the sequence attributes beside the last-sent anchor (Slice 16). The
  file read, its error wrappers, `get_sequence_attrs` and most of
  `get_sequence_attrs_to_extend` then become fallbacks for rows saved without
  attributes.
- `make_h5_files` keeps a `with_metadata=False` branch, a `sequence_globals`
  parameter and its own `get_sequence_attrs_to_extend` fallback that no
  production caller reaches.
- `compile_shots` keeps and coerces a caller-chosen `shot_id`, but no caller
  supplies one.
- The record's next run number is always the stored path's index plus one. The
  remote join also passes `SUBMISSION_MODE_ADD_SHOTS` only for its anchor to be
  overwritten from the record.
- The anchor rules are explained in three places, and `n_runs` in three.
  Comments and docstrings in the range outnumber its code lines by about two to
  one.
- The `shot_status` state strings and the refusal prefix exist only on
  runmanager's side, so labscript-optimization retypes them. Exposing them from
  `runmanager/remote.py`, which the client already imports, gives one source.
- The shot-id text rule is written twice (`compile_shots` and
  `_normalise_item`), and `get_shot_statuses` does not coerce, so id 7 and id
  '7' answer differently.
- `forget_last_sent` recomputes the anchor path that
  `get_last_sent_from_queue_filepath` computes.
- The tests re-declare stand-ins per file instead of in `tests/fixtures.py`:
  labconfigs, sequence dictionaries, `QueueManager` construction, and the
  `SubmittingApp` set-up.
- `_shot_for_entry` builds the (globals, frozen globals) pair separately from
  `expand_pending_shots`, and the two already differ.

# Lazy Compile Queue In `RunmanagerQueueSimple`

> **This is the design note as written, not a description of the code today.**
> Two things in it have since changed, on the `RunmanagerControl` branch, and
> the note says "dropped" in four places that are now wrong:
>
> * `queue_request_next()` is gone; `queue_exchange()` reaches
>   `RunManager.offer_shot()`. See the callout under "BLACS request path".
> * **A lazy shot that fails to compile is no longer dropped.** It stays at the
>   head of the queue and goes red with the reason, and it is not compiled
>   again. Same callout.
>
> Everything else still holds.

## Summary
Add a queue compile-mode selector to runmanager so queued shots can be submitted in either:
- `Eager compile`: current behavior
- `Lazy compile`: enqueue skeleton HDF5 shot files and compile them only when BLACS requests them

The implementation should reuse the existing queue/default-shot plumbing and existing globals-resolution helpers. The key rule for lazy compile is:
- scanned globals are frozen from Engage time
- all other globals are resolved from the current globals state at BLACS request time

Queued lazy shots keep the skeleton filepath created at Engage time. If a lazy shot fails to compile when BLACS requests it, it is dropped.

## Implementation Changes
### Runmanager queue model and UI
- Add a compile-mode pulldown to the existing queue pane with `Eager compile` and `Lazy compile`.
- Extend the existing queue state in `queueing.py` to store per-item mode metadata so the queue can contain a mix of eager and lazy items.
- Keep the widget display path-only; metadata should stay internal to the queue controller/state.
- Persist and restore the queue compile-mode selection in the existing queue state.

### Engage / queue population
- Keep using the existing run-file creation path that already produces the skeleton HDF5 files.
- In `Eager compile` mode, keep the current behavior: compile in the existing compile thread, then enqueue the compiled shot.
- In `Lazy compile` mode, do not compile during Engage. Instead, enqueue the skeleton shot path immediately, tagged as lazy.
- Do not introduce a second queueing pipeline. Reuse the current compile/queue flow and branch only at the “compile now vs queue skeleton now” decision point.

### BLACS request path

> **Later change.** `queue_request_next()` no longer exists. It, and the
> acceptance, rejection and completion calls that went with it, were replaced
> on the `RunmanagerControl` branch by one `queue_exchange(outcome,
> request_shot)`, which reaches `RunManager.offer_shot()`. Read
> `queue_request_next()` below as `offer_shot()`.
>
> **A lazy shot that fails to compile is no longer dropped.** The rest of this
> section still holds — it is still compiled off the request thread when BLACS
> asks for it, and still keeps its Engage-time filepath — but the row now stays
> at the head of the queue and goes red with the reason, which is what every
> other kind of failure does there since Retry/Drop was removed. Dropping it
> was indistinguishable from the queue draining normally, and that is exactly
> what a labscript file that could not compile looked like: rows vanishing one
> per request with no shot ever running.
>
> It is not compiled again either. A compile that fails partway leaves the
> `devices` and `calibrations` groups in the shot file, and labscript refuses to
> compile into a file that has them, so the same row can never succeed. Deleting
> it — which takes the half-written file with it — is the way on.

- Extend the existing `queue_request_next()` logic in runmanager.
- When the next queue item is eager, keep the current behavior and return its agnostic path immediately.
- When the next queue item is lazy:
  - read the shot’s frozen per-shot globals from the queued skeleton file
  - call the existing `get_queue_compile_globals(active_groups, shot_globals_overrides)` helper to rebuild globals using current values except scanned overrides
  - rewrite the existing queued skeleton file using the current globals result
  - compile that same file with the existing compile path already used for the default shot
  - return the agnostic path of that same file
- Keep the queued skeleton filepath and sequence attrs created at Engage time; do not allocate a new path at request time.
- If lazy compile fails at request time, drop that item from the queue and return nothing for that request.

### Existing abstractions to reuse
- Reuse `make_run_files()` / `make_single_run_file()` for skeleton-file creation and rewriting.
- Reuse `compile_run_file()` for the actual compile subprocess call.
- Reuse `get_queue_compile_globals()` for the “current globals except scanned ones” rule.
- Reuse the existing queue/default-shot `queue_request_next()` path rather than adding a second JIT protocol.
- Reuse the existing shot-globals HDF5 read path already available in the suite to recover the frozen scanned values from queued skeleton files.

## Public / Internal Interface Changes
- Internal queue state gains per-item compile-mode metadata.
- Runmanager queue settings gain one new persisted field: compile mode.
- No BLACS RPC shape change.
- No user-facing labscript or lyse API change.
- Queue semantics allow mixed eager and lazy items.

## Test Plan
- In `Eager compile` mode, verify existing behavior is unchanged:
  - Engage creates and compiles shots immediately
  - queued shots run in BLACS as before
- In `Lazy compile` mode, verify:
  - Engage creates skeleton HDF5 files and enqueues them without compiling
  - BLACS request triggers compile only at request time
  - a scanned global keeps its Engage-time per-shot value
  - a non-scanned global uses its current value at request time
  - the returned shot path is the queued skeleton path, not a new path
- Verify a mixed queue:
  - eager item then lazy item
  - lazy item then eager item
- Verify `View shot(s)` in lazy mode:
  - shot is sent to runviewer only after request-time compile succeeds
- Verify failure behavior:
  - a lazy compile failure drops that queue item
  - subsequent requests continue with later items/default-shot behavior
- Verify queue state restore:
  - compile-mode pulldown persists
  - mixed queued items restore with correct mode metadata

## Assumptions And Defaults
- Active branch is `RunmanagerQueueSimple` in every touched repo.
- This batch should touch `runmanager` only unless implementation proves otherwise.
- Lazy queue items keep their Engage-time filepath and sequence attrs.
- Compile failure for a lazy queue item drops that item.
- The existing default-shot JIT compile path remains separate and unchanged in behavior.

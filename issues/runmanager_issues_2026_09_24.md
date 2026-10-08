# runmanager cleanup candidates

One simplification left in runmanager's tests, found by a code review. It has
not been verified.

- The tests re-declare stand-ins per file instead of in `tests/fixtures.py`:
  sequence dictionaries, `QueueManager` construction, and the `SubmittingApp`
  set-up. `labconfig` is already shared there. It waits for the paused test
  cleanup, which cuts tests first.

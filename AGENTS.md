# Working in runmanager

## Tests must not invoke the application

`runmanager/__main__.py` builds a `Splash` and calls `.show()` at module scope,
and `Splash.__init__` creates the `QApplication`. `splash.hide()` runs only
inside `if __name__ == '__main__'`. So **importing `runmanager.__main__` puts a
banner on the user's screen and leaves it there** — during a test run, during a
REPL session, during anything.

Prefer importing the leaf module: `runmanager/queueing.py`, `globals_file.py`
and `analysis_submission.py` have no import-time side effects and need no
stubbing.

When a test must borrow from `__main__` — to exercise a real method rather than
a description of it — **import it from `tests/fixtures.py`, never from
`runmanager.__main__`**:

    from fixtures import RunManager, RemoteServer

`fixtures` stubs `labscript_utils.splash` in `sys.modules` and then imports the
application once, so no `QApplication` is created at all rather than one being
created and hidden. The stub only works if it is installed before that import,
and one module owning both is what keeps that ordering constraint out of every
test file, where a reordered import block would quietly undo it. Add what you
need to `fixtures.py` rather than importing the application somewhere new.

Tests needing a `QApplication` build their own. A test that genuinely renders —
geometry or pixel assertions — must still call `.show()`, because Qt does not
lay out or paint an unshown widget; run those under `QT_QPA_PLATFORM=offscreen`.
`blacs/tests/test_plugins_compat.py` is the worked example of the other
technique: loading a module by path with its dependencies stubbed, restoring
them in a `finally`. Do not copy that restore into `fixtures.py`. It can restore
because it discards the module it loaded; ours leaves the application imported
for the rest of the run, so putting the real splash module back would leave the
application holding the stub while anything importing it later got the real one
— two `Splash` classes in one process.

## Running the tests

From inside this repository, never from the workspace root — a workspace-root
cwd puts a `zprocess/` directory on the path that shadows the installed one:

    ~/miniforge3/envs/labscript/bin/python -m pytest tests -q

The `python` first on `PATH` has neither pytest nor the suite; the `labscript`
conda environment has both.

Nothing else is needed on the command line. `tests/conftest.py` sets
`LABSCRIPT_NO_ERROR_DIALOG`, which stops `labscript_utils.excepthook` spawning a
tkinter window per unhandled exception — it has to be set before that module is
imported, because it reads the variable once into a constant at import time.
Exceptions are still logged and still reach stderr.

`setdefault` leaves an explicit setting alone, so `LABSCRIPT_NO_ERROR_DIALOG=0`
gives you the dialog back, as do `false`, `no`, `off`, the empty string and not
setting it at all; anything else suppresses it. A test of the dialog itself can
instead assign to `labscript_utils.excepthook.NO_ERROR_DIALOG`, which the module
reads where it uses it.

There is no `QT_QPA_PLATFORM` here, unlike the repositories whose layout tests
show a window. Nothing in this suite renders, so there is nothing to send
offscreen.

## The BLACS handover is a two-repo contract

runmanager owns the shot queue; BLACS asks for shots and reports what became of
them. Neither half can be read from the other's source, so the rules are written
down in `blacs/docs/source/shot-management.rst`, which is the contract of
record. Change one side against it, not against the other side's code.

Cross-repo changes must be on **the same branch name in every repository they
touch**, or the halves ship apart and neither works.

## More

The workspace `AGENTS.md`, one directory up, carries the longer reasoning and
the conventions shared across the suite. This file exists because work often
happens inside one repository with no view of that one.

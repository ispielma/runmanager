"""Settings that have to be in place before the suite is imported.

Running the tests is not supposed to put anything on the screen of whoever runs
them. `labscript_utils.excepthook` installs a `sys.excepthook` that spawns a
tkinter subprocess window per unhandled exception, up to ten per process, and
importing runmanager installs it. A test that raises where the runner cannot
catch it -- in a thread, say -- then leaves windows to be closed by hand.

The timing matters, which is why this is here and not in a fixture. The
excepthook reads the variable once, at its own import, into a module-level
constant; setting it afterwards does nothing. pytest imports conftest before the
test modules, and it is a test module importing `fixtures` that first pulls in
the excepthook, so this runs in time.

Exceptions are still logged and still reach stderr, so nothing diagnostic is
lost.

`setdefault` leaves an explicit setting alone, so exporting the variable
yourself decides: `LABSCRIPT_NO_ERROR_DIALOG=0` gives you the dialog back, as do
`false`, `no`, `off`, the empty string, and not setting it at all.

That was not always true, and the difference is worth keeping rather than
deleting. Until labscript-utils `8719676` the variable was read as
`bool(os.environ.get(...))`, so *any* non-empty value suppressed the dialog and
`=0` suppressed it exactly as `=1` did -- which bit hardest at a test of the
dialog itself, the one case the rule exempts. A comment elsewhere in the suite
still describing that is stale, not describing a case this one misses.

A test of the dialog can also set `labscript_utils.excepthook.NO_ERROR_DIALOG`
directly. The module reads that name where it uses it, so assigning to it works
at any point, whereas the environment is still consulted only once.

Only this one variable. runmanager's tests build widgets but never show one, so
there is nothing here to render and `QT_QPA_PLATFORM` would be noise -- the
repositories that set it do so because their layout tests must show a window
for Qt to lay it out at all.
"""
import os

os.environ.setdefault('LABSCRIPT_NO_ERROR_DIALOG', '1')

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

`setdefault` does not give you the dialog back, though, and it is worth knowing
why before you try. The excepthook reads the variable as
`bool(os.environ.get(...))`, so *any* non-empty value suppresses the dialog --
`LABSCRIPT_NO_ERROR_DIALOG=0` suppresses it exactly as `=1` does. Only unsetting
the variable, or setting it to the empty string, turns the dialog back on. The
place this bites is a test of the error dialog itself, the one case the rule is
meant to exempt: reaching for `=0` there produces no dialog and no clue why. Set
`labscript_utils.excepthook.NO_ERROR_DIALOG = False` in the test instead, which
says what it means and does not depend on the environment at all.

Only this one variable. runmanager's tests build widgets but never show one, so
there is nothing here to render and `QT_QPA_PLATFORM` would be noise -- the
repositories that set it do so because their layout tests must show a window
for Qt to lay it out at all.
"""
import os

os.environ.setdefault('LABSCRIPT_NO_ERROR_DIALOG', '1')

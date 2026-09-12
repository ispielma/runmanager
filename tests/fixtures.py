"""Stand-ins shared by runmanager's tests.

Importing ``runmanager.__main__`` builds a ``Splash`` and calls ``show()`` on it
at module scope, and the ``splash.hide()`` that would take it down again runs
only under ``if __name__ == '__main__'``. So a test that imports the module puts
the startup banner on the screen of whoever ran the suite and leaves it there,
for the life of the process.

Stubbing ``labscript_utils.splash`` before that import fixes it at the source
rather than papering over it: with the stub in place no ``QApplication`` is
created at all, so there is no window to leak. Hiding the splash afterwards
would not do as well, because the application would still have been built and
the banner would still have flickered up.

The stub only works if it is installed *before* ``runmanager.__main__`` is
imported, which is an ordering constraint no test file should have to carry --
a reordered import block would quietly bring the banner back. So this module
owns both the stub and the import, and re-exports what the suite borrows from
the application. Tests import those names from here and never from
``runmanager.__main__`` directly, which is the same arrangement BLACS uses in
``blacs/tests/fixtures.py``.

Tests that need a ``QApplication`` build their own; ``test_queueing`` and
``test_blacs_status`` both already do. Nothing here creates one, and nothing
here shows a widget.

``blacs/tests/test_plugins_compat.py`` is the worked example of this technique
applied to a leaf module, loading it by path with its dependencies stubbed.
"""
import sys
import types
import warnings


class Splash(object):
    """Enough of ``labscript_utils.splash.Splash`` to be imported and called.

    The real one builds the ``QApplication`` in its ``__init__``, which is the
    thing being avoided, so every method here does nothing.
    """

    def __init__(self, *args, **kwargs):
        pass

    def show(self):
        pass

    def hide(self):
        pass

    def update_text(self, text):
        pass


def _stub_splash_module():
    """Put a splash module in ``sys.modules`` that cannot build a window.

    This is not undone afterwards. ``runmanager.__main__`` stays imported for
    the life of the test process, so restoring the real module would leave the
    application holding a reference to the stub while anything importing it
    later got the real one -- two different ``Splash`` classes for one process.
    Nothing in the suite wants the real splash.
    """
    module = types.ModuleType('labscript_utils.splash')
    module.Splash = Splash
    # runmanager's __main__ imports Splash and nothing else from here. BLACS's
    # also imports get_qapplication, so a stub shared between the two repos
    # would need that name as well; this one is deliberately only what
    # runmanager asks for, so that it fails loudly if that changes.
    sys.modules['labscript_utils.splash'] = module


_stub_splash_module()

with warnings.catch_warnings():
    # labscript_utils.excepthook installs a warning logger that calls the
    # deprecated logging.warn, which warns in turn, so any warning raised while
    # it is installed recurses until the stack runs out. Importing the
    # application installs it, so the import is done under a suppressed filter.
    # Done once, here, so that no test module has to repeat it.
    warnings.simplefilter('ignore')
    import runmanager.__main__ as main_module  # noqa: E402
    from runmanager.__main__ import (  # noqa: E402
        FingerTabWidget,
        RemoteServer,
        RunManager,
        TreeView,
    )

# The module itself, for the tests that patch names in its namespace rather
# than borrow a method from it. Exported for the same reason the classes are:
# so that no test file names runmanager.__main__ and inherits the ordering
# constraint along with it.
__all__ = [
    'FingerTabWidget',
    'RemoteServer',
    'RunManager',
    'Splash',
    'TreeView',
    'main_module',
]

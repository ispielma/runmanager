"""The save panel behind *New globals file*.

A save panel decides the extension of the file it returns. Given a name that
already has one it keeps that one, and typing replaces only the stem. Given a
bare folder it has no extension to preserve and supplies one of its own, taken
from the host's type database rather than from the name filter the dialog asked
for -- so the file that comes back need not be a ``.toml`` file at all.

What is pinned here is that the handler hands the panel a filename carrying the
extension the new globals file is meant to have, and that the guard behind the
panel still supplies that extension for a name returned without one.

The panel belongs to the operating system and cannot be driven from a test, so
the seam under test is the call into it: ``getSaveFileName`` is replaced and the
arguments it was given are read back.
"""
import os
import shutil
import tempfile
import unittest

# fixtures stubs the splash and does the guarded import of the
# application, once, for every test module. Importing
# runmanager.__main__ here instead would show the startup banner.
from fixtures import RunManager, main_module


class FakeRunManager(object):
    """Enough of a RunManager for ``on_new_globals_file_clicked`` to run.

    ``RunManager.__init__`` builds the whole application, which this one
    handler has no use for.
    """

    on_new_globals_file_clicked = RunManager.on_new_globals_file_clicked

    def __init__(self, folder):
        # The dialog's parent widget. Never reached, because the call into the
        # panel is intercepted.
        self.ui = None
        self.last_opened_globals_folder = folder
        self.opened = []

    def open_globals_file(self, globals_file):
        self.opened.append(globals_file)


class NewGlobalsFileDialogTests(unittest.TestCase):
    """What the handler asks the panel for, and what it does with the answer."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

        # Recorded arguments of the call into the panel, and the name the panel
        # is to hand back. A falsy name is a cancelled dialog.
        self.calls = []
        self.answer = ''

        self.file_dialog = main_module.QtWidgets.QFileDialog
        self.saved_get_save_file_name = self.file_dialog.getSaveFileName
        self.file_dialog.getSaveFileName = self.get_save_file_name
        self.addCleanup(self.restore)

        self.app = FakeRunManager(self.folder)

    def restore(self):
        self.file_dialog.getSaveFileName = self.saved_get_save_file_name

    def get_save_file_name(self, parent, caption, directory, filter=None):
        self.calls.append(directory)
        return (self.answer, filter)

    def suggested_path(self):
        """The name the panel was offered."""
        self.app.on_new_globals_file_clicked()
        self.assertEqual(len(self.calls), 1, 'the panel is opened once')
        return self.calls[0]

    def test_the_offered_filename_carries_the_toml_extension(self):
        # Cancelled, so the handler goes no further than the panel.
        self.answer = ''

        suggested = self.suggested_path()

        self.assertTrue(
            suggested.lower().endswith('.toml'),
            'the panel keeps the extension it is given and replaces only the '
            'stem, so the name offered has to end in .toml; got %r' % suggested,
        )

    def test_the_offered_filename_sits_in_the_last_opened_folder(self):
        self.answer = ''

        suggested = self.suggested_path()

        self.assertEqual(
            os.path.dirname(suggested),
            self.folder,
            'the panel still opens where globals files were last opened',
        )

    def test_a_name_returned_without_the_extension_is_given_one(self):
        # A user who clears the extension the panel offered, or a panel that
        # returns a bare stem, still gets a .toml file.
        self.answer = os.path.join(self.folder, 'test')

        self.app.on_new_globals_file_clicked()

        self.assertEqual(
            self.app.opened,
            [os.path.join(self.folder, 'test.toml')],
            'the created file is the returned name plus the extension',
        )
        self.assertTrue(
            os.path.isfile(os.path.join(self.folder, 'test.toml')),
            'and it is that name that was written',
        )

    def test_a_name_returned_with_the_extension_keeps_it_as_it_is(self):
        self.answer = os.path.join(self.folder, 'test.toml')

        self.app.on_new_globals_file_clicked()

        self.assertEqual(
            self.app.opened,
            [os.path.join(self.folder, 'test.toml')],
            'the extension is not doubled',
        )


if __name__ == '__main__':
    unittest.main()

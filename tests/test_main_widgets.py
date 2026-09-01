"""The custom widgets runmanager's main window is built from.

These three calls were replaced when runmanager moved to PyQt6, and then came
back: merging branches that predated the move restored the file's older lines,
and Production went back to crashing on startup. Nothing noticed, because the
widgets are only built when the window is. Constructing them here is enough --
QFontMetrics.width, QApplication.globalStrut and QPalette.Foreground were all
removed outright in Qt6, so any of them raises AttributeError the moment its
line runs.
"""
import unittest

import labscript_utils.h5_lock  # must precede h5py, as runmanager does
import h5py

from qtutils.qt import QtCore, QtGui, QtWidgets

import runmanager.__main__ as runmanager_main


def a_qapplication():
    """One QApplication for the whole process, as Qt requires."""
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication(['test'])


class FingerTabBarTests(unittest.TestCase):
    """The vertical tab bar down the side of the globals pane."""

    def setUp(self):
        self.qapplication = a_qapplication()
        self.tabs = QtWidgets.QTabWidget()
        self.bar = runmanager_main.FingerTabBarWidget(self.tabs)
        self.tabs.setTabBar(self.bar)
        self.addCleanup(self.tabs.deleteLater)

    def test_a_tab_is_sized_to_fit_its_name(self):
        # tabSizeHint measures the label: QFontMetrics.width in Qt5,
        # horizontalAdvance in Qt6. Short names all come out at minwidth, so
        # the long one is what shows the measurement happened at all.
        self.tabs.addTab(QtWidgets.QWidget(), 'x')
        self.tabs.addTab(QtWidgets.QWidget(), 'a globals group with a very ' * 3)

        narrow = self.bar.tabSizeHint(0)
        wide = self.bar.tabSizeHint(1)

        self.assertEqual(narrow.width(), self.bar.minwidth)
        self.assertGreater(wide.width(), self.bar.minwidth)

    def test_a_tab_bar_taller_than_its_parent_paints(self):
        # Overflowing the parent is what reaches the scroll-button branch,
        # which is where globalStrut() was.
        for i in range(6):
            self.tabs.addTab(QtWidgets.QWidget(), 'tab %d' % i)
        self.tabs.resize(140, 40)
        self.tabs.show()
        self.addCleanup(self.tabs.hide)

        self.bar.repaint()
        self.qapplication.processEvents()

        self.assertIsNotNone(
            self.bar.paint_clip, 'the overflow branch did not run'
        )


class ItemViewTests(unittest.TestCase):
    """The globals and shot-output views, which recolour their selection."""

    def setUp(self):
        self.qapplication = a_qapplication()

    def test_the_views_take_their_highlight_colour(self):
        # __init__ reads the palette's foreground: QPalette.Foreground in Qt5,
        # WindowText in Qt6.
        for widget in (runmanager_main.TreeView(), runmanager_main.TableView()):
            self.addCleanup(widget.deleteLater)
            palette = widget.palette()
            self.assertEqual(
                palette.color(
                    QtGui.QPalette.Active, QtGui.QPalette.Highlight
                ).name(),
                QtGui.QColor(widget.COLOR_HIGHLIGHT).name(),
                type(widget).__name__,
            )


if __name__ == '__main__':
    unittest.main()

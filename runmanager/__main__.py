#####################################################################
#                                                                   #
# __main__.py                                                       #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program runmanager, in the labscript     #
# suite (see http://labscriptsuite.org), and is licensed under the  #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""Runmanager GUI and supporting code
"""

import queue
import os
import sys
import labscript_utils.excepthook

APPLICATION_NAME = 'runmanager'
RUNMANAGER_DIR = os.path.dirname(__file__)

# Associate app windows with OS menu shortcuts:
import desktop_app
desktop_app.set_process_appid(APPLICATION_NAME)

# Splash screen
from labscript_utils.splash import Splash
splash = Splash(
    os.path.join(RUNMANAGER_DIR, 'runmanager.svg'), application_name=APPLICATION_NAME
)
splash.show()

splash.update_text('importing standard library modules')
import time
import contextlib
import subprocess
import threading
import ast
import pprint
import traceback
import signal
import unicodedata
from pathlib import Path

splash.update_text('importing matplotlib')
# Evaluation of globals happens in a thread with the pylab module imported.
# Although we don't care about plotting, importing pylab makes Qt calls. We
# can't have that from a non main thread, so we'll just disable matplotlib's
# GUI integration:
import matplotlib
matplotlib.use('Agg')

from qtutils.qt import QtCore, QtGui, QtWidgets
from qtutils.qt.QtCore import pyqtSignal as Signal

splash.update_text('importing labscript suite modules')
from labscript_utils.ls_zprocess import zmq_get, ProcessTree, ZMQServer
from labscript_utils.labconfig import (
    LabConfig,
    LabscriptApplication,
    save_appconfig,
    load_appconfig,
)
from labscript_utils.file_utils import next_available_indexed_filepath
from labscript_utils.lookup_format import unescape_braces
from labscript_utils.setup_logging import setup_logging
import labscript_utils.shared_drive as shared_drive
from labscript_utils import dedent
from zprocess import raise_exception_in_thread
import runmanager
import runmanager.remote
from runmanager.analysis_submission import AnalysisSubmission
from runmanager.queueing import (
    COMPILE_MODE_EAGER,
    COMPILE_MODE_LAZY,
    EMPTY_QUEUE_DEFAULT_LABSCRIPT,
    EMPTY_QUEUE_NOTHING,
    QueueManager,
    RunmanagerQueueWidget,
)

from qtutils import (
    inmain,
    inmain_decorator,
    UiLoader,
    inthread,
    DisconnectContextManager,
    qtlock,
)
from labscript_utils.qtwidgets.outputbox import OutputBox
import qtutils.icons

GLOBAL_MONOSPACE_FONT = "Consolas" if os.name == 'nt' else "Ubuntu Mono"

runmanager_dir = RUNMANAGER_DIR

process_tree = ProcessTree.instance()

# Set a meaningful name for zprocess.locking's client id:
process_tree.zlock_client.set_process_name(APPLICATION_NAME)

SUBMISSION_MODE_NEW_FOLDER = 'new_folder'
SUBMISSION_MODE_ADD_SHOTS = 'add_shots'
SUBMISSION_MODE_NEW_FOLDER_CLEAR_QUEUE = 'new_folder_clear_queue'
SUBMISSION_MODE_ADD_SHOTS_CLEAR_QUEUE = 'add_shots_clear_queue'


def log_if_global(g, g_list, message):
    """logs a message if the global name "g" is in "g_list"
    
    useful if you want to print out a message inside a loop over globals,
    but only for a particular global (or set of globals).
    
    If g_list is empty, then it will use the hardcoded list below
    (useful if you want to change the behaviour globally)    
    """
    if not isinstance(g_list, list):
        g_list = [g_list]
        
    if not g_list:
        g_list = [] # add global options here
    
    if g in g_list:
        logger.info(message)

    
def composite_colors(r0, g0, b0, a0, r1, g1, b1, a1):
    """composite a second colour over a first with given alpha values and return the
    result"""
    a0 /= 255
    a1 /= 255
    a = a0 + a1 - a0 * a1
    r = (a1 * r1 + (1 - a1) * a0 * r0) / a
    g = (a1 * g1 + (1 - a1) * a0 * g0) / a
    b = (a1 * b1 + (1 - a1) * a0 * b0) / a
    return [int(round(x)) for x in (r, g, b, 255 * a)]


def _relative_luminance(color):
    def channel(value):
        value /= 255.0
        if value <= 0.03928:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.red())
        + 0.7152 * channel(color.green())
        + 0.0722 * channel(color.blue())
    )


def _contrast_ratio(color_a, color_b):
    luminance_a = _relative_luminance(color_a)
    luminance_b = _relative_luminance(color_b)
    lighter = max(luminance_a, luminance_b)
    darker = min(luminance_a, luminance_b)
    return (lighter + 0.05) / (darker + 0.05)


def expression_cell_colors(palette=None):
    if palette is None:
        qapplication = QtWidgets.QApplication.instance()
        palette = qapplication.palette() if qapplication is not None else QtGui.QPalette()
    text_color = palette.color(QtGui.QPalette.Text)
    active_candidates = [QtGui.QColor('#D7ECFF'), QtGui.QColor('#234A75')]
    error_candidates = [QtGui.QColor('#F79494'), QtGui.QColor('#8A2E2E')]
    active = max(active_candidates, key=lambda color: _contrast_ratio(text_color, color))
    error = max(error_candidates, key=lambda color: _contrast_ratio(text_color, color))
    return error.name(), active.name()


def hidden_unicode_issues(text):
    issues = []
    for index, char in enumerate(text, 1):
        category = unicodedata.category(char)
        if char in '\t\n\r':
            continue
        if not char.isprintable() or category in {'Cf', 'Cc', 'Cs', 'Co'}:
            issues.append(
                'col %d: U+%04X %s'
                % (index, ord(char), unicodedata.name(char, 'UNKNOWN'))
            )
    return issues


def tooltip_with_hidden_unicode_warning(base_tooltip, text):
    issues = hidden_unicode_issues(text)
    if not issues:
        return base_tooltip
    warning = 'Contains hidden Unicode character(s): ' + '; '.join(issues)
    if base_tooltip:
        return base_tooltip + '\n' + warning
    return warning


@inmain_decorator()
def error_dialog(message):
    QtWidgets.QMessageBox.warning(app.ui, 'runmanager', message)


@inmain_decorator()
def question_dialog(message):
    reply = QtWidgets.QMessageBox.question(app.ui, 'runmanager', message,
                                       QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
    return (reply == QtWidgets.QMessageBox.Yes)


@contextlib.contextmanager
def nested(*contextmanagers):
    if contextmanagers:
        with contextmanagers[0]:
            with nested(*contextmanagers[1:]):
                yield
    else:
        yield


def scroll_view_to_row_if_current(view, item):
    """Checks to see if the item is in the row of the current item. If it is, scrolls
    the treeview/tableview vertically to ensure that row is visible. This is done by
    recording the horizontal scroll position, then using view.scrollTo(), and then
    restoring the horizontal position"""
    horizontal_scrollbar = view.horizontalScrollBar()
    existing_horizontal_position = horizontal_scrollbar.value()
    index = item.index()
    current_row = view.currentIndex().row()
    if index.row() == current_row:
        view.scrollTo(index)
        horizontal_scrollbar.setValue(existing_horizontal_position)


class FingerTabBarWidget(QtWidgets.QTabBar):

    """A TabBar with the tabs on the left and the text horizontal. Credit to
    @LegoStormtroopr, https://gist.github.com/LegoStormtroopr/5075267. We will
    promote the TabBar from the ui file to one of these."""

    def __init__(self, parent=None, minwidth=180, minheight=30, **kwargs):
        QtWidgets.QTabBar.__init__(self, parent, **kwargs)
        self.minwidth = minwidth
        self.minheight = minheight
        self.iconPosition = kwargs.pop('iconPosition', QtWidgets.QTabWidget.West)
        self._movable = None
        self.tab_movable = {}
        self.paint_clip = None

    def setMovable(self, movable, index=None):
        """Set tabs movable on an individual basis, or set for all tabs if no
        index specified"""
        if index is None:
            self._movable = movable
            self.tab_movable = {}
            QtWidgets.QTabBar.setMovable(self, movable)
        else:
            self.tab_movable[int(index)] = bool(movable)

    def isMovable(self, index=None):
        if index is None:
            if self._movable is None:
                self._movable = QtWidgets.QTabBar.isMovable(self)
            return self._movable
        return self.tab_movable.get(index, self._movable)

    def indexAtPos(self, point):
        for index in range(self.count()):
            if self.tabRect(index).contains(point):
                return index

    def mousePressEvent(self, event):
        index = self.indexAtPos(event.pos())
        if not self.tab_movable.get(index, self.isMovable()):
            QtWidgets.QTabBar.setMovable(self, False)  # disable dragging until they release the mouse
        return QtWidgets.QTabBar.mousePressEvent(self, event)

    def mouseReleaseEvent(self, event):
        if self.isMovable():
            # Restore this in case it was temporarily disabled by mousePressEvent
            QtWidgets.QTabBar.setMovable(self, True)
        return QtWidgets.QTabBar.mouseReleaseEvent(self, event)

    def tabLayoutChange(self):
        total_height = 0
        for index in range(self.count()):
            tabRect = self.tabRect(index)
            total_height += tabRect.height()
        if total_height > self.parent().height():
            # Don't paint over the top of the scroll buttons:
            scroll_buttons_area_height = 2*max(self.style().pixelMetric(QtWidgets.QStyle.PM_TabBarScrollButtonWidth),
                                               qapplication.globalStrut().width())
            self.paint_clip = self.width(), self.parent().height() - scroll_buttons_area_height
        else:
            self.paint_clip = None

    def paintEvent(self, event):
        painter = QtWidgets.QStylePainter(self)
        if self.paint_clip is not None:
            painter.setClipRect(0, 0, *self.paint_clip)

        option = QtWidgets.QStyleOptionTab()
        for index in range(self.count()):
            tabRect = self.tabRect(index)
            self.initStyleOption(option, index)
            painter.drawControl(QtWidgets.QStyle.CE_TabBarTabShape, option)
            if not self.tabIcon(index).isNull():
                icon = self.tabIcon(index).pixmap(self.iconSize())
                alignment = QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
                tabRect.moveLeft(10)
                painter.drawItemPixmap(tabRect, alignment, icon)
                tabRect.moveLeft(self.iconSize().width() + 15)
            else:
                tabRect.moveLeft(10)
            painter.drawText(tabRect, QtCore.Qt.AlignVCenter, self.tabText(index))
        if self.paint_clip is not None:
            x_clip, y_clip = self.paint_clip
            painter.setClipping(False)
            palette = self.palette()
            mid_color = palette.color(QtGui.QPalette.Mid)
            painter.setPen(mid_color)
            painter.drawLine(0, y_clip, x_clip, y_clip)
        painter.end()


    def tabSizeHint(self, index):
        fontmetrics = QtGui.QFontMetrics(self.font())
        text_width = fontmetrics.width(self.tabText(index))
        text_height = fontmetrics.height()
        height = text_height + 15
        height = max(self.minheight, height)
        width = text_width + 15

        button = self.tabButton(index, QtWidgets.QTabBar.RightSide)
        if button is not None:
            height = max(height, button.height() + 7)
            # Same amount of space around the button horizontally as it has vertically:
            width += button.width() + height - button.height()
        width = max(self.minwidth, width)
        return QtCore.QSize(width, height)

    def setTabButton(self, index, geometry, button):
        if not isinstance(button, TabToolButton):
            raise TypeError('Not a TabToolButton, won\'t paint correctly. Use a TabToolButton')
        result = QtWidgets.QTabBar.setTabButton(self, index, geometry, button)
        button.move(*button.get_correct_position())
        return result


class TabToolButton(QtWidgets.QToolButton):
    def __init__(self, *args, **kwargs):
        QtWidgets.QToolButton.__init__(self, *args, **kwargs)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

    def paintEvent(self, event):
        painter = QtWidgets.QStylePainter(self)
        paint_clip = self.parent().paint_clip
        if paint_clip is not None:
            point = QtCore.QPoint(*paint_clip)
            global_point = self.parent().mapToGlobal(point)
            local_point = self.mapFromGlobal(global_point)
            painter.setClipRect(0, 0, local_point.x(), local_point.y())
        option = QtWidgets.QStyleOptionToolButton()
        self.initStyleOption(option)
        painter.drawComplexControl(QtWidgets.QStyle.CC_ToolButton, option)

    def get_correct_position(self):
        parent = self.parent()
        for index in range(parent.count()):
            if parent.tabButton(index, QtWidgets.QTabBar.RightSide) is self:
                break
        else:
            raise LookupError('Tab not found')
        tabRect = parent.tabRect(index)
        tab_x, tab_y, tab_width, tab_height = tabRect.x(), tabRect.y(), tabRect.width(), tabRect.height()
        size = self.sizeHint()
        width = size.width()
        height = size.height()
        padding = int((tab_height - height) / 2)
        correct_x = tab_x + tab_width - width - padding
        correct_y = tab_y + padding
        return correct_x, correct_y

    def moveEvent(self, event):
        try:
            correct_x, correct_y = self.get_correct_position()
        except LookupError:
            return # Things aren't initialised yet
        if self.x() != correct_x or self.y() != correct_y:
            # Move back! I shall not be moved!
            self.move(correct_x, correct_y)
        return QtWidgets.QToolButton.moveEvent(self, event)


class FingerTabWidget(QtWidgets.QTabWidget):

    """A QTabWidget equivalent which uses our FingerTabBarWidget"""

    def __init__(self, parent, *args):
        QtWidgets.QTabWidget.__init__(self, parent, *args)
        self.setTabBar(FingerTabBarWidget(self))

    def addTab(self, *args, **kwargs):
        closeable = kwargs.pop('closable', False)
        index = QtWidgets.QTabWidget.addTab(self, *args, **kwargs)
        self.setTabClosable(index, closeable)
        return index

    def setTabClosable(self, index, closable):
        right_button = self.tabBar().tabButton(index, QtWidgets.QTabBar.RightSide)
        if closable:
            if not right_button:
                # Make one:
                close_button = TabToolButton(self.parent())
                close_button.setIcon(QtGui.QIcon(':/qtutils/fugue/cross'))
                self.tabBar().setTabButton(index, QtWidgets.QTabBar.RightSide, close_button)
                close_button.clicked.connect(lambda: self._on_close_button_clicked(close_button))
        else:
            if right_button:
                # Get rid of it:
                self.tabBar().setTabButton(index, QtWidgets.QTabBar.RightSide, None)

    def _on_close_button_clicked(self, button):
        for index in range(self.tabBar().count()):
            if self.tabBar().tabButton(index, QtWidgets.QTabBar.RightSide) is button:
                self.tabCloseRequested.emit(index)
                break


class ItemView(object):
    """Mixin for QTableView and QTreeView that emits a custom signal leftClicked(index)
    after a left click on a valid index, and doubleLeftClicked(index) (in addition) on
    double click. Also has modified tab and arrow key behaviour and custom selection
    highlighting."""
    leftClicked = Signal(QtCore.QModelIndex)
    doubleLeftClicked = Signal(QtCore.QModelIndex)

    COLOR_HIGHLIGHT = "#40308CC6" # Semitransparent blue

    def __init__(self, *args):
        super(ItemView, self).__init__(*args)
        self._pressed_index = None
        self._double_click = False
        self.setAutoScroll(False)
        p = self.palette()
        for group in [QtGui.QPalette.Active, QtGui.QPalette.Inactive]:
            p.setColor(
                group,
                QtGui.QPalette.Highlight,
                QtGui.QColor(self.COLOR_HIGHLIGHT))
            p.setColor(
                group,
                QtGui.QPalette.HighlightedText,
                p.color(QtGui.QPalette.Active, QtGui.QPalette.Foreground)
            )
        self.setPalette(p)

    def mousePressEvent(self, event):
        result = super(ItemView, self).mousePressEvent(event)
        index = self.indexAt(event.pos())
        if event.button() == QtCore.Qt.LeftButton and index.isValid():
            self._pressed_index = self.indexAt(event.pos())
        return result

    def leaveEvent(self, event):
        result = super(ItemView, self).leaveEvent(event)
        self._pressed_index = None
        self._double_click = False
        return result

    def mouseDoubleClickEvent(self, event):
        # Ensure our left click event occurs regardless of whether it is the
        # second click in a double click or not
        result = super(ItemView, self).mouseDoubleClickEvent(event)
        index = self.indexAt(event.pos())
        if event.button() == QtCore.Qt.LeftButton and index.isValid():
            self._pressed_index = self.indexAt(event.pos())
            self._double_click = True
        return result

    def mouseReleaseEvent(self, event):
        result = super(ItemView, self).mouseReleaseEvent(event)
        index = self.indexAt(event.pos())
        if event.button() == QtCore.Qt.LeftButton and index.isValid() and index == self._pressed_index:
            self.leftClicked.emit(index)
            if self._double_click:
                self.doubleLeftClicked.emit(index)
        self._pressed_index = None
        self._double_click = False
        return result

    def keyPressEvent(self, event):
        if event.key() in [QtCore.Qt.Key_Space, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Return]:
            item = self.model().itemFromIndex(self.currentIndex())
            if item.isEditable():
                # Space/enter edits editable items:
                self.edit(self.currentIndex())
            else:
                # Space/enter on non-editable items simulates a left click:
                self.leftClicked.emit(self.currentIndex())
        return super(ItemView, self).keyPressEvent(event)

    def moveCursor(self, cursor_action, keyboard_modifiers):
        current_index = self.currentIndex()
        current_row, current_column = current_index.row(), current_index.column()
        if cursor_action == QtWidgets.QAbstractItemView.MoveUp:
            return current_index.sibling(current_row - 1, current_column)
        elif cursor_action == QtWidgets.QAbstractItemView.MoveDown:
            return current_index.sibling(current_row + 1, current_column)
        elif cursor_action == QtWidgets.QAbstractItemView.MoveLeft:
            return current_index.sibling(current_row, current_column - 1)
        elif cursor_action == QtWidgets.QAbstractItemView.MoveRight:
            return current_index.sibling(current_row, current_column + 1)
        elif cursor_action == QtWidgets.QAbstractItemView.MovePrevious:
            return current_index.sibling(current_row, current_column - 1)
        elif cursor_action == QtWidgets.QAbstractItemView.MoveNext:
            return current_index.sibling(current_row, current_column + 1)
        else:
            return super(ItemView, self).moveCursor(cursor_action, keyboard_modifiers)


class TreeView(ItemView, QtWidgets.QTreeView):
    """Treeview version of our customised ItemView"""
    def __init__(self, parent=None):
        super(TreeView, self).__init__(parent)
        # Set columns to their minimum size, disabling resizing. Caller may still
        # configure a specific section to stretch:
        self.header().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents
        )
        self.setItemDelegate(ItemDelegate(self))


class TableView(ItemView, QtWidgets.QTableView):
    """TableView version of our customised ItemView"""
    def __init__(self, parent=None):
        super(TableView, self).__init__(parent)
        # Set rows and columns to the minimum size, disabling interactive resizing.
        # Caller may still configure a specific column to stretch:
        self.verticalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents
        )
        self.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents
        )
        self.horizontalHeader().sectionResized.connect(self.on_column_resized)
        self.setItemDelegate(ItemDelegate(self))
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.horizontalHeader().setHighlightSections(False)

    def on_column_resized(self, col):
        for row in range(self.model().rowCount()):
            self.resizeRowToContents(row)


class AlternatingColorModel(QtGui.QStandardItemModel):

    def __init__(self, view):
        QtGui.QStandardItemModel.__init__(self)
        # How much darker in each channel is the alternate base color compared
        # to the base color?
        self.view = view
        palette = view.palette()
        self.normal_color = palette.color(QtGui.QPalette.Base)
        self.alternate_color = palette.color(QtGui.QPalette.AlternateBase)
        r, g, b, a = self.normal_color.getRgb()
        alt_r, alt_g, alt_b, alt_a = self.alternate_color.getRgb()
        self.delta_r = alt_r - r
        self.delta_g = alt_g - g
        self.delta_b = alt_b - b
        self.delta_a = alt_a - a

        # A cache, store brushes so we don't have to recalculate them. Is faster.
        self.bg_brushes = {}

    def get_bgbrush(self, normal_brush, alternate, selected):
        """Get cell colour as a function of its ordinary colour, whether it is on an odd
        row, and whether it is selected."""
        normal_rgb = normal_brush.color().getRgb() if normal_brush is not None else None
        try:
            return self.bg_brushes[normal_rgb, alternate, selected]
        except KeyError:
            pass
        # Get the colour of the cell with alternate row shading:
        if normal_rgb is None:
            # No colour has been set. Use palette colours:
            if alternate:
                bg_color = self.alternate_color
            else:
                bg_color = self.normal_color
        else:
            bg_color = normal_brush.color()
            if alternate:
                # Modify alternate rows:
                r, g, b, a = normal_rgb
                alt_r = min(max(r + self.delta_r, 0), 255)
                alt_g = min(max(g + self.delta_g, 0), 255)
                alt_b = min(max(b + self.delta_b, 0), 255)
                alt_a = min(max(a + self.delta_a, 0), 255)
                bg_color = QtGui.QColor(alt_r, alt_g, alt_b, alt_a)

        # If parent is a TableView, we handle selection highlighting as part of the
        # background colours:
        if selected and isinstance(self.view, QtWidgets.QTableView):
            # Overlay highlight colour:
            r_s, g_s, b_s, a_s = QtGui.QColor(ItemView.COLOR_HIGHLIGHT).getRgb()
            r_0, g_0, b_0, a_0 = bg_color.getRgb()
            rgb = composite_colors(r_0, g_0, b_0, a_0, r_s, g_s, b_s, a_s)
            bg_color = QtGui.QColor(*rgb)

        brush = QtGui.QBrush(bg_color)
        self.bg_brushes[normal_rgb, alternate, selected] = brush
        return brush

    def data(self, index, role):
        """When background color data is being requested, returns modified colours for
        every second row, according to the palette of the view. This has the effect of
        making the alternate colours visible even when custom colors have been set - the
        same shading will be applied to the custom colours. Only really looks sensible
        when the normal and alternate colors are similar. Also applies selection
        highlight colour (using ItemView.COLOR_HIGHLIGHT), similarly with alternate-row
        shading, for the case of a QTableView."""
        if role == QtCore.Qt.BackgroundRole:
            normal_brush = QtGui.QStandardItemModel.data(self, index, QtCore.Qt.BackgroundRole)
            selected = index in self.view.selectedIndexes()
            alternate = index.row() % 2
            return self.get_bgbrush(normal_brush, alternate, selected)
        return QtGui.QStandardItemModel.data(self, index, role)


class Editor(QtWidgets.QTextEdit):
    """Popup editor with word wrapping and automatic resizing."""
    def __init__(self, parent):
        QtWidgets.QTextEdit.__init__(self, parent)
        self.setWordWrapMode(QtGui.QTextOption.WordWrap)
        self.setAcceptRichText(False)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.textChanged.connect(self.update_size)
        self.initial_height = None

    def update_size(self):
        if self.initial_height is not None:
            # Temporarily shrink back to the initial height, just so that the document
            # size below returns the preferred size rather than the current size.
            # QTextDocument doesn't have a sizeHint of minimumSizeHint method, so this
            # is the best we can do to get its minimum size.
            self.setFixedHeight(self.initial_height)
        preferred_height = self.document().size().toSize().height()
        # Do not shrink smaller than the initial height:
        if self.initial_height is not None and preferred_height >= self.initial_height:
            self.setFixedHeight(preferred_height)

    def resizeEvent(self, event):
        result = QtWidgets.QTextEdit.resizeEvent(self, event)
        # Record the initial height after it is first set:
        if self.initial_height is None:
            self.initial_height = self.height()
        return result
        


class ItemDelegate(QtWidgets.QStyledItemDelegate):

    """An item delegate with a larger row height and column width, faint grey vertical
    lines between columns, and a custom editor for handling multi-line data"""
    MIN_ROW_HEIGHT = 22
    EXTRA_ROW_HEIGHT = 6
    EXTRA_COL_WIDTH = 20

    def __init__(self, *args, **kwargs):
        QtWidgets.QStyledItemDelegate.__init__(self, *args, **kwargs)
        self._pen = QtGui.QPen()
        self._pen.setWidth(1)
        self._pen.setColor(QtGui.QColor.fromRgb(128, 128, 128, 64))

    def sizeHint(self, *args):
        size = QtWidgets.QStyledItemDelegate.sizeHint(self, *args)
        if size.height() <= self.MIN_ROW_HEIGHT:
            height = self.MIN_ROW_HEIGHT
        else:
            # Esnure cells with multiple lines of text still have some padding:
            height = size.height() + self.EXTRA_ROW_HEIGHT
        return QtCore.QSize(size.width() + self.EXTRA_COL_WIDTH, height)

    def paint(self, painter, option, index):
        if isinstance(self.parent(), QtWidgets.QTableView):
            # Disable rendering of selection highlight for TableViews, they handle
            # it themselves with the background colour data:
            option.state &= ~(QtWidgets.QStyle.State_Selected)
        QtWidgets.QStyledItemDelegate.paint(self, painter, option, index)
        if index.column() > 0:
            painter.setPen(self._pen)
            painter.drawLine(option.rect.topLeft(), option.rect.bottomLeft())

    def eventFilter(self, obj, event):
        """Filter events before they get to the editor, so that editing is ended when
        the user presses tab, shift-tab or enter (which otherwise would not end editing
        in a QTextEdit)."""
        if event.type() == QtCore.QEvent.KeyPress:
            if event.key() in [QtCore.Qt.Key_Enter, QtCore.Qt.Key_Return]:
                # Allow shift-enter
                if not event.modifiers() & QtCore.Qt.ShiftModifier:
                    self.commitData.emit(obj)
                    self.closeEditor.emit(obj)
                    return True
            elif event.key() == QtCore.Qt.Key_Tab:
                self.commitData.emit(obj)
                self.closeEditor.emit(obj, QtWidgets.QStyledItemDelegate.EditNextItem)
                return True
            elif event.key() == QtCore.Qt.Key_Backtab:
                self.commitData.emit(obj)
                self.closeEditor.emit(obj, QtWidgets.QStyledItemDelegate.EditPreviousItem)
                return True
        return QtWidgets.QStyledItemDelegate.eventFilter(self, obj, event)

    def createEditor(self, parent, option, index):
        return Editor(parent)

    def setEditorData(self, editor, index):
        editor.setPlainText(index.data())
        font = index.data(QtCore.Qt.FontRole)
        default_font = qapplication.font(self.parent())
        if font is None:
            font = default_font
        font.setPointSize(default_font.pointSize())
        editor.setFont(font)
        font_height = QtGui.QFontMetrics(font).height()
        padding = (self.MIN_ROW_HEIGHT - font_height) / 2 - 1
        editor.document().setDocumentMargin(padding)
        editor.selectAll()
        
    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText())


class GroupTab(object):
    GLOBALS_COL_SCAN_ENABLED = 0
    GLOBALS_COL_JIT_ENABLED = 1
    GLOBALS_COL_NAME = 2
    GLOBALS_COL_DEFAULT = 3
    GLOBALS_COL_SCAN = 4
    GLOBALS_COL_UNITS = 5
    GLOBALS_COL_EXPANSION = 6

    GLOBALS_ROLE_IS_DUMMY_ROW = QtCore.Qt.UserRole + 1
    GLOBALS_ROLE_SORT_DATA = QtCore.Qt.UserRole + 2
    GLOBALS_ROLE_PREVIOUS_TEXT = QtCore.Qt.UserRole + 3
    GLOBALS_ROLE_PREVIOUS_CHECKSTATE = QtCore.Qt.UserRole + 4
    GLOBALS_ROLE_IS_BOOL = QtCore.Qt.UserRole + 5

    GLOBALS_DUMMY_ROW_TEXT = '<Click to add global>'

    def __init__(self, tabWidget, globals_file, group_name):
        self.tabWidget = tabWidget

        loader = UiLoader()
        loader.registerCustomWidget(TableView)
        self.ui = loader.load(os.path.join(runmanager_dir, 'group.ui'))
        self.ui.gridLayout.setColumnStretch(1, 1)
        self.tabWidget.addTab(self.ui, group_name, closable=True)

        self.color_error, self.color_active = expression_cell_colors()
        self.set_file_and_group_name(globals_file, group_name)

        self.globals_model = AlternatingColorModel(view=self.ui.tableView_globals)
        self.globals_model.setHorizontalHeaderLabels(
            ['Scan?', 'JIT?', 'Name', 'Default', 'Scan', 'Units', 'Expansion']
        )
        self.globals_model.setSortRole(self.GLOBALS_ROLE_SORT_DATA)
        self.ui.tableView_globals.setModel(self.globals_model)
        self.ui.tableView_globals.setSelectionBehavior(QtWidgets.QTableView.SelectRows)
        self.ui.tableView_globals.setSelectionMode(QtWidgets.QTableView.ExtendedSelection)
        self.ui.tableView_globals.setSortingEnabled(True)
        self.ui.tableView_globals.setEditTriggers(
            QtWidgets.QTableView.AnyKeyPressed | QtWidgets.QTableView.EditKeyPressed
        )
        self.ui.tableView_globals.horizontalHeader().setStretchLastSection(False)
        self.ui.tableView_globals.horizontalHeader().setSectionResizeMode(
            self.GLOBALS_COL_DEFAULT, QtWidgets.QHeaderView.Stretch
        )
        self.ui.tableView_globals.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.action_globals_delete_selected = QtWidgets.QAction(
            QtGui.QIcon(':qtutils/fugue/minus'), 'Delete selected global(s)', self.ui
        )
        self.action_globals_set_selected_true = QtWidgets.QAction(
            QtGui.QIcon(':qtutils/fugue/ui-check-box'), 'Set selected Booleans True', self.ui
        )
        self.action_globals_set_selected_false = QtWidgets.QAction(
            QtGui.QIcon(':qtutils/fugue/ui-check-box-uncheck'),
            'Set selected Booleans False',
            self.ui,
        )
        self.connect_signals()

        self.populate_model()
        for col in range(self.globals_model.columnCount()):
            if col != self.GLOBALS_COL_DEFAULT:
                self.ui.tableView_globals.resizeColumnToContents(col)
        if self.ui.tableView_globals.columnWidth(self.GLOBALS_COL_NAME) < 200:
            self.ui.tableView_globals.setColumnWidth(self.GLOBALS_COL_NAME, 200)
        if self.ui.tableView_globals.columnWidth(self.GLOBALS_COL_DEFAULT) < 220:
            self.ui.tableView_globals.setColumnWidth(self.GLOBALS_COL_DEFAULT, 220)
        if self.ui.tableView_globals.columnWidth(self.GLOBALS_COL_UNITS) < 100:
            self.ui.tableView_globals.setColumnWidth(self.GLOBALS_COL_UNITS, 100)
        if self.ui.tableView_globals.columnWidth(self.GLOBALS_COL_SCAN) < 220:
            self.ui.tableView_globals.setColumnWidth(self.GLOBALS_COL_SCAN, 220)
        if self.ui.tableView_globals.columnWidth(self.GLOBALS_COL_EXPANSION) < 100:
            self.ui.tableView_globals.setColumnWidth(self.GLOBALS_COL_EXPANSION, 100)

        self.tab_contains_errors = False

    def connect_signals(self):
        self.ui.tableView_globals.leftClicked.connect(self.on_tableView_globals_leftClicked)
        self.ui.tableView_globals.customContextMenuRequested.connect(
            self.on_tableView_globals_context_menu_requested
        )
        self.action_globals_set_selected_true.triggered.connect(
            lambda: self.on_globals_set_selected_bools_triggered('True')
        )
        self.action_globals_set_selected_false.triggered.connect(
            lambda: self.on_globals_set_selected_bools_triggered('False')
        )
        self.action_globals_delete_selected.triggered.connect(
            self.on_globals_delete_selected_triggered
        )
        self.globals_model.itemChanged.connect(self.on_globals_model_item_changed)
        self.globals_model_item_changed_disconnected = DisconnectContextManager(
            self.globals_model.itemChanged, self.on_globals_model_item_changed
        )

    def set_file_and_group_name(self, globals_file, group_name):
        self.globals_file = globals_file
        self.group_name = group_name
        self.ui.label_globals_file.setText(globals_file)
        self.ui.label_group_name.setText(group_name)
        index = self.tabWidget.indexOf(self.ui)
        self.tabWidget.setTabText(index, group_name)
        self.tabWidget.setTabToolTip(index, '%s\n(%s)' % (group_name, globals_file))

    def set_tab_icon(self, icon_string):
        index = self.tabWidget.indexOf(self.ui)
        icon = QtGui.QIcon(icon_string) if icon_string is not None else QtGui.QIcon()
        if self.tabWidget.tabIcon(index).cacheKey() != icon.cacheKey():
            logger.info('setting tab icon')
            self.tabWidget.setTabIcon(index, icon)

    def populate_model(self):
        globals_details = runmanager.get_globals_details(
            {self.group_name: self.globals_file}
        )[self.group_name]
        for name, details in globals_details.items():
            row = self.make_global_row(
                name,
                default=details['default'],
                units=details['units'],
                scan_enabled=details['scan_enabled'],
                jit_enabled=details['jit_enabled'],
                scan=details['scan'],
                expansion=details['expansion'],
            )
            self.globals_model.appendRow(row)
            self.update_scan_controls(name)
            self._update_boolean_state(name)
            self.on_globals_model_expansion_changed(row[self.GLOBALS_COL_EXPANSION])

        dummy_scan_enabled_item = QtGui.QStandardItem()
        dummy_scan_enabled_item.setData(True, self.GLOBALS_ROLE_IS_DUMMY_ROW)
        dummy_scan_enabled_item.setFlags(QtCore.Qt.NoItemFlags)
        dummy_scan_enabled_item.setToolTip('Click to add global')

        dummy_name_item = QtGui.QStandardItem(self.GLOBALS_DUMMY_ROW_TEXT)
        dummy_name_item.setFont(QtGui.QFont(GLOBAL_MONOSPACE_FONT))
        dummy_name_item.setToolTip('Click to add global')
        dummy_name_item.setData(True, self.GLOBALS_ROLE_IS_DUMMY_ROW)
        dummy_name_item.setData(self.GLOBALS_DUMMY_ROW_TEXT, self.GLOBALS_ROLE_PREVIOUS_TEXT)
        dummy_name_item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsEditable)

        dummy_jit_enabled_item = QtGui.QStandardItem()
        dummy_jit_enabled_item.setData(True, self.GLOBALS_ROLE_IS_DUMMY_ROW)
        dummy_jit_enabled_item.setFlags(QtCore.Qt.NoItemFlags)
        dummy_jit_enabled_item.setToolTip('Click to add global')

        dummy_default_item = QtGui.QStandardItem()
        dummy_default_item.setData(True, self.GLOBALS_ROLE_IS_DUMMY_ROW)
        dummy_default_item.setFlags(QtCore.Qt.NoItemFlags)
        dummy_default_item.setToolTip('Click to add global')

        dummy_scan_item = QtGui.QStandardItem()
        dummy_scan_item.setData(True, self.GLOBALS_ROLE_IS_DUMMY_ROW)
        dummy_scan_item.setFlags(QtCore.Qt.NoItemFlags)
        dummy_scan_item.setToolTip('Click to add global')

        dummy_units_item = QtGui.QStandardItem()
        dummy_units_item.setData(True, self.GLOBALS_ROLE_IS_DUMMY_ROW)
        dummy_units_item.setFlags(QtCore.Qt.NoItemFlags)
        dummy_units_item.setToolTip('Click to add global')

        dummy_expansion_item = QtGui.QStandardItem()
        dummy_expansion_item.setData(True, self.GLOBALS_ROLE_IS_DUMMY_ROW)
        dummy_expansion_item.setFlags(QtCore.Qt.NoItemFlags)
        dummy_expansion_item.setToolTip('Click to add global')

        self.globals_model.appendRow(
            [
                dummy_scan_enabled_item,
                dummy_jit_enabled_item,
                dummy_name_item,
                dummy_default_item,
                dummy_scan_item,
                dummy_units_item,
                dummy_expansion_item,
            ]
        )
        self.ui.tableView_globals.sortByColumn(self.GLOBALS_COL_NAME, QtCore.Qt.AscendingOrder)

    def make_global_row(
        self,
        name,
        default='',
        units='',
        scan_enabled=False,
        jit_enabled=False,
        scan='',
        expansion='',
    ):
        logger.debug('%s:%s - make global row: %s ', self.globals_file, self.group_name, name)

        name_item = QtGui.QStandardItem(name)
        name_item.setData(name, self.GLOBALS_ROLE_SORT_DATA)
        name_item.setData(name, self.GLOBALS_ROLE_PREVIOUS_TEXT)
        name_item.setToolTip(name)
        name_item.setFont(QtGui.QFont(GLOBAL_MONOSPACE_FONT))

        default_item = QtGui.QStandardItem(default)
        default_item.setData(default, self.GLOBALS_ROLE_SORT_DATA)
        default_item.setData(str(default), self.GLOBALS_ROLE_PREVIOUS_TEXT)
        default_item.setFont(QtGui.QFont(GLOBAL_MONOSPACE_FONT))
        self.update_expression_item_metadata(default_item, 'Evaluating...')

        scan_enabled_item = QtGui.QStandardItem()
        scan_enabled_item.setCheckable(True)
        scan_enabled_item.setCheckState(
            QtCore.Qt.Checked if scan_enabled else QtCore.Qt.Unchecked
        )
        scan_enabled_item.setData(scan_enabled, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
        scan_enabled_item.setData(scan_enabled, self.GLOBALS_ROLE_SORT_DATA)
        scan_enabled_item.setEditable(False)
        scan_enabled_item.setToolTip('Use the Scan expression when generating sequences.')

        jit_enabled_item = QtGui.QStandardItem()
        jit_enabled_item.setCheckable(True)
        jit_enabled_item.setCheckState(
            QtCore.Qt.Checked if jit_enabled else QtCore.Qt.Unchecked
        )
        jit_enabled_item.setData(jit_enabled, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
        jit_enabled_item.setData(jit_enabled, self.GLOBALS_ROLE_SORT_DATA)
        jit_enabled_item.setEditable(False)
        jit_enabled_item.setToolTip('Re-evaluate this global at compile time.')

        scan_item = QtGui.QStandardItem(scan)
        scan_item.setData(scan, self.GLOBALS_ROLE_SORT_DATA)
        scan_item.setData(scan, self.GLOBALS_ROLE_PREVIOUS_TEXT)
        scan_item.setFont(QtGui.QFont(GLOBAL_MONOSPACE_FONT))
        self.update_expression_item_metadata(scan_item, 'Used when Scan? is checked.')

        units_item = QtGui.QStandardItem(units)
        units_item.setData(units, self.GLOBALS_ROLE_SORT_DATA)
        units_item.setData(units, self.GLOBALS_ROLE_PREVIOUS_TEXT)
        units_item.setData(False, self.GLOBALS_ROLE_IS_BOOL)
        units_item.setToolTip('')

        expansion_item = QtGui.QStandardItem(expansion)
        expansion_item.setData(expansion, self.GLOBALS_ROLE_SORT_DATA)
        expansion_item.setData(expansion, self.GLOBALS_ROLE_PREVIOUS_TEXT)
        expansion_item.setToolTip('')

        return [
            scan_enabled_item,
            jit_enabled_item,
            name_item,
            default_item,
            scan_item,
            units_item,
            expansion_item,
        ]

    def on_tableView_globals_leftClicked(self, index):
        if qapplication.keyboardModifiers() != QtCore.Qt.NoModifier:
            return
        item = self.globals_model.itemFromIndex(index)
        name_index = index.sibling(index.row(), self.GLOBALS_COL_NAME)
        name_item = self.globals_model.itemFromIndex(name_index)
        global_name = name_item.text()
        if item.data(self.GLOBALS_ROLE_IS_DUMMY_ROW):
            self.ui.tableView_globals.setCurrentIndex(name_index)
            self.ui.tableView_globals.edit(name_index)
        elif item.column() == self.GLOBALS_COL_UNITS and item.data(self.GLOBALS_ROLE_IS_BOOL):
            active_item = self.get_active_value_item(global_name)
            active_item.setText('False' if item.checkState() == QtCore.Qt.Checked else 'True')
        elif item.column() not in (self.GLOBALS_COL_SCAN_ENABLED, self.GLOBALS_COL_JIT_ENABLED):
            if (
                self.ui.tableView_globals.currentIndex() != index
                or self.ui.tableView_globals.state() != QtWidgets.QTreeView.EditingState
            ):
                self.ui.tableView_globals.setCurrentIndex(index)
                self.ui.tableView_globals.edit(index)

    def on_globals_model_item_changed(self, item):
        if item.column() == self.GLOBALS_COL_NAME:
            self.on_globals_model_name_changed(item)
        elif item.column() == self.GLOBALS_COL_DEFAULT:
            self.on_globals_model_default_changed(item)
        elif item.column() == self.GLOBALS_COL_UNITS:
            self.on_globals_model_units_changed(item)
        elif item.column() == self.GLOBALS_COL_SCAN_ENABLED:
            self.on_globals_model_scan_enabled_changed(item)
        elif item.column() == self.GLOBALS_COL_JIT_ENABLED:
            self.on_globals_model_jit_enabled_changed(item)
        elif item.column() == self.GLOBALS_COL_SCAN:
            self.on_globals_model_scan_changed(item)
        elif item.column() == self.GLOBALS_COL_EXPANSION:
            self.on_globals_model_expansion_changed(item)

    def on_globals_model_name_changed(self, item):
        item_text = item.text()
        if item.data(self.GLOBALS_ROLE_IS_DUMMY_ROW):
            if item_text != self.GLOBALS_DUMMY_ROW_TEXT:
                self.new_global(item_text)
        else:
            new_global_name = item_text
            previous_global_name = item.data(self.GLOBALS_ROLE_PREVIOUS_TEXT)
            if new_global_name != previous_global_name:
                self.rename_global(previous_global_name, new_global_name)

    def on_globals_model_default_changed(self, item):
        new_default = item.text()
        previous_default = item.data(self.GLOBALS_ROLE_PREVIOUS_TEXT)
        global_name = self.globals_model.itemFromIndex(
            item.index().sibling(item.index().row(), self.GLOBALS_COL_NAME)
        ).text()
        if new_default != previous_default:
            self.change_global_default(global_name, previous_default, new_default)

    def on_globals_model_units_changed(self, item):
        new_units = item.text()
        previous_units = item.data(self.GLOBALS_ROLE_PREVIOUS_TEXT)
        global_name = self.globals_model.itemFromIndex(
            item.index().sibling(item.index().row(), self.GLOBALS_COL_NAME)
        ).text()
        if new_units != previous_units:
            self.change_global_units(global_name, previous_units, new_units)

    def on_globals_model_scan_enabled_changed(self, item):
        new_state = item.checkState() == QtCore.Qt.Checked
        previous_state = item.data(self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
        global_name = self.globals_model.itemFromIndex(
            item.index().sibling(item.index().row(), self.GLOBALS_COL_NAME)
        ).text()
        if new_state != previous_state:
            self.change_global_scan_enabled(global_name, previous_state, new_state)

    def on_globals_model_jit_enabled_changed(self, item):
        new_state = item.checkState() == QtCore.Qt.Checked
        previous_state = item.data(self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
        global_name = self.globals_model.itemFromIndex(
            item.index().sibling(item.index().row(), self.GLOBALS_COL_NAME)
        ).text()
        if new_state != previous_state:
            self.change_global_jit_enabled(global_name, previous_state, new_state)

    def on_globals_model_scan_changed(self, item):
        new_scan = item.text()
        previous_scan = item.data(self.GLOBALS_ROLE_PREVIOUS_TEXT)
        global_name = self.globals_model.itemFromIndex(
            item.index().sibling(item.index().row(), self.GLOBALS_COL_NAME)
        ).text()
        if new_scan != previous_scan:
            self.change_global_scan(global_name, previous_scan, new_scan)

    def on_globals_model_expansion_changed(self, item):
        new_expansion = item.text()
        previous_expansion = item.data(self.GLOBALS_ROLE_PREVIOUS_TEXT)
        global_name = self.globals_model.itemFromIndex(
            item.index().sibling(item.index().row(), self.GLOBALS_COL_NAME)
        ).text()
        with self.globals_model_item_changed_disconnected:
            scan_enabled_item = self.get_global_item_by_name(
                global_name, self.GLOBALS_COL_SCAN_ENABLED
            )
            scan_enabled = scan_enabled_item.checkState() == QtCore.Qt.Checked
            if not scan_enabled:
                item.setData('', self.GLOBALS_ROLE_SORT_DATA)
                item.setToolTip('Enable Scan? to set an expansion mode.')
                item.setData(None, QtCore.Qt.DecorationRole)
            elif new_expansion == 'outer':
                item.setIcon(QtGui.QIcon(':qtutils/custom/outer'))
                item.setToolTip(
                    'This global will be interpreted as a list of values, and will be '
                    'outer producted with other lists to form a larger parameter space.'
                )
            elif new_expansion:
                item.setIcon(QtGui.QIcon(':qtutils/custom/zip'))
                item.setToolTip(
                    'This global will be interpreted as a list of values, and will be '
                    "iterated over in lock-step with other globals in the '%s' zip group."
                    % new_expansion
                )
            else:
                item.setData(None, QtCore.Qt.DecorationRole)
                item.setToolTip(
                    'This global will be interpreted as a single value and passed to '
                    'compilation as-is.'
                )
        if new_expansion != previous_expansion:
            self.change_global_expansion(global_name, previous_expansion, new_expansion)

    def on_tableView_globals_context_menu_requested(self, point):
        menu = QtWidgets.QMenu(self.ui)
        menu.addAction(self.action_globals_set_selected_true)
        menu.addAction(self.action_globals_set_selected_false)
        menu.addAction(self.action_globals_delete_selected)
        menu.exec_(QtGui.QCursor.pos())

    def on_globals_delete_selected_triggered(self):
        selected_indexes = self.ui.tableView_globals.selectedIndexes()
        selected_items = (self.globals_model.itemFromIndex(index) for index in selected_indexes)
        name_items = [item for item in selected_items if item.column() == self.GLOBALS_COL_NAME]
        confirm_multiple = len(name_items) > 1
        if confirm_multiple and not question_dialog("Delete %d globals?" % len(name_items)):
            return
        for item in name_items:
            self.delete_global(item.text(), confirm=not confirm_multiple)

    def on_globals_set_selected_bools_triggered(self, state):
        selected_indexes = self.ui.tableView_globals.selectedIndexes()
        selected_items = [self.globals_model.itemFromIndex(index) for index in selected_indexes]
        name_items = [item for item in selected_items if item.column() == self.GLOBALS_COL_NAME]
        for name_item in name_items:
            self.get_active_value_item(name_item.text()).setText(state)

    def close(self):
        self.tabWidget.removeTab(self.tabWidget.indexOf(self.ui))

    def get_global_item_by_name(self, global_name, column, previous_name=None):
        possible_name_items = self.globals_model.findItems(global_name, column=self.GLOBALS_COL_NAME)
        if previous_name is not None:
            possible_name_items = [
                item
                for item in possible_name_items
                if item.data(self.GLOBALS_ROLE_PREVIOUS_TEXT) == previous_name
            ]
        elif global_name != self.GLOBALS_DUMMY_ROW_TEXT:
            possible_name_items = [
                item
                for item in possible_name_items
                if not item.data(self.GLOBALS_ROLE_IS_DUMMY_ROW)
            ]
        if len(possible_name_items) > 1:
            raise LookupError('Multiple items found')
        if not possible_name_items:
            raise LookupError('No item found')
        name_item = possible_name_items[0]
        return self.globals_model.itemFromIndex(
            name_item.index().sibling(name_item.index().row(), column)
        )

    def ensure_editable_globals_file(self):
        self.globals_file = app.ensure_editable_globals_file(self.globals_file, parent=self.ui)
        return self.globals_file

    def get_active_value_item(self, global_name):
        scan_enabled_item = self.get_global_item_by_name(
            global_name, self.GLOBALS_COL_SCAN_ENABLED
        )
        target_column = (
            self.GLOBALS_COL_SCAN
            if scan_enabled_item.checkState() == QtCore.Qt.Checked
            else self.GLOBALS_COL_DEFAULT
        )
        return self.get_global_item_by_name(global_name, target_column)

    def _update_boolean_state(self, global_name):
        active_item = self.get_active_value_item(global_name)
        units_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_UNITS)
        value = active_item.text()
        if value == 'True':
            units_item.setData(True, self.GLOBALS_ROLE_IS_BOOL)
            units_item.setText('Bool')
            units_item.setData('!1', self.GLOBALS_ROLE_SORT_DATA)
            units_item.setEditable(False)
            units_item.setCheckState(QtCore.Qt.Checked)
            units_item.setBackground(QtGui.QBrush(QtGui.QColor('#63F731')))
        elif value == 'False':
            units_item.setData(True, self.GLOBALS_ROLE_IS_BOOL)
            units_item.setText('Bool')
            units_item.setData('!0', self.GLOBALS_ROLE_SORT_DATA)
            units_item.setEditable(False)
            units_item.setCheckState(QtCore.Qt.Unchecked)
            units_item.setBackground(QtGui.QBrush(QtGui.QColor('#608060')))
        else:
            was_bool = units_item.data(self.GLOBALS_ROLE_IS_BOOL)
            units_item.setData(False, self.GLOBALS_ROLE_IS_BOOL)
            units_item.setEditable(True)
            units_item.setData(None, QtCore.Qt.CheckStateRole)
            units_item.setData(None, QtCore.Qt.BackgroundRole)
            if was_bool and units_item.text() == 'Bool':
                units_item.setText('')

    def update_scan_controls(self, global_name):
        scan_enabled_item = self.get_global_item_by_name(
            global_name, self.GLOBALS_COL_SCAN_ENABLED
        )
        jit_enabled_item = self.get_global_item_by_name(
            global_name, self.GLOBALS_COL_JIT_ENABLED
        )
        default_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_DEFAULT)
        scan_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_SCAN)
        expansion_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_EXPANSION)
        scan_enabled = scan_enabled_item.checkState() == QtCore.Qt.Checked
        jit_enabled = jit_enabled_item.checkState() == QtCore.Qt.Checked
        default_tooltip = 'Used for standard shots and when Scan? is unchecked.'
        scan_tooltip = (
            'Expression used when Scan? is checked.'
            if scan_enabled
            else 'Editable, but used only when Scan? is checked.'
        )
        with self.globals_model_item_changed_disconnected:
            scan_enabled_item.setData(scan_enabled, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
            scan_enabled_item.setData(scan_enabled, self.GLOBALS_ROLE_SORT_DATA)
            jit_enabled_item.setData(jit_enabled, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
            jit_enabled_item.setData(jit_enabled, self.GLOBALS_ROLE_SORT_DATA)
            if scan_enabled:
                jit_enabled_item.setToolTip('Disable Scan? to enable JIT?.')
            else:
                jit_enabled_item.setToolTip('Re-evaluate this global at compile time.')
            scan_item.setEditable(True)
            if scan_enabled:
                expansion_item.setEditable(True)
                expansion_item.setToolTip(expansion_item.toolTip() or 'Evaluating...')
                expansion_item.setData(expansion_item.text(), self.GLOBALS_ROLE_SORT_DATA)
            else:
                expansion_item.setEditable(False)
                expansion_item.setText('')
                expansion_item.setData('', self.GLOBALS_ROLE_PREVIOUS_TEXT)
                expansion_item.setData('', self.GLOBALS_ROLE_SORT_DATA)
                expansion_item.setData(None, QtCore.Qt.DecorationRole)
                expansion_item.setToolTip('Enable Scan? to set an expansion mode.')
            self.update_expression_item_metadata(default_item, default_tooltip)
            self.update_expression_item_metadata(scan_item, scan_tooltip)
        active_item = scan_item if scan_enabled else default_item
        active_tooltip = scan_tooltip if scan_enabled else default_tooltip
        if not active_item.text().strip():
            self.update_expression_backgrounds(global_name, error=True)
            self.update_expression_item_metadata(
                active_item,
                active_tooltip + ' Expression is empty.',
                QtGui.QIcon(':qtutils/fugue/exclamation'),
            )
        else:
            self.update_expression_backgrounds(global_name)

    def update_expression_backgrounds(self, global_name, error=False):
        default_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_DEFAULT)
        scan_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_SCAN)
        active_item = self.get_active_value_item(global_name)
        inactive_item = default_item if active_item is scan_item else scan_item
        with self.globals_model_item_changed_disconnected:
            inactive_item.setData(None, QtCore.Qt.BackgroundRole)
            active_item.setBackground(
                QtGui.QBrush(QtGui.QColor(self.color_error if error else self.color_active))
            )

    def update_expression_item_metadata(self, item, base_tooltip, icon=None):
        tooltip = tooltip_with_hidden_unicode_warning(base_tooltip, item.text())
        if item.toolTip() != tooltip:
            item.setToolTip(tooltip)
        if icon is not None:
            item.setIcon(icon)
        elif hidden_unicode_issues(item.text()):
            item.setIcon(QtGui.QIcon(':qtutils/fugue/exclamation-red'))
        elif not item.icon().isNull():
            item.setData(None, QtCore.Qt.DecorationRole)

    def do_model_sort(self):
        header = self.ui.tableView_globals.horizontalHeader()
        self.ui.tableView_globals.sortByColumn(
            header.sortIndicatorSection(), header.sortIndicatorOrder()
        )

    def new_global(self, global_name):
        logger.info('%s:%s - new global: %s', self.globals_file, self.group_name, global_name)
        item = self.get_global_item_by_name(
            global_name, self.GLOBALS_COL_NAME, previous_name=self.GLOBALS_DUMMY_ROW_TEXT
        )
        try:
            self.ensure_editable_globals_file()
            runmanager.new_global(self.globals_file, self.group_name, global_name)
        except Exception as e:
            error_dialog(str(e))
        else:
            global_row = self.make_global_row(global_name)
            self.globals_model.insertRow(self.globals_model.rowCount() - 1, global_row)
            self.update_scan_controls(global_name)
            self._update_boolean_state(global_name)
            self.do_model_sort()
            default_item = self.get_global_item_by_name(
                global_name, self.GLOBALS_COL_DEFAULT, previous_name=global_name
            )
            self.ui.tableView_globals.setCurrentIndex(default_item.index())
            self.ui.tableView_globals.edit(default_item.index())
            self.globals_changed()
        finally:
            item.setText(self.GLOBALS_DUMMY_ROW_TEXT)

    def rename_global(self, previous_global_name, new_global_name):
        logger.info(
            '%s:%s - rename global: %s -> %s',
            self.globals_file,
            self.group_name,
            previous_global_name,
            new_global_name,
        )
        item = self.get_global_item_by_name(
            new_global_name, self.GLOBALS_COL_NAME, previous_name=previous_global_name
        )
        try:
            self.ensure_editable_globals_file()
            runmanager.rename_global(
                self.globals_file, self.group_name, previous_global_name, new_global_name
            )
        except Exception as e:
            error_dialog(str(e))
            item.setText(previous_global_name)
        else:
            item.setData(new_global_name, self.GLOBALS_ROLE_PREVIOUS_TEXT)
            item.setData(new_global_name, self.GLOBALS_ROLE_SORT_DATA)
            self.do_model_sort()
            item.setToolTip(new_global_name)
            self.globals_changed()
            default_item = self.get_global_item_by_name(new_global_name, self.GLOBALS_COL_DEFAULT)
            if (
                not default_item.text()
                and self.ui.tableView_globals.state() != QtWidgets.QAbstractItemView.EditingState
            ):
                self.ui.tableView_globals.setCurrentIndex(default_item.index())
                self.ui.tableView_globals.edit(default_item.index())
            else:
                scroll_view_to_row_if_current(self.ui.tableView_globals, item)

    def change_global_default(self, global_name, previous_default, new_default, interactive=True):
        logger.info(
            '%s:%s - change global default: %s = %s -> %s',
            self.globals_file,
            self.group_name,
            global_name,
            previous_default,
            new_default,
        )
        item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_DEFAULT)
        if not interactive:
            with self.globals_model_item_changed_disconnected:
                item.setText(new_default)
        previous_background = item.background()
        previous_icon = item.icon()
        item.setData(new_default, self.GLOBALS_ROLE_PREVIOUS_TEXT)
        item.setData(new_default, self.GLOBALS_ROLE_SORT_DATA)
        item.setIcon(QtGui.QIcon(':qtutils/fugue/hourglass'))
        args = global_name, previous_default, new_default, item, previous_background, previous_icon
        if interactive:
            QtCore.QTimer.singleShot(1, lambda: self.complete_change_global_default(*args))
        else:
            self.complete_change_global_default(*args, interactive=False)

    def complete_change_global_default(
        self,
        global_name,
        previous_default,
        new_default,
        item,
        previous_background,
        previous_icon,
        interactive=True,
    ):
        try:
            self.ensure_editable_globals_file()
            runmanager.set_value(self.globals_file, self.group_name, global_name, new_default)
        except Exception as e:
            if interactive:
                error_dialog(str(e))
            with self.globals_model_item_changed_disconnected:
                item.setText(previous_default)
                item.setData(previous_default, self.GLOBALS_ROLE_PREVIOUS_TEXT)
                item.setData(previous_default, self.GLOBALS_ROLE_SORT_DATA)
                item.setData(previous_background, QtCore.Qt.BackgroundRole)
                item.setIcon(previous_icon)
            if not interactive:
                raise
        else:
            self.do_model_sort()
            self.update_expression_item_metadata(item, 'Evaluating...')
            self._update_boolean_state(global_name)
            self.globals_changed()
            if not interactive:
                return
            units_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_UNITS)
            if (
                not units_item.text()
                and self.ui.tableView_globals.state() != QtWidgets.QAbstractItemView.EditingState
            ):
                self.ui.tableView_globals.setCurrentIndex(units_item.index())
                self.ui.tableView_globals.edit(units_item.index())
            else:
                scroll_view_to_row_if_current(self.ui.tableView_globals, item)

    def change_global_units(self, global_name, previous_units, new_units):
        logger.info(
            '%s:%s - change units: %s = %s -> %s',
            self.globals_file,
            self.group_name,
            global_name,
            previous_units,
            new_units,
        )
        item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_UNITS)
        try:
            self.ensure_editable_globals_file()
            runmanager.set_units(self.globals_file, self.group_name, global_name, new_units)
        except Exception as e:
            error_dialog(str(e))
            item.setText(previous_units)
        else:
            item.setData(new_units, self.GLOBALS_ROLE_PREVIOUS_TEXT)
            item.setData(new_units, self.GLOBALS_ROLE_SORT_DATA)
            self.do_model_sort()
            scroll_view_to_row_if_current(self.ui.tableView_globals, item)

    def change_global_scan_enabled(self, global_name, previous_state, new_state, interactive=True):
        logger.info(
            '%s:%s - change global scan enabled: %s = %s -> %s',
            self.globals_file,
            self.group_name,
            global_name,
            previous_state,
            new_state,
        )
        item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_SCAN_ENABLED)
        jit_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_JIT_ENABLED)
        try:
            self.ensure_editable_globals_file()
            runmanager.set_scan_enabled(
                self.globals_file, self.group_name, global_name, new_state
            )
        except Exception as e:
            if interactive:
                error_dialog(str(e))
            with self.globals_model_item_changed_disconnected:
                item.setCheckState(QtCore.Qt.Checked if previous_state else QtCore.Qt.Unchecked)
                item.setData(previous_state, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
                item.setData(previous_state, self.GLOBALS_ROLE_SORT_DATA)
            self.update_scan_controls(global_name)
            if not interactive:
                raise
        else:
            with self.globals_model_item_changed_disconnected:
                item.setCheckState(QtCore.Qt.Checked if new_state else QtCore.Qt.Unchecked)
                item.setData(new_state, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
                item.setData(new_state, self.GLOBALS_ROLE_SORT_DATA)
                if new_state:
                    jit_item.setCheckState(QtCore.Qt.Unchecked)
                    jit_item.setData(False, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
                    jit_item.setData(False, self.GLOBALS_ROLE_SORT_DATA)
                expansion_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_EXPANSION)
                if not expansion_item.text():
                    expansion_item.setText('outer')
                    expansion_item.setData('outer', self.GLOBALS_ROLE_PREVIOUS_TEXT)
                    expansion_item.setData('outer', self.GLOBALS_ROLE_SORT_DATA)
            self.update_scan_controls(global_name)
            self._update_boolean_state(global_name)
            if new_state:
                self.on_globals_model_expansion_changed(
                    self.get_global_item_by_name(global_name, self.GLOBALS_COL_EXPANSION)
                )
            self.do_model_sort()
            self.globals_changed()
            if not interactive:
                return
            focus_item = self.get_active_value_item(global_name)
            if (
                new_state
                and not focus_item.text()
                and self.ui.tableView_globals.state() != QtWidgets.QAbstractItemView.EditingState
            ):
                self.ui.tableView_globals.setCurrentIndex(focus_item.index())
                self.ui.tableView_globals.edit(focus_item.index())
            else:
                scroll_view_to_row_if_current(self.ui.tableView_globals, item)

    def change_global_jit_enabled(self, global_name, previous_state, new_state, interactive=True):
        logger.info(
            '%s:%s - change global jit enabled: %s = %s -> %s',
            self.globals_file,
            self.group_name,
            global_name,
            previous_state,
            new_state,
        )
        item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_JIT_ENABLED)
        scan_item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_SCAN_ENABLED)
        try:
            self.ensure_editable_globals_file()
            runmanager.set_jit_enabled(
                self.globals_file, self.group_name, global_name, new_state
            )
        except Exception as e:
            if interactive:
                error_dialog(str(e))
            with self.globals_model_item_changed_disconnected:
                item.setCheckState(QtCore.Qt.Checked if previous_state else QtCore.Qt.Unchecked)
                item.setData(previous_state, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
                item.setData(previous_state, self.GLOBALS_ROLE_SORT_DATA)
            self.update_scan_controls(global_name)
            if not interactive:
                raise
        else:
            with self.globals_model_item_changed_disconnected:
                item.setCheckState(QtCore.Qt.Checked if new_state else QtCore.Qt.Unchecked)
                item.setData(new_state, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
                item.setData(new_state, self.GLOBALS_ROLE_SORT_DATA)
                if new_state:
                    scan_item.setCheckState(QtCore.Qt.Unchecked)
                    scan_item.setData(False, self.GLOBALS_ROLE_PREVIOUS_CHECKSTATE)
                    scan_item.setData(False, self.GLOBALS_ROLE_SORT_DATA)
            self.update_scan_controls(global_name)
            self.do_model_sort()
            self.globals_changed()
            if not interactive:
                return
            scroll_view_to_row_if_current(self.ui.tableView_globals, item)

    def change_global_scan(self, global_name, previous_scan, new_scan, interactive=True):
        logger.info(
            '%s:%s - change global scan: %s = %s -> %s',
            self.globals_file,
            self.group_name,
            global_name,
            previous_scan,
            new_scan,
        )
        item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_SCAN)
        try:
            self.ensure_editable_globals_file()
            runmanager.set_scan(self.globals_file, self.group_name, global_name, new_scan)
        except Exception as e:
            if interactive:
                error_dialog(str(e))
            item.setText(previous_scan)
            if not interactive:
                raise
        else:
            item.setData(new_scan, self.GLOBALS_ROLE_PREVIOUS_TEXT)
            item.setData(new_scan, self.GLOBALS_ROLE_SORT_DATA)
            self.do_model_sort()
            self.update_expression_item_metadata(item, 'Evaluating...')
            self._update_boolean_state(global_name)
            self.globals_changed()
            if not interactive:
                return
            scroll_view_to_row_if_current(self.ui.tableView_globals, item)

    def change_global_expansion(self, global_name, previous_expansion, new_expansion):
        logger.info(
            '%s:%s - change expansion: %s = %s -> %s',
            self.globals_file,
            self.group_name,
            global_name,
            previous_expansion,
            new_expansion,
        )
        item = self.get_global_item_by_name(global_name, self.GLOBALS_COL_EXPANSION)
        try:
            self.ensure_editable_globals_file()
            runmanager.set_expansion(
                self.globals_file, self.group_name, global_name, new_expansion
            )
        except Exception as e:
            error_dialog(str(e))
            item.setText(previous_expansion)
        else:
            item.setData(new_expansion, self.GLOBALS_ROLE_PREVIOUS_TEXT)
            item.setData(new_expansion, self.GLOBALS_ROLE_SORT_DATA)
            self.do_model_sort()
            self.globals_changed()
            scroll_view_to_row_if_current(self.ui.tableView_globals, item)

    def globals_changed(self):
        app.globals_changed()

    def delete_global(self, global_name, confirm=True):
        logger.info('%s:%s - delete global: %s', self.globals_file, self.group_name, global_name)
        if confirm and not question_dialog("Delete the global '%s'?" % global_name):
            return
        self.ensure_editable_globals_file()
        runmanager.delete_global(self.globals_file, self.group_name, global_name)
        self.globals_model.removeRow(
            self.get_global_item_by_name(global_name, self.GLOBALS_COL_NAME).row()
        )
        self.globals_changed()

    def update_parse_indication(self, active_groups, sequence_globals, evaled_globals):
        if self.group_name in active_groups and active_groups[self.group_name] == self.globals_file:
            self.tab_contains_errors = False
            for i in range(self.globals_model.rowCount()):
                name_item = self.globals_model.item(i, self.GLOBALS_COL_NAME)
                if name_item.data(self.GLOBALS_ROLE_IS_DUMMY_ROW):
                    continue
                default_item = self.globals_model.item(i, self.GLOBALS_COL_DEFAULT)
                scan_enabled_item = self.globals_model.item(i, self.GLOBALS_COL_SCAN_ENABLED)
                scan_item = self.globals_model.item(i, self.GLOBALS_COL_SCAN)
                expansion_item = self.globals_model.item(i, self.GLOBALS_COL_EXPANSION)
                global_name = name_item.text()
                value = evaled_globals[self.group_name][global_name]
                active_item = (
                    scan_item
                    if scan_enabled_item.checkState() == QtCore.Qt.Checked
                    else default_item
                )
                inactive_item = default_item if active_item is scan_item else scan_item

                _, _, expansion = sequence_globals[self.group_name][global_name]
                with self.globals_model_item_changed_disconnected:
                    if expansion_item.data(self.GLOBALS_ROLE_PREVIOUS_TEXT) != expansion:
                        expansion_item.setData(expansion, self.GLOBALS_ROLE_PREVIOUS_TEXT)
                    if expansion_item.data(self.GLOBALS_ROLE_SORT_DATA) != expansion:
                        expansion_item.setData(expansion, self.GLOBALS_ROLE_SORT_DATA)
                expansion_item.setText(expansion)
                inactive_base_tooltip = (
                    'Used for standard shots and when Scan? is unchecked.'
                    if active_item is scan_item
                    else 'Used only when Scan? is checked.'
                )
                self.update_expression_item_metadata(inactive_item, inactive_base_tooltip)
                if isinstance(value, Exception):
                    self.update_expression_backgrounds(global_name, error=True)
                    self.update_expression_item_metadata(
                        active_item,
                        '%s: %s' % (value.__class__.__name__, str(value)),
                        QtGui.QIcon(':qtutils/fugue/exclamation'),
                    )
                    self.tab_contains_errors = True
                else:
                    self.update_expression_backgrounds(global_name)
                    self.update_expression_item_metadata(active_item, repr(value))
                self._update_boolean_state(global_name)
            self.set_tab_icon(':qtutils/fugue/exclamation' if self.tab_contains_errors else None)
        else:
            self.set_tab_icon(None)
            for row in range(self.globals_model.rowCount()):
                default_item = self.globals_model.item(row, self.GLOBALS_COL_DEFAULT)
                if default_item.data(self.GLOBALS_ROLE_IS_DUMMY_ROW):
                    continue
                scan_item = self.globals_model.item(row, self.GLOBALS_COL_SCAN)
                for item in (default_item, scan_item):
                    self.update_expression_item_metadata(item, 'Group inactive')
                self.update_expression_backgrounds(
                    self.globals_model.item(row, self.GLOBALS_COL_NAME).text()
                )


class RunmanagerMainWindow(QtWidgets.QMainWindow):
    # A signal to show that the window is shown and painted.
    firstPaint = Signal()

    def __init__(self, *args, **kwargs):
        QtWidgets.QMainWindow.__init__(self, *args, **kwargs)
        self._previously_painted = False

    def closeEvent(self, event):
        if app.on_close_event():
            return QtWidgets.QMainWindow.closeEvent(self, event)
        else:
            event.ignore()

    def paintEvent(self, event):
        result = QtWidgets.QMainWindow.paintEvent(self, event)
        if not self._previously_painted:
            self._previously_painted = True
            self.firstPaint.emit()
        return result


class PoppedOutOutputBoxWindow(QtWidgets.QDialog):

    def closeEvent(self, event):
        app.on_output_popout_button_clicked()


class RunManager(LabscriptApplication):

    app_name = 'runmanager'
    default_config_filename = 'runmanager.toml'

    # Constants for the model in the axes tab:
    AXES_COL_NAME = 0
    AXES_COL_LENGTH = 1
    AXES_COL_SHUFFLE = 2
    AXES_ROLE_NAME = QtCore.Qt.UserRole + 1

    # Constants for the model in the groups tab:
    GROUPS_COL_NAME = 0
    GROUPS_COL_ACTIVE = 1
    GROUPS_COL_DELETE = 2
    GROUPS_COL_OPENCLOSE = 3
    GROUPS_ROLE_IS_DUMMY_ROW = QtCore.Qt.UserRole + 1
    GROUPS_ROLE_PREVIOUS_NAME = QtCore.Qt.UserRole + 2
    GROUPS_ROLE_SORT_DATA = QtCore.Qt.UserRole + 3
    GROUPS_ROLE_GROUP_IS_OPEN = QtCore.Qt.UserRole + 4
    GROUPS_DUMMY_ROW_TEXT = '<Click to add group>'

    def __init__(self):
        splash.update_text('loading graphical interface')
        loader = UiLoader()
        loader.registerCustomWidget(FingerTabWidget)
        loader.registerCustomWidget(TreeView)
        self.ui = loader.load(
            os.path.join(runmanager_dir, 'main.ui'), RunmanagerMainWindow()
        )
        self.init_config_window_title()

        self.output_box = OutputBox(self.ui.verticalLayout_output_tab)

        # Add a 'pop-out' button to the output tab:
        output_tab_index = self.ui.tabWidget.indexOf(self.ui.tab_output)
        self.output_popout_button = TabToolButton(self.ui.tabWidget.parent())
        self.output_popout_button.setIcon(QtGui.QIcon(':/qtutils/fugue/arrow-out'))
        self.output_popout_button.setToolTip('Toggle whether the output box is in a separate window')
        self.ui.tabWidget.tabBar().setTabButton(output_tab_index, QtWidgets.QTabBar.RightSide, self.output_popout_button)
        # Fix the first three tabs in place:
        for index in range(3):
            self.ui.tabWidget.tabBar().setMovable(False, index=index)
        # Whether or not the output box is currently popped out:
        self.output_box_is_popped_out = False
        # The window it will be moved to when popped out:
        self.output_box_window = PoppedOutOutputBoxWindow(self.ui, QtCore.Qt.WindowSystemMenuHint)
        self.output_box_window_verticalLayout = QtWidgets.QVBoxLayout(self.output_box_window)
        self.output_box_window_verticalLayout.setContentsMargins(0, 0, 0, 0)
        self.output_box_window.setWindowTitle('runmanager output')
        self.output_box_window.resize(800, 1000)
        # A flag telling the compilation worker to abort:
        self.compilation_aborted = threading.Event()
        self.setup_config()
        self.setup_axes_tab()
        self.setup_groups_tab()
        self.queue_manager = QueueManager(
            prepare_run_file=self.prepare_queue_shot,
            compile_run_file=self.compile_run_file,
            send_to_runviewer=self.send_to_runviewer,
            output=self.output_box.output,
            compilation_aborted=self.compilation_aborted,
            set_abort_enabled=lambda enabled: inmain(
                self.ui.pushButton_abort.setEnabled, enabled
            ),
        )
        self.setup_queue_tab()
        run_view_layout = self.ui.findChild(QtWidgets.QLayout, 'verticalLayout_2')
        self.analysis_submission = AnalysisSubmission(self, run_view_layout)
        self.connect_signals()

        # The last location from which a labscript file was selected, defaults
        # to labscriptlib:
        self.last_opened_labscript_folder = self.exp_config.get('paths', 'labscriptlib')
        # The last location from which a globals file was selected, defaults
        # to experiment_shot_storage:
        self.last_opened_globals_folder = self.exp_config.get('paths', 'experiment_shot_storage')
        # The last file to which the user saved or loaded a configuration:
        self.last_save_config_file = None
        # The last manually selected shot output folder, defaults to
        # experiment_shot_storage:
        self.last_selected_shot_output_folder = self.exp_config.get('paths', 'experiment_shot_storage')
        self.shared_drive_prefix = self.exp_config.get('paths', 'shared_drive')
        self.experiment_shot_storage = self.exp_config.get('paths', 'experiment_shot_storage')
        # Store the currently open groups as {(globals_filename, group_name): GroupTab}
        self.currently_open_groups = {}

        # A thread that will evaluate globals when they change, allowing us to
        # show their values and any errors in the tabs they came from.
        self.preparse_globals_thread = threading.Thread(target=self.preparse_globals_loop)
        self.preparse_globals_thread.daemon = True
        # A Queue for informing the preparser thread when globals have changed, and thus
        # need parsing again. It is a queue rather than a threading.Event() so that
        # callers can call Queue.join() to wait for parsing to complete in a race-free
        # way
        self.preparse_globals_required = queue.Queue()
        self.preparse_globals_thread.start()

        # A few attributes for self.guess_expansion_modes() to keep track of
        # its state, and thus detect changes:
        self.previous_evaled_globals = {}
        self.previous_global_hierarchy = {}
        self.previous_expansion_types = {}
        self.previous_expansions = {}

        # The prospective number of shots resulting from compilation
        self.n_shots = None

        self.compiler_lock = threading.Lock()
        self._next_default_shot_index = {}

        splash.update_text('starting compiler subprocess')
        # Start the compiler subprocess:
        self.to_child, self.from_child, self.child = process_tree.subprocess(
            os.path.join(runmanager_dir, 'batch_compiler.py'),
            output_redirection_port=self.output_box.port,
        )

        # Is blank until a labscript file is selected:
        self.previous_default_output_folder = ''

        # Start a thread to monitor the time of day and create new shot output
        # folders for each day:
        inthread(self.rollover_shot_output_folder)
        self.non_default_folder = None

        # The data from the last time we saved the configuration, so we can
        # know if something's changed:
        self.last_save_data = None

        # autoload a config file, if labconfig is set to do so:
        active_config_file = self.get_default_config_file()
        try:
            autoload_config_file = self.exp_config.get('runmanager', 'autoload_config_file')
        except (LabConfig.NoOptionError, LabConfig.NoSectionError):
            self.output_box.output('Ready.\n\n')
        else:
            active_config_file = autoload_config_file
            self.ui.setEnabled(False)
            self.output_box.output('Loading default config file %s...' % autoload_config_file)

            def load_the_config_file():
                try:
                    self.load_configuration(autoload_config_file)
                    self.output_box.output('done.\n')
                except Exception as e:
                    self.output_box.output('\nCould not load config file: %s: %s\n\n' %
                                           (e.__class__.__name__, str(e)), red=True)
                else:
                    self.output_box.output('Ready.\n\n')
                finally:
                    self.ui.setEnabled(True)
            # Defer this until 50ms after the window has shown,
            # so that the GUI pops up faster in the meantime
            self.ui.firstPaint.connect(lambda: QtCore.QTimer.singleShot(50, load_the_config_file))

        self.set_config_window_title(active_config_file)
        splash.update_text('done')
        self.ui.show()

    def setup_config(self):
        required_config_params = {"default": ["apparatus_name"],
                                  "programs": ["text_editor",
                                               "text_editor_arguments",
                                               ],
                                  "ports": ['BLACS', 'runviewer', 'lyse'],
                                  "paths": ["shared_drive",
                                            "experiment_shot_storage",
                                            "labscriptlib",
                                            ],
                                  }
        self.exp_config = LabConfig(required_params = required_config_params)

    def setup_axes_tab(self):
        self.axes_model = QtGui.QStandardItemModel()

        # Setup the model columns and link to the treeview
        name_header_item = QtGui.QStandardItem('Name')
        name_header_item.setToolTip('The name of the global or zip group being iterated over')
        self.axes_model.setHorizontalHeaderItem(self.AXES_COL_NAME, name_header_item)

        length_header_item = QtGui.QStandardItem('Length')
        length_header_item.setToolTip('The number of elements in the axis of the parameter space')
        self.axes_model.setHorizontalHeaderItem(self.AXES_COL_LENGTH, length_header_item)

        shuffle_header_item = QtGui.QStandardItem('Shuffle')
        shuffle_header_item.setToolTip('Whether or not the order of the axis should be randomised')
        shuffle_header_item.setIcon(QtGui.QIcon(':qtutils/fugue/arrow-switch'))
        self.axes_model.setHorizontalHeaderItem(self.AXES_COL_SHUFFLE, shuffle_header_item)

        self.ui.treeView_axes.setModel(self.axes_model)

        # Setup stuff for a custom context menu:
        self.ui.treeView_axes.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)

        # Make the actions for the context menu:
        self.action_axes_check_selected = QtWidgets.QAction(QtGui.QIcon(':qtutils/fugue/ui-check-box'),
                                                        'Check selected', self.ui)
        self.action_axes_uncheck_selected = QtWidgets.QAction(QtGui.QIcon(':qtutils/fugue/ui-check-box-uncheck'),
                                                          'Uncheck selected', self.ui)
                                                          
        # setup header widths
        self.ui.treeView_axes.header().setStretchLastSection(False)
        self.ui.treeView_axes.header().setSectionResizeMode(self.AXES_COL_NAME, QtWidgets.QHeaderView.Stretch)
                                                          
    def setup_groups_tab(self):
        self.groups_model = QtGui.QStandardItemModel()
        self.groups_model.setHorizontalHeaderLabels(['File/group name', 'Active', 'Delete', 'Open/Close'])
        self.groups_model.setSortRole(self.GROUPS_ROLE_SORT_DATA)
        self.ui.treeView_groups.setModel(self.groups_model)
        self.ui.treeView_groups.setAnimated(True)  # Pretty
        self.ui.treeView_groups.setSelectionMode(QtWidgets.QTreeView.ExtendedSelection)
        self.ui.treeView_groups.setSortingEnabled(True)
        self.ui.treeView_groups.sortByColumn(self.GROUPS_COL_NAME, QtCore.Qt.AscendingOrder)
        # Set column widths:
        self.ui.treeView_groups.setColumnWidth(self.GROUPS_COL_NAME, 400)
        # Make it so the user can just start typing on an item to edit:
        self.ui.treeView_groups.setEditTriggers(QtWidgets.QTreeView.AnyKeyPressed |
                                                QtWidgets.QTreeView.EditKeyPressed |
                                                QtWidgets.QTreeView.SelectedClicked)
        # Ensure the clickable region of the open/close button doesn't extend forever:
        self.ui.treeView_groups.header().setStretchLastSection(False)
        # Stretch the filpath/groupname column to fill available space:
        self.ui.treeView_groups.header().setSectionResizeMode(
            self.GROUPS_COL_NAME, QtWidgets.QHeaderView.Stretch
        )
        # Shrink columns other than the 'name' column to the size of their headers:
        for column in range(self.groups_model.columnCount()):
            if column != self.GROUPS_COL_NAME:
                self.ui.treeView_groups.resizeColumnToContents(column)

        self.ui.treeView_groups.setTextElideMode(QtCore.Qt.ElideMiddle)
        # Setup stuff for a custom context menu:
        self.ui.treeView_groups.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)

        # Make the actions for the context menu:
        self.action_groups_set_selection_active = QtWidgets.QAction(
            QtGui.QIcon(':qtutils/fugue/ui-check-box'), 'Set selected group(s) active', self.ui)
        self.action_groups_set_selection_inactive = QtWidgets.QAction(
            QtGui.QIcon(':qtutils/fugue/ui-check-box-uncheck'), 'Set selected group(s) inactive', self.ui)
        self.action_groups_delete_selected = QtWidgets.QAction(
            QtGui.QIcon(':qtutils/fugue/minus'), 'Delete selected group(s)', self.ui)
        self.action_groups_open_selected = QtWidgets.QAction(
            QtGui.QIcon(':/qtutils/fugue/plus'), 'Open selected group(s)', self.ui)
        self.action_groups_close_selected_groups = QtWidgets.QAction(
            QtGui.QIcon(':/qtutils/fugue/cross'), 'Close selected group(s)', self.ui)
        self.action_groups_close_selected_files = QtWidgets.QAction(
            QtGui.QIcon(':/qtutils/fugue/cross'), 'Close selected file(s)', self.ui)

        # A counter for keeping track of the recursion depth of
        # self._groups_model_active_changed(). This is used so that some
        # actions can be taken in response to initial data changes, but not to
        # flow-on changes made by the method itself:
        self.on_groups_model_active_changed_recursion_depth = 0

    def setup_queue_tab(self):
        self.tab_queue = QtWidgets.QWidget(self.ui.tabWidget)
        self.tab_queue.setObjectName('tab_queue')
        self.tab_queue.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.verticalLayout_queue_tab = QtWidgets.QVBoxLayout(self.tab_queue)
        self.verticalLayout_queue_tab.setObjectName('verticalLayout_queue_tab')
        controls_layout = QtWidgets.QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(QtWidgets.QLabel('Compile mode', self.tab_queue))
        self.queue_compile_mode_combo = QtWidgets.QComboBox(self.tab_queue)
        self.queue_compile_mode_combo.addItem('Eager compile', COMPILE_MODE_EAGER)
        self.queue_compile_mode_combo.addItem('Lazy compile', COMPILE_MODE_LAZY)
        controls_layout.addWidget(self.queue_compile_mode_combo)
        controls_layout.addWidget(QtWidgets.QLabel('When queue is empty', self.tab_queue))
        self.queue_empty_policy_combo = QtWidgets.QComboBox(self.tab_queue)
        self.queue_empty_policy_combo.addItem('Send nothing', EMPTY_QUEUE_NOTHING)
        self.queue_empty_policy_combo.addItem(
            'Send default shot', EMPTY_QUEUE_DEFAULT_LABSCRIPT
        )
        controls_layout.addWidget(self.queue_empty_policy_combo)
        controls_layout.addStretch(1)
        self.queue_widget = RunmanagerQueueWidget(self.tab_queue)
        self.queue_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.queue_last_sent_label = QtWidgets.QLabel(self.tab_queue)
        self.queue_last_sent_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        self.queue_last_sent_label.setAlignment(
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
        )
        self.queue_last_sent_label.setWordWrap(True)
        self.verticalLayout_queue_tab.addLayout(controls_layout)
        self.verticalLayout_queue_tab.addWidget(self.queue_widget)
        self.verticalLayout_queue_tab.addWidget(self.queue_last_sent_label)
        self.ui.tabWidget.insertTab(0, self.tab_queue, 'Queue')
        queue_tab_index = self.ui.tabWidget.indexOf(self.tab_queue)
        self.ui.tabWidget.tabBar().setMovable(False, index=queue_tab_index)
        self.ui.tabWidget.setCurrentWidget(self.tab_queue)
        self.refresh_queue_tab()

    def connect_signals(self):
        # The button that pops the output box in and out:
        self.output_popout_button.clicked.connect(self.on_output_popout_button_clicked)

        # The menu items:
        self.ui.actionLoad_configuration.triggered.connect(self.on_load_configuration_triggered)
        self.ui.actionRevert_configuration.triggered.connect(self.on_revert_configuration_triggered)
        self.ui.actionSave_configuration.triggered.connect(self.on_save_configuration_triggered)
        self.ui.actionSave_configuration_as.triggered.connect(self.on_save_configuration_as_triggered)
        self.ui.actionQuit.triggered.connect(self.ui.close)

        # labscript file and folder selection stuff:
        self.ui.toolButton_select_labscript_file.clicked.connect(self.on_select_labscript_file_clicked)
        self.ui.toolButton_select_default_labscript_file.clicked.connect(
            self.on_select_default_labscript_file_clicked
        )
        self.ui.toolButton_select_shot_output_folder.clicked.connect(self.on_select_shot_output_folder_clicked)
        self.ui.toolButton_edit_labscript_file.clicked.connect(self.on_edit_labscript_file_clicked)
        self.ui.toolButton_edit_default_labscript_file.clicked.connect(
            self.on_edit_default_labscript_file_clicked
        )
        self.ui.toolButton_reset_shot_output_folder.clicked.connect(self.on_reset_shot_output_folder_clicked)
        self.ui.lineEdit_labscript_file.textChanged.connect(self.on_labscript_file_text_changed)
        self.ui.lineEdit_default_labscript_file.textChanged.connect(
            self.on_default_labscript_file_text_changed
        )
        self.ui.lineEdit_shot_output_folder.textChanged.connect(self.on_shot_output_folder_text_changed)

        # Control buttons; engage, abort, restart subprocess:
        self.setup_engage_submission_menu()
        self.ui.pushButton_engage.clicked.connect(self.on_engage_clicked)
        self.ui.pushButton_abort.clicked.connect(self.on_abort_clicked)
        self.ui.pushButton_restart_subprocess.clicked.connect(self.on_restart_subprocess_clicked)
        
        # shuffle master control
        self.ui.pushButton_shuffle.stateChanged.connect(self.on_master_shuffle_clicked)

        # Tab closebutton clicked:
        self.ui.tabWidget.tabCloseRequested.connect(self.on_tabCloseRequested)

        # Axes tab; right click menu, menu actions, reordering
        # self.ui.treeView_axes.customContextMenuRequested.connect(self.on_treeView_axes_context_menu_requested)
        self.action_axes_check_selected.triggered.connect(self.on_axes_check_selected_triggered)
        self.action_axes_uncheck_selected.triggered.connect(self.on_axes_uncheck_selected_triggered)
        self.ui.toolButton_axis_to_top.clicked.connect(self.on_axis_to_top_clicked)
        self.ui.toolButton_axis_up.clicked.connect(self.on_axis_up_clicked)
        self.ui.toolButton_axis_down.clicked.connect(self.on_axis_down_clicked)
        self.ui.toolButton_axis_to_bottom.clicked.connect(self.on_axis_to_bottom_clicked)
        # axes tab item changed handler
        self.axes_model.itemChanged.connect(self.on_axes_item_changed)
        self.axes_model.rowsRemoved.connect(self.update_global_shuffle_state)
        self.axes_model.rowsInserted.connect(self.update_global_shuffle_state)

        # Groups tab; right click menu, menu actions, open globals file, new globals file, diff globals file,
        self.ui.treeView_groups.customContextMenuRequested.connect(self.on_treeView_groups_context_menu_requested)
        self.action_groups_set_selection_active.triggered.connect(
            lambda: self.on_groups_set_selection_active_triggered(QtCore.Qt.Checked))
        self.action_groups_set_selection_inactive.triggered.connect(
            lambda: self.on_groups_set_selection_active_triggered(QtCore.Qt.Unchecked))
        self.action_groups_delete_selected.triggered.connect(self.on_groups_delete_selected_triggered)
        self.action_groups_open_selected.triggered.connect(self.on_groups_open_selected_triggered)
        self.action_groups_close_selected_groups.triggered.connect(self.on_groups_close_selected_groups_triggered)
        self.action_groups_close_selected_files.triggered.connect(self.on_groups_close_selected_files_triggered)

        self.ui.pushButton_open_globals_file.clicked.connect(self.on_open_globals_file_clicked)
        self.ui.pushButton_new_globals_file.clicked.connect(self.on_new_globals_file_clicked)
        self.ui.pushButton_diff_globals_file.clicked.connect(self.on_diff_globals_file_clicked)
        self.ui.treeView_groups.leftClicked.connect(self.on_treeView_groups_leftClicked)
        self.ui.treeView_groups.doubleLeftClicked.connect(self.on_treeView_groups_doubleLeftClicked)
        self.groups_model.itemChanged.connect(self.on_groups_model_item_changed)
        # A context manager with which we can temporarily disconnect the above connection.
        self.groups_model_item_changed_disconnected = DisconnectContextManager(
            self.groups_model.itemChanged, self.on_groups_model_item_changed)

        self.queue_manager.queueChanged.connect(self.refresh_queue_tab)
        self.queue_compile_mode_combo.currentIndexChanged.connect(
            self.on_queue_compile_mode_changed
        )
        self.queue_empty_policy_combo.currentIndexChanged.connect(
            self.on_queue_empty_policy_changed
        )
        self.queue_widget.deleteRowsRequested.connect(self.on_queue_delete_rows_requested)
        
        # Keyboard shortcuts:
        engage_shortcut = QtWidgets.QShortcut('F5', self.ui,
            lambda: self.ui.pushButton_engage.clicked.emit(False))
        engage_shortcut.setAutoRepeat(False)
        QtWidgets.QShortcut('ctrl+W', self.ui, self.close_current_tab)
        QtWidgets.QShortcut('ctrl+Tab', self.ui, lambda: self.switch_tabs(+1))
        QtWidgets.QShortcut('ctrl+shift+Tab', self.ui, lambda: self.switch_tabs(-1))

    def on_close_event(self):
        save_data = self.get_save_data()
        if self.last_save_data is not None and save_data != self.last_save_data:
            message = ('Current configuration (which groups are active/open and other GUI state) '
                       'has changed: save config file \'%s\'?' % self.last_save_config_file)
            reply = QtWidgets.QMessageBox.question(self.ui, 'Quit runmanager', message,
                                               QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel)
            if reply == QtWidgets.QMessageBox.Cancel:
                return False
            if reply == QtWidgets.QMessageBox.Yes:
                self.save_configuration(self.last_save_config_file)
        self.analysis_submission.shutdown()
        self.queue_manager.shutdown()
        self.to_child.put(['quit', None])
        self.output_box.shutdown()
        return True

    def close_current_tab(self):
        current_tab_widget = self.ui.tabWidget.currentWidget()
        for (globals_file, group_name), tab in self.currently_open_groups.items():
            if tab.ui is current_tab_widget:
                self.close_group(globals_file, group_name)

    def switch_tabs(self, change):
        current_index = self.ui.tabWidget.currentIndex()
        n_tabs = self.ui.tabWidget.count()
        new_index = (current_index + change) % n_tabs
        self.ui.tabWidget.setCurrentIndex(new_index)

    def on_queue_delete_rows_requested(self, rows):
        self.queue_manager.delete_rows(rows)

    def on_queue_empty_policy_changed(self, index):
        empty_queue_policy = self.queue_empty_policy_combo.itemData(index)
        if empty_queue_policy is None:
            return
        self.queue_manager.set_empty_queue_policy(empty_queue_policy)

    def on_queue_compile_mode_changed(self, index):
        compile_mode = self.queue_compile_mode_combo.itemData(index)
        if compile_mode is None:
            return
        self.queue_manager.set_compile_mode(compile_mode)

    @inmain_decorator()
    def refresh_queue_tab(self):
        controller = self.queue_manager.controller
        self.queue_widget.set_queue_paths(controller.get_queue_display_items())
        state = controller.get_queue_state()
        last_sent_from_queue = state['last_sent_from_queue']
        if last_sent_from_queue:
            self.queue_last_sent_label.setText(
                'Last sent from queue: %s' % last_sent_from_queue
            )
            self.queue_last_sent_label.setToolTip(last_sent_from_queue)
        else:
            self.queue_last_sent_label.setText('Last sent from queue: nothing')
            self.queue_last_sent_label.setToolTip('')

    def on_output_popout_button_clicked(self):
        if self.output_box_is_popped_out:
            self.ui.verticalLayout_output_tab.addWidget(self.output_box.output_textedit)
            self.output_box_window.hide()
            self.output_popout_button.setIcon(QtGui.QIcon(':/qtutils/fugue/arrow-out'))
        else:
            # pop it out
            # self.ui.verticalLayout_output_tab.remove(self.output_box)
            self.output_box_window_verticalLayout.addWidget(self.output_box.output_textedit)
            self.output_popout_button.setIcon(QtGui.QIcon(':/qtutils/fugue/arrow-in'))
            self.output_box_window.show()
        self.output_box_is_popped_out = not self.output_box_is_popped_out

    def select_labscript_file(self, title):
        labscript_file = QtWidgets.QFileDialog.getOpenFileName(
            self.ui, title, self.last_opened_labscript_folder, "Python files (*.py)"
        )
        if type(labscript_file) is tuple:
            labscript_file, _ = labscript_file

        if not labscript_file:
            return None
        labscript_file = os.path.abspath(labscript_file)
        if not os.path.isfile(labscript_file):
            error_dialog("No such file %s." % labscript_file)
            return None
        self.last_opened_labscript_folder = os.path.dirname(labscript_file)
        return labscript_file

    def on_select_labscript_file_clicked(self, checked):
        labscript_file = self.select_labscript_file('Select labscript file')
        if not labscript_file:
            return
        self.ui.lineEdit_labscript_file.setText(labscript_file)

    def on_select_default_labscript_file_clicked(self, checked):
        labscript_file = self.select_labscript_file('Select default-shot labscript file')
        if not labscript_file:
            return
        self.ui.lineEdit_default_labscript_file.setText(labscript_file)

    def edit_labscript_file(self, labscript_file):
        editor_path = self.exp_config.get('programs', 'text_editor')
        editor_args = self.exp_config.get('programs', 'text_editor_arguments')
        if not labscript_file:
            return
        if not editor_path:
            error_dialog("No editor specified in the labconfig.")
        if '{file}' in editor_args:
            editor_args = [arg if arg != '{file}' else labscript_file for arg in editor_args.split()]
        else:
            editor_args = [labscript_file] + editor_args.split()
        try:
            subprocess.Popen([editor_path] + editor_args)
        except Exception as e:
            error_dialog("Unable to launch text editor specified in %s. Error was: %s" %
                         (self.exp_config.config_path, str(e)))

    def on_edit_labscript_file_clicked(self, checked):
        self.edit_labscript_file(self.ui.lineEdit_labscript_file.text())

    def on_edit_default_labscript_file_clicked(self, checked):
        self.edit_labscript_file(self.ui.lineEdit_default_labscript_file.text())

    def on_select_shot_output_folder_clicked(self, checked):
        shot_output_folder = QtWidgets.QFileDialog.getExistingDirectory(self.ui,
                                                                    'Select shot output folder',
                                                                    self.last_selected_shot_output_folder)
        if type(shot_output_folder) is tuple:
            shot_output_folder, _ = shot_output_folder

        if not shot_output_folder:
            # User cancelled selection
            return
        # Convert to standard platform specific path, otherwise Qt likes forward slashes:
        shot_output_folder = os.path.abspath(shot_output_folder)
        # Save the containing folder for use next time we open the dialog box:
        self.last_selected_shot_output_folder = os.path.dirname(shot_output_folder)
        # Write the file to the lineEdit:
        self.ui.lineEdit_shot_output_folder.setText(shot_output_folder)
        # Update our knowledge about whether this is the default output folder or not:
        self.check_output_folder_update()

    def on_reset_shot_output_folder_clicked(self, checked):
        current_default_output_folder = self.get_default_output_folder()
        if current_default_output_folder is None:
            return
        self.ui.lineEdit_shot_output_folder.setText(current_default_output_folder)
        self.check_output_folder_update()

    def on_labscript_file_text_changed(self, text):
        # Blank out the 'edit labscript file' button if no labscript file is
        # selected
        enabled = bool(text)
        self.ui.toolButton_edit_labscript_file.setEnabled(enabled)
        # Blank out the 'select shot output folder' button if no labscript
        # file is selected:
        self.ui.toolButton_select_shot_output_folder.setEnabled(enabled)
        self.ui.lineEdit_labscript_file.setToolTip(text)
        # Check if the output folder needs to be updated:
        self.check_output_folder_update()

    def on_default_labscript_file_text_changed(self, text):
        self.ui.toolButton_edit_default_labscript_file.setEnabled(bool(text))
        self.ui.lineEdit_default_labscript_file.setToolTip(text)
        self.queue_manager.set_default_labscript_file(text)
        self.refresh_queue_tab()

    def on_shot_output_folder_text_changed(self, text):
        # Blank out the 'reset default output folder' button if the user is
        # already using the default output folder
        if text == self.get_default_output_folder():
            self.non_default_folder = False
        else:
            self.non_default_folder = True
        self.ui.toolButton_reset_shot_output_folder.setEnabled(self.non_default_folder)
        self.ui.label_non_default_folder.setVisible(self.non_default_folder)
        self.ui.lineEdit_shot_output_folder.setToolTip(text)

    def setup_engage_submission_menu(self):
        button = self.ui.pushButton_engage
        button.setPopupMode(QtWidgets.QToolButton.DelayedPopup)
        self.engage_submission_menu = QtWidgets.QMenu(button)
        self.engage_new_sequence_action = self.engage_submission_menu.addAction(
            'Add shots to new sequence'
        )
        self.engage_new_sequence_action.triggered.connect(
            lambda: self.on_engage_clicked(submission_mode=SUBMISSION_MODE_NEW_FOLDER)
        )
        self.engage_new_sequence_action.setToolTip(
            'Submit the new batch as a new shot sequence.'
        )
        self.engage_new_sequence_action.setStatusTip(
            'Submit the new batch as a new shot sequence.'
        )
        self.engage_add_shots_action = self.engage_submission_menu.addAction(
            'Add shots to last sequence'
        )
        self.engage_add_shots_action.triggered.connect(
            lambda: self.on_engage_clicked(submission_mode=SUBMISSION_MODE_ADD_SHOTS)
        )
        self.engage_replace_queue_action = self.engage_submission_menu.addAction(
            'Empty queue, then add shots to new sequence'
        )
        self.engage_replace_queue_action.triggered.connect(
            lambda: self.on_engage_clicked(
                submission_mode=SUBMISSION_MODE_NEW_FOLDER_CLEAR_QUEUE
            )
        )
        self.engage_replace_queue_action.setToolTip(
            'Delete the remaining queued shots, then submit the replacement batch as a new shot sequence.'
        )
        self.engage_replace_queue_action.setStatusTip(
            'Delete the remaining queued shots, then submit the replacement batch as a new shot sequence.'
        )
        self.engage_add_clear_action = self.engage_submission_menu.addAction(
            'Empty queue, then add shots to last sequence'
        )
        self.engage_add_clear_action.triggered.connect(
            lambda: self.on_engage_clicked(
                submission_mode=SUBMISSION_MODE_ADD_SHOTS_CLEAR_QUEUE
            )
        )
        self.engage_add_clear_action.setToolTip(
            'Delete the remaining queued shots, then submit the replacement batch onto the same shot sequence.'
        )
        self.engage_add_clear_action.setStatusTip(
            'Delete the remaining queued shots, then submit the replacement batch onto the same shot sequence.'
        )
        self.engage_submission_menu.aboutToShow.connect(
            self.update_engage_submission_menu_actions
        )
        button.setMenu(self.engage_submission_menu)
        button.setToolTip(
            """<html><head/><body><p>Compile pending shots, submit them to BLACS if "run shots" is checked, and send them to runviewer if "view shots" is checked.</p><p>Press and hold to choose alternate queue submission modes.</p><p><span style="font-style:italic;">Empty queue, then add shots to new sequence</span> and <span style="font-style:italic;">Empty queue, then add shots to last sequence</span> delete the remaining queued shots before submitting the replacement batch. With lazy compile enabled, later compile failures are still possible when BLACS requests those shots.</p></body></html>"""
        )

    def get_queue_append_filepath(self):
        queue_paths = self.queue_manager.get_queue_paths()
        if not queue_paths:
            return None
        return os.path.abspath(queue_paths[-1])

    def get_last_sent_from_queue_filepath(self):
        queue_state = self.queue_manager.get_queue_state()
        last_sent_from_queue = queue_state.get('last_sent_from_queue')
        if not last_sent_from_queue:
            return None
        return os.path.abspath(shared_drive.path_to_local(last_sent_from_queue))

    def can_use_alternate_submission_mode(self):
        return (
            self.ui.checkBox_run_shots.isChecked()
            and self.get_queue_append_filepath() is not None
        )

    def update_engage_submission_menu_actions(self):
        enabled = self.can_use_alternate_submission_mode()
        self.engage_add_shots_action.setEnabled(enabled)
        self.engage_replace_queue_action.setEnabled(enabled)
        self.engage_add_clear_action.setEnabled(enabled)

    def reindex_run_file_infos(
        self,
        run_file_infos,
        output_folder,
        filename_prefix,
        indexed_path_base=None,
        index_start=None,
    ):
        if not run_file_infos:
            return run_file_infos
        if indexed_path_base is None:
            indexed_path_base = os.path.join(output_folder, '{}.h5'.format(filename_prefix))
        candidate_stem = os.path.splitext(os.path.basename(indexed_path_base))[0]
        _, _, index_str = candidate_stem.rpartition('_')
        width = len(index_str) if index_str.isdigit() else 1
        suffix_format = '_{index:0%dd}' % width
        next_index = index_start
        for run_file_info in run_file_infos:
            run_file, next_index = next_available_indexed_filepath(
                indexed_path_base,
                suffix_format,
                start=next_index,
            )
            run_file_info['path'] = run_file
            next_index += 1
        return run_file_infos

    def on_engage_clicked(self, checked=False, submission_mode=SUBMISSION_MODE_NEW_FOLDER):
        logger.info('Engage')
        try:
            send_to_BLACS = self.ui.checkBox_run_shots.isChecked()
            send_to_runviewer = self.ui.checkBox_view_shots.isChecked()
            if not send_to_BLACS and not send_to_runviewer:
                self.output_box.output(
                    "Warning: neither 'Run shot(s)' nor 'View shot(s)' is selected.\n\n",
                    red=True,
                )
                return
            queue_append_filepath = None
            if submission_mode != SUBMISSION_MODE_NEW_FOLDER:
                queue_append_filepath = self.get_queue_append_filepath()
                if not send_to_BLACS:
                    self.output_box.output(
                        "Warning: alternate queue submission modes require 'Run shot(s)' to be selected.\n\n",
                        red=True,
                    )
                    return
                if queue_append_filepath is None:
                    self.output_box.output(
                        'Warning: alternate queue submission modes require shots in the queue.\n\n',
                        red=True,
                    )
                    return
            labscript_file = self.ui.lineEdit_labscript_file.text()
            # even though we shuffle on a per global basis, if ALL of the globals are set to shuffle, then we may as well shuffle again. This helps shuffle shots more randomly than just shuffling within each level (because without this, you would still do all shots with the outer most variable the same, etc)
            shuffle = self.ui.pushButton_shuffle.checkState() == QtCore.Qt.Checked
            if not labscript_file:
                raise Exception('Error: No labscript file selected')
            output_folder = self.ui.lineEdit_shot_output_folder.text()
            if not output_folder:
                raise Exception('Error: No output folder selected')
            logger.info('Parsing globals...')
            active_groups = self.get_active_groups()
            # Get ordering of expansion globals
            expansion_order = {}
            for i in range(self.axes_model.rowCount()):
                item = self.axes_model.item(i, self.AXES_COL_NAME)
                shuffle_item = self.axes_model.item(i, self.AXES_COL_SHUFFLE)
                name = item.data(self.AXES_ROLE_NAME)
                expansion_order[name] = {'order':i, 'shuffle':shuffle_item.checkState()}
            
            try:
                sequenceglobals, shots, evaled_globals, global_hierarchy, expansions = self.parse_globals(active_groups, expansion_order=expansion_order)
            except Exception as e:
                raise Exception('Error parsing globals:\n%s\nCompilation aborted.' % str(e))
            logger.info('Making h5 files')
            globals_details = runmanager.get_globals_details(active_groups)
            indexed_path_base = None
            index_start = None
            if submission_mode == SUBMISSION_MODE_ADD_SHOTS:
                indexed_path_base = queue_append_filepath
            elif submission_mode == SUBMISSION_MODE_NEW_FOLDER_CLEAR_QUEUE:
                self.queue_manager.clear()
            elif submission_mode == SUBMISSION_MODE_ADD_SHOTS_CLEAR_QUEUE:
                indexed_path_base = (
                    self.get_last_sent_from_queue_filepath() or queue_append_filepath
                )
                index_start = 0
                self.queue_manager.clear()
            labscript_file, run_files = self.make_h5_files(
                labscript_file,
                output_folder,
                sequenceglobals,
                shots,
                shuffle,
                with_metadata=True,
                indexed_path_base=indexed_path_base,
                index_start=index_start,
            )
            compile_mode = self.queue_compile_mode_combo.currentData()
            queue_records = []
            for run_file_info in run_files:
                queue_records.append(
                    {
                        'path': run_file_info['path'],
                        'labscript_file': labscript_file,
                        'active_groups': dict(active_groups),
                        'compile_mode': compile_mode,
                        'compiled': False,
                        'frozen_globals': runmanager.get_frozen_globals(
                            globals_details,
                            run_file_info['shot_globals'],
                        ),
                        'sequence_attrs': run_file_info['sequence_attrs'],
                        'run_no': run_file_info['run_no'],
                        'n_runs': run_file_info['n_runs'],
                    }
                )
            self.ui.pushButton_abort.setEnabled(True)
            self.queue_manager.compile_shots(
                queue_records, send_to_BLACS, send_to_runviewer
            )
        except Exception as e:
            self.output_box.output('%s\n\n' % str(e), red=True)
        logger.info('end engage')

    def on_abort_clicked(self):
        self.compilation_aborted.set()

    def on_restart_subprocess_clicked(self):
        # Kill and restart the compilation subprocess
        self.to_child.put(['quit', None])
        self.from_child.put(['done', False])
        time.sleep(0.1)
        self.output_box.output('Asking subprocess to quit...')
        timeout_time = time.time() + 2
        QtCore.QTimer.singleShot(50, lambda: self.check_child_exited(timeout_time, kill=False))

    def check_child_exited(self, timeout_time, kill=False):
        self.child.poll()
        if self.child.returncode is None and time.time() < timeout_time:
            QtCore.QTimer.singleShot(50, lambda: self.check_child_exited(timeout_time, kill))
            return
        elif self.child.returncode is None:
            if not kill:
                self.child.terminate()
                self.output_box.output('not responding.\n')
                timeout_time = time.time() + 2
                QtCore.QTimer.singleShot(50, lambda: self.check_child_exited(timeout_time, kill=True))
                return
            else:
                self.child.kill()
                self.output_box.output('Killed\n', red=True)
        elif kill:
            self.output_box.output('Terminated\n', red=True)
        else:
            self.output_box.output('done.\n')
        self.output_box.output('Spawning new compiler subprocess...')
        self.to_child, self.from_child, self.child = process_tree.subprocess(
            os.path.join(runmanager_dir, 'batch_compiler.py'),
            output_redirection_port=self.output_box.port,
        )
        self.output_box.output('done.\n')
        self.output_box.output('Ready.\n\n')

    def on_tabCloseRequested(self, index):
        tab_page = self.ui.tabWidget.widget(index)
        for (globals_file, group_name), group_tab in self.currently_open_groups.items():
            if group_tab.ui is tab_page:
                self.close_group(globals_file, group_name)
                break

    def on_treeView_axes_context_menu_requested(self, point):
        raise NotImplementedError
        # menu = QtWidgets.QMenu(self.ui)
        # menu.addAction(self.action_axes_check_selected)
        # menu.addAction(self.action_axes_uncheck_selected)
        # menu.exec_(QtGui.QCursor.pos())
        pass

    def on_axes_check_selected_triggered(self, *args):
        raise NotImplementedError

    def on_axes_uncheck_selected_triggered(self, *args):
        raise NotImplementedError
        
    def on_axis_to_top_clicked(self, checked):
        # Get the selection model from the treeview
        selection_model = self.ui.treeView_axes.selectionModel()    
        # Create a list of select row indices
        selected_row_list = [index.row() for index in sorted(selection_model.selectedRows())]
        # For each row selected
        for i,row in enumerate(selected_row_list):
            # only move the row while it is not element 0, and the row above it is not selected
            # (note that while a row above may have been initially selected, it should by now, be one row higher
            # since we start moving elements of the list upwards starting from the lowest index)
            while row > 0 and (row-1) not in selected_row_list:
                # Remove the selected row
                items = self.axes_model.takeRow(row)
                # Add the selected row into a position one above
                self.axes_model.insertRow(row-1,items)
                # Since it is now a newly inserted row, select it again
                selection_model.select(self.axes_model.indexFromItem(items[0]),QtCore.QItemSelectionModel.SelectCurrent|QtCore.QItemSelectionModel.Rows)
                # reupdate the list of selected indices to reflect this change
                selected_row_list[i] -= 1
                row -= 1
                    
        self.update_axes_indentation()

    def on_axis_up_clicked(self, checked):
        # Get the selection model from the treeview
        selection_model = self.ui.treeView_axes.selectionModel()    
        # Create a list of select row indices
        selected_row_list = [index.row() for index in sorted(selection_model.selectedRows())]
        # For each row selected
        for i,row in enumerate(selected_row_list):
            # only move the row if it is not element 0, and the row above it is not selected
            # (note that while a row above may have been initially selected, it should by now, be one row higher
            # since we start moving elements of the list upwards starting from the lowest index)
            if row > 0 and (row-1) not in selected_row_list:
                # Remove the selected row
                items = self.axes_model.takeRow(row)
                # Add the selected row into a position one above
                self.axes_model.insertRow(row-1,items)
                # Since it is now a newly inserted row, select it again
                selection_model.select(self.axes_model.indexFromItem(items[0]),QtCore.QItemSelectionModel.SelectCurrent|QtCore.QItemSelectionModel.Rows)
                # reupdate the list of selected indices to reflect this change
                selected_row_list[i] -= 1
                
        self.update_axes_indentation()

    def on_axis_down_clicked(self, checked):
        # Get the selection model from the treeview
        selection_model = self.ui.treeView_axes.selectionModel()    
        # Create a list of select row indices
        selected_row_list = [index.row() for index in reversed(sorted(selection_model.selectedRows()))]
        # For each row selected
        for i,row in enumerate(selected_row_list):
            # only move the row if it is not the last element, and the row above it is not selected
            # (note that while a row below may have been initially selected, it should by now, be one row lower
            # since we start moving elements of the list upwards starting from the highest index)
            if row < self.axes_model.rowCount()-1 and (row+1) not in selected_row_list:
                # Remove the selected row
                items = self.axes_model.takeRow(row)
                # Add the selected row into a position one above
                self.axes_model.insertRow(row+1,items)
                # Since it is now a newly inserted row, select it again
                selection_model.select(self.axes_model.indexFromItem(items[0]),QtCore.QItemSelectionModel.SelectCurrent|QtCore.QItemSelectionModel.Rows)
                # reupdate the list of selected indices to reflect this change
                selected_row_list[i] += 1
            
        self.update_axes_indentation()

    def on_axis_to_bottom_clicked(self, checked):
        selection_model = self.ui.treeView_axes.selectionModel()    
        # Create a list of select row indices
        selected_row_list = [index.row() for index in reversed(sorted(selection_model.selectedRows()))]
        # For each row selected
        for i,row in enumerate(selected_row_list):
            # only move the row while it is not the last element, and the row above it is not selected
            # (note that while a row below may have been initially selected, it should by now, be one row lower
            # since we start moving elements of the list upwards starting from the highest index)
            while row < self.axes_model.rowCount()-1 and (row+1) not in selected_row_list:
                # Remove the selected row
                items = self.axes_model.takeRow(row)
                # Add the selected row into a position one above
                self.axes_model.insertRow(row+1,items)
                # Since it is now a newly inserted row, select it again
                selection_model.select(self.axes_model.indexFromItem(items[0]),QtCore.QItemSelectionModel.SelectCurrent|QtCore.QItemSelectionModel.Rows)
                # reupdate the list of selected indices to reflect this change
                selected_row_list[i] += 1
                row += 1
                
        self.update_axes_indentation()
        
    def on_axes_item_changed(self, item):
        if item.column() == self.AXES_COL_SHUFFLE:
            self.update_global_shuffle_state()
            
    def update_global_shuffle_state(self, *args, **kwargs):
        all_checked = True
        none_checked = True
        for i in range(self.axes_model.rowCount()):
            check_state = self.axes_model.item(i, self.AXES_COL_SHUFFLE).checkState() == QtCore.Qt.Checked
            all_checked = all_checked and check_state
            none_checked = none_checked and not check_state
            
        if not all_checked and not none_checked:
            self.ui.pushButton_shuffle.setTristate(True)
            self.ui.pushButton_shuffle.setCheckState(QtCore.Qt.PartiallyChecked)
        elif none_checked and not all_checked:
            self.ui.pushButton_shuffle.setTristate(False)
            self.ui.pushButton_shuffle.setCheckState(QtCore.Qt.Unchecked)
        elif all_checked and not none_checked:
            self.ui.pushButton_shuffle.setTristate(False)
            self.ui.pushButton_shuffle.setCheckState(QtCore.Qt.Checked)
        else:
            # No axes. Set if partially checked, otherwise else leave it alone:
            if self.ui.pushButton_shuffle.checkState() == QtCore.Qt.PartiallyChecked:
                self.ui.pushButton_shuffle.setTristate(False)
                self.ui.pushButton_shuffle.setCheckState(QtCore.Qt.Checked)


    def on_master_shuffle_clicked(self, state):
        if state in [QtCore.Qt.Checked, QtCore.Qt.Unchecked]:
            self.ui.pushButton_shuffle.setTristate(False)
            for i in range(self.axes_model.rowCount()):
                item = self.axes_model.item(i, self.AXES_COL_SHUFFLE)
                if item.checkState() != state:
                    self.axes_model.item(i, self.AXES_COL_SHUFFLE).setCheckState(state)

    def on_treeView_groups_context_menu_requested(self, point):
        menu = QtWidgets.QMenu(self.ui)
        menu.addAction(self.action_groups_set_selection_active)
        menu.addAction(self.action_groups_set_selection_inactive)
        menu.addAction(self.action_groups_delete_selected)
        menu.addAction(self.action_groups_open_selected)
        menu.addAction(self.action_groups_close_selected_groups)
        menu.addAction(self.action_groups_close_selected_files)
        copy_menu = QtWidgets.QMenu('Copy selected group(s) to...', menu)
        copy_menu.setIcon(QtGui.QIcon(':/qtutils/fugue/blue-document-copy'))
        menu.addMenu(copy_menu)
        move_menu = QtWidgets.QMenu('Move selected group(s) to...', menu)
        move_menu.setIcon(QtGui.QIcon(':/qtutils/fugue/blue-document--arrow'))
        menu.addMenu(move_menu)

        # Create a dict of all filepaths -> filenames
        filenames = {}
        for index in range(self.groups_model.rowCount()):
            filepath = self.groups_model.item(index, self.GROUPS_COL_NAME).text()
            filenames[filepath] = filepath.split(os.sep)[-1]

        # expand duplicate filenames until there is nomore duplicates
        new_filename = {}
        i = 2
        while new_filename != filenames:
            for filepath, filename in filenames.items():
                if list(filenames.values()).count(filename) > 1:
                    new_filename[filepath] = os.sep.join(filepath.split(os.sep)[-i:])
                else:
                    new_filename[filepath] = filename
            filenames = new_filename
            i += 1

        # add all filenames to the copy and move submenu
        for filepath, filename in filenames.items():
            copy_menu.addAction(filename, lambda filepath=filepath: self.on_groups_copy_selected_groups_triggered(filepath, False))
            move_menu.addAction(filename, lambda filepath=filepath: self.on_groups_copy_selected_groups_triggered(filepath, True))

        menu.exec_(QtGui.QCursor.pos())

    def on_groups_copy_selected_groups_triggered(self, dest_globals_file=None, delete_source_group=False):
        selected_indexes = self.ui.treeView_groups.selectedIndexes()
        selected_items = (self.groups_model.itemFromIndex(index) for index in selected_indexes)
        name_items = [item for item in selected_items
                      if item.column() == self.GROUPS_COL_NAME
                      and item.parent() is not None]
        for item in name_items:
            source_globals_file = item.parent().text()
            self.copy_group(source_globals_file, item.text(), dest_globals_file, delete_source_group)

    def on_groups_set_selection_active_triggered(self, checked_state):
        selected_indexes = self.ui.treeView_groups.selectedIndexes()
        # Filter to only include the 'active' column:
        selected_items = (self.groups_model.itemFromIndex(index) for index in selected_indexes)
        active_items = (item for item in selected_items
                        if item.column() == self.GROUPS_COL_ACTIVE
                        and item.parent() is not None)
        for item in active_items:
            item.setCheckState(checked_state)

    def on_groups_delete_selected_triggered(self):
        selected_indexes = self.ui.treeView_groups.selectedIndexes()
        selected_items = (self.groups_model.itemFromIndex(index) for index in selected_indexes)
        name_items = [item for item in selected_items
                      if item.column() == self.GROUPS_COL_NAME
                      and item.parent() is not None]
        # If multiple selected, show 'delete n groups?' message. Otherwise,
        # pass confirm=True to self.delete_group so it can show the regular
        # message.
        confirm_multiple = (len(name_items) > 1)
        if confirm_multiple:
            if not question_dialog("Delete %d groups?" % len(name_items)):
                return
        for item in name_items:
            globals_file = item.parent().text()
            group_name = item.text()
            self.delete_group(globals_file, group_name, confirm=not confirm_multiple)

    def on_groups_open_selected_triggered(self):
        selected_indexes = self.ui.treeView_groups.selectedIndexes()
        selected_items = [self.groups_model.itemFromIndex(index) for index in selected_indexes]
        name_items = [item for item in selected_items
                      if item.column() == self.GROUPS_COL_NAME
                      and item.parent() is not None]

        # Include all grous of selected globals files:
        for item in selected_items:
            if item.parent() is None:
                children = [item.child(i) for i in range(item.rowCount())]
                # Exclude <add new group> item, which is not selectable
                name_items += [child for child in children if child.isSelectable() ]

        filenames = set(item.parent().text() for item in name_items)
        for item in name_items:
            globals_file = item.parent().text()
            group_name = item.text()
            if (globals_file, group_name) not in self.currently_open_groups:
                self.open_group(globals_file, group_name, trigger_preparse=False)
        if name_items:
            self.globals_changed()

    def on_groups_close_selected_groups_triggered(self):
        selected_indexes = self.ui.treeView_groups.selectedIndexes()
        selected_items = (self.groups_model.itemFromIndex(index) for index in selected_indexes)
        name_items = [item for item in selected_items
                      if item.column() == self.GROUPS_COL_NAME
                      and item.parent() is not None]
        for item in name_items:
            globals_file = item.parent().text()
            group_name = item.text()
            if (globals_file, group_name) in self.currently_open_groups:
                self.close_group(globals_file, group_name)

    def on_groups_close_selected_files_triggered(self):
        selected_indexes = self.ui.treeView_groups.selectedIndexes()
        selected_items = (self.groups_model.itemFromIndex(index) for index in selected_indexes)
        name_items = [item for item in selected_items
                      if item.column() == self.GROUPS_COL_NAME
                      and item.parent() is None]
        child_openclose_items = [item.child(i, self.GROUPS_COL_OPENCLOSE)
                                 for item in name_items
                                 for i in range(item.rowCount())]
        child_is_open = [child_item.data(self.GROUPS_ROLE_GROUP_IS_OPEN)
                         for child_item in child_openclose_items]
        if any(child_is_open):
            if not question_dialog('Close %d file(s)? This will close %d currently open group(s).' %
                                   (len(name_items), child_is_open.count(True))):
                return
        for item in name_items:
            globals_file = item.text()
            self.close_globals_file(globals_file, confirm=False)

    def on_open_globals_file_clicked(self):
        globals_file = QtWidgets.QFileDialog.getOpenFileName(self.ui,
                                                         'Select globals file',
                                                         self.last_opened_globals_folder,
                                                         "Globals files (*.toml *.h5 *.hdf5)")
        if type(globals_file) is tuple:
            globals_file, _ = globals_file

        if not globals_file:
            # User cancelled selection
            return
        # Convert to standard platform specific path, otherwise Qt likes forward slashes:
        globals_file = os.path.abspath(globals_file)
        if not os.path.isfile(globals_file):
            error_dialog("No such file %s." % globals_file)
            return
        # Save the containing folder for use next time we open the dialog box:
        self.last_opened_globals_folder = os.path.dirname(globals_file)
        # Open the file:
        self.open_globals_file(globals_file)

    def on_new_globals_file_clicked(self):
        globals_file = QtWidgets.QFileDialog.getSaveFileName(self.ui,
                                                         'Create new globals file',
                                                         self.last_opened_globals_folder,
                                                         "TOML files (*.toml)")
        if type(globals_file) is tuple:
            globals_file, _ = globals_file

        if not globals_file:
            # User cancelled
            return
        if not globals_file.lower().endswith('.toml'):
            globals_file += '.toml'
        # Convert to standard platform specific path, otherwise Qt likes
        # forward slashes:
        globals_file = os.path.abspath(globals_file)
        # Save the containing folder for use next time we open the dialog box:
        self.last_opened_globals_folder = os.path.dirname(globals_file)
        # Create the new file and open it:
        runmanager.new_globals_file(globals_file)
        self.open_globals_file(globals_file)

    def on_diff_globals_file_clicked(self):
        globals_file = QtWidgets.QFileDialog.getOpenFileName(self.ui,
                                                         'Select globals file to compare',
                                                         self.last_opened_globals_folder,
                                                         "Globals files (*.toml *.h5 *.hdf5)")
        if type(globals_file) is tuple:
            globals_file, _ = globals_file

        if not globals_file:
            # User cancelled
            return

        # Convert to standard platform specific path, otherwise Qt likes forward slashes:
        globals_file = os.path.abspath(globals_file)

        # Get runmanager's globals
        active_groups = self.get_active_groups()
        if active_groups is None:
            # Invalid group selection
            return

        # Get file's globals groups
        other_groups = runmanager.get_all_groups(globals_file)

        # Display the output tab so the user can see the output:
        self.ui.tabWidget.setCurrentWidget(self.ui.tab_output)
        self.output_box.output('Globals diff with:\n%s\n\n' % globals_file)

        # Do the globals diff
        globals_diff_table = runmanager.globals_diff_groups(active_groups, other_groups)
        self.output_box.output(globals_diff_table)
        self.output_box.output('Ready.\n\n')

    def on_treeView_groups_leftClicked(self, index):
        """Here we respond to user clicks on the treeview. We do the following:

        - If the user clicks on the <click to add group> dummy row, we go into
          edit mode on it so they can enter the name of the new group they
          want.
        - If the user clicks on the icon to open or close a globals file or a
          group, we call the appropriate open and close methods and update the
          open/close data role on the model.
        - If the user clicks delete on a globals group, we call a delete
          method, which deletes it after confirmation, and closes it if it was
          open.
          """
        if qapplication.keyboardModifiers() != QtCore.Qt.NoModifier:
            # Only handle mouseclicks with no keyboard modifiers.
            return
        item = self.groups_model.itemFromIndex(index)
        # The 'name' item in the same row:
        name_index = index.sibling(index.row(), self.GROUPS_COL_NAME)
        name_item = self.groups_model.itemFromIndex(name_index)
        # The parent item, None if there is no parent:
        parent_item = item.parent()
        # What kind of row did the user click on?
        # A globals file, a group, or a 'click to add group' row?
        if item.data(self.GROUPS_ROLE_IS_DUMMY_ROW):
            # They clicked on an 'add new group' row. Enter editing
            # mode on the name item so they can enter a name for
            # the new group:
            self.ui.treeView_groups.setCurrentIndex(name_index)
            self.ui.treeView_groups.edit(name_index)
        if item.column() == self.GROUPS_COL_ACTIVE:
            # They clicked on the active column. Toggle the checkbox. We do
            # this manually because setting the item checkable means the model
            # changes before we catch the mouse click. This is a pain because
            # we want the ensuing sorting (if the user is sorting by the
            # enabled column) to keep the the selection. If the user only
            # selected the column by clicking on it, then the sort happens
            # before they selected it, and the resort happens without a visual
            # indication of where the item went, because it never got
            # selected.
            state = item.checkState()
            if state in (QtCore.Qt.Unchecked, QtCore.Qt.PartiallyChecked):
                item.setCheckState(QtCore.Qt.Checked)
            elif state == QtCore.Qt.Checked:
                item.setCheckState(QtCore.Qt.Unchecked)
            else:
                raise AssertionError('Invalid Check state')
            # If this changed the sort order, ensure the item is still visible:
            scroll_view_to_row_if_current(self.ui.treeView_groups, item)
        elif parent_item is None:
            # They clicked on a globals file row.
            globals_file = name_item.text()
            # What column did they click on?
            if item.column() == self.GROUPS_COL_OPENCLOSE:
                # They clicked the close button. Close the file:
                self.close_globals_file(globals_file)
        else:
            # They clicked on a globals group row.
            globals_file = parent_item.text()
            group_name = name_item.text()
            # What column did they click on?
            if item.column() == self.GROUPS_COL_DELETE:
                # They clicked the delete button. Delete the group:
                self.delete_group(globals_file, group_name, confirm=True)
            elif item.column() == self.GROUPS_COL_OPENCLOSE:
                # They clicked the open/close button. Which is it, open or close?
                group_is_open = item.data(self.GROUPS_ROLE_GROUP_IS_OPEN)
                if group_is_open:
                    self.close_group(globals_file, group_name)
                else:
                    self.open_group(globals_file, group_name)

    def on_treeView_groups_doubleLeftClicked(self, index):
        item = self.groups_model.itemFromIndex(index)
        # The parent item, None if there is no parent:
        parent_item = item.parent()
        if item.data(self.GROUPS_ROLE_IS_DUMMY_ROW):
            return
        elif parent_item and item.column() == self.GROUPS_COL_NAME:
            # it's a group name item. What's the group and file name?
            globals_file = parent_item.text()
            group_name = item.text()
            if (globals_file, group_name) not in self.currently_open_groups:
                self.open_group(globals_file, group_name)
            # Focus the tab:
            group_tab = self.currently_open_groups[globals_file, group_name]
            for i in range(self.ui.tabWidget.count()):
                if self.ui.tabWidget.widget(i) is group_tab.ui:
                    self.ui.tabWidget.setCurrentIndex(i)
                    break

    def on_groups_model_item_changed(self, item):
        """This function is for responding to data changes in the model. The
        methods for responding to changes different columns do different
        things. Mostly they make other data changes for model consistency, but
        also group creation and renaming is handled in response to changes to
        the 'name' column. When we change things elsewhere, we prefer to only
        change one thing, and the rest of the changes are triggered here. So
        here we do the following:

        Be careful not to recurse unsafely into this method - changing
        something that itself triggers further changes is fine so long as they
        peter out and don't get stuck in a loop. If recursion needs to be
        stopped, one can disconnect the signal temporarily with the context
        manager self.groups_model_item_changed_disconnected. But use this
        sparingly, otherwise there's the risk that some required data updates
        will be forgotten about and won't happen.
        """
        if item.column() == self.GROUPS_COL_NAME:
            self.on_groups_model_name_changed(item)
        elif item.column() == self.GROUPS_COL_ACTIVE:
            self.on_groups_model_active_changed(item)
        elif item.column() == self.GROUPS_COL_OPENCLOSE:
            self.on_groups_model_openclose_changed(item)

    def on_groups_model_name_changed(self, item):
        """Handles group renaming and creation of new groups due to the user
        editing the <click to add group> item"""
        parent_item = item.parent()
        # File rows are supposed to be uneditable, but just to be sure we have
        # a group row:
        assert parent_item is not None
        if item.data(self.GROUPS_ROLE_IS_DUMMY_ROW):
            item_text = item.text()
            if item_text != self.GROUPS_DUMMY_ROW_TEXT:
                # The user has made a new globals group by editing the <click
                # to add group> item.
                globals_file = parent_item.text()
                group_name = item_text
                self.new_group(globals_file, group_name)
        else:
            # User has renamed a globals group.
            new_group_name = item.text()
            previous_group_name = item.data(self.GROUPS_ROLE_PREVIOUS_NAME)
            # Ensure it truly is a name change, and not something else about
            # the item changing:
            if new_group_name != previous_group_name:
                globals_file = parent_item.text()
                self.rename_group(globals_file, previous_group_name, new_group_name)

    def on_groups_model_active_changed(self, item):
        """Sets the sort data for the item in response to its check state
        changing. Also, if this is the first time this function has been
        called on the stack, that is, the change was initiated externally
        instead of via recursion from this function itself, then set the check
        state of other items for consistency. This entails checking/unchecking
        all group rows in response to the file row's check state changing, or
        changing the file row's check state to reflect the check state of the
        child group rows. That's why we need to keep track of the recursion
        depth - so that those changes we make don't in turn cause further
        changes. But we don't disconnect the on_changed signal altogether,
        because we still want to do the update of the sort data, and anything
        else that might be added in future."""
        self.on_groups_model_active_changed_recursion_depth += 1
        try:
            check_state = item.checkState()
            # Ensure sort data matches active state:
            item.setData(check_state, self.GROUPS_ROLE_SORT_DATA)
            if self.on_groups_model_active_changed_recursion_depth > 1:
                # Prevent all below code from running in response to data changes
                # initiated from within this method itself. The code above this
                # check still runs in response to all changes.
                return

            parent_item = item.parent()
            if parent_item is not None:
                # A 'group active' checkbox changed due to external action (not from this method itself).
                # Update the parent file checkbox to reflect the state of its children
                children = [parent_item.child(i, self.GROUPS_COL_ACTIVE) for i in range(parent_item.rowCount())]
                child_states = [child.checkState() for child in children
                                if not child.data(self.GROUPS_ROLE_IS_DUMMY_ROW)]
                parent_active_index = parent_item.index().sibling(parent_item.index().row(), self.GROUPS_COL_ACTIVE)
                parent_active_item = self.groups_model.itemFromIndex(parent_active_index)
                if all(state == QtCore.Qt.Checked for state in child_states):
                    parent_active_item.setCheckState(QtCore.Qt.Checked)
                elif all(state == QtCore.Qt.Unchecked for state in child_states):
                    parent_active_item.setCheckState(QtCore.Qt.Unchecked)
                else:
                    parent_active_item.setCheckState(QtCore.Qt.PartiallyChecked)
            else:
                # A 'file active' checkbox changed due to external action (not from this method itself).
                # Update the check state of all children to match.
                name_index = item.index().sibling(item.index().row(), self.GROUPS_COL_NAME)
                name_item = self.groups_model.itemFromIndex(name_index)
                checkstate = item.checkState()
                children = [name_item.child(i, self.GROUPS_COL_ACTIVE) for i in range(name_item.rowCount())]
                for child in children:
                    if not child.data(self.GROUPS_ROLE_IS_DUMMY_ROW):
                        child.setCheckState(checkstate)
        finally:
            self.on_groups_model_active_changed_recursion_depth -= 1
            if self.on_groups_model_active_changed_recursion_depth == 0:
                self.do_model_sort()
                # Trigger a preparse to occur:
                self.globals_changed()

    def on_groups_model_openclose_changed(self, item):
        """Sets item sort data and icon in response to the open/close state of a group
        changing."""
        parent_item = item.parent()
        # The open/close state of a globals group changed. It is definitely a
        # group, not a file, as the open/close state of a file shouldn't be
        # changing.
        assert parent_item is not None  # Just to be sure.
        # Ensure the sort data matches the open/close state:
        group_is_open = item.data(self.GROUPS_ROLE_GROUP_IS_OPEN)
        item.setData(group_is_open, self.GROUPS_ROLE_SORT_DATA)
        # Set the appropriate icon and tooltip. Changing the icon causes
        # itemChanged to be emitted, even if it the same icon, and even if we
        # were to use the same QIcon instance. So to avoid infinite recursion
        # we temporarily disconnect the signal whilst we set the icons.
        with self.groups_model_item_changed_disconnected:
            if group_is_open:
                item.setIcon(QtGui.QIcon(':qtutils/fugue/cross'))
                item.setToolTip('Close globals group.')
            else:
                item.setIcon(QtGui.QIcon(':qtutils/fugue/plus'))
                item.setToolTip('Load globals group into runmanager.')
            self.do_model_sort()
            # If this changed the sort order, ensure the item is still visible:
            scroll_view_to_row_if_current(self.ui.treeView_groups, item)

    @inmain_decorator()
    def get_default_output_folder(self):
        """Returns what the default output folder would be right now, based on
        the current date and selected labscript file. Returns empty string if
        no labscript file is selected. Does not create the default output
        folder, does not check if it exists."""
        current_labscript_file = self.ui.lineEdit_labscript_file.text()
        if not current_labscript_file:
            return ''
        _, default_output_folder, _ = runmanager.new_sequence_details(
            current_labscript_file,
            config=self.exp_config,
            increment_sequence_index=False,
        )
        default_output_folder = os.path.normpath(default_output_folder)
        # This is still a template: unresolved globals[...] lookups are shown as
        # written, and literal braces are escaped for the per-shot pass. Undo
        # that escaping for display.
        return unescape_braces(default_output_folder)

    def rollover_shot_output_folder(self):
        """Runs in a thread, checking every 30 seconds if the default output folder has
        changed, likely because the date has changed, but also possible because another
        instance of runmanager has incremented the sequence index. If the defaulr output
        folder has changed, and if runmanager is configured to use the default output
        folder, sets the folder in which compiled shots will be put. Does not create the
        folder if it does not already exist, this will be done at compile-time."""
        while True:
            time.sleep(30)
            try:
                self.check_output_folder_update()
            except Exception as e:
                # Don't stop the thread.
                logger.exception("error checking default output folder")

    @inmain_decorator()
    def check_output_folder_update(self):
        """Do a single check of whether the output folder needs updating. This
        is implemented as a separate function to the above loop so that the
        whole check happens at once in the Qt main thread and hence is atomic
        and can't be interfered with by other Qt calls in the program."""
        current_default_output_folder = self.get_default_output_folder()
        if current_default_output_folder is None:
            # No labscript file selected:
            return
        currently_selected_output_folder = self.ui.lineEdit_shot_output_folder.text()
        if current_default_output_folder != self.previous_default_output_folder:
            # It's a new day, or a new labscript file.
            # Is the user using default folders?
            if currently_selected_output_folder == self.previous_default_output_folder:
                # Yes they are. In that case, update to use the new folder:
                self.ui.lineEdit_shot_output_folder.setText(current_default_output_folder)
            self.previous_default_output_folder = current_default_output_folder

    @inmain_decorator()
    def globals_changed(self):
        """Called from either self, a GroupTab, or the RemoteServer to inform runmanager
        that something about globals has changed, and that they need parsing again."""
        self.ui.pushButton_engage.setEnabled(False)
        self.preparse_globals_required.put(None)

    def update_axes_indentation(self):
        for i in range(self.axes_model.rowCount()):
            item = self.axes_model.item(i, self.AXES_COL_NAME)
            text = item.text().lstrip()
            text = '    '*i + text
            item.setText(text)
            
    @inmain_decorator()  # Is called by preparser thread
    def update_axes_tab(self, expansions, dimensions):
        # get set of expansions
        expansion_list = []
        for global_name, expansion in expansions.items():
            if expansion:
                if expansion == 'outer':
                    expansion_list.append('outer '+global_name)
                else:
                    expansion_list.append('zip '+expansion)
                
        expansion_list = set(expansion_list)
                
        # find items to delete
        for i in reversed(range(self.axes_model.rowCount())):
            item = self.axes_model.item(i, self.AXES_COL_NAME)
            name = item.data(self.AXES_ROLE_NAME)
            if name not in expansion_list:
                item = self.axes_model.takeRow(i)
                del item
            else:
                length_item = self.axes_model.item(i, self.AXES_COL_LENGTH)
                if name in dimensions:
                    length_item.setText("{}".format(dimensions[name]))
                else:
                    length_item.setText('Unknown')
                
                # remove from expansions list so we don't add it again
                expansion_list.remove(name)
            
        # add new rows
        for expansion_name in expansion_list:
            shuffle = self.ui.pushButton_shuffle.checkState() != QtCore.Qt.Unchecked
            self.add_item_to_axes_model(expansion_name, shuffle, dimensions)
                
        self.update_axes_indentation() 

    def add_item_to_axes_model(self, expansion_name, shuffle, dimensions = None):
        if dimensions is None:
            dimensions = {}
        
        items = []
        
        expansion_type, name = expansion_name.split()
        name_item = QtGui.QStandardItem(name)
        name_item.setData(expansion_name, self.AXES_ROLE_NAME)
        if expansion_type == 'outer':
            name_item.setIcon(QtGui.QIcon(':qtutils/custom/outer'))
        else:
            name_item.setIcon(QtGui.QIcon(':qtutils/custom/zip'))
        items.append(name_item)
        
        length = 'Unknown'
        if expansion_name in dimensions:
            length = "{}".format(dimensions[expansion_name])
        length_item = QtGui.QStandardItem(length)
        items.append(length_item)
        
        shuffle_item = QtGui.QStandardItem()
        shuffle_item.setCheckable(True)
        shuffle_item.setCheckState(QtCore.Qt.Checked if shuffle else QtCore.Qt.Unchecked)
        
        items.append(shuffle_item)
        
        self.axes_model.appendRow(items)
    
    @inmain_decorator()  # Is called by preparser thread
    def update_tabs_parsing_indication(self, active_groups, sequence_globals, evaled_globals, n_shots):
        for group_tab in self.currently_open_groups.values():
            group_tab.update_parse_indication(active_groups, sequence_globals, evaled_globals)
        self.ui.pushButton_engage.setEnabled(True)
        if n_shots == 1:
            n_shots_string = '(1 shot)'
        else:
            n_shots_string = '({} shots)'.format(n_shots)
        self.ui.pushButton_engage.setText('Engage {}'.format(n_shots_string))

    def preparse_globals(self):
        active_groups = self.get_active_groups()
        if active_groups is None:
            # There was an error, get_active_groups has already shown
            # it to the user.
            return
        # Expansion mode is automatically updated when the global's
        # type changes. If this occurs, we will have to parse again to
        # include the change:
        while True:
            results = self.parse_globals(active_groups, raise_exceptions=False, expand_globals=False, return_dimensions = True)
            sequence_globals, shots, evaled_globals, global_hierarchy, expansions, dimensions = results
            self.n_shots = len(shots)
            expansions_changed = self.guess_expansion_modes(
                active_groups, evaled_globals, global_hierarchy, expansions)
            if not expansions_changed:
                # Now expand globals while parsing to calculate the number of shots.
                # this must only be done after the expansion type guessing has been updated to avoid exceptions
                # when changing a zip group from a list to a single value
                results = self.parse_globals(active_groups, raise_exceptions=False, expand_globals=True, return_dimensions = True)
                sequence_globals, shots, evaled_globals, global_hierarchy, expansions, dimensions = results
                self.n_shots = len(shots)
                break
        self.update_tabs_parsing_indication(active_groups, sequence_globals, evaled_globals, self.n_shots)
        self.update_axes_tab(expansions, dimensions)

    def preparse_globals_loop(self):
        """Runs in a thread, waiting on a threading.Event that tells us when
        some globals have changed, and calls parse_globals to evaluate them
        all before feeding the results back to the relevant tabs to be
        displayed."""
        while True:
            try:
                # Wait until we're needed:
                self.preparse_globals_required.get()
                n_requests = 1
                # Wait until the main thread is idle before clearing the queue of
                # requests. This way if preparsing is triggered multiple times within
                # the main thread before it becomes idle, we can respond to this all at
                # once, once they are all done, rather than starting too early and
                # having to preparse again.
                with qtlock:
                    while True:
                        try:
                            self.preparse_globals_required.get(block=False)
                            n_requests += 1
                        except queue.Empty:
                            break
                # Do some work:
                self.preparse_globals()
                # Tell any callers calling preparse_globals_required.join() that we are
                # done with their request:
                for _ in range(n_requests):
                    self.preparse_globals_required.task_done()
            except Exception:
                # Raise the error, but keep going so we don't take down the
                # whole thread if there is a bug.
                exc_info = sys.exc_info()
                raise_exception_in_thread(exc_info)

    def wait_until_preparse_complete(self):
        """Block until the preparse loop has finished pending work"""
        self.preparse_globals_required.join()

    def get_group_item_by_name(self, globals_file, group_name, column, previous_name=None):
        """Returns an item from the row representing a globals group in the
        groups model. Which item is returned is set by the column argument."""
        parent_item = self.groups_model.findItems(globals_file, column=self.GROUPS_COL_NAME)[0]
        possible_name_items = self.groups_model.findItems(group_name, QtCore.Qt.MatchRecursive,
                                                          column=self.GROUPS_COL_NAME)
        # Don't accidentally match on other groups or files with the same name
        # as this group:
        possible_name_items = [item for item in possible_name_items if item.parent() == parent_item]
        if previous_name is not None:
            # Also filter by previous name, useful for telling rows apart when
            # a rename is in progress and two rows may temporarily contain the
            # same name (though the rename code with throw an error and revert
            # it).
            possible_name_items = [item for item in possible_name_items
                                   if item.data(self.GROUPS_ROLE_PREVIOUS_NAME) == previous_name]
        elif group_name != self.GROUPS_DUMMY_ROW_TEXT:
            # Don't return the dummy item unless they asked for it explicitly
            # - if a new group is being created, its name might be
            # simultaneously present in its own row and the dummy row too.
            possible_name_items = [item for item in possible_name_items
                                   if not item.data(self.GROUPS_ROLE_IS_DUMMY_ROW)]

        if len(possible_name_items) > 1:
            raise LookupError('Multiple items found')
        elif not possible_name_items:
            raise LookupError('No item found')
        name_item = possible_name_items[0]
        name_index = name_item.index()
        # Found the name item, get the sibling item for the column requested:
        item_index = name_index.sibling(name_index.row(), column)
        item = self.groups_model.itemFromIndex(item_index)
        return item

    def do_model_sort(self):
        header = self.ui.treeView_groups.header()
        sort_column = header.sortIndicatorSection()
        sort_order = header.sortIndicatorOrder()
        self.ui.treeView_groups.sortByColumn(sort_column, sort_order)

    @inmain_decorator()  # Can be called from a non-main thread
    def get_active_groups(self, interactive=True):
        """Returns active groups in the format {group_name: globals_file}.
        Displays an error dialog and returns None if multiple groups of the
        same name are selected, this is invalid - selected groups must be
        uniquely named. If interactive=False, raises the exception instead."""
        active_groups = {}
        for i in range(self.groups_model.rowCount()):
            file_name_item = self.groups_model.item(i, self.GROUPS_COL_NAME)
            for j in range(file_name_item.rowCount()):
                group_name_item = file_name_item.child(j, self.GROUPS_COL_NAME)
                group_active_item = file_name_item.child(j, self.GROUPS_COL_ACTIVE)
                if group_active_item.checkState() == QtCore.Qt.Checked:
                    group_name = group_name_item.text()
                    globals_file = file_name_item.text()
                    if group_name in active_groups:
                        msg = (
                            'There are two active groups named %s. ' % group_name
                            + 'Active groups must have unique names.'
                        )
                        if interactive:
                            error_dialog(msg)
                            return
                        raise RuntimeError(msg)
                    active_groups[group_name] = globals_file
        return active_groups

    def replace_globals_file_path(self, old_path, new_path):
        if old_path == new_path:
            return new_path
        matching_items = self.groups_model.findItems(old_path, column=self.GROUPS_COL_NAME)
        if not matching_items:
            return new_path
        if self.groups_model.findItems(new_path, column=self.GROUPS_COL_NAME):
            raise RuntimeError("A globals file named %s is already open." % new_path)
        file_name_item = matching_items[0]
        with self.groups_model_item_changed_disconnected:
            file_name_item.setText(new_path)
            file_name_item.setToolTip(new_path)
            file_name_item.setData(new_path, self.GROUPS_ROLE_SORT_DATA)
        updated_groups = {}
        for (globals_file, group_name), group_tab in self.currently_open_groups.items():
            if globals_file == old_path:
                group_tab.set_file_and_group_name(new_path, group_name)
                updated_groups[new_path, group_name] = group_tab
            else:
                updated_groups[globals_file, group_name] = group_tab
        self.currently_open_groups = updated_groups
        self.do_model_sort()
        return new_path

    def ensure_editable_globals_file(self, globals_file, parent=None):
        if not runmanager.globals_file_requires_conversion(globals_file):
            return globals_file
        suggested_path = runmanager.default_toml_globals_file(globals_file)
        destination = QtWidgets.QFileDialog.getSaveFileName(
            parent or self.ui,
            'Convert globals file to TOML for editing',
            suggested_path,
            "TOML files (*.toml)",
        )
        if type(destination) is tuple:
            destination, _ = destination
        if not destination:
            raise RuntimeError('Conversion to TOML cancelled.')
        if not destination.lower().endswith('.toml'):
            destination += '.toml'
        destination = os.path.abspath(destination)
        if destination != globals_file and self.groups_model.findItems(destination, column=self.GROUPS_COL_NAME):
            raise RuntimeError("A globals file named %s is already open." % destination)
        runmanager.convert_globals_file(globals_file, destination)
        self.last_opened_globals_folder = os.path.dirname(destination)
        return self.replace_globals_file_path(globals_file, destination)

    def open_globals_file(self, globals_file):
        # Do nothing if this file is already open:
        if self.groups_model.findItems(globals_file, column=self.GROUPS_COL_NAME):
            return globals_file

        # Legacy HDF5 globals and shot files remain readable from the file
        # selector. Conversion to TOML is deferred until the user attempts an
        # edit operation.
        # Get the groups:
        groups = runmanager.get_grouplist(globals_file)
        # Add the parent row:
        file_name_item = QtGui.QStandardItem(globals_file)
        file_name_item.setEditable(False)
        file_name_item.setToolTip(globals_file)
        # Sort column by name:
        file_name_item.setData(globals_file, self.GROUPS_ROLE_SORT_DATA)

        file_active_item = QtGui.QStandardItem()
        file_active_item.setCheckState(QtCore.Qt.Unchecked)
        # Sort column by CheckState - must keep this updated when checkstate changes:
        file_active_item.setData(QtCore.Qt.Unchecked, self.GROUPS_ROLE_SORT_DATA)
        file_active_item.setEditable(False)
        file_active_item.setToolTip('Check to set all the file\'s groups as active.')

        file_delete_item = QtGui.QStandardItem()  # Blank, only groups have a delete button
        file_delete_item.setEditable(False)
        # Must be set to something so that the dummy row doesn't get sorted first:
        file_delete_item.setData(False, self.GROUPS_ROLE_SORT_DATA)

        file_close_item = QtGui.QStandardItem()
        file_close_item.setIcon(QtGui.QIcon(':qtutils/fugue/cross'))
        file_close_item.setEditable(False)
        file_close_item.setToolTip('Close globals file.')

        self.groups_model.appendRow([file_name_item, file_active_item, file_delete_item, file_close_item])

        # Add the groups as children:
        for group_name in groups:
            row = self.make_group_row(group_name)
            file_name_item.appendRow(row)

        # Finally, add the <Click to add group> row at the bottom:
        dummy_name_item = QtGui.QStandardItem(self.GROUPS_DUMMY_ROW_TEXT)
        dummy_name_item.setToolTip('Click to add group')
        # This lets later code know that this row does
        # not correspond to an actual globals group:
        dummy_name_item.setData(True, self.GROUPS_ROLE_IS_DUMMY_ROW)
        dummy_name_item.setData(self.GROUPS_DUMMY_ROW_TEXT, self.GROUPS_ROLE_PREVIOUS_NAME)
        dummy_name_item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsEditable)  # Clears the 'selectable' flag

        dummy_active_item = QtGui.QStandardItem()
        dummy_active_item.setData(True, self.GROUPS_ROLE_IS_DUMMY_ROW)
        dummy_active_item.setFlags(QtCore.Qt.NoItemFlags)

        dummy_delete_item = QtGui.QStandardItem()
        dummy_delete_item.setData(True, self.GROUPS_ROLE_IS_DUMMY_ROW)
        dummy_delete_item.setFlags(QtCore.Qt.NoItemFlags)

        dummy_open_close_item = QtGui.QStandardItem()
        dummy_open_close_item.setData(True, self.GROUPS_ROLE_IS_DUMMY_ROW)
        dummy_open_close_item.setFlags(QtCore.Qt.NoItemFlags)

        # Not setting anything as the above items' sort role has the effect of
        # ensuring this row is always sorted to the end of the list, without
        # us having to implement any custom sorting methods or subclassing
        # anything, yay.

        file_name_item.appendRow([dummy_name_item, dummy_active_item, dummy_delete_item, dummy_open_close_item])
        # Expand the child items to be visible:
        self.ui.treeView_groups.setExpanded(file_name_item.index(), True)
        self.globals_changed()
        self.do_model_sort()
        # If this changed the sort order, ensure the file item is visible:
        scroll_view_to_row_if_current(self.ui.treeView_groups, file_name_item)
        return globals_file

    def make_group_row(self, group_name):
        """Returns a new row representing one group in the groups tab, ready to be
        inserted into the model."""
        group_name_item = QtGui.QStandardItem(group_name)
        # We keep the previous name around so that we can detect what changed:
        group_name_item.setData(group_name, self.GROUPS_ROLE_PREVIOUS_NAME)
        # Sort column by name:
        group_name_item.setData(group_name, self.GROUPS_ROLE_SORT_DATA)

        group_active_item = QtGui.QStandardItem()
        group_active_item.setCheckState(QtCore.Qt.Unchecked)
        # Sort column by CheckState - must keep this updated whenever the
        # checkstate changes:
        group_active_item.setData(QtCore.Qt.Unchecked, self.GROUPS_ROLE_SORT_DATA)
        group_active_item.setEditable(False)
        group_active_item.setToolTip(
            'Whether or not the globals within this group should be used by runmanager for compilation.')

        group_delete_item = QtGui.QStandardItem()
        group_delete_item.setIcon(QtGui.QIcon(':qtutils/fugue/minus'))
        # Must be set to something so that the dummy row doesn't get sorted first:
        group_delete_item.setData(False, self.GROUPS_ROLE_SORT_DATA)
        group_delete_item.setEditable(False)
        group_delete_item.setToolTip('Delete globals group from file.')

        group_open_close_item = QtGui.QStandardItem()
        group_open_close_item.setIcon(QtGui.QIcon(':qtutils/fugue/plus'))
        group_open_close_item.setData(False, self.GROUPS_ROLE_GROUP_IS_OPEN)
        # Sort column by whether group is open - must keep this manually
        # updated when the state changes:
        group_open_close_item.setData(False, self.GROUPS_ROLE_SORT_DATA)
        group_open_close_item.setEditable(False)
        group_open_close_item.setToolTip('Load globals group into runmananger.')

        row = [group_name_item, group_active_item, group_delete_item, group_open_close_item]
        return row

    def close_globals_file(self, globals_file, confirm=True):
        item = self.groups_model.findItems(globals_file, column=self.GROUPS_COL_NAME)[0]
        # Close any open groups in this globals file:

        child_name_items = [item.child(i, self.GROUPS_COL_NAME) for i in range(item.rowCount())]
        child_openclose_items = [item.child(i, self.GROUPS_COL_OPENCLOSE) for i in range(item.rowCount())]
        child_is_open = [child_item.data(self.GROUPS_ROLE_GROUP_IS_OPEN)
                         for child_item in child_openclose_items]
        if confirm and any(child_is_open):
            if not question_dialog('Close %s? This will close %d currently open group(s).' %
                                   (globals_file, child_is_open.count(True))):
                return
        to_close = [name_item for name_item, is_open in zip(child_name_items, child_is_open) if is_open]
        for name_item in to_close:
            group_name = name_item.text()
            self.close_group(globals_file, group_name)

        # Remove the globals file from the model:
        self.groups_model.removeRow(item.row())
        self.globals_changed()

    def copy_group(self, source_globals_file, source_group_name, dest_globals_file=None, delete_source_group=False):
        """This function copys a group of globals with the name source_group_name from the file
            source_globals_file to a new file dest_globals_file. If delete_source_group is True
            the source group is deleted after copying"""
        if delete_source_group and source_globals_file == dest_globals_file:
            return
        try:
            if delete_source_group:
                source_globals_file = self.ensure_editable_globals_file(source_globals_file)
            if dest_globals_file is None:
                dest_globals_file = source_globals_file
            dest_globals_file = self.ensure_editable_globals_file(dest_globals_file)
            if source_globals_file == dest_globals_file and delete_source_group:
                source_globals_file = dest_globals_file
            dest_group_name = runmanager.copy_group(source_globals_file, source_group_name, dest_globals_file, delete_source_group)
        except Exception as e:
            error_dialog(str(e))
        else:
            # Insert the newly created globals group into the model, as a
            # child row of the new globals file.
            # find the new groups parent row by filepath
            for index in range(self.groups_model.rowCount()):
                if self.groups_model.item(index, self.GROUPS_COL_NAME).text() == dest_globals_file:
                    parent_row = self.groups_model.item(index)
                    break

            last_index = parent_row.rowCount()
            # Insert it as the row before the last (dummy) row:
            group_row = self.make_group_row(dest_group_name)
            parent_row.insertRow(last_index - 1, group_row)
            self.do_model_sort()

            # Open the group
            self.open_group(dest_globals_file, dest_group_name)
            name_item = group_row[self.GROUPS_COL_NAME]
            self.globals_changed()
            self.ui.treeView_groups.setCurrentIndex(name_item.index())

            # delete original
            if delete_source_group:
                self.delete_group(source_globals_file, source_group_name, confirm=False)

            # If this changed the sort order, ensure the group item is still visible:
            scroll_view_to_row_if_current(self.ui.treeView_groups, name_item)

    def new_group(self, globals_file, group_name):
        item = self.get_group_item_by_name(globals_file, group_name, self.GROUPS_COL_NAME,
                                           previous_name=self.GROUPS_DUMMY_ROW_TEXT)
        try:
            globals_file = self.ensure_editable_globals_file(globals_file)
            runmanager.new_group(globals_file, group_name)
        except Exception as e:
            error_dialog(str(e))
        else:
            # Insert the newly created globals group into the model, as a
            # child row of the globals file it belong to.
            group_row = self.make_group_row(group_name)
            last_index = item.parent().rowCount()
            # Insert it as the row before the last (dummy) row:
            item.parent().insertRow(last_index - 1, group_row)
            self.do_model_sort()
            # Open the group and mark it active:
            self.open_group(globals_file, group_name)
            active_item = group_row[self.GROUPS_COL_ACTIVE]
            name_item = group_row[self.GROUPS_COL_NAME]
            active_item.setCheckState(QtCore.Qt.Checked)
            self.globals_changed()
            self.ui.treeView_groups.setCurrentIndex(name_item.index())
            # If this changed the sort order, ensure the group item is still visible:
            scroll_view_to_row_if_current(self.ui.treeView_groups, name_item)
        finally:
            # Set the dummy row's text back ready for another group to be created:
            item.setText(self.GROUPS_DUMMY_ROW_TEXT)

    def open_group(self, globals_file, group_name, trigger_preparse=True):
        assert (globals_file, group_name) not in self.currently_open_groups  # sanity check
        group_tab = GroupTab(self.ui.tabWidget, globals_file, group_name)
        self.currently_open_groups[globals_file, group_name] = group_tab

        # Set the open/close state in the groups_model. itemChanged will be
        # emitted and self.on_groups_model_item_changed will handle updating
        # the other data roles, icons etc:
        openclose_item = self.get_group_item_by_name(globals_file, group_name, self.GROUPS_COL_OPENCLOSE)
        openclose_item.setData(True, self.GROUPS_ROLE_GROUP_IS_OPEN)
        # Trigger a preparse to occur in light of this. Calling code can
        # disable this so that multiple groups can be opened at once without
        # triggering a preparse. If they do so, they should call
        # self.globals_changed() themselves.
        if trigger_preparse:
            self.globals_changed()

    def rename_group(self, globals_file, previous_group_name, new_group_name):
        item = self.get_group_item_by_name(globals_file, new_group_name, self.GROUPS_COL_NAME,
                                           previous_name=previous_group_name)
        try:
            globals_file = self.ensure_editable_globals_file(globals_file)
            runmanager.rename_group(globals_file, previous_group_name, new_group_name)
        except Exception as e:
            error_dialog(str(e))
            # Set the item text back to the old name, since the rename failed:
            item.setText(previous_group_name)
        else:
            item.setData(new_group_name, self.GROUPS_ROLE_PREVIOUS_NAME)
            item.setData(new_group_name, self.GROUPS_ROLE_SORT_DATA)
            self.do_model_sort()
            # If this changed the sort order, ensure the group item is still visible:
            scroll_view_to_row_if_current(self.ui.treeView_groups, item)
            group_tab = self.currently_open_groups.pop((globals_file, previous_group_name), None)
            if group_tab is not None:
                # Change labels and tooltips appropriately if the group is open:
                group_tab.set_file_and_group_name(globals_file, new_group_name)
                # Re-add it to the dictionary under the new name:
                self.currently_open_groups[globals_file, new_group_name] = group_tab

    def close_group(self, globals_file, group_name):
        group_tab = self.currently_open_groups.pop((globals_file, group_name), None)
        assert group_tab is not None  # Just in case
        group_tab.close()
        openclose_item = self.get_group_item_by_name(globals_file, group_name, self.GROUPS_COL_OPENCLOSE)
        openclose_item.setData(False, self.GROUPS_ROLE_GROUP_IS_OPEN)

    def delete_group(self, globals_file, group_name, confirm=True):
        if confirm:
            if not question_dialog("Delete the group '%s'?" % group_name):
                return
        # If the group is open, close it:
        group_tab = self.currently_open_groups.get((globals_file, group_name))
        if group_tab is not None:
            self.close_group(globals_file, group_name)
        globals_file = self.ensure_editable_globals_file(globals_file)
        runmanager.delete_group(globals_file, group_name)
        # Find the entry for this group in self.groups_model and remove it:
        name_item = self.get_group_item_by_name(globals_file, group_name, self.GROUPS_COL_NAME)
        name_item.parent().removeRow(name_item.row())
        self.globals_changed()

    def on_save_configuration_triggered(self):
        if self.last_save_config_file is None:
            self.on_save_configuration_as_triggered()
            self.ui.actionSave_configuration_as.setEnabled(True)
            self.ui.actionRevert_configuration.setEnabled(True)
        else:
            self.save_configuration(self.last_save_config_file)

    def on_revert_configuration_triggered(self):
        save_data = self.get_save_data()
        if self.last_save_data is not None and save_data != self.last_save_data:
            message = 'Revert configuration to the last saved state in \'%s\'?' % self.last_save_config_file
            reply = QtWidgets.QMessageBox.question(self.ui, 'Load configuration', message,
                                               QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel)
            if reply == QtWidgets.QMessageBox.Cancel:
                return
            elif reply == QtWidgets.QMessageBox.Yes:
                self.load_configuration(self.last_save_config_file)
        else:
            error_dialog('no changes to revert')

    def on_save_configuration_as_triggered(self):
        if self.last_save_config_file is not None:
            default = self.last_save_config_file
        else:
            default = self.get_default_config_file(ensure_directory=True)
        save_file = QtWidgets.QFileDialog.getSaveFileName(self.ui,
                                                      'Select  file to save current runmanager configuration',
                                                      default,
                                                      "Config files (*.toml)")
        if type(save_file) is tuple:
            save_file, _ = save_file

        if not save_file:
            # User cancelled
            return
        # Convert to standard platform specific path, otherwise Qt likes
        # forward slashes:
        save_file = os.path.abspath(save_file)
        self.save_configuration(save_file)
        self.ui.actionSave_configuration.setText(
            'Save configuration {}'.format(self.last_save_config_file)
        )

    def get_save_data(self):
        # Get the currently open files and active groups:
        h5_files_open = []
        active_groups = []
        for i in range(self.groups_model.rowCount()):
            file_name_item = self.groups_model.item(i, self.GROUPS_COL_NAME)
            globals_file_name = file_name_item.text()
            h5_files_open.append(globals_file_name)
            for j in range(file_name_item.rowCount()):
                group_name_item = file_name_item.child(j, self.GROUPS_COL_NAME)
                group_name = group_name_item.text()
                group_active_item = file_name_item.child(j, self.GROUPS_COL_ACTIVE)
                if group_active_item.checkState() == QtCore.Qt.Checked:
                    active_groups.append((globals_file_name, group_name))
        # Get the currently open groups:
        groups_open = []
        for i in range(self.ui.tabWidget.count()):
            tab_page = self.ui.tabWidget.widget(i)
            for (globals_file_name, group_name), group_tab in self.currently_open_groups.items():
                if group_tab.ui is tab_page:
                    groups_open.append((globals_file_name, group_name))
                    break
        # Get the labscript file, output folder, and whether the output folder
        # is default:
        current_labscript_file = self.ui.lineEdit_labscript_file.text()
        shot_output_folder = self.ui.lineEdit_shot_output_folder.text()
        is_using_default_shot_output_folder = (shot_output_folder == self.get_default_output_folder())
        # Only save the shot output folder if not using the default, that way
        # the folder updating as the day rolls over will not be detected as a
        # change to the save data:
        if is_using_default_shot_output_folder:
            shot_output_folder = ''

        send_to_runviewer = self.ui.checkBox_view_shots.isChecked()
        send_to_blacs = self.ui.checkBox_run_shots.isChecked()
        shuffle = self.ui.pushButton_shuffle.isChecked()

        # axes tab information
        axes = []
        for i in range(self.axes_model.rowCount()):
            name_item = self.axes_model.item(i, self.AXES_COL_NAME)
            shuffle_item = self.axes_model.item(i, self.AXES_COL_SHUFFLE)
            shuffle_state = shuffle_item.checkState()
            
            axes.append((name_item.data(self.AXES_ROLE_NAME), 1 if shuffle_state == QtCore.Qt.Checked else 0))
        
        save_data = {'h5_files_open': h5_files_open,
                     'active_groups': active_groups,
                     'groups_open': groups_open,
                     'current_labscript_file': current_labscript_file,
                     'shot_output_folder': shot_output_folder,
                     'is_using_default_shot_output_folder': is_using_default_shot_output_folder,
                     'send_to_runviewer': send_to_runviewer,
                     'send_to_blacs': send_to_blacs,
                     'shuffle': shuffle,
                     'axes': axes,
                     'queue_state': self.queue_manager.export_state(),
                     'analysis_submission': self.analysis_submission.get_configuration_data()}
        return save_data

    def save_configuration(self, save_file):
        save_data = self.get_save_data()
        save_file = save_appconfig(save_file, {'runmanager_state': save_data})
        self.last_save_config_file = save_file
        self.last_save_data = save_data
        self.set_config_window_title(save_file)

    def on_load_configuration_triggered(self):
        save_data = self.get_save_data()
        if self.last_save_data is not None and save_data != self.last_save_data:
            message = ('Current configuration (which groups are active/open and other GUI state) '
                       'has changed: save config file \'%s\'?' % self.last_save_config_file)
            reply = QtWidgets.QMessageBox.question(self.ui, 'Load configuration', message,
                                               QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel)
            if reply == QtWidgets.QMessageBox.Cancel:
                return
            if reply == QtWidgets.QMessageBox.Yes:
                self.save_configuration(self.last_save_config_file)

        if self.last_save_config_file is not None:
            default = self.last_save_config_file
        else:
            default = self.get_default_config_file()

        file = QtWidgets.QFileDialog.getOpenFileName(self.ui,
                                                 'Select runmanager configuration file to load',
                                                 default,
                                                 "Config files (*.toml *.ini)")
        if type(file) is tuple:
            file, _ = file

        if not file:
            # User cancelled
            return
        # Convert to standard platform specific path, otherwise Qt likes
        # forward slashes:
        file = os.path.abspath(file)
        self.load_configuration(file)

    def load_configuration(self, filename):
        # Close all files:
        save_data = self.get_save_data()
        for globals_file in save_data['h5_files_open']:
            self.close_globals_file(globals_file, confirm=False)
        # Ensure folder exists, if this was opened programmatically we are
        # creating the file, so the directory had better exist!
        appconfig, save_target = load_appconfig(filename, return_save_path=True)
        self.last_save_config_file = save_target
        self.ui.actionSave_configuration.setText('Save configuration %s'%save_target)
        runmanager_config = appconfig.get('runmanager_state', {})

        has_been_a_warning = [False]
        def warning(message):
            if not has_been_a_warning[0]:
                has_been_a_warning[0] = True
                self.output_box.output('\n')
            self.output_box.output('Warning: %s\n' % message, red=True)

        opened_globals_files = {}
        for globals_file in runmanager_config.get('h5_files_open', []):
            if os.path.exists(globals_file):
                try:
                    opened_globals_file = self.open_globals_file(globals_file)
                    opened_globals_files[globals_file] = opened_globals_file
                    self.last_opened_globals_folder = os.path.dirname(opened_globals_file)
                except Exception:
                    raise_exception_in_thread(sys.exc_info())
                    continue
            else:
                self.output_box.output('\nWarning: globals file %s no longer exists\n' % globals_file, red=True)

        for globals_file, group_name in runmanager_config.get('active_groups', []):
            globals_file = opened_globals_files.get(globals_file, globals_file)
            try:
                group_active_item = self.get_group_item_by_name(globals_file, group_name, self.GROUPS_COL_ACTIVE)
                group_active_item.setCheckState(QtCore.Qt.Checked)
            except LookupError:
                warning("previously active group '%s' in %s no longer exists" % (group_name, globals_file))

        for globals_file, group_name in runmanager_config.get('groups_open', []):
            globals_file = opened_globals_files.get(globals_file, globals_file)
            # First check if it exists:
            try:
                self.get_group_item_by_name(globals_file, group_name, self.GROUPS_COL_NAME)
            except LookupError:
                warning("previously open group '%s' in %s no longer exists" % (group_name, globals_file))
            else:
                self.open_group(globals_file, group_name)

        current_labscript_file = runmanager_config.get('current_labscript_file')
        if current_labscript_file is not None:
            if os.path.exists(current_labscript_file):
                self.ui.lineEdit_labscript_file.setText(current_labscript_file)
                self.last_opened_labscript_folder = os.path.dirname(current_labscript_file)
            elif current_labscript_file:
                warning('previously selected labscript file %s no longer exists' % current_labscript_file)

        shot_output_folder = runmanager_config.get('shot_output_folder')
        if shot_output_folder is not None:
            self.ui.lineEdit_shot_output_folder.setText(shot_output_folder)
            self.last_selected_shot_output_folder = os.path.dirname(shot_output_folder)

        if runmanager_config.get('is_using_default_shot_output_folder', False):
            default_output_folder = self.get_default_output_folder()
            self.ui.lineEdit_shot_output_folder.setText(default_output_folder)
            self.last_selected_shot_output_folder = os.path.dirname(default_output_folder)

        send_to_runviewer = runmanager_config.get('send_to_runviewer')
        if send_to_runviewer is not None:
            self.ui.checkBox_view_shots.setChecked(send_to_runviewer)

        send_to_blacs = runmanager_config.get('send_to_blacs')
        if send_to_blacs is not None:
            self.ui.checkBox_run_shots.setChecked(send_to_blacs)
        
        # clear the axes model first
        if self.axes_model.rowCount():
            self.axes_model.removeRows(0, self.axes_model.rowCount())

        # set the state of the global shuffle button. This ensure that if no axes items get loaded afterwards
        # (e.g. because the globals in the saved config file are no longer expansion globals), then we still have 
        # an approximate state for the shuffle button that will apply to whatever globals are to be expanded.
        if runmanager_config.get('shuffle', False):
            self.ui.pushButton_shuffle.setChecked(True)

        # Now load the axes states (order and shuffle). This will also ensure the shuffle button matches the 
        # state of these items (since we don't save/restore the tri-state nature of the global shuffle button
        axes = runmanager_config.get('axes')
        if axes is not None and isinstance(axes, list):
            # clear model
            for name, shuffle in axes:
                self.add_item_to_axes_model(name, shuffle)
            self.update_axes_indentation() 

        queue_state = runmanager_config.get('queue_state')
        if isinstance(queue_state, dict):
            self.queue_manager.restore_state(queue_state)
            restored_queue_state = self.queue_manager.get_queue_state()
            self.ui.lineEdit_default_labscript_file.setText(
                restored_queue_state['default_labscript_file']
            )
            index = self.queue_compile_mode_combo.findData(
                restored_queue_state['compile_mode']
            )
            if index != -1:
                self.queue_compile_mode_combo.setCurrentIndex(index)
            index = self.queue_empty_policy_combo.findData(
                restored_queue_state['empty_queue_policy']
            )
            if index != -1:
                self.queue_empty_policy_combo.setCurrentIndex(index)

        self.analysis_submission.restore_configuration_data(
            runmanager_config.get('analysis_submission')
        )

        # Set as self.last_save_data:
        save_data = self.get_save_data()
        self.last_save_data = save_data
        self.ui.actionSave_configuration_as.setEnabled(True)
        self.ui.actionRevert_configuration.setEnabled(True)
        self.set_config_window_title(save_target)

    def compile_run_file(self, labscript_file, run_file):
        with self.compiler_lock:
            self.to_child.put(['compile', [labscript_file, run_file]])
            signal, success = self.from_child.get()
        assert signal == 'done'
        return success

    def parse_globals(self, active_groups, raise_exceptions=True, expand_globals=True, expansion_order = None, return_dimensions = False):
        sequence_globals = runmanager.get_globals(active_groups)
        #logger.info('got sequence globals')
        evaled_globals, global_hierarchy, expansions = runmanager.evaluate_globals(sequence_globals, raise_exceptions)
        #logger.info('evaluated sequence globals')
        if expand_globals:
            if return_dimensions:
                shots, dimensions = runmanager.expand_globals(sequence_globals, evaled_globals, expansion_order, return_dimensions=return_dimensions)
            else:
                shots = runmanager.expand_globals(sequence_globals, evaled_globals, expansion_order)
        else:
            shots = []
            dimensions = {}
        #logger.info('expanded sequence globals')
        if return_dimensions:
            return sequence_globals, shots, evaled_globals, global_hierarchy, expansions, dimensions
        else:
            return sequence_globals, shots, evaled_globals, global_hierarchy, expansions

    def guess_expansion_modes(self, active_groups, evaled_globals, global_hierarchy, expansions):
        """This function is designed to be called iteratively. It changes the
        expansion type of globals that reference other globals - such that
        globals referencing an iterable global will be zipped with it, rather
        than outer producted. Each time this method is called,
        self.parse_globals should also be called, so that the globals are
        evaluated with their new expansion modes, if they changed. This should
        be performed repeatedly until there are no more changes. Note that
        this method does not return what expansion types it thinks globals
        should have - it *actually writes them to the globals HDF5 file*. So
        it is up to later code to ensure it re-reads the expansion mode from
        the HDF5 file before proceeding. At present this method is only called
        from self.preparse_globals(), so see there to see how it fits in with
        everything else. This method uses four instance attributes to store
        state: self.previous_evaled_globals, self.previous_global_hierarchy,
        self.previous_expansion_types and self.previous_expansions. This is
        neccesary so that it can detect changes."""

        # Do nothing if there were exceptions:
        for group_name in evaled_globals:
            for global_name in evaled_globals[group_name]:
                value = evaled_globals[group_name][global_name]
                if isinstance(value, Exception):
                    # Let ExpansionErrors through through, as they occur
                    # when the user has changed the value without changing
                    # the expansion type:
                    if isinstance(value, runmanager.ExpansionError):
                        continue
                    return False
        # Did the guessed expansion type for any of the globals change?
        expansion_types_changed = False
        expansion_types = {}
        for group_name in evaled_globals:
            for global_name in evaled_globals[group_name]:
                new_value = evaled_globals[group_name][global_name]
                try:
                    previous_value = self.previous_evaled_globals[group_name][global_name]
                except KeyError:
                    # This variable is used to guess the expansion type
                    # 
                    # If we already have an expansion specified for this, but
                    # don't have a previous value, then we should use the 
                    # new_value for the guess as we are likely loading from HDF5
                    # file for the first time (and either way, don't want to 
                    # overwrite what the user has put in the expansion type)
                    #
                    # If we don't have an expansion...
                    # then we set it to '0' which will result in an
                    # expansion type guess of '' (emptys string) This will
                    # either result in nothing being done to the expansion
                    # type or the expansion type being found to be 'outer',
                    # which will then make it go through the machinery below
                    if global_name in expansions and expansions[global_name]:
                        previous_value = new_value
                    else:
                        previous_value = 0

                new_guess = runmanager.guess_expansion_type(new_value)
                previous_guess = runmanager.guess_expansion_type(previous_value)

                if new_guess == 'outer':
                    expansion_types[global_name] = {'previous_guess': previous_guess,
                                                    'new_guess': new_guess,
                                                    'group_name': group_name,
                                                    'value': new_value
                                                    }
                elif new_guess != previous_guess:
                    filename = active_groups[group_name]
                    runmanager.set_expansion(filename, group_name, global_name, new_guess)
                    expansions[global_name] = new_guess
                    expansion_types_changed = True

        # recursively find dependencies and add them to a zip group!
        def find_dependencies(global_name, global_hierarchy, expansion_types):
            results = set()
            for name, dependencies in global_hierarchy.items():
                if name in expansion_types and global_name in dependencies:
                    results.add(name)
                    results = results.union(find_dependencies(name, global_hierarchy, expansion_types))
            return results

        def global_depends_on_global_with_outer_product(global_name, global_hierarchy, expansions):
            if global_name not in global_hierarchy:
                return False
            else:
                for dependency in global_hierarchy[global_name]:
                    if expansions[dependency]:
                        return True

        def set_expansion_type_guess(expansion_types, expansions, global_name, expansion_to_set, new=True):
            if new:
                key = 'new_guess'
            else:
                key = 'previous_guess'
                
            # debug logging
            log_if_global(global_name, [], 'setting expansion type for new dependency' if new else 'setting expansion type for old dependencies')
            
            
            # only do this if the expansion is *not* already set to a specific zip group
            if global_name in expansions and expansions[global_name] != '' and expansions[global_name] != 'outer':
                expansion_types[global_name][key] = expansions[global_name]
                
                # debug logging
                log_if_global(global_name, [], 'Using existing expansion %s for %s'%(expansions[global_name], global_name))
            else:
                expansion_types[global_name][key] = expansion_to_set
                expansions[global_name] = expansion_to_set
                
                # debug logging
                log_if_global(global_name, [], 'Using existing expansion %s for %s'%(expansion_to_set, global_name))
            
        
        for global_name in sorted(expansion_types):
            # we have a global that does not depend on anything that has an
            # expansion type of 'outer'
            if (not global_depends_on_global_with_outer_product(global_name, global_hierarchy, expansions)
                    and not isinstance(expansion_types[global_name]['value'], runmanager.ExpansionError)):
                current_dependencies = find_dependencies(global_name, global_hierarchy, expansion_types)

                # if this global has other globals that use it, then add them
                # all to a zip group with the name of this global
                if current_dependencies:
                    for dependency in current_dependencies:                        
                        set_expansion_type_guess(expansion_types, expansions, dependency,  str(global_name))
                            
                    set_expansion_type_guess(expansion_types, expansions, global_name,  str(global_name))

        for global_name in sorted(self.previous_expansion_types):
            if (not global_depends_on_global_with_outer_product(
                global_name, self.previous_global_hierarchy, self.previous_expansions)
                    and not isinstance(self.previous_expansion_types[global_name]['value'], runmanager.ExpansionError)):
                old_dependencies = find_dependencies(global_name, self.previous_global_hierarchy, self.previous_expansion_types)
                # if this global has other globals that use it, then add them
                # all to a zip group with the name of this global
                if old_dependencies:
                    for dependency in old_dependencies:
                        if dependency in expansion_types:
                            set_expansion_type_guess(expansion_types, self.previous_expansions, dependency, str(global_name), new=False)
                    if global_name in expansion_types:
                        set_expansion_type_guess(expansion_types, self.previous_expansions, global_name, str(global_name), new=False)

        for global_name, guesses in expansion_types.items():
            if guesses['new_guess'] != guesses['previous_guess']:
                filename = active_groups[guesses['group_name']]
                runmanager.set_expansion(
                    filename, str(guesses['group_name']), str(global_name), str(guesses['new_guess']))
                expansions[global_name] = guesses['new_guess']
                expansion_types_changed = True

        # Now check everything that has an expansion type not equal to outer.
        # If it has one, but is not iteratble, remove it from teh zip group
        for group_name in evaled_globals:
            for global_name in evaled_globals[group_name]:
                if expansions[global_name] and expansions[global_name] != 'outer':
                    try:
                        iter(evaled_globals[group_name][global_name])
                    except Exception:
                        filename = active_groups[group_name]
                        runmanager.set_expansion(filename, group_name, global_name, '')
                        expansion_types_changed = True

        self.previous_evaled_globals = evaled_globals
        self.previous_global_hierarchy = global_hierarchy
        self.previous_expansion_types = expansion_types
        self.previous_expansions = expansions

        return expansion_types_changed

    def make_h5_files(
        self,
        labscript_file,
        output_folder,
        sequence_globals,
        shots,
        shuffle,
        with_metadata=False,
        indexed_path_base=None,
        index_start=None,
    ):
        sequence_attrs, default_output_dir, filename_prefix = runmanager.new_sequence_details(
            labscript_file, config=self.exp_config, increment_sequence_index=True
        )
        if output_folder == self.previous_default_output_folder:
            # The user is using dthe efault output folder. Just in case the sequence
            # index has been updated or the date has changed, use the default_output dir
            # obtained from new_sequence_details, as it is race-free, whereas the one
            # from the UI may be out of date since we only update it once a second.
            output_folder = default_output_dir
        self.check_output_folder_update()
        run_output_folder = (
            os.path.dirname(os.path.abspath(indexed_path_base))
            if indexed_path_base is not None
            else output_folder
        )
        if indexed_path_base is not None:
            run_files = list(
                runmanager.make_run_files(
                    run_output_folder,
                    sequence_globals,
                    shots,
                    sequence_attrs,
                    filename_prefix,
                    shuffle,
                    return_infos=True,
                    create_files=False,
                )
            )
            run_files = self.reindex_run_file_infos(
                run_files,
                run_output_folder,
                filename_prefix,
                indexed_path_base=indexed_path_base,
                index_start=index_start,
            )
            if not with_metadata:
                for run_file_info in run_files:
                    runmanager.make_single_run_file(
                        run_file_info['path'],
                        sequence_globals,
                        run_file_info['shot_globals'],
                        run_file_info['sequence_attrs'],
                        run_file_info['run_no'],
                        run_file_info['n_runs'],
                    )
                run_files = [run_file_info['path'] for run_file_info in run_files]
        else:
            run_files = runmanager.make_run_files(
                run_output_folder,
                sequence_globals,
                shots,
                sequence_attrs,
                filename_prefix,
                shuffle,
                return_infos=with_metadata,
                create_files=not with_metadata,
            )
        logger.debug(run_files)
        return labscript_file, run_files

    def prepare_queue_shot(self, item):
        active_groups = item.get('active_groups') or inmain(
            self.get_active_groups, interactive=False
        )
        if active_groups is None:
            raise RuntimeError('No active globals groups available for queued compilation.')
        sequence_globals, runglobals = runmanager.get_queue_compile_globals(
            active_groups, item['frozen_globals']
        )
        runmanager.make_single_run_file(
            item['path'],
            sequence_globals,
            runglobals,
            item['sequence_attrs'],
            item['run_no'],
            item['n_runs'],
        )

    def send_to_BLACS(self, run_file, BLACS_hostname):
        port = int(self.exp_config.get('ports', 'BLACS'))
        agnostic_path = shared_drive.path_to_agnostic(run_file)
        self.output_box.output('Submitting run file %s.\n' % os.path.basename(run_file))
        try:
            response = zmq_get(port, BLACS_hostname, data=agnostic_path)
            if 'added successfully' in response:
                self.output_box.output(response)
            else:
                raise Exception(response)
        except Exception as e:
            self.output_box.output('Couldn\'t submit job to control server: %s\n' % str(e), red=True)
            self.compilation_aborted.set()

    def send_to_runviewer(self, run_file):
        runviewer_port = int(self.exp_config.get('ports', 'runviewer'))
        agnostic_path = shared_drive.path_to_agnostic(run_file)
        try:
            response = zmq_get(runviewer_port, 'localhost', data='hello', timeout=1)
            if 'hello' not in response:
                raise Exception(response)
        except Exception as e:
            logger.info('runviewer not running, attempting to start...')
            # Runviewer not running, start it:
            if os.name == 'nt':
                creationflags = 0x00000008  # DETACHED_PROCESS from the win32 API
                scripts_dir = desktop_app.environment.get_scripts_dir('runviewer')
                subprocess.Popen([str(scripts_dir / 'runviewer-gui')],
                                 creationflags=creationflags, stdout=None, stderr=None,
                                 close_fds=True)
            else:
                devnull = open(os.devnull, 'w')
                if not os.fork():
                    os.setsid()
                    subprocess.Popen([sys.executable, '-m', 'runviewer'],
                                     stdin=devnull, stdout=devnull, stderr=devnull, close_fds=True)
                    os._exit(0)
            try:
                zmq_get(runviewer_port, 'localhost', data='hello', timeout=15)
            except Exception as e:
                self.output_box.output('Couldn\'t submit shot to runviewer: %s\n\n' % str(e), red=True)

        try:
            response = zmq_get(runviewer_port, 'localhost', data=agnostic_path, timeout=0.5)
            if 'ok' not in response:
                raise Exception(response)
            else:
                self.output_box.output('Shot %s sent to runviewer.\n' % os.path.basename(run_file))
        except Exception as e:
            self.output_box.output('Couldn\'t submit shot to runviewer: %s\n\n' % str(e), red=True)

    def queue_request_next(self):
        item = self.queue_manager.pop_next()
        if item is None:
            queue_state = self.queue_manager.get_queue_state()
            if queue_state['empty_queue_policy'] != EMPTY_QUEUE_DEFAULT_LABSCRIPT:
                self.queue_manager.set_last_sent_from_queue(None)
                return None
            labscript_file = queue_state['default_labscript_file']
            if not labscript_file:
                self.queue_manager.set_last_sent_from_queue(None)
                return None
            if not os.path.isfile(labscript_file):
                raise RuntimeError(
                    'Default-shot labscript file does not exist: %s' % labscript_file
                )
            active_groups = inmain(self.get_active_groups, interactive=False)
            if active_groups is None:
                self.queue_manager.set_last_sent_from_queue(None)
                return None
            sequence_globals, runglobals = runmanager.get_default_shot_globals(
                active_groups
            )
            send_to_runviewer = inmain(self.ui.checkBox_view_shots.isChecked)
            sequence_attrs, output_folder, filename_prefix = (
                runmanager.new_sequence_details(
                    labscript_file,
                    config=self.exp_config,
                    increment_sequence_index=True,
                    default=True,
                    format_globals=runglobals,
                )
            )
            run_file_base = os.path.join(
                output_folder,
                '{}.h5'.format(filename_prefix),
            )
            start = self._next_default_shot_index.get(run_file_base, 0)
            run_file, default_index = next_available_indexed_filepath(
                run_file_base,
                '_{index}',
                start=start,
            )
            self._next_default_shot_index[run_file_base] = default_index + 1
            runmanager.make_single_run_file(
                run_file,
                sequence_globals,
                runglobals,
                sequence_attrs,
                0,
                1,
            )
            success = self.queue_manager.compile_shot(
                {'path': run_file, 'labscript_file': labscript_file},
                send_to_runviewer=send_to_runviewer,
            )
            if not success:
                raise RuntimeError(
                    'Compilation failed for %s' % os.path.basename(run_file)
                )
        else:
            run_file = item['path']
            if not item['compiled']:
                send_to_runviewer = inmain(self.ui.checkBox_view_shots.isChecked)
                success = self.queue_manager.compile_shot(
                    item, send_to_runviewer=send_to_runviewer
                )
                if not success:
                    self.queue_manager.set_last_sent_from_queue(None)
                    return None
        agnostic_path = shared_drive.path_to_agnostic(run_file)
        self.queue_manager.set_last_sent_from_queue(agnostic_path)
        return agnostic_path


class RemoteServer(ZMQServer):
    def __init__(self):
        port = app.exp_config.getint(
            'ports', 'runmanager', fallback=runmanager.remote.DEFAULT_PORT
        )
        ZMQServer.__init__(self, port=port)

    def _get_active_global_locations(self):
        active_groups = inmain(app.get_active_groups, interactive=False)
        sequence_globals = runmanager.get_globals(active_groups)
        locations = {}
        for group_name, group_globals in sequence_globals.items():
            globals_file = active_groups[group_name]
            for global_name in group_globals:
                if global_name in locations:
                    other_file, other_group = locations[global_name]
                    msg = """Global %s is defined in multiple active groups: %s and %s"""
                    raise RuntimeError(msg % (global_name, other_group, group_name))
                locations[global_name] = (globals_file, group_name)
        return active_groups, sequence_globals, locations

    def _get_global_field_values(self, getter):
        _, _, locations = self._get_active_global_locations()
        values = {}
        for global_name, (globals_file, group_name) in locations.items():
            values[global_name] = getter(globals_file, group_name, global_name)
        return values

    def _get_expression_field_values(self, getter, raw=False):
        active_groups, _, locations = self._get_active_global_locations()
        sequence_globals = {group_name: {} for group_name in active_groups}
        for global_name, (globals_file, group_name) in locations.items():
            expression = getter(globals_file, group_name, global_name)
            sequence_globals[group_name][global_name] = (expression, '', '')
        if raw:
            values = {}
            for group_globals in sequence_globals.values():
                values.update(
                    {name: expression for name, (expression, _, _) in group_globals.items()}
                )
            return values
        evaled_globals, _, _ = runmanager.evaluate_globals(
            sequence_globals, raise_exceptions=False
        )
        values = {}
        for group_globals in evaled_globals.values():
            values.update(group_globals)
        return values

    @staticmethod
    def _coerce_remote_boolean(value, name):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise TypeError('%s must be a bool, not %s' % (name, value.__class__.__name__))

    @inmain_decorator()
    def _set_expression_field_values(self, getter, setter, changer_name, globals, raw=False):
        _, _, locations = self._get_active_global_locations()
        try:
            for global_name, new_value in globals.items():
                if not raw:
                    new_value = repr(new_value)
                elif not isinstance(new_value, (str, bytes)):
                    msg = "global %s must be a string if raw=True, not %s"
                    raise TypeError(msg % (global_name, new_value.__class__.__name__))
                try:
                    globals_file, group_name = locations[global_name]
                except KeyError:
                    raise ValueError(
                        "Global %s not found in any active group" % global_name
                    )
                previous_value = getter(globals_file, group_name, global_name)
                comments = runmanager.find_comments(previous_value)
                if comments:
                    comment_start, comment_end = comments[-1]
                    if comment_end == len(previous_value):
                        new_value += previous_value[comment_start:comment_end]
                try:
                    group_tab = app.currently_open_groups[globals_file, group_name]
                except KeyError:
                    globals_file = app.ensure_editable_globals_file(globals_file)
                    setter(globals_file, group_name, global_name, new_value)
                else:
                    getattr(group_tab, changer_name)(
                        global_name, previous_value, new_value, interactive=False
                    )
        finally:
            app.globals_changed()

    @inmain_decorator()
    def _set_boolean_field_values(self, getter, setter, changer_name, globals):
        _, _, locations = self._get_active_global_locations()
        try:
            for global_name, new_value in globals.items():
                new_value = self._coerce_remote_boolean(
                    new_value, 'global %s' % global_name
                )
                try:
                    globals_file, group_name = locations[global_name]
                except KeyError:
                    raise ValueError(
                        "Global %s not found in any active group" % global_name
                    )
                previous_value = getter(globals_file, group_name, global_name)
                try:
                    group_tab = app.currently_open_groups[globals_file, group_name]
                except KeyError:
                    globals_file = app.ensure_editable_globals_file(globals_file)
                    setter(globals_file, group_name, global_name, new_value)
                else:
                    getattr(group_tab, changer_name)(
                        global_name, previous_value, new_value, interactive=False
                    )
        finally:
            app.globals_changed()

    def handle_get_default_globals(self, raw=False):
        return self._get_expression_field_values(runmanager.get_value, raw=raw)

    def handle_get_globals(self, raw=False):
        active_groups = inmain(app.get_active_groups, interactive=False)
        sequence_globals = runmanager.get_globals(active_groups)
        if raw:
            values = {}
            for group_globals in sequence_globals.values():
                values.update(
                    {name: expression for name, (expression, _, _) in group_globals.items()}
                )
            return values
        evaled_globals, _, _ = runmanager.evaluate_globals(
            sequence_globals, raise_exceptions=False
        )
        values = {}
        for group_globals in evaled_globals.values():
            values.update(group_globals)
        return values

    def handle_get_scan_globals(self, raw=False):
        return self._get_expression_field_values(runmanager.get_scan, raw=raw)

    def handle_get_scan_enabled(self):
        return self._get_global_field_values(runmanager.get_scan_enabled)

    def handle_get_jit_enabled(self):
        return self._get_global_field_values(runmanager.get_jit_enabled)

    def handle_set_default_globals(self, globals, raw=False):
        return self._set_expression_field_values(
            runmanager.get_value,
            runmanager.set_value,
            'change_global_default',
            globals,
            raw=raw,
        )

    def handle_set_globals(self, globals, raw=False):
        return self.handle_set_default_globals(globals, raw=raw)

    def handle_set_scan_globals(self, globals, raw=False):
        return self._set_expression_field_values(
            runmanager.get_scan,
            runmanager.set_scan,
            'change_global_scan',
            globals,
            raw=raw,
        )

    def handle_set_scan_enabled(self, globals):
        return self._set_boolean_field_values(
            runmanager.get_scan_enabled,
            runmanager.set_scan_enabled,
            'change_global_scan_enabled',
            globals,
        )

    def handle_set_jit_enabled(self, globals):
        return self._set_boolean_field_values(
            runmanager.get_jit_enabled,
            runmanager.set_jit_enabled,
            'change_global_jit_enabled',
            globals,
        )

    def handle_engage(self):
        app.wait_until_preparse_complete()
        inmain(app.on_engage_clicked)

    @inmain_decorator()
    def handle_abort(self):
        app.on_abort_clicked()

    @inmain_decorator()
    def handle_get_run_shots(self):
        return app.ui.checkBox_run_shots.isChecked()

    @inmain_decorator()
    def handle_set_run_shots(self, value):
        app.ui.checkBox_run_shots.setChecked(
            self._coerce_remote_boolean(value, 'value')
        )

    @inmain_decorator()
    def handle_get_view_shots(self):
        return app.ui.checkBox_view_shots.isChecked()

    @inmain_decorator()
    def handle_set_view_shots(self, value):
        app.ui.checkBox_view_shots.setChecked(
            self._coerce_remote_boolean(value, 'value')
        )

    @inmain_decorator()
    def handle_get_shuffle(self):
        return app.ui.pushButton_shuffle.isChecked()

    @inmain_decorator()
    def handle_set_shuffle(self, value):
        app.ui.pushButton_shuffle.setChecked(
            self._coerce_remote_boolean(value, 'value')
        )

    def handle_n_shots(self):
        # Wait until any current preparsing is done, to ensure this is not racy w.r.t
        # previous remote calls:
        app.wait_until_preparse_complete()
        return app.n_shots

    @inmain_decorator()
    def handle_get_labscript_file(self):
        labscript_file = app.ui.lineEdit_labscript_file.text()
        return os.path.abspath(labscript_file)

    @inmain_decorator()
    def handle_set_labscript_file(self, value):
        labscript_file = os.path.abspath(value)
        app.ui.lineEdit_labscript_file.setText(labscript_file)

    @inmain_decorator()
    def handle_get_shot_output_folder(self):
        shot_output_folder = app.ui.lineEdit_shot_output_folder.text()
        return os.path.abspath(shot_output_folder)

    @inmain_decorator()
    def handle_set_shot_output_folder(self, value):
        shot_output_folder = os.path.abspath(value)
        app.ui.lineEdit_shot_output_folder.setText(shot_output_folder)

    def handle_error_in_globals(self):
        try:
            # This will raise an exception if there are multiple active groups of the
            # same name:
            active_groups = inmain(app.get_active_groups, interactive=False)
            sequence_globals = runmanager.get_globals(active_groups)
            # This will raise an exception if any of the globals can't be evaluated:
            runmanager.evaluate_globals(sequence_globals, raise_exceptions=True)
        except Exception:
            return True
        return False

    def handle_is_output_folder_default(self):
        return not app.non_default_folder

    @inmain_decorator()
    def handle_reset_shot_output_folder(self):
        app.on_reset_shot_output_folder_clicked(None)

    def handle_queue_request_next(self):
        return app.queue_request_next()

    def handle_notify_shot_complete(self, filepath):
        return app.analysis_submission.notify_shot_complete(filepath)

    def handler(self, request_data):
        cmd, args, kwargs = request_data
        if cmd == 'hello':
            return 'hello'
        elif cmd == '__version__':
            return runmanager.__version__
        try:
            return getattr(self, 'handle_' + cmd)(*args, **kwargs)
        except Exception as e:
            msg = traceback.format_exc()
            msg = "Runmanager server returned an exception:\n" + msg
            return e.__class__(msg)


if __name__ == "__main__":
    logger = setup_logging(APPLICATION_NAME)
    labscript_utils.excepthook.set_logger(logger)
    logger.info('\n\n===============starting===============\n')
    qapplication = QtWidgets.QApplication.instance()
    if qapplication is None:
        qapplication = QtWidgets.QApplication(sys.argv)
    qapplication.setAttribute(QtCore.Qt.AA_DontShowIconsInMenus, False)
    app = RunManager()
    splash.update_text('Starting remote server')
    remote_server = RemoteServer()
    splash.hide()

    # Let the interpreter run every 500ms so it sees Ctrl-C interrupts:
    timer = QtCore.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)
    # Upon seeing a ctrl-c interrupt, quit the event loop
    signal.signal(signal.SIGINT, lambda *args: qapplication.exit())

    qapplication.exec_()
    remote_server.shutdown()

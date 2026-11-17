"""Small widget factories: buttons, menus and layout helpers.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFrame, QMenu, QPushButton, QToolButton

from gui_qt.icons import icon
from gui_qt.selector_button import SplitPressHighlighter
from gui_qt.theme.theme_qt import contrast_text


class MainWindow:

    # --------------------------------------------------------------- widgets
    def _action_button(self, text: str, icon_name: str,
                       compact: bool = False,
                       tint: str | None = None) -> QToolButton:
        """Flat toolbar-style button with icon + label (mockup look).
        *compact* uses the smaller footer icon size. *tint* recolours a mono
        glyph to a theme colour (like the Settings icon)."""
        px = self._FOOT_ICON_PX if compact else self._ICON_PX
        b = QToolButton()
        b.setText(text)
        b.setIcon(icon(icon_name, px, color=tint))
        b.setIconSize(QSize(px, px))
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setObjectName("FooterButton" if compact else "ActionButton")
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _populate_menu(self, menu: "QMenu", items: "list[tuple]") -> None:
        """Fill *menu* from *items*. Each entry is None (separator) or
        (label, callback) — where callback may be a list of (label, callback)
        pairs, which becomes a submenu."""
        for entry in items:
            if entry is None:
                menu.addSeparator()
                continue
            label, cb = entry
            if isinstance(cb, list):
                sub = menu.addMenu(label)
                self._populate_menu(sub, cb)
            elif cb is not None:
                act = menu.addAction(label)
                act.triggered.connect(lambda _=False, c=cb: c())
            else:
                menu.addAction(label)   # inert label (placeholder)

    def _menu_action_button(self, text: str, icon_name: str,
                            items: "list[tuple]",
                            tint: str | None = None) -> QToolButton:
        """Like _action_button but a split button with a dropdown menu.
        *items* is a list of (label, callback|None); None inserts a separator.
        If the second element is a list of (label, callback) pairs it becomes a
        submenu instead. Highlights (button + arrow) while the menu is open via
        the `menuOpen` property, mirroring SelectorButton."""
        b = self._action_button(text, icon_name, tint=tint)
        b.setProperty("split", True)
        b.setPopupMode(QToolButton.MenuButtonPopup)
        menu = QMenu(b)
        self._populate_menu(menu, items)
        b.setMenu(menu)
        # Open on press (not release), and set menuOpen BEFORE the synchronous
        # sunken repaint (SplitPressHighlighter) so body + arrow light together
        # instead of the arrow lagging one menu-build behind — mirrors
        # SelectorButton. Matters most for Wizard, whose menu rebuild on
        # aboutToShow probes the filesystem.
        b.pressed.connect(b.showMenu)
        b.installEventFilter(SplitPressHighlighter(b))

        def _set_open(on):
            b.setProperty("menuOpen", on)
            b.style().unpolish(b); b.style().polish(b)
        menu.aboutToShow.connect(lambda: _set_open(True))
        menu.aboutToHide.connect(lambda: _set_open(False))
        b._menu = menu
        return b

    def _text_button(self, text: str, compact: bool = False) -> QToolButton:
        """Flat text-only button (same chrome as the action buttons)."""
        b = QToolButton()
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextOnly)
        b.setObjectName("FooterButton" if compact else "ActionButton")
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _color_button(self, text: str, color: str,
                      compact: bool = False) -> QPushButton:
        """Solid colored button (plugin tools, matching the Tk app)."""
        pad = "4px 10px" if compact else "6px 14px"
        fs = "12px" if compact else "14px"
        fg = contrast_text(color)   # black or white — whichever reads on the fill
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:{color}; color:{fg}; border:none;"
            f" padding:{pad}; border-radius:4px; font-size:{fs};"
            f" font-weight:600;}}"
            f"QPushButton:hover{{background:{color};}}")
        return b

    def _equalize_button_widths(self, *buttons) -> None:
        """Give every button the width of the widest one so a row of related
        buttons (e.g. Pack/Unpack BSA) lines up regardless of label length."""
        w = max((b.sizeHint().width() for b in buttons), default=0)
        for b in buttons:
            b.setMinimumWidth(w)

    def _icon_button(self, icon_name: str, tip: str = "") -> QToolButton:
        b = QToolButton()
        b.setIcon(icon(icon_name, self._ICON_PX))
        b.setIconSize(QSize(self._ICON_PX, self._ICON_PX))
        b.setAutoRaise(True)
        b.setCursor(Qt.PointingHandCursor)
        if tip:
            b.setToolTip(tip)
        return b

    def _group_sep(self) -> QFrame:
        s = QFrame()
        s.setFrameShape(QFrame.VLine)
        s.setObjectName("GroupSep")
        s.setFixedWidth(2)
        return s

    def _line_visible(self, severity: str) -> bool:
        """Whether a line of this severity passes the current Error/Warning
        filter. With no filter active every line shows; when a filter is active
        only lines of that severity show."""
        active = getattr(self, "_log_filter", None)
        if active is None:
            return True
        return severity == active

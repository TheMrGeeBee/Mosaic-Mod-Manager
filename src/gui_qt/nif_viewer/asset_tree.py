"""Left-hand tree of the NIF viewer: base game first, then each mod in load
order; inside each, folders then files, names ascending. Children of a root are
built lazily when it is first expanded (the base game alone is ~60k files).
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QStyledItemDelegate, QStyle

from Utils.nif.asset_catalog import BASE, AssetCatalog, AssetEntry

EntryRole = Qt.UserRole + 1
SourceRole = Qt.UserRole + 2
WarnRole = Qt.UserRole + 3          # True for a mesh in the wrong NIF format
_WARN = QColor(214, 158, 62)


class _Node:
    __slots__ = ("name", "kind", "parent", "children", "entry", "root_key", "loaded")

    def __init__(self, name, kind, parent=None, entry=None, root_key=None):
        self.name = name
        self.kind = kind                 # "root" | "dir" | "file"
        self.parent = parent
        self.children: list[_Node] = []
        self.entry: "AssetEntry | None" = entry
        self.root_key = root_key         # BASE ("") or mod name, roots only
        self.loaded = kind != "root"

    def row(self) -> int:
        return self.parent.children.index(self) if self.parent else 0


def _sort(node: _Node):
    node.children.sort(key=lambda n: (n.kind != "dir", n.name.lower()))
    for c in node.children:
        if c.kind == "dir":
            _sort(c)


def build_hierarchy(root: _Node, entries: list[AssetEntry]) -> None:
    """Fill *root* with folder/file nodes for *entries* (sorted, folders first)."""
    folders: dict[str, _Node] = {}
    for e in entries:
        parts = e.path.split("/")
        parent, so_far = root, ""
        for seg in parts[:-1]:
            so_far = f"{so_far}/{seg}" if so_far else seg
            node = folders.get(so_far)
            if node is None:
                node = _Node(seg, "dir", parent)
                parent.children.append(node)
                folders[so_far] = node
            parent = node
        parent.children.append(_Node(parts[-1], "file", parent, entry=e))
    _sort(root)
    root.loaded = True


class AssetTreeModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._catalog: "AssetCatalog | None" = None
        self._root = _Node("", "root")
        self._entries: dict[str, list[AssetEntry]] = {}
        self._filter = ""
        self._only_overridden = False
        self._hide_incompatible = True
        self._match_count = 0

    # -- data ---------------------------------------------------------------------
    def set_catalog(self, catalog: "AssetCatalog | None"):
        self._catalog = catalog
        self._entries.clear()
        self._filter = ""
        self._rebuild()

    def set_filter(self, text: str) -> int:
        """Show only roots/files matching *text* (case-insensitive substring of a
        mod name or file path). Returns the number of matching files, or -1 when
        no filter is active."""
        text = text.strip().lower()
        if text != self._filter:
            self._filter = text
            self._rebuild()
        return self._match_count if self.filtering() else -1

    def set_only_overridden(self, on: bool) -> int:
        """Restrict the tree to files another layer (base game or mod) also
        provides. Combines with the text filter; returns as set_filter does."""
        if on != self._only_overridden:
            self._only_overridden = on
            self._rebuild()
        return self._match_count if self.filtering() else -1

    def set_hide_incompatible(self, on: bool):
        """Hide meshes whose NIF format isn't the game's (once known — see
        AssetCatalog.scan_formats). Not a user "filter": filtering() ignores it."""
        if on != self._hide_incompatible:
            self._hide_incompatible = on
            self._rebuild()

    def refresh_incompatible(self):
        """Call after scan_formats() finished: hides newly found files, or (when
        they are shown) just repaints their warning marks."""
        if self._hide_incompatible and self._catalog and self._catalog.incompatible_count():
            self._rebuild()
        else:
            self.layoutChanged.emit()

    def filtering(self) -> bool:
        return bool(self._filter) or self._only_overridden

    def match_count(self) -> int:
        return self._match_count

    def _root_entries(self, key: str) -> list[AssetEntry]:
        if key not in self._entries:
            cat = self._catalog
            self._entries[key] = (cat.base_entries() if key == BASE
                                  else cat.mod_entries(key)) if cat else []
        return self._entries[key]

    def _entries_for(self, key: str) -> list[AssetEntry]:
        """A root's entries minus hidden-incompatible meshes."""
        entries = self._root_entries(key)
        cat = self._catalog
        if key != BASE and self._hide_incompatible and cat and cat.incompatible_count():
            return [e for e in entries if cat.incompatible_label(e) is None]
        return entries

    def _rebuild(self):
        self.beginResetModel()
        self._root = _Node("", "root")
        self._match_count = 0
        cat = self._catalog
        if cat is not None:
            keys = [BASE] + cat.mods()
            contested = cat.contested_keys() if self._only_overridden else frozenset()
            for key in keys:
                name = cat.base_name if key == BASE else key
                node = _Node(name, "root", self._root, root_key=key)
                if self.filtering():
                    name_match = bool(self._filter) and self._filter in name.lower()

                    def keep(e, name_match=name_match):
                        return ((name_match or not self._filter or self._filter in e.path)
                                and (not self._only_overridden or e.path in contested))

                    hits = [e for e in self._entries_for(key) if keep(e)]
                    if not hits:
                        continue
                    self._match_count += len(hits)
                    # A mod matched by name alone keeps its lazy children.
                    if not (name_match and not self._only_overridden):
                        build_hierarchy(node, hits)
                elif (key != BASE and self._hide_incompatible
                        and cat.incompatible_count() and not self._entries_for(key)):
                    continue                     # everything this mod ships is hidden
                self._root.children.append(node)
        self.endResetModel()

    def node(self, index: QModelIndex) -> _Node:
        return index.internalPointer() if index.isValid() else self._root

    # -- QAbstractItemModel ---------------------------------------------------------
    def index(self, row, col, parent=QModelIndex()):
        if not self.hasIndex(row, col, parent):
            return QModelIndex()
        return self.createIndex(row, col, self.node(parent).children[row])

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        p = index.internalPointer().parent
        return QModelIndex() if p is None or p is self._root else self.createIndex(p.row(), 0, p)

    def rowCount(self, parent=QModelIndex()):
        return len(self.node(parent).children)

    def columnCount(self, parent=QModelIndex()):
        return 1

    def hasChildren(self, parent=QModelIndex()):
        n = self.node(parent)
        return True if (n.kind == "root" and not n.loaded) else bool(n.children)

    def canFetchMore(self, parent):
        n = self.node(parent)
        return n.kind == "root" and not n.loaded and n is not self._root

    def fetchMore(self, parent):
        n = self.node(parent)
        entries = self._entries_for(n.root_key)
        tmp = _Node("", "root")
        build_hierarchy(tmp, entries)
        if tmp.children:
            self.beginInsertRows(parent, 0, len(tmp.children) - 1)
            n.children = tmp.children
            for c in n.children:
                c.parent = n
            n.loaded = True
            self.endInsertRows()
        else:
            n.loaded = True

    def flags(self, index):
        return (Qt.ItemIsEnabled | Qt.ItemIsSelectable) if index.isValid() else Qt.NoItemFlags

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        n: _Node = index.internalPointer()
        if role == Qt.DisplayRole:
            return n.name
        if role == EntryRole:
            return n.entry
        bad = (self._catalog.incompatible_label(n.entry)
               if n.entry is not None and self._catalog is not None else None)
        if role == WarnRole:
            return bad is not None
        if role == SourceRole and n.entry is not None:
            return f"{bad} · {n.entry.source_label}" if bad and n.entry.source_label else (bad or n.entry.source_label)
        if role == Qt.ForegroundRole and n.entry is not None:
            if bad:
                return _WARN
            if not n.entry.is_winner:
                return QColor(130, 130, 130)
        if role == Qt.FontRole and n.kind == "root":
            f = QFont()
            f.setBold(True)
            return f
        if role == Qt.ToolTipRole and n.entry is not None:
            e = n.entry
            src = e.archive if e.kind == "bsa" else "loose file"
            owner = self._catalog.base_name if e.mod == BASE else e.mod
            tip = f"{e.path}\n{owner} — {src}"
            if bad:
                tip += (f"\n⚠ {bad} format — not this game's mesh format "
                        "(unconverted meshes may crash the game)")
            if not e.is_winner and self._catalog is not None:
                w = self._catalog.resolve(e.path)
                if w is not None:
                    by = self._catalog.base_name if w.mod == BASE else w.mod
                    tip += f"\nOverridden by {by}" + (f" ({w.archive})" if w.archive else "")
            return tip
        return None


class AssetTreeDelegate(QStyledItemDelegate):
    """Name on the left, the source archive (e.g. ``3DNPC.bsa``) dimmed on the right."""

    def paint(self, painter, option, index):
        tag = index.data(SourceRole)
        if not tag:
            super().paint(painter, option, index)
            return
        painter.save()
        fm = option.fontMetrics
        tag_w = min(fm.horizontalAdvance(tag) + 10, option.rect.width() // 2)
        name_opt = type(option)(option)
        name_opt.rect.setRight(option.rect.right() - tag_w)
        super().paint(painter, name_opt, index)
        selected = bool(option.state & QStyle.State_Selected)
        warn = bool(index.data(WarnRole))
        painter.setPen(option.palette.highlightedText().color() if selected
                       else (_WARN if warn else QColor(120, 140, 170)))
        painter.drawText(option.rect.adjusted(0, 0, -4, 0), Qt.AlignRight | Qt.AlignVCenter,
                         fm.elidedText(tag, Qt.ElideMiddle, tag_w - 6))
        painter.restore()

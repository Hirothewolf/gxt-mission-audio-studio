"""Grouped, identity-preserving navigation for Qt GXT categories."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem


_CATEGORY_ROLE = int(Qt.ItemDataRole.UserRole)
_KIND_ROLE = _CATEGORY_ROLE + 1
_GROUPS = (
    ('missions', 'Missões'),
    ('menus', 'Menus'),
    ('tips', 'Dicas/tutoriais'),
    ('vehicles', 'Veículos'),
    ('weapons', 'Armas'),
    ('activities', 'Minijogos/Eventos'),
    ('other', 'Outros textos'),
)
_GROUP_BY_KIND = {
    'menu': 'menus', 'menus': 'menus',
    'tip': 'tips', 'tips': 'tips', 'tutorial': 'tips', 'tutorials': 'tips',
    'vehicle': 'vehicles', 'vehicles': 'vehicles',
    'weapon': 'weapons', 'weapons': 'weapons',
    'minigame': 'activities', 'minigames': 'activities', 'race': 'activities',
    'races': 'activities', 'event': 'activities', 'events': 'activities',
    'common': 'other', 'other': 'other', 'other_text': 'other',
}
_GROUP_BY_ID = {
    'help': 'menus',
    'vehicles': 'vehicles',
    'weapons': 'weapons',
    'races': 'activities',
    'minigames': 'activities',
    'common': 'other',
    'other': 'other',
}


def _normal(value):
    import unicodedata
    return ''.join(ch for ch in unicodedata.normalize('NFKD', str(value or '').casefold())
                   if not unicodedata.combining(ch)).strip()


def _group_id(category_id, category):
    """Prefer catalog section/kind; use stable catalog IDs for legacy entries."""
    section = _normal(category.get('section'))
    if section in ('missoes', 'mission', 'missions'):
        return 'missions'
    kind = _normal(category.get('kind'))
    if kind in _GROUP_BY_KIND:
        return _GROUP_BY_KIND[kind]
    if str(category_id) == 'all':
        return None
    return _GROUP_BY_ID.get(str(category_id), 'other')


class GroupedNavigation(QTreeWidget):
    """Collapsible catalog groups with category IDs and pair membership intact.

    ``categorySelected`` emits the catalog category ID for a leaf and ``None``
    for a group header. For a group, call ``selected_member_ids()`` to obtain
    the ordered, deduplicated union of its member ``(table, hash)`` pairs.
    """

    categorySelected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(self.SelectionMode.SingleSelection)
        self._categories = {}
        self._category_items = {}
        self._group_items = {}
        self._all_item = None
        self._filter_text = ''
        self._expanded_before_filter = None
        self.currentItemChanged.connect(self._selection_changed)

    def set_categories(self, categories):
        """Replace navigation data, preserving a selected catalog ID if possible."""
        old_category_id = self.selected_category_id()
        current = self.currentItem()
        old_group_id = (current.data(0, _CATEGORY_ROLE)
                        if current and current.data(0, _KIND_ROLE) == 'group' else None)
        self._categories = dict(categories or {})
        self._category_items.clear()
        self._group_items.clear()
        self._all_item = None
        self._filter_text = ''
        self._expanded_before_filter = None

        blocked = self.blockSignals(True)
        self.clear()
        for group_id, title in _GROUPS:
            group_item = QTreeWidgetItem([title])
            group_item.setData(0, _CATEGORY_ROLE, 'group:' + group_id)
            group_item.setData(0, _KIND_ROLE, 'group')
            group_item.setToolTip(0, title)
            self.addTopLevelItem(group_item)
            self._group_items[group_id] = group_item

        all_category = self._categories.get('all')
        if all_category is not None:
            self._all_item = self._make_category_item('all', all_category)
            self.insertTopLevelItem(0, self._all_item)

        for category_id, category in sorted(
                self._categories.items(),
                key=lambda pair: (str(pair[1].get('label', pair[0])).casefold(), str(pair[0]))):
            if category_id == 'all':
                continue
            group_id = _group_id(category_id, category)
            parent = self._group_items.get(group_id, self._group_items['other'])
            item = self._make_category_item(category_id, category)
            parent.addChild(item)
            self._category_items[category_id] = item

        # Keep large mission lists folded so the other groups remain visible.
        for group_item in self._group_items.values():
            group_item.setExpanded(group_item.childCount() <= 12)

        # Legacy catalogs combine menus and help. Keep that membership intact
        # instead of adding an empty, non-functional tutorials category.
        if ('help' in self._categories and
                not any(_group_id(cid, value) == 'tips' for cid, value in self._categories.items())):
            self._group_items['menus'].setText(0, 'Menus e dicas')
            self._group_items['menus'].setToolTip(0, 'Menus, ajuda e tutoriais reunidos pelo catálogo atual.')

        self.blockSignals(blocked)
        restore = self._category_items.get(old_category_id) if old_category_id is not None else None
        if restore is None and old_group_id:
            group_key = old_group_id.partition(':')[2]
            restore = self._group_items.get(group_key)
        if restore is not None:
            self.setCurrentItem(restore)
            if restore.parent():
                restore.parent().setExpanded(True)
        self._apply_filter()

    def _make_category_item(self, category_id, category):
        label = str(category.get('label') or category_id)
        tables = category.get('tables') or []
        if tables:
            label += ' · ' + ', '.join(str(table) for table in tables)
        item = QTreeWidgetItem([label])
        item.setData(0, _CATEGORY_ROLE, category_id)
        item.setData(0, _KIND_ROLE, 'category')
        item.setToolTip(0, str(category.get('evidence') or label))
        return item

    def selected_category_id(self):
        item = self.currentItem()
        if item is None or item.data(0, _KIND_ROLE) != 'category':
            return None
        return item.data(0, _CATEGORY_ROLE)

    def select_category(self, category_id):
        """Select a category by its unchanged catalog ID; return whether found."""
        item = self._category_items.get(category_id)
        if category_id == 'all':
            item = self._all_item
        if item is None:
            return False
        if item.parent():
            item.parent().setExpanded(True)
        self.setCurrentItem(item)
        return True

    def selected_member_ids(self):
        """Return selected category members or a group's ordered member union."""
        item = self.currentItem()
        if item is None:
            return []
        kind = item.data(0, _KIND_ROLE)
        if kind == 'category':
            category_ids = [item.data(0, _CATEGORY_ROLE)]
        elif kind == 'group':
            category_ids = [item.child(i).data(0, _CATEGORY_ROLE)
                            for i in range(item.childCount())
                            if item.child(i).data(0, _KIND_ROLE) == 'category']
        else:
            return []
        result, seen = [], set()
        for category_id in category_ids:
            category = self._categories.get(category_id, {})
            for pair in category.get('members', []):
                identity = (str(pair[0]), int(pair[1]))
                if identity not in seen:
                    seen.add(identity)
                    result.append(identity)
        return result

    def set_filter(self, text):
        """Filter category leaves while retaining matching parents and selection."""
        value = str(text or '').strip().casefold()
        if value and not self._filter_text:
            self._expanded_before_filter = {
                group_id: item.isExpanded() for group_id, item in self._group_items.items()
            }
        self._filter_text = value
        self._apply_filter()

    def _apply_filter(self):
        query = self._filter_text
        if self._all_item is not None:
            category = self._categories['all']
            searchable = ' '.join((str(self._all_item.text(0)), 'all',
                                   ' '.join(category.get('tables') or []))).casefold()
            self._all_item.setHidden(bool(query and query not in searchable))
        for group_id, group in self._group_items.items():
            group_match = bool(query and query in group.text(0).casefold())
            visible_children = 0
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                if child.data(0, _KIND_ROLE) == 'notice':
                    visible = not query or group_match or query in child.text(0).casefold()
                else:
                    category_id = child.data(0, _CATEGORY_ROLE)
                    category = self._categories.get(category_id, {})
                    searchable = ' '.join((child.text(0), str(category_id),
                                           ' '.join(category.get('tables') or []))).casefold()
                    visible = not query or group_match or query in searchable
                child.setHidden(not visible)
                visible_children += int(visible)
            group.setHidden(group.childCount() == 0 or bool(query and not group_match and visible_children == 0))
            if query and visible_children:
                group.setExpanded(True)
        if not query and self._expanded_before_filter is not None:
            for group_id, expanded in self._expanded_before_filter.items():
                if group_id in self._group_items:
                    self._group_items[group_id].setExpanded(expanded)
            self._expanded_before_filter = None

    def _selection_changed(self, current, _previous):
        if current is None:
            self.categorySelected.emit(None)
        elif current.data(0, _KIND_ROLE) == 'category':
            self.categorySelected.emit(current.data(0, _CATEGORY_ROLE))
        else:
            self.categorySelected.emit(None)

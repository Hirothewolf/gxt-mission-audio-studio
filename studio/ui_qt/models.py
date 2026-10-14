"""Scalable Qt table and filter models for GXT document rows."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from .classic_filters import normalized_text


class GxtTableModel(QAbstractTableModel):
    """A flat table model whose row identity is always (table, hash)."""

    NameColumn = 0
    HashColumn = 1
    TextColumn = 2
    ComparisonColumn = 3
    TableColumn = 4
    AudioColumn = 5
    ReviewColumn = 6
    InstalledColumn = 7
    MarksColumn = 8
    StateColumn = 9
    ColumnCount = 10

    IdentityRole = int(Qt.ItemDataRole.UserRole) + 1
    RowRole = int(Qt.ItemDataRole.UserRole) + 2
    FlagsRole = int(Qt.ItemDataRole.UserRole) + 3

    _HEADERS = ('Chave', 'Hash', 'Texto atual', 'Comparação', 'Tabela', 'Original F5', 'Dublado F6',
                'Mod F8', '★ / ✓ / Nota', 'Estado')

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._identities = {}
        self._search_values = []
        self._sort_values = []
        self._search_values_without_tags = []
        self.text_editor = None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == self.TextColumn and callable(self.text_editor):
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if (role != Qt.ItemDataRole.EditRole or not index.isValid() or
                index.column() != self.TextColumn or not callable(self.text_editor)):
            return False
        row = self.row_at(index.row())
        return bool(row and self.text_editor(row['table'], row['hash'], str(value)))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.ColumnCount

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == self.IdentityRole:
            return row['table'], row['hash']
        if role == self.RowRole:
            return row
        if role == self.FlagsRole:
            return row.get('flags', set())
        if role == Qt.ItemDataRole.EditRole and index.column() == self.TextColumn:
            return row.get('text', '')
        if role == Qt.ItemDataRole.ToolTipRole:
            detail = row.get('audio_tooltip') if index.column() in (self.AudioColumn, self.ReviewColumn, self.InstalledColumn) else None
            if detail:
                return '%s · 0x%08X\n%s' % (row['table'], row['hash'], detail)
            return '%s · 0x%08X' % (row['table'], row['hash'])
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        column = index.column()
        if column == self.NameColumn:
            return row.get('name', '')
        if column == self.HashColumn:
            return '0x%08X' % row['hash']
        if column == self.TextColumn:
            return row.get('text', '')
        if column == self.ComparisonColumn:
            return row.get('comparison') or ''
        if column == self.TableColumn:
            return row['table']
        if column == self.AudioColumn:
            return row.get('original_label') or row.get('audio_label', '')
        if column == self.ReviewColumn:
            return row.get('review_label', '')
        flags = row.get('flags', set())
        if column == self.InstalledColumn:
            if row.get('installed_label'):
                return row['installed_label']
            return 'Conferido' if 'inst' in flags else ('Diferente do take' if 'not_installed' in flags else 'Consultar' if 'audio' in flags else '')
        if column == self.MarksColumn:
            return ' '.join(label for name, label in (('fav', '★'), ('rev', '✓'), ('note', 'Nota')) if name in flags)
        if column == self.StateColumn:
            return 'Não salvo' if 'edited' in flags else '≠ dif.' if 'different' in flags else ''
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if 0 <= section < self.ColumnCount:
                return self._HEADERS[section]
        return None

    def set_rows(self, rows):
        """Replace all document rows; callers pass the document's row dictionaries."""
        normalized = []
        for row in rows:
            normalized.append({
                'table': str(row['table']),
                'hash': int(row['hash']),
                'name': str(row.get('name') or ''),
                'text': str(row.get('text') or ''),
                'comparison': row.get('comparison'),
                'flags': set(row.get('flags') or ()),
                'audio_label': str(row.get('audio_label') or ''),
                'review_label': str(row.get('review_label') or ''),
                'note': str(row.get('note') or ''),
                'original_label': str(row.get('original_label') or ''),
                'installed_label': str(row.get('installed_label') or ''),
                'audio_tooltip': str(row.get('audio_tooltip') or ''),
            })
        self.beginResetModel()
        self._rows = normalized
        self._identities = {
            (row['table'], row['hash']): index
            for index, row in enumerate(self._rows)
        }
        self._search_values = [self._search_value(row) for row in self._rows]
        self._search_values_without_tags = [self._search_value(row, True) for row in self._rows]
        self._sort_values = [self._sort_row(row) for row in self._rows]
        self.endResetModel()

    def row_at(self, row):
        if not 0 <= row < len(self._rows):
            return None
        return self._rows[row]

    def update_row(self, row):
        index = self.row_for_identity(row['table'], row['hash'])
        if index < 0:
            return
        current = self._rows[index]
        current.update({key: row[key] for key in ('text', 'comparison', 'flags', 'audio_label', 'review_label', 'note',
                       'original_label', 'installed_label', 'audio_tooltip') if key in row})
        self._sort_values[index] = self._sort_row(current)
        self._search_values[index] = self._search_value(current)
        self._search_values_without_tags[index] = self._search_value(current, True)
        self.dataChanged.emit(self.index(index, 0), self.index(index, self.ColumnCount - 1))

    def identity_at(self, row):
        item = self.row_at(row)
        return (item['table'], item['hash']) if item else None

    def row_for_identity(self, table, key_hash):
        return self._identities.get((table, int(key_hash)), -1)

    @staticmethod
    def _search_value(row, ignore_tags=False):
        text = normalized_text(row['text'], ignore_tags)
        comparison = normalized_text(row.get('comparison') or '', ignore_tags)
        return '\n'.join((row['table'], row['name'], '%08X' % row['hash'], text,
                          comparison, row.get('audio_label') or '', row.get('note') or '')).casefold()

    def search_value(self, row, ignore_tags=False):
        if not 0 <= row < len(self._search_values):
            return ''
        return (self._search_values_without_tags if ignore_tags else self._search_values)[row]

    @staticmethod
    def _sort_row(row):
        flags = row.get('flags', set())
        return (row.get('name', '').casefold(), (row['table'].casefold(), row['hash']),
            row.get('text', '').casefold(), (row.get('comparison') or '').casefold(), row['table'].casefold(),
            row.get('audio_label', '').casefold(), row.get('review_label', '').casefold(),
            row.get('installed_label') or ('conferido' if 'inst' in flags else 'consultar'), ' '.join(sorted(flags & {'fav', 'rev', 'note'})),
            'não salvo' if 'edited' in flags else 'dif' if 'different' in flags else '')


class GxtSortFilterProxyModel(QSortFilterProxyModel):
    """Case-insensitive substring filter with stable numeric hash sorting."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter_text = ''
        self._ignore_tags = False
        self.setDynamicSortFilter(True)

    def set_filter_text(self, text):
        text = str(text or '').casefold().strip()
        if text == self._filter_text:
            return
        self.beginFilterChange()
        self._filter_text = text
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filter_text(self):
        return self._filter_text

    def set_ignore_tags(self, enabled):
        enabled = bool(enabled)
        if enabled == self._ignore_tags:
            return
        self.beginFilterChange()
        self._ignore_tags = enabled
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def ignore_tags(self):
        return self._ignore_tags

    def filterAcceptsRow(self, source_row, source_parent):
        if not self._filter_text:
            return True
        source = self.sourceModel()
        if not isinstance(source, GxtTableModel):
            return super().filterAcceptsRow(source_row, source_parent)
        return self._filter_text in source.search_value(source_row, self._ignore_tags)

    def lessThan(self, left, right):
        source = self.sourceModel()
        if isinstance(source, GxtTableModel):
            return source._sort_values[left.row()][left.column()] < source._sort_values[right.row()][right.column()]
        return super().lessThan(left, right)

    def identity_at(self, proxy_row):
        proxy_index = self.index(proxy_row, 0)
        if not proxy_index.isValid():
            return None
        source_index = self.mapToSource(proxy_index)
        source = self.sourceModel()
        return source.identity_at(source_index.row()) if isinstance(source, GxtTableModel) else None

    def row_for_identity(self, table, key_hash):
        source = self.sourceModel()
        if not isinstance(source, GxtTableModel):
            return -1
        source_row = source.row_for_identity(table, key_hash)
        if source_row < 0:
            return -1
        proxy_index = self.mapFromSource(source.index(source_row, 0))
        return proxy_index.row() if proxy_index.isValid() else -1


# Names that read naturally at call sites and preserve one implementation.
QtGxtTableModel = GxtTableModel
QtGxtFilterProxyModel = GxtSortFilterProxyModel

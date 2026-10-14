import struct
import tempfile
import unittest
from pathlib import Path

from studio.gxt.codec import decode_text
from studio.gxt.model import Gxt
from studio.ui_qt.document import QtDocument
from studio.ui_qt.models import GxtSortFilterProxyModel, GxtTableModel


def fixture_gxt(entries, table='MAIN'):
    """Build a minimal, valid GXT v4 with a single table."""
    data = bytearray()
    offsets = []
    for _hash, value in entries:
        offsets.append(len(data))
        data.extend(value + b'\0')
    keys = b''.join(struct.pack('<II', offset, key) for (key, _), offset in zip(entries, offsets))
    block = b'TKEY' + struct.pack('<I', len(keys)) + keys
    block += b'TDAT' + struct.pack('<I', len(data)) + data
    table_header = table.encode('latin-1').ljust(8, b'\0') + struct.pack('<I', 24)
    return struct.pack('<HH', 4, 8) + b'TABL' + struct.pack('<I', 12) + table_header + block


class QtDocumentTests(unittest.TestCase):
    def test_edits_roundtrip_through_undo_redo_and_safe_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'texts.gxt'
            original = fixture_gxt([(0x1234, b'Old'), (0x5678, b'Keep')])
            path.write_bytes(original)
            document = QtDocument()

            document.load(path)
            self.assertEqual(document.rows([('MAIN', 0x1234)]), [{
                'table': 'MAIN', 'hash': 0x1234, 'name': '0x00001234',
                'text': 'Old', 'comparison': None, 'flags': set(),
            }])
            document.set_text('MAIN', 0x1234, 'Novo')
            self.assertTrue(document.dirty)
            self.assertEqual(document.undo(), True)
            self.assertEqual(document.rows([('MAIN', 0x1234)])[0]['text'], 'Old')
            self.assertEqual(document.redo(), True)
            self.assertEqual(document.rows([('MAIN', 0x1234)])[0]['text'], 'Novo')

            document.save()
            self.assertFalse(document.dirty)
            self.assertEqual(Path(str(path) + '.bak').read_bytes(), original)
            self.assertEqual(decode_text(Gxt(path).t['MAIN']['cur'][0x1234]), 'Novo')

    def test_comparison_rows_keep_table_hash_identity_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / 'current.gxt'
            reference = Path(tmp) / 'reference.gxt'
            current.write_bytes(fixture_gxt([(0x1234, b'Novo'), (0x5678, b'Igual')]))
            reference.write_bytes(fixture_gxt([(0x1234, b'Old'), (0x5678, b'Igual')]))
            document = QtDocument()
            document.load(current)
            document.load_comparison(reference)

            rows = document.rows([('MAIN', 0x1234), ('MAIN', 0x5678)])
            self.assertEqual(rows[0]['comparison'], 'Old')
            self.assertIn('different', rows[0]['flags'])
            self.assertEqual(rows[1]['comparison'], 'Igual')
            self.assertNotIn('different', rows[1]['flags'])

    def test_unrepresentable_text_is_rejected_without_poisoning_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'texts.gxt'
            path.write_bytes(fixture_gxt([(0x1234, b'Old')]))
            document = QtDocument()
            document.load(path)
            with self.assertRaises(ValueError):
                document.set_text('MAIN', 0x1234, 'unsupported 🙂')
            self.assertEqual(document.rows([('MAIN', 0x1234)])[0]['text'], 'Old')
            self.assertFalse(document.dirty)
            self.assertFalse(document.undo())

    def test_csv_import_is_atomic_and_matches_table_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'texts.gxt'
            path.write_bytes(fixture_gxt([(0x1234, b'Old'), (0x5678, b'Keep')]))
            document = QtDocument()
            document.load(path)
            csv_path = Path(tmp) / 'changes.csv'
            csv_path.write_text(
                'tabela;id;atual\nMAIN;0x00001234;Novo\nOTHER;0x00005678;wrong\n'
                'MAIN;0x00005678;unsupported 🙂\n',
                encoding='utf-8-sig',
            )

            report = document.import_csv(csv_path)
            self.assertEqual(report['changed'], 1)
            self.assertEqual(report['missing'], 1)
            self.assertEqual(report['incompatible'], 1)
            self.assertEqual(document.rows([('MAIN', 0x1234)])[0]['text'], 'Novo')
            self.assertEqual(document.rows([('MAIN', 0x5678)])[0]['text'], 'Keep')
            self.assertTrue(document.undo())
            self.assertEqual(document.rows([('MAIN', 0x1234)])[0]['text'], 'Old')

    def test_proxy_preserves_table_hash_identity_for_large_tables(self):
        from PySide6.QtCore import Qt

        rows = [
            {'table': 'MAIN', 'hash': i, 'name': f'KEY_{i:05d}', 'text': f'Line {i}',
             'comparison': None, 'flags': set()}
            for i in range(16000)
        ]
        model = GxtTableModel()
        model.set_rows(rows)
        proxy = GxtSortFilterProxyModel()
        proxy.setSourceModel(model)
        proxy.set_filter_text('Line 15999')

        self.assertEqual(proxy.rowCount(), 1)
        index = proxy.index(0, model.TextColumn)
        self.assertEqual(proxy.data(index, model.IdentityRole), ('MAIN', 15999))
        proxy.set_filter_text('')
        proxy.sort(model.HashColumn, Qt.SortOrder.DescendingOrder)
        self.assertEqual(proxy.rowCount(), 16000)
        self.assertEqual(proxy.identity_at(0), ('MAIN', 15999))


if __name__ == '__main__':
    unittest.main()

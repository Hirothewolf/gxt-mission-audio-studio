import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studio.projects.review import ReviewStore, text_hash
from studio.ui_qt.classic_filters import FILTERS, accepts, prepare_filter_snapshot, row_flags
from studio.ui_qt.models import GxtTableModel, GxtSortFilterProxyModel


class ClassicFilterTests(unittest.TestCase):
    def make_window(self, root):
        store = ReviewStore(root)
        return SimpleNamespace(
            document=SimpleNamespace(gxt=SimpleNamespace(t={'MAIN': {'cur': {0x1234: b'Hello'}}})),
            context=SimpleNamespace(review_store=store),
            ignore_tags=SimpleNamespace(isChecked=lambda: False),
            enrich=lambda row: dict(row, line_id='MAIN|00001234', info={}),
            workspace=SimpleNamespace(get=lambda _identity: {}),
        )

    def row(self, **changes):
        return dict(dict(table='MAIN', hash=0x1234, name='HELLO', text='Hello',
                         comparison='Hello', flags=set()), **changes)

    def test_all_classic_predicates_and_existing_aliases(self):
        cases = [
            ('Diferentes da comparação', {'dif'}, set()),
            ('Iguais à comparação', {'igual'}, {'dif'}),
            ('Editadas (não salvas)', {'edit'}, set()),
            ('Só no arquivo editado', {'solo'}, {'igual'}),
            ('Com aviso de lint', {'lint'}, set()),
            ('Com áudio original', {'stream_link'}, set()),
            ('Sem áudio original', set(), {'audio'}),
            ('Com dublagem', {'dub'}, {'take'}),
            ('Sem dublagem (tem original)', {'audio'}, {'audio', 'dub'}),
            ('Favoritas ★', {'fav'}, set()),
            ('Com nota', {'note'}, set()),
            ('Revisadas', {'rev'}, set()),
            ('Não revisadas', set(), {'rev'}),
            ('Instalada no ModLoader', {'inst'}, {'dub'}),
            ('Dublada, ainda não instalada', {'dub', 'not_installed'}, {'dub', 'installation_unknown'}),
        ]
        for name, positive, negative in cases:
            with self.subTest(name=name):
                self.assertIn(name, FILTERS)
                self.assertTrue(accepts(name, positive))
                self.assertFalse(accepts(name, {'flags': negative}))
        self.assertTrue(accepts('Diferentes', {'different'}))
        self.assertTrue(accepts('Editadas', {'edited'}))
        self.assertTrue(accepts('Com take', {'take'}))
        self.assertTrue(accepts('Favoritas', {'fav'}))

    def test_ignore_tags_changes_comparison_and_missing_reference_is_solo(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = self.make_window(tmp)
            row = self.row(text='~r~Hello   world', comparison='Hello world')
            self.assertIn('different', row_flags(window, row))
            window.ignore_tags = SimpleNamespace(isChecked=lambda: True)
            flags = row_flags(window, row)
            self.assertIn('igual', flags)
            self.assertNotIn('different', flags)
            flags = row_flags(window, self.row(comparison=None))
            self.assertIn('solo', flags)
            self.assertNotIn('igual', flags)

    def test_approved_take_requires_real_matching_bytes_and_installation_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = self.make_window(tmp)
            store = window.context.review_store
            take = store.directory / 'take.wav'
            take.parent.mkdir(parents=True, exist_ok=True)
            take.write_bytes(b'fixture audio bytes')
            store.data['lines']['MAIN|00001234'] = {
                'approved': 'take', 'candidates': [dict(id='take', status='approved',
                    text_hash=text_hash('Hello'), path=str(take),
                    sha256=hashlib.sha256(take.read_bytes()).hexdigest())]}
            prepare_filter_snapshot(window)
            flags = row_flags(window, self.row())
            self.assertIn('dub', flags)
            self.assertIn('installation_unknown', flags)
            self.assertNotIn('inst', flags)
            self.assertFalse(accepts('Dublada, ainda não instalada', flags))
            window.context.filter_installation_evidence = {'MAIN|00001234': False}
            prepare_filter_snapshot(window)
            self.assertTrue(accepts('Dublada, ainda não instalada', row_flags(window, self.row())))
            window.context.filter_installation_evidence = {'MAIN|00001234': True}
            prepare_filter_snapshot(window)
            self.assertTrue(accepts('Instalada no ModLoader', row_flags(window, self.row())))
            self.assertNotIn('dub', row_flags(window, self.row(text='Changed')))
            take.write_bytes(b'tampered bytes')
            prepare_filter_snapshot(window)
            self.assertNotIn('dub', row_flags(window, self.row()))

    def test_snapshot_revalidates_when_text_changes_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = self.make_window(tmp)
            store = window.context.review_store
            take = store.directory / 'take.wav'
            take.parent.mkdir(parents=True, exist_ok=True)
            take.write_bytes(b'audio')
            store.data['lines']['MAIN|00001234'] = {'approved': 'take', 'candidates': [
                dict(id='take', status='approved', text_hash=text_hash('Hello'),
                     path=str(take), sha256=hashlib.sha256(b'audio').hexdigest())]}
            window.document.gxt.t['MAIN']['cur'][0x1234] = b'Changed'
            prepare_filter_snapshot(window)
            self.assertNotIn('dub', row_flags(window, self.row(text='Changed')))
            window.document.gxt.t['MAIN']['cur'][0x1234] = b'Hello'
            prepare_filter_snapshot(window)
            self.assertIn('dub', row_flags(window, self.row()))
            with patch.object(store, 'approved_path', side_effect=AssertionError('per-row I/O')):
                with patch.object(Path, 'stat', side_effect=AssertionError('per-row I/O')):
                    for _ in range(30):
                        self.assertIn('dub', row_flags(window, self.row()))

    def test_notes_participate_in_search_and_tagless_cache_updates(self):
        model = GxtTableModel()
        proxy = GxtSortFilterProxyModel()
        proxy.setSourceModel(model)
        model.set_rows([self.row(text='Hello ~r~ world', note='Needs timing review')])
        proxy.set_filter_text('timing review')
        self.assertEqual(proxy.rowCount(), 1)
        proxy.set_filter_text('hello world')
        self.assertEqual(proxy.rowCount(), 0)
        proxy.set_ignore_tags(True)
        self.assertEqual(proxy.rowCount(), 1)
        model.update_row(self.row(text='Other ~g~ words', note='Reviewed'))
        self.assertEqual(proxy.rowCount(), 0)
        proxy.set_filter_text('other words')
        self.assertEqual(proxy.rowCount(), 1)
        proxy.set_filter_text('timing')
        self.assertEqual(proxy.rowCount(), 0)
        proxy.set_filter_text('reviewed')
        self.assertEqual(proxy.rowCount(), 1)


if __name__ == '__main__':
    unittest.main()


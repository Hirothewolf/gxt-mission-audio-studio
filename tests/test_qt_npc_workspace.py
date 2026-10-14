"""NPC workspace model and save safety; no synthesis or game installation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt, QItemSelectionModel
from PySide6.QtWidgets import QApplication

from studio.ui_qt.npc_workspace import NpcWorkspace, NpcTableModel, RECORD_ROLE
from studio.projects.review import text_hash


def row(sid, *, archive='SPC_EA', status='ASR_DRAFT', language='en', probability=.95, text='hello'):
    return dict(stable_audio_id=sid, archive=archive, bank_index=1, sound_index=2,
        source_audio='/unused/original.wav', source_sha256='source-hash',
        duration_seconds=1.25, reference_text=text, text=text, scope='NPC', status=status,
        asr_language=language, asr_language_probability=probability, speaker='UNKNOWN')


class FakePlayer:
    def __init__(self): self.events = []
    def play(self, path): self.events.append(('play', path))
    def compare(self, paths): self.events.append(('compare', paths))
    def stop(self): self.events.append(('stop',))


class FakeStore:
    def __init__(self): self.candidates = {}; self.statuses = []; self.validation_threads = []
    def validate_candidate_source(self, line_id, candidate_id):
        self.validation_threads.append(threading.get_ident())
        candidate = self.candidates[candidate_id]
        if candidate['line_id'] != line_id: raise ValueError('candidate mismatch')
        return dict(candidate)
    def set_status(self, line_id, candidate_id, status, text, **kwargs):
        self.validation_threads.append(threading.get_ident())
        self.statuses.append((line_id, candidate_id, status, text, kwargs))


class FakeContext:
    def __init__(self, root, records):
        self.project_root = Path(root)
        self.records = {r['stable_audio_id']: r for r in records}
        self.player = FakePlayer()
        self.review_store = FakeStore()
        self.game_audio = SimpleNamespace(_game_is_running=lambda: False)
        self.saved = []
        self.installed = []
        self.save_threads = []
        self.install_threads = []
        self.texts = {}
        self.stale_edits = {}
        self.catalog_changed_edits = set()
    def current_text(self, record): return self.texts.get(record['stable_audio_id'], record['reference_text'])
    def save_text(self, record, text):
        self.save_threads.append(threading.get_ident())
        self.texts[record['stable_audio_id']] = text
        self.stale_edits.pop(record['stable_audio_id'], None)
    def info(self, record): return dict(record)
    def original_path(self, record): return Path(record['source_audio'])
    def installed_path(self, record): return None
    def audio_evidence(self, record, source='mod', take_path=None):
        return {'path': take_path if source == 'take' else None, 'state': 'available' if take_path else 'not_found'}
    def approved_path(self, line_id, text): return None
    def chosen_path(self, line_id, text): return None
    def install(self, records, progress=None):
        self.install_threads.append(threading.get_ident())
        self.installed.extend(records)
        return {'installed': [r['line_id'] for r in records], 'skipped': [], 'errors': []}


class NpcWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        records = [
            row('SPC_EA:B000:S000', text='english first'),
            row('SPC_EA:B000:S001', status='ASR_DRAFT', language='pt', probability=.55, text='fala baixa'),
            row('SPC_EA:B001:S000', status='NO_SPEECH_DETECTED', language=None, probability=0, text=''),
            row('SPC_EB:B000:S000', archive='SPC_EB', text='other archive'),
        ]
        self.context = FakeContext(self.temp.name, records)
        self.workspace = NpcWorkspace.__new__(NpcWorkspace)
        # Build a small test context through the supported no-I/O constructor hook.
        self.workspace = NpcWorkspace(self.context, catalogue=SimpleNamespace(
            records=tuple(records), npc_records=tuple(records),
            summary={'total': 4, 'npc_count': 4, 'player_count': 0,
                     'non_english_speech': 1, 'uncertain_language_speech': 1},
            catalog_path=Path(self.temp.name) / 'catalog.jsonl'),
            context_factory=lambda base, catalogue: self.context)
        self.workspace.show()
        self.addCleanup(self.workspace.close)

    def test_virtual_model_keeps_physical_identity_and_shows_uncertain_rows(self):
        model = self.workspace.table.model().sourceModel()
        self.assertIsInstance(model, NpcTableModel)
        self.assertEqual(model.rowCount(), 4)
        ids = {model.record_at(i)['stable_audio_id'] for i in range(4)}
        self.assertEqual(len(ids), 4)
        self.assertIn('SPC_EA:B001:S000', ids)  # no-speech remains visible
        self.workspace.select_record('SPC_EA:B000:S001')
        self.assertEqual(self.workspace.selected_record()['stable_audio_id'], 'SPC_EA:B000:S001')

    def test_original_asr_transcript_remains_visible_separate_from_edited_target(self):
        sid = 'SPC_EA:B000:S000'
        self.workspace.select_record(sid)
        model = self.workspace.model
        source_row = self.workspace._source_row_by_id[sid]
        original_index = model.index(source_row, 4)
        target_index = model.index(source_row, 5)
        self.assertEqual(model.headerData(4, Qt.Orientation.Horizontal), 'Transcrição original (ASR)')
        self.assertEqual(model.data(original_index), 'english first')
        original_sort_key = model.sort_key(source_row, 4)
        self.workspace.editor.setPlainText('Portuguese edited target')
        self.assertEqual(model.data(original_index), 'english first')
        self.assertEqual(model.data(target_index), 'Portuguese edited target')
        self.assertEqual(model.sort_key(source_row, 4), original_sort_key)
        tooltip = model.data(original_index, Qt.ItemDataRole.ToolTipRole)
        self.assertIn('Idioma ASR: en', tooltip)
        self.assertIn('95.0%', tooltip)

    def test_filter_and_archive_keep_selected_record_identity(self):
        self.workspace.select_record('SPC_EB:B000:S000')
        self.workspace.set_language('all')
        self.workspace.set_archive('SPC_EA')
        self.assertEqual(self.workspace.table.model().rowCount(), 3)
        self.assertEqual(self.workspace.selected_record()['stable_audio_id'], 'SPC_EA:B000:S000')
        self.workspace.set_filter('fala baixa')
        self.assertEqual(self.workspace.table.model().rowCount(), 1)
        self.assertEqual(self.workspace.table.model().record_at(0)['stable_audio_id'], 'SPC_EA:B000:S001')
        self.assertEqual(self.workspace.selected_record()['stable_audio_id'], 'SPC_EA:B000:S001')
        self.assertEqual(self.workspace.table.selectionModel().currentIndex().row(), 0)

    def test_switching_row_flushes_old_editor_before_loading_new_record(self):
        first, second = 'SPC_EA:B000:S000', 'SPC_EA:B000:S001'
        self.workspace.set_language('all')
        self.workspace.select_record(first)
        self.workspace.editor.setPlainText('edited first row')
        source_row = self.workspace._source_row_by_id[second]
        target = self.workspace.proxy.mapFromSource(self.workspace.model.index(source_row, 0))
        self.workspace.table.selectionModel().setCurrentIndex(target,
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        self.assertEqual(self.context.current_text(self.context.records[first]), 'edited first row')
        self.assertEqual(self.workspace.selected_record()['stable_audio_id'], second)
        self.assertEqual(self.workspace.editor.toPlainText(), 'fala baixa')

    def test_filter_flushes_draft_and_moves_selection_to_visible_physical_row(self):
        first, second = 'SPC_EA:B000:S000', 'SPC_EA:B000:S001'
        self.workspace.set_language('all')
        self.workspace.select_record(first)
        self.workspace.editor.setPlainText('edited before filter')
        self.workspace.set_filter('fala baixa')
        current = self.workspace.table.selectionModel().currentIndex()
        visible = self.workspace.proxy.data(current, RECORD_ROLE)
        self.assertEqual(self.context.current_text(self.context.records[first]), 'edited before filter')
        self.assertEqual(self.workspace.selected_record()['stable_audio_id'], second)
        self.assertEqual(visible['stable_audio_id'], second)
        self.assertEqual(self.workspace.editor.toPlainText(), 'fala baixa')

    def test_empty_filter_clears_active_record_and_restore_selects_visible_row(self):
        self.workspace.set_language('all')
        self.workspace.editor.setPlainText('preserve this draft')
        self.workspace.set_filter('no such NPC text')
        self.assertEqual(self.workspace.proxy.rowCount(), 0)
        self.assertIsNone(self.workspace.selected_record())
        self.assertEqual(self.workspace.editor.toPlainText(), '')
        self.assertFalse(self.workspace.editor.isEnabled())
        self.assertFalse(self.workspace.play_original_button.isEnabled())
        self.assertFalse(self.workspace.generate_button.isEnabled())
        self.workspace.set_filter('')
        current = self.workspace.table.selectionModel().currentIndex()
        active = self.workspace.selected_record()
        self.assertTrue(current.isValid())
        self.assertEqual(self.workspace.proxy.data(current, RECORD_ROLE)['stable_audio_id'],
                         active['stable_audio_id'])
        self.assertEqual(active['stable_audio_id'], 'SPC_EA:B000:S000')
        self.assertEqual(self.context.current_text(self.context.records['SPC_EA:B000:S000']),
                         'preserve this draft')

    def test_filter_that_keeps_active_physical_id_keeps_editor_and_highlight_aligned(self):
        sid = 'SPC_EA:B000:S000'
        self.workspace.select_record(sid)
        self.workspace.set_filter('english')
        current = self.workspace.table.selectionModel().currentIndex()
        self.assertEqual(self.workspace.selected_record()['stable_audio_id'], sid)
        self.assertEqual(self.workspace.proxy.data(current, RECORD_ROLE)['stable_audio_id'], sid)
        self.assertEqual(self.workspace.editor.toPlainText(), 'english first')

    def test_save_requires_exact_selected_candidate_and_game_stopped(self):
        sid = 'SPC_EA:B000:S000'
        self.workspace.select_record(sid)
        self.workspace.editor.setPlainText('target text')
        hashed = text_hash('target text')
        self.workspace.select_candidate(dict(id='old', line_id='NPC|' + sid,
            sha256='old-hash', text='target text', text_hash=hashed, path='/old.wav'))
        self.context.review_store.candidates['old'] = dict(id='old', line_id='NPC|' + sid,
            sha256='changed-hash', text='target text', text_hash=hashed, path='/old.wav')
        self.assertTrue(self.workspace.save_text_audio())
        deadline = time.monotonic() + 3
        while self.workspace.operation_active() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertEqual(self.context.installed, [])
        self.assertEqual(self.context.review_store.statuses, [])
        self.assertNotEqual(self.context.review_store.validation_threads[-1], threading.get_ident())

        candidate = dict(id='new', line_id='NPC|' + sid, sha256='new-hash',
            text='target text', text_hash=hashed, path='/new.wav')
        self.context.review_store.candidates.update(old=dict(candidate, id='old'), new=candidate)
        self.workspace.select_candidate(candidate)
        self.assertTrue(self.workspace.save())
        deadline = time.monotonic() + 3
        while self.workspace.operation_active() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertEqual(self.context.review_store.statuses[0][1:4], ('new', 'approved', 'target text'))
        self.assertEqual(self.context.installed[0]['line_id'], 'NPC|' + sid)
        self.assertTrue(all(thread_id != threading.get_ident()
                            for thread_id in self.context.review_store.validation_threads))
        self.assertNotEqual(self.context.install_threads[-1], threading.get_ident())
        self.assertNotIn(sid, self.workspace._candidate_choices)

        self.workspace.select_candidate(dict(candidate, id='newer', sha256='newer-hash'))
        self.context.game_audio._game_is_running = lambda: True
        self.assertFalse(self.workspace.save_text_audio())
        self.assertEqual(len(self.context.installed), 1)

    def test_generation_result_is_bound_to_frozen_physical_line(self):
        sid = 'SPC_EA:B000:S000'
        self.workspace.select_record(sid)
        self.workspace.editor.setPlainText('target text')
        candidate = dict(id='fresh', sha256='fresh-hash', text='target text',
                         text_hash=text_hash('target text'), path='/fresh.wav')
        self.workspace._generation = SimpleNamespace(record={'line_id': 'NPC|' + sid}, busy=False)
        self.workspace._generation_candidate_ready(candidate)
        self.assertEqual(self.workspace._candidate['line_id'], 'NPC|' + sid)
        self.assertEqual(self.workspace._candidate_signature['candidate_id'], 'fresh')

    def test_stale_edit_requires_text_save_before_generation(self):
        sid = 'SPC_EA:B000:S000'
        self.context.stale_edits[sid] = 'source hash changed'
        self.workspace.select_record(sid)
        self.assertFalse(self.workspace.generate_button.isEnabled())
        self.assertFalse(self.workspace.generate())
        self.assertTrue(self.workspace.save_text())
        self.assertNotIn(sid, self.context.stale_edits)
        self.assertTrue(self.workspace.generate_button.isEnabled())

    def test_stale_unchanged_text_stays_stale_on_navigation_until_explicit_save(self):
        stale_id = 'SPC_EA:B000:S000'
        self.context.stale_edits[stale_id] = 'source hash changed'
        self.workspace.set_language('all')
        self.workspace.select_record(stale_id)
        self.assertTrue(self.workspace.select_record('SPC_EA:B000:S001'))
        self.assertIn(stale_id, self.context.stale_edits)
        self.assertNotIn(stale_id, self.context.texts)

        self.workspace.select_record(stale_id)
        self.workspace.set_filter('fala baixa')
        self.assertIn(stale_id, self.context.stale_edits)
        self.assertNotIn(stale_id, self.context.texts)
        self.assertEqual(self.workspace.selected_record()['stable_audio_id'], 'SPC_EA:B000:S001')

        self.workspace.set_filter('')
        self.workspace.select_record(stale_id)
        self.assertTrue(self.workspace.save_text())
        self.assertNotIn(stale_id, self.context.stale_edits)
        self.assertEqual(self.context.texts[stale_id], 'english first')

    def test_edit_on_stale_source_blocks_navigation_until_explicit_save(self):
        stale_id = 'SPC_EA:B000:S000'
        self.context.stale_edits[stale_id] = 'source hash changed'
        self.workspace.select_record(stale_id)
        self.workspace.editor.setPlainText('new reviewed wording')
        self.assertFalse(self.workspace.select_record('SPC_EA:B000:S001'))
        self.assertEqual(self.workspace.selected_record()['stable_audio_id'], stale_id)
        self.assertEqual(self.workspace.editor.toPlainText(), 'new reviewed wording')
        self.assertNotIn(stale_id, self.context.texts)
        self.assertIn('Salvar só texto', self.workspace.status_label.text())

    def test_selected_records_returns_explicit_selection_or_current_only(self):
        first, second = 'SPC_EA:B000:S000', 'SPC_EA:B000:S001'
        self.workspace.select_record(first)
        self.workspace.set_language('all')
        self.workspace.editor.setPlainText('unsaved target')
        self.workspace.table.clearSelection()
        selected = self.workspace.selected_records()
        self.assertEqual([r['stable_audio_id'] for r in selected], [first])
        self.assertEqual(selected[0]['text'], 'unsaved target')
        selection = self.workspace.table.selectionModel()
        for sid in (first, second):
            source_row = self.workspace._source_row_by_id[sid]
            idx = self.workspace.proxy.mapFromSource(self.workspace.model.index(source_row, 0))
            selection.select(idx, QItemSelectionModel.SelectionFlag.Select |
                QItemSelectionModel.SelectionFlag.Rows)
        self.assertEqual({r['stable_audio_id'] for r in self.workspace.selected_records()}, {first, second})
        self.assertTrue(all(r['line_id'].startswith('NPC|') and 'info' in r
                            for r in self.workspace.selected_records()))

    def test_save_without_pending_candidate_only_saves_text(self):
        sid = 'SPC_EA:B000:S000'
        self.workspace.select_record(sid)
        self.workspace.editor.setPlainText('text-only draft')
        self.assertTrue(self.workspace.save())
        self.assertEqual(self.context.current_text(self.workspace.selected_record()), 'text-only draft')
        self.assertEqual(self.context.review_store.statuses, [])
        self.assertEqual(self.context.installed, [])

    def test_batch_generated_candidates_persist_selection_without_approval(self):
        sid = 'SPC_EA:B000:S000'
        self.workspace.select_record(sid)
        record = self.workspace.selected_record()
        candidate = dict(id='batch-take', sha256='batch-sha', text=record['text'],
                         text_hash=text_hash(record['text']), path='/batch.wav', status='pending')
        self.context.review_store.candidates[candidate['id']] = dict(candidate, line_id=record['line_id'])
        self.assertEqual(self.workspace.accept_generated([dict(record=record, candidate=candidate)]), 1)
        self.assertEqual(self.workspace._candidate['id'], 'batch-take')
        self.assertEqual(self.context.review_store.statuses, [])
        sidecar = Path(self.temp.name) / 'workspace/npc_selected_candidates.json'
        self.assertTrue(sidecar.is_file())
        self.assertEqual(self.workspace._candidate_choices[sid]['candidate_id'], 'batch-take')
        rows = tuple(self.context.records.values())
        catalogue = SimpleNamespace(records=rows, npc_records=rows, summary={'total': 4})
        restored = NpcWorkspace(self.context, catalogue=catalogue,
            context_factory=lambda base, catalog: self.context)
        self.addCleanup(restored.close)
        restored.select_record(sid)
        self.assertEqual(restored._candidate['id'], 'batch-take')
        self.assertEqual(self.context.review_store.statuses, [])

    def test_text_edit_invalidates_persisted_take_selection(self):
        sid = 'SPC_EA:B000:S000'
        self.workspace.select_record(sid)
        self.workspace.editor.setPlainText('target text')
        candidate = dict(id='chosen', line_id='NPC|' + sid, sha256='chosen-sha',
            text='target text', text_hash=text_hash('target text'), path='/chosen.wav')
        self.workspace.select_candidate(candidate)
        self.assertIn(sid, self.workspace._candidate_choices)
        self.workspace.editor.setPlainText('changed after generation')
        self.assertNotIn(sid, self.workspace._candidate_choices)
        self.assertFalse(self.workspace.save_button.isEnabled())

    def test_no_speech_cannot_start_generation_and_text_only_remains_available(self):
        self.workspace.select_record('SPC_EA:B001:S000')
        self.assertFalse(self.workspace.generate_button.isEnabled())
        self.assertTrue(self.workspace.editor.isEnabled())

    def test_language_filter_defaults_to_english_and_keeps_alternatives_accessible(self):
        self.assertEqual(self.workspace.language_combo.currentData(), 'en')
        self.assertEqual(self.workspace.table.model().rowCount(), 2)
        self.workspace.set_language('other')
        self.assertEqual(self.workspace.table.model().rowCount(), 0)
        self.workspace.set_language('low')
        self.assertEqual(self.workspace.table.model().rowCount(), 1)
        self.workspace.set_language('no_speech')
        self.assertEqual(self.workspace.table.model().rowCount(), 1)
        self.workspace.set_language('all')
        self.assertEqual(self.workspace.table.model().rowCount(), 4)

    def test_lazy_verified_catalog_load_runs_off_ui_thread_and_emits_summary(self):
        self.workspace.close()
        self.workspace = NpcWorkspace(self.context, context_factory=lambda base, catalogue: self.context)
        records = tuple(self.context.records.values())
        catalogue = SimpleNamespace(records=records, npc_records=records,
            summary={'total': 4, 'npc_count': 4, 'player_count': 0, 'catalog_sha256': 'fixture'},
            catalog_path=Path(self.temp.name) / 'catalog.jsonl')
        emitted = []
        self.workspace.summaryChanged.connect(emitted.append)
        with patch('studio.audio.npc_catalog.load_verified_catalog', return_value=catalogue):
            worker = self.workspace.load()
            self.assertIsNotNone(worker)
            deadline = time.monotonic() + 3
            while self.workspace.operation_active() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.app.processEvents()
        self.assertIs(self.workspace.catalogue, catalogue)
        self.assertEqual(self.workspace.table.model().rowCount(), 2)
        self.assertTrue(emitted)
        self.assertEqual(emitted[-1]['loaded_npc_count'], 4)
        self.assertFalse(self.workspace.operation_active())


if __name__ == '__main__':
    unittest.main()

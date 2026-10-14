import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock

from studio.projects.review import ReviewStore
from studio.projects.bulk_audio import collect_ai_rows, approve_rows, install_rows


def wav(path):
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000)
        f.writeframes(b'\x01\x01' * 800)


class BulkAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ReviewStore(self.root)
        self.source = self.root / 'source.wav'; wav(self.source)

    def context(self, line='MAIN|00000001', text='Teste', ref=('SCRIPT', 1, 1)):
        return dict(line_id=line, text=text, label=line, ref=ref, legacy_path=None)

    def test_matching_pending_is_approved_stale_and_rejected_are_preserved(self):
        good = self.store.import_candidate('MAIN|00000001', self.source, 'Teste', 'AI')
        stale = self.store.import_candidate('MAIN|00000002', self.source, 'Antigo', 'AI')
        bad = self.store.import_candidate('MAIN|00000003', self.source, 'Teste', 'AI')
        self.store.set_status('MAIN|00000003', bad['id'], 'rejected', 'Teste')
        rows = collect_ai_rows(self.store, [self.context('MAIN|%08x' % i) for i in (1, 2, 3)])
        result = approve_rows(self.store, rows)
        self.assertEqual(len(result['approved']), 1)
        self.assertEqual(self.store.list_candidates('MAIN|00000002', 'Teste')[0]['status'], 'pending')
        self.assertEqual(self.store.list_candidates('MAIN|00000003', 'Teste')[0]['status'], 'rejected')
        self.assertEqual(self.store.data['lines']['MAIN|00000001']['approved'], good['id'])

    def test_existing_approved_wins_over_newer_pending(self):
        a = self.store.import_candidate('MAIN|00000001', self.source, 'Teste', 'AI')
        self.store.set_status('MAIN|00000001', a['id'], 'approved', 'Teste')
        self.store.import_candidate('MAIN|00000001', self.source, 'Teste', 'AI')
        rows = collect_ai_rows(self.store, [self.context()])
        self.assertEqual(rows[0]['candidate_id'], a['id'])

    def test_repeated_legacy_confirmation_reuses_saved_copy(self):
        ctx = self.context(); ctx['legacy_path'] = self.source
        rows = collect_ai_rows(self.store, [ctx])
        self.assertEqual(len(approve_rows(self.store, rows, allow_legacy=True)['approved']), 1)
        self.assertEqual(len(approve_rows(self.store, rows, allow_legacy=True)['approved']), 1)
        self.assertEqual(len(self.store.list_candidates(ctx['line_id'], ctx['text'])), 1)

    def test_legacy_requires_explicit_binding_and_copy_preserves_source(self):
        ctx = self.context(); ctx['legacy_path'] = self.source
        rows = collect_ai_rows(self.store, [ctx])
        self.assertEqual(len(approve_rows(self.store, rows)['approved']), 0)
        result = approve_rows(self.store, rows, allow_legacy=True)
        self.assertEqual(len(result['approved']), 1)
        self.assertTrue(self.source.is_file())
        self.assertIsNotNone(self.store.approved_path(ctx['line_id'], ctx['text']))

    def test_changed_wav_and_duration_override_are_not_silently_accepted(self):
        a = self.store.import_candidate('MAIN|00000001', self.source, 'Teste', 'AI',
                                        metadata={'duration_override_required': True})
        rows = collect_ai_rows(self.store, [self.context()])
        self.assertEqual(len(approve_rows(self.store, rows)['approved']), 0)
        self.assertEqual(len(approve_rows(self.store, rows, duration_override=True)['approved']), 1)
        self.store.resolve_path(a['path']).write_bytes(b'bad')
        self.assertEqual(len(approve_rows(self.store, rows, duration_override=True)['approved']), 0)

    def test_install_groups_one_call_per_pak_and_never_routes_streams_as_sfx(self):
        contexts = [self.context('MAIN|%08x' % i, ref=ref) for i, ref in enumerate(
            [('SCRIPT', 1, 1), ('SCRIPT', 1, 2), ('SPC_EA', 1, 1), None], 1)]
        for ctx in contexts:
            a = self.store.import_candidate(ctx['line_id'], self.source, ctx['text'], 'AI')
            self.store.set_status(ctx['line_id'], a['id'], 'approved', ctx['text'])
        ga = Mock()
        ga.planejar.side_effect = lambda items, mod: [dict(x, problemas=[]) for x in items]
        ga.instalar.side_effect = lambda plan, mod: dict(ok=True, instalados=len(plan), backup='backup')
        result = install_rows(self.store, collect_ai_rows(self.store, contexts), ga, 'Mod')
        self.assertEqual(ga.instalar.call_count, 2)
        self.assertEqual(len(result['installed']), 3)
        self.assertEqual(len(result['skipped']), 1)

    def test_conflicting_duplicate_slot_is_blocked_not_last_wins(self):
        contexts = [self.context('MAIN|00000001'), self.context('MAIN|00000002')]
        other = self.root / 'other.wav'; wav(other)
        with other.open('ab') as f: f.write(b'different')
        for ctx, source in zip(contexts, [self.source, other]):
            a = self.store.import_candidate(ctx['line_id'], source, ctx['text'], 'AI')
            self.store.set_status(ctx['line_id'], a['id'], 'approved', ctx['text'])
        ga = Mock()
        result = install_rows(self.store, collect_ai_rows(self.store, contexts), ga, 'Mod')
        self.assertEqual(len(result['installed']), 0)
        self.assertEqual(len(result['skipped']), 2)
        ga.instalar.assert_not_called()


if __name__ == '__main__':
    unittest.main()

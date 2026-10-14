import os
import tempfile
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from studio.ui_qt import audio_sync
from studio.ui_qt.document import QtDocument

class PerformanceTests(unittest.TestCase):
    def test_ui_digest_reuses_and_invalidates_even_restored_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive'
            path.write_bytes(b'aaaa')
            # Stable files can be cached. New writes deliberately bypass the
            # cache while Windows change timestamps can still coalesce.
            time.sleep(1.1)
            audio_sync._UI_HASHES.clear()
            real_hash = audio_sync.sha256_arquivo
            with patch.object(audio_sync, 'sha256_arquivo', wraps=real_hash) as digest:
                first = audio_sync._ui_sha256(path)
                self.assertEqual(first, audio_sync._ui_sha256(path))
                self.assertEqual(1, digest.call_count)
                old = path.stat()
                path.write_bytes(b'bbbb')
                os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
                self.assertNotEqual(first, audio_sync._ui_sha256(path))
                self.assertEqual(2, digest.call_count)
                replacement = path.with_name('replacement')
                replacement.write_bytes(b'cccc')
                os.replace(replacement, path)
                self.assertNotEqual(first, audio_sync._ui_sha256(path))
                self.assertEqual(3, digest.call_count)

    def test_watcher_retains_strict_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive'
            path.write_bytes(b'a')
            real_hash = audio_sync.sha256_arquivo
            with patch.object(audio_sync, 'sha256_arquivo', wraps=real_hash) as digest:
                audio_sync.file_signature(path)
                audio_sync.file_signature(path)
                self.assertEqual(2, digest.call_count)

    def test_fresh_file_is_rehashed_even_with_coalesced_windows_change_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive'
            path.write_bytes(b'aaaa')
            old = path.stat()
            stamp = audio_sync._change_time(path, old)
            with patch.object(audio_sync, '_change_time', return_value=stamp):
                first = audio_sync._ui_sha256(path)
                path.write_bytes(b'bbbb')
                os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
                self.assertNotEqual(first, audio_sync._ui_sha256(path))

    def test_negative_name_cache_includes_source_identities(self):
        document = QtDocument(SimpleNamespace(key_names={'MAIN|00000001':'EXACT'}))
        def source(hashes):
            return SimpleNamespace(t={'MAIN': {'hashes': hashes}})
        with patch('studio.ui_qt.document.carregar_cache_nomes', return_value={}), patch('studio.ui_qt.document.recuperar_nomes', return_value={}) as recover:
            document.gxt = source([1, 2])
            self.assertEqual({1:'EXACT'}, document.names('MAIN'))
            document._names.clear()
            self.assertEqual({1:'EXACT'}, document.names('MAIN'))
            self.assertEqual(1, recover.call_count)
            document.gxt = source([1, 2, 3])
            document._names.clear()
            document.names('MAIN')
            self.assertEqual(2, recover.call_count)
            self.assertEqual(('MAIN', [2, 3]), recover.call_args.args)

    def test_reopening_in_new_document_reuses_deterministic_missing_names(self):
        catalog = SimpleNamespace(key_names={'MAIN|00000001': 'EXACT'})
        with patch('studio.ui_qt.document.carregar_cache_nomes', return_value={}), patch('studio.ui_qt.document.recuperar_nomes', return_value={}) as recover:
            for _ in range(2):
                document = QtDocument(catalog)
                document.gxt = SimpleNamespace(t={'MAIN': {'hashes': [1, 2]}})
                self.assertEqual(document.names('MAIN'), {1: 'EXACT'})
            self.assertEqual(recover.call_count, 1)

if __name__ == '__main__':
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from studio.ui.app import App
from studio.script.mapping import ref_do_registro


class AudioReindexTests(unittest.TestCase):
    def app(self):
        app = SimpleNamespace(
            audio_scan_thread=None, closed=False, g=True, sel=('INTRO1', 1),
            caminhos_audio_base=lambda: [], caminhos_audio_events=lambda: [],
            base_audio=SimpleNamespace(scan=Mock(), records=[]),
            original_audio_cache={'old': 1}, nomes={'INTRO1': {}},
            busca_cache_linhas={'old': 1}, busca_cache_filtro='old',
            script_db={'old': 1}, status=Mock(), ui=lambda callback: callback(),
            limpar_cache_dublado=Mock(), preencher_tabelas=Mock(),
            atualizar_linhas=Mock(), ao_escolher_linha=Mock(),
        )
        app._recarregar_vinculos_audio = lambda: App._recarregar_vinculos_audio(app)
        return app

    def test_reindex_refreshes_streams_and_mapping_even_without_wav_roots(self):
        app = self.app()
        new_db = {('INTRO1', 1): {'stable_audio_id': 'POLICE:PRO@24'}}
        with patch('studio.audio.stream_catalog.refresh_available_catalog', return_value={'BEATS:ALIGNMENT_CANDIDATE': 24}) as refresh, \
                patch('studio.ui.app.recarregar_script_cache', return_value=new_db), \
                patch('studio.ui.app.messagebox.showinfo'):
            App.reindexar_audios_base(app)
            app.audio_scan_thread.join(5)
        refresh.assert_called_once()
        self.assertIs(app.script_db, new_db)
        self.assertEqual(app.original_audio_cache, {})
        self.assertEqual(app.nomes, {})
        app.preencher_tabelas.assert_called_once()
        app.ao_escolher_linha.assert_called_once()

    def test_stream_failure_is_reported_but_fresh_mapping_still_loaded(self):
        app = self.app()
        with patch('studio.audio.stream_catalog.refresh_available_catalog', side_effect=ValueError('source mismatch')), \
                patch('studio.ui.app.recarregar_script_cache', return_value={'new': 2}):
            App.reindexar_audios_base(app, silencioso=True)
            app.audio_scan_thread.join(5)
        self.assertEqual(app.script_db, {'new': 2})
        self.assertIn('source mismatch', app.status.call_args.args[0])

    def test_stream_records_never_get_sfx_coordinates_from_legacy_metadata(self):
        for record in (
            {'stable_audio_id': 'POLICE:PRO@24', 'bank_num': 1, 'sound_num': 1},
            {'pak': 'CUTSCENE', 'bank_index': 0, 'sound_index': 0},
            {'audio_route': {'family': 'streams'}, 'path': 'SCRIPT/Bank_001/sound_001.wav'},
        ):
            with self.subTest(record=record):
                self.assertIsNone(ref_do_registro(record))

    def test_plain_script_project_without_stream_inputs_is_valid(self):
        from studio.audio.stream_catalog import refresh_available_catalog
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(refresh_available_catalog(Path(directory)), {})

    def test_route_only_stream_failure_does_not_search_script_or_aliases(self):
        app = self.app()
        app.original_audio_cache.clear()
        app.base_audio.resolve = Mock(return_value=('wrong-script.wav', 'alias', 95))
        app.jogo = Mock()
        record = {'audio_route': {'family': 'streams', 'container': 'BEATS', 'track': 'Track_010'}}
        with patch('studio.tts.stream_reference.resolve_stream_reference', side_effect=ValueError('unresolved')):
            result = App.resolver_audio_original(app, 'INTRO1', 1, ainfo=record, chave='PRO@24')
        self.assertIsNone(result[0])
        app.base_audio.resolve.assert_not_called()
        app.jogo.original_para.assert_not_called()


if __name__ == '__main__':
    unittest.main()

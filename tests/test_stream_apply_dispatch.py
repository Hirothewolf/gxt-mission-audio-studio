import sys
import types
import unittest
from unittest.mock import Mock, patch

from studio.projects.bulk_audio import install_rows


class StreamDispatchTests(unittest.TestCase):
    def test_stream_rows_delegate_once_without_sfx_calls(self):
        rows = [dict(line_id='CUT|1', text='Texto', path='take.wav', sha256='proof',
                     info={'stable_audio_id': 'CUTSCENE:Track_001:1'}),
                dict(line_id='CUT|2', text='Texto 2', path='other.wav', sha256='proof2',
                     info={'container': 'BEATS'})]
        expected = dict(installed=['CUT|1'], skipped=[{'line_id':'CUT|2','reason':'blocked'}],
                        errors=[], banks=[{'stream':'Track_001'}])
        backend = types.ModuleType('studio.audio.stream_install')
        backend.install_stream_rows = Mock(return_value=expected)
        store, game, progress = Mock(), Mock(), Mock()
        with patch.dict(sys.modules, {'studio.audio.stream_install': backend}):
            actual = install_rows(store, rows, game, 'mod', progress)
        self.assertEqual(actual, expected)
        backend.install_stream_rows.assert_called_once_with(store, rows, game, 'mod', progress)
        store.approved_path.assert_not_called()
        game.planejar.assert_not_called()
        game.instalar.assert_not_called()

    def test_unknown_route_keeps_skip_without_loading_stream_backend(self):
        game = Mock()
        actual = install_rows(Mock(), [{'line_id':'MAIN|1', 'ref':None}], game, 'mod')
        self.assertEqual(len(actual['skipped']), 1)
        game.instalar.assert_not_called()

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_cutscene_batch as fx

REPORT, TRACK, lookup = fx.REPORT, fx.TRACK, fx.lookup


class CutsceneBatchWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest('sem display para Tk')
        self.root.withdraw()
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch('studio.config.get_project_root', return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        app = SimpleNamespace(root=self.root, g=None, c=None, abrir_estudio_cutscenes=lambda: None)
        from studio.ui.cutscene_batch_ui import CutsceneBatchWindow
        self.win = CutsceneBatchWindow(app, autoload=False)
        self.win._lookup = lookup
        self.tracks = [{'track': TRACK}, {'track': 'Track_008'}]

    def tearDown(self):
        if hasattr(self, 'win'):
            self.win.closed = True
            self.win.executor.shutdown(wait=False)
            self.win.win.destroy()
            self.root.destroy()
            self.tmp.cleanup()

    def test_lista_faixas_com_contagens_e_previa_do_plano(self):
        self.win._apply_catalog(REPORT, self.tracks)
        tree = self.win.tracks_tree
        self.assertEqual(tree.get_children(), (TRACK, 'Track_008'))
        faixa, cena, falas, geraveis, avisos, excluidas, estado = tree.item(TRACK, 'values')
        self.assertEqual((int(falas), int(excluidas)), (7, 2))
        self.assertEqual(estado, 'Não gerada')
        tree.selection_set(TRACK)
        self.win._show_details()
        chaves = [self.win.exc_tree.item(i, 'values')[0] for i in self.win.exc_tree.get_children()]
        self.assertIn('D_SHORT', chaves)        # janela curta: pede decisão
        self.assertNotIn('E_SAME', chaves)      # não traduzida: rotina

    def test_velocidade_invalida_e_modo(self):
        self.win.speed_var.set('abc')
        with self.assertRaises(ValueError):
            self.win._policy()
        self.win.speed_var.set('3')
        with self.assertRaises(ValueError):
            self.win._policy()
        self.win.speed_var.set('1,1')
        self.win.mode_var.set('Duração livre')
        policy = self.win._policy()
        self.assertEqual((policy.speed, policy.duration_mode), (1.1, 'livre'))
        self.assertEqual(policy.background_mode, 'voice_only')
        self.win.background_var.set('Preservar áudio original fora dos trechos')
        self.assertEqual(self.win._policy().background_mode, 'preserve_original')

    def test_missing_ptbr_result_is_visible_as_silence(self):
        self.win._apply_catalog(REPORT, self.tracks)
        row = dict(self.win.plans[TRACK][0], outcome='MISSING_PTBR', final_reasons=['GENERATION_FAILED'], error='test failure')
        self.win.results[TRACK] = {'items': [row], 'render': 'silent-ptbr.wav', 'draft': None}
        self.win.tracks_tree.selection_set(TRACK)
        self.win._refresh_row(TRACK)
        self.assertIn('1 sem PT-BR', self.win.tracks_tree.item(TRACK, 'values')[6])
        rows = [self.win.exc_tree.item(i, 'values') for i in self.win.exc_tree.get_children()]
        self.assertEqual(rows[0][3], 'Sem take PT-BR (silêncio)')

    def test_resultado_atualiza_estado_e_excecoes(self):
        self.win._apply_catalog(REPORT, self.tracks)
        run = fx.RunTests('run_batch')
        run.setUp()
        self.addCleanup(run.tearDown)
        result = run.run_batch()
        self.win.results[TRACK] = result
        self.win._refresh_row(TRACK)
        self.assertIn('exceções', self.win.tracks_tree.item(TRACK, 'values')[6])
        self.win.tracks_tree.selection_set(TRACK)
        self.win._show_details()
        rows = [self.win.exc_tree.item(i, 'values') for i in self.win.exc_tree.get_children()]
        self.assertEqual(sorted(r[0] for r in rows), ['B_LONG', 'C_SILENT', 'D_SHORT', 'G_FAIL', 'H_SHORT_TAKE'])
        self.assertIn('Mantida original', {r[0]: r[3] for r in rows}['B_LONG'])
        self.assertIn('Faixa de revisão', self.win.detail_var.get())


if __name__ == '__main__':
    unittest.main()

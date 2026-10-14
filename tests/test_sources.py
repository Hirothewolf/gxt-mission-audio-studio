import struct
import tempfile
import unittest
from pathlib import Path

from studio.core.sources import (
    formatar_tamanho, geometria_segura, inspecionar_fontes, proximos_passos,
)


def _arvore(raiz, scm=True, gxt=True, sfx=('SCRIPT', 'SPC_EA'), cuts=True):
    raiz = Path(raiz)
    if scm:
        (raiz / 'data' / 'script').mkdir(parents=True)
        (raiz / 'data' / 'script' / 'main.scm').write_bytes(b'\0' * 2048)
    if gxt:
        (raiz / 'text').mkdir()
        (raiz / 'text' / 'american.gxt').write_bytes(b'\0' * 100)
    if sfx is not None:
        (raiz / 'audio' / 'SFX').mkdir(parents=True)
        for n in sfx:
            (raiz / 'audio' / 'SFX' / n).write_bytes(b'x')
    if cuts:
        (raiz / 'anim').mkdir()
        (raiz / 'anim' / 'cuts.img').write_bytes(b'VER2' + struct.pack('<I', 42))
    return raiz


class InspecionarFontesTests(unittest.TestCase):
    def por_chave(self, fontes):
        return {f.chave: f for f in fontes}

    def test_instalacao_completa_marca_as_quatro_fontes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self.por_chave(inspecionar_fontes(_arvore(tmp)))
            self.assertEqual(set(f), {'scm', 'texto', 'audio', 'cutscenes'})
            self.assertTrue(all(x.ok for x in f.values()))
            self.assertIn('2.0 KB', f['scm'].detalhe)
            self.assertIn('42 entradas', f['cutscenes'].detalhe)
            self.assertIn('SCRIPT', f['audio'].detalhe)

    def test_sem_pasta_do_jogo_nao_levanta_e_avisa(self):
        for entrada in (None, '', '/pasta/que/nao/existe'):
            fontes = inspecionar_fontes(entrada)
            self.assertEqual(len(fontes), 4)
            self.assertFalse(any(x.ok for x in fontes))
            self.assertTrue(all(x.avisos for x in fontes))
            self.assertTrue(all(x.pasta is None for x in fontes))

    def test_pastas_ausentes_ficam_pendentes_com_aviso(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self.por_chave(inspecionar_fontes(_arvore(tmp, scm=False, cuts=False)))
            self.assertFalse(f['scm'].ok)
            self.assertFalse(f['cutscenes'].ok)
            self.assertIn('data/script/main.scm', f['scm'].avisos[0])
            self.assertTrue(f['texto'].ok)

    def test_audio_sem_banco_script_nao_conta_como_pronto(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self.por_chave(inspecionar_fontes(_arvore(tmp, sfx=('SPC_EA',))))
            self.assertFalse(f['audio'].ok)
            self.assertIsNotNone(f['audio'].caminho)
            self.assertTrue(any('SCRIPT' in a for a in f['audio'].avisos))

    def test_nomes_com_outras_maiusculas_sao_achados(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            (raiz / 'DATA' / 'Script').mkdir(parents=True)
            (raiz / 'DATA' / 'Script' / 'MAIN.SCM').write_bytes(b'0')
            f = self.por_chave(inspecionar_fontes(raiz))
            self.assertTrue(f['scm'].ok)

    def test_mod_ativo_sobrescreve_gxt_e_scm_como_no_modloader(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = _arvore(tmp)
            mod = raiz / 'modloader' / 'MeuMod' / 'text'
            mod.mkdir(parents=True)
            (mod / 'american.gxt').write_bytes(b'0' * 5000)
            f = self.por_chave(inspecionar_fontes(raiz, 'MeuMod'))
            self.assertEqual(f['texto'].caminho, mod / 'american.gxt')
            self.assertIn('sobrescrito pelo mod ativo', f['texto'].detalhe)
            self.assertNotIn('sobrescrito', f['scm'].detalhe)
            # sem informar o mod, vale o jogo base
            base = self.por_chave(inspecionar_fontes(raiz))
            self.assertEqual(base['texto'].caminho, raiz / 'text' / 'american.gxt')

    def test_lista_outros_idiomas_e_aceita_gxt_sem_american(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = _arvore(tmp, gxt=False)
            (raiz / 'text').mkdir()
            (raiz / 'text' / 'brazilian.gxt').write_bytes(b'0')
            t = self.por_chave(inspecionar_fontes(raiz))['texto']
            self.assertTrue(t.ok)
            self.assertIn('brazilian.gxt', t.detalhe)
            self.assertTrue(t.avisos)

    def test_img_v1_e_formato_desconhecido(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = _arvore(tmp, cuts=False)
            (raiz / 'anim').mkdir()
            (raiz / 'anim' / 'cuts.img').write_bytes(b'abcdefghij')
            self.assertIn('não reconhecido', self.por_chave(inspecionar_fontes(raiz))['cutscenes'].detalhe)
            (raiz / 'anim' / 'cuts.dir').write_bytes(b'')
            self.assertIn('IMG v1', self.por_chave(inspecionar_fontes(raiz))['cutscenes'].detalhe)

    def test_pasta_a_abrir_e_a_que_contem_o_arquivo(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = _arvore(tmp)
            f = self.por_chave(inspecionar_fontes(raiz))
            self.assertEqual(f['scm'].pasta, raiz / 'data' / 'script')
            self.assertEqual(f['audio'].pasta, raiz / 'audio' / 'SFX')


class ProximosPassosTests(unittest.TestCase):
    def test_checklist_reflete_o_estado(self):
        with tempfile.TemporaryDirectory() as tmp:
            passos = proximos_passos(tmp, gxt_aberto=True, comparacao_aberta=False, saat_ok=False)
            self.assertEqual([ok for _t, ok in passos], [True, True, False, False])
        self.assertFalse(proximos_passos(None, False, False, False)[0][1])


class GeometriaSeguraTests(unittest.TestCase):
    TELA = (0, 0, 1366, 768)

    def test_geometria_valida_e_mantida(self):
        self.assertEqual(geometria_segura('1000x700+10+20', *self.TELA), '1000x700+10+20')

    def test_janela_maior_que_a_tela_e_reduzida(self):
        self.assertEqual(geometria_segura('1819x851+1+1', *self.TELA), '1366x768+0+0')

    def test_janela_fora_da_tela_volta_para_a_area_visivel(self):
        self.assertEqual(geometria_segura('1000x700+3000+900', *self.TELA), '1000x700+366+68')
        self.assertEqual(geometria_segura('1000x700+-2500+-40', *self.TELA), '1000x700+0+0')

    def test_monitor_secundario_a_esquerda_e_respeitado(self):
        virtual = (-1920, 0, 3286, 1080)
        self.assertEqual(geometria_segura('1200x800+-1900+10', *virtual), '1200x800+-1900+10')

    def test_tamanho_minimo_e_aplicado_quando_cabe(self):
        self.assertEqual(geometria_segura('300x200', *self.TELA), '900x560')

    def test_entrada_ilegivel_devolve_none(self):
        for ruim in (None, '', 'abc', '1000+10+10'):
            self.assertIsNone(geometria_segura(ruim, *self.TELA))

    def test_formatar_tamanho(self):
        self.assertEqual(formatar_tamanho(512), '512 B')
        self.assertEqual(formatar_tamanho(1536), '1.5 KB')
        self.assertEqual(formatar_tamanho(5 * 1024 ** 2), '5.0 MB')


if __name__ == '__main__':
    unittest.main()

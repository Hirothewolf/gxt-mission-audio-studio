import csv
import json
import math
import struct
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import Mock, patch
from pathlib import Path

from studio.audio import cutscene_batch as batch
from studio.audio import cutscene_studio as studio

TRACK = 'Track_007'
RATE = 8000


def write_wav(path, seconds, rate=24000, channels=1, amplitude=0.3, freq=220):
    frames = int(seconds * rate)
    samples = []
    for i in range(frames):
        v = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        samples.extend([v] * channels)
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(channels); out.setsampwidth(2); out.setframerate(rate)
        out.writeframes(struct.pack('<%dh' % len(samples), *samples))


def rec(n, key, start, dur, track=TRACK, **extra):
    row = {'pak': 'CUTSCENE', 'gxt_stable_id': 'CAS:%08X' % n, 'event_name': key, 'scene': 'CASINO',
           'caption_start_ms': int(start * 1000), 'caption_duration_ms': int(dur * 1000),
           'stream': {'track': track}}
    row.update(extra)
    return row


TEXTS = {  # hash -> (EN, PT)
    1: ('Hello there my friend.', 'Olá, meu amigo, tudo bem?'),
    2: ('We have to go now!', 'Temos que ir agora mesmo!'),
    3: ('Are you listening to me?', 'Você está me ouvindo aí?'),
    4: ('Go!', 'Vai!'),
    5: ('Same line.', 'Same line.'),
    6: ('This one will fail.', 'Esta vai falhar mesmo.'),
    7: ('Short take please.', 'Quero um take curtinho.'),
}
REPORT = {'sources': {}, 'records': [
    rec(1, 'A_OK', 1.0, 3.0), rec(2, 'B_LONG', 4.5, 4.0), rec(3, 'C_SILENT', 9.0, 2.5),
    rec(4, 'D_SHORT', 12.0, 0.5), rec(5, 'E_SAME', 13.0, 2.0), rec(6, 'G_FAIL', 15.5, 3.0),
    rec(7, 'H_SHORT_TAKE', 19.0, 3.0), rec(9, 'OTHER', 1.0, 2.0, track='Track_008')]}


def lookup(table, value):
    return TEXTS.get(value, ('', ''))


class PlanTests(unittest.TestCase):
    def plan(self, report=REPORT, duration=24.0, **kw):
        kw.setdefault('policy', batch.Policy(background_mode='preserve_original'))
        return {i['key']: i for i in batch.plan_track(report, TRACK, lookup, duration, **kw)}

    def test_apenas_a_faixa_pedida_em_ordem_de_tempo(self):
        items = batch.plan_track(REPORT, TRACK, lookup, 24.0)
        self.assertEqual([i['key'] for i in items][:3], ['A_OK', 'B_LONG', 'C_SILENT'])
        self.assertNotIn('OTHER', [i['key'] for i in items])

    def test_status_e_motivos(self):
        p = self.plan()
        self.assertEqual(p['A_OK']['status'], 'READY')
        self.assertEqual(p['A_OK']['reasons'], [])
        self.assertEqual(p['D_SHORT']['status'], 'SKIP')
        self.assertIn('REF_TOO_SHORT', p['D_SHORT']['reasons'])
        self.assertIn('PT_EQUALS_EN', p['E_SAME']['reasons'])
        self.assertEqual(p['E_SAME']['status'], 'SKIP')

    def test_janela_vem_da_legenda_sem_margem(self):
        a = self.plan()['A_OK']
        self.assertEqual((a['start'], a['end']), (1.0, 4.0))

    def test_sobreposicao_preserva_limites_e_original_por_padrao(self):
        report = {'records': [rec(1, 'A', 1.0, 5.0), rec(2, 'B', 4.0, 3.0)]}
        p = self.plan(report)
        self.assertEqual(p['A']['end'], 6.0)
        for key in ('A', 'B'):
            self.assertIn('OVERLAP_REQUIRES_REVIEW', p[key]['reasons'])
            self.assertEqual(p[key]['status'], 'SKIP')

    def test_sobreposicao_opt_in_preserva_ambas_as_falas(self):
        report = {'records': [rec(1, 'A', 1.0, 5.0), rec(2, 'B', 4.0, 3.0)]}
        p = self.plan(report, policy=batch.Policy(allow_overlap=True))
        self.assertEqual([(p[k]['start'], p[k]['end']) for k in ('A', 'B')], [(1., 6.), (4., 7.)])
        for key in ('A', 'B'):
            self.assertIn('OVERLAP_MIX', p[key]['reasons'])
            self.assertEqual(p[key]['status'], 'WARN')

    def test_texto_longo_demais_para_a_janela_e_excluido(self):
        texts = {1: ('x', 'palavras ' * 20)}
        items = batch.plan_track({'records': [rec(1, 'A', 0, 3.0)]}, TRACK, lambda t, v: texts[v], 10)
        self.assertIn('RATE_TOO_HIGH', items[0]['reasons'])
        self.assertEqual(items[0]['status'], 'SKIP')

    def test_fora_da_faixa_e_corte_no_fim(self):
        report = {'records': [rec(1, 'IN', 22.0, 5.0), rec(2, 'OUT', 30.0, 3.0)]}
        p = self.plan(report, duration=24.0)
        self.assertIn('CLIPPED_TO_TRACK_END', p['IN']['reasons'])
        self.assertEqual(p['IN']['end'], 24.0)
        self.assertIn('OUT_OF_TRACK', p['OUT']['reasons'])

    def test_sem_traducao_marcador_nao_verbal_e_ambiguidade(self):
        texts = {1: ('Hello there.', ''), 2: ('Hello there.', '~r~'), 3: ('AARGH', 'Arghh'), 4: ('Hello.', 'Olá aí.')}
        report = {'records': [rec(1, 'A', 0, 3), rec(2, 'B', 4, 3), rec(3, 'C', 8, 3),
                              rec(4, 'D', 12, 3, cutscene_ambiguous=True)]}
        p = {i['key']: i['reasons'] for i in batch.plan_track(report, TRACK, lambda t, v: texts[v], 30)}
        self.assertIn('NO_PT', p['A'])
        self.assertIn('NO_PT_SPEECH', p['B'])
        self.assertIn('AMBIGUOUS', p['D'])

    def test_plan_all_e_resumo(self):
        plans = batch.plan_all(REPORT, lookup, {TRACK: 24.0})
        self.assertEqual(sorted(plans), [TRACK, 'Track_008'])
        counts = batch.summarize(plans[TRACK])
        self.assertEqual(counts['total'], 7)
        self.assertEqual(counts['generate'], counts['READY'] + counts['WARN'])
        self.assertEqual(counts['SKIP'], 2)

    def test_todo_codigo_tem_explicacao(self):
        for code in batch.REASONS:
            info = batch.explain(code)
            self.assertTrue(info['message'])


class JudgeTests(unittest.TestCase):
    P = batch.Policy()

    def metrics(self, duration, rms=0.1, clip=0.0):
        return {'duration': duration, 'rms': rms, 'peak': .5, 'clip_ratio': clip}

    def test_veredictos(self):
        self.assertEqual(batch.judge_take(self.metrics(3.0), 3.0, self.P), ('OK', []))
        self.assertEqual(batch.judge_take(self.metrics(3.0, rms=0.0), 3.0, self.P), ('FAIL', ['TAKE_SILENT']))
        self.assertEqual(batch.judge_take(self.metrics(4.0), 3.0, self.P), ('FAIL', ['TAKE_TOO_LONG']))
        self.assertEqual(batch.judge_take(self.metrics(3.3), 3.0, self.P), ('WARN', ['TAKE_EXTENDED']))
        self.assertEqual(batch.judge_take(self.metrics(1.0), 3.0, self.P), ('WARN', ['TAKE_SHORT']))
        self.assertEqual(batch.judge_take(self.metrics(3.0, clip=.05), 3.0, self.P), ('WARN', ['TAKE_CLIPPED']))

    def test_analyze_take_mede_duracao_e_nivel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.wav'
            write_wav(path, 1.5, amplitude=0.5)
            m = batch.analyze_take(path)
            self.assertAlmostEqual(m['duration'], 1.5, delta=0.05)
            self.assertGreater(m['rms'], 0.2)
            quiet = Path(tmp) / 'q.wav'
            write_wav(quiet, 1.0, amplitude=0.0)
            self.assertLess(batch.analyze_take(quiet)['rms'], 0.001)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'src' / (TRACK + '.wav')
        self.source.parent.mkdir()
        write_wav(self.source, 24.0, rate=RATE, channels=2, amplitude=0.2, freq=110)
        self.track = {'path': self.source, 'track': TRACK, 'sha256': studio.digest(self.source),
                      'archive_sha256': None, 'archive_path': None}
        self.calls = []
        self.items = batch.plan_track(REPORT, TRACK, lookup, 24.0)

    def tearDown(self):
        self.tmp.cleanup()

    def synth(self, item, reference, output, cancel_event=None):
        self.calls.append(item['key'])
        window = item['end'] - item['start']
        key = item['key']
        if key == 'G_FAIL':
            raise RuntimeError('motor caiu')
        seconds = {'B_LONG': window * 1.6, 'H_SHORT_TAKE': window * .4}.get(key, window * .9)
        write_wav(output, seconds, amplitude=0.0 if key == 'C_SILENT' else 0.3)

    def run_batch(self, items=None, **kw):
        kw.setdefault('policy', batch.Policy(background_mode='preserve_original'))
        return batch.run_track(self.root, {'sources': {}}, self.track, items or self.items, self.synth, **kw)

    def by_key(self, result):
        return {r['key']: r for r in result['items']}

    def test_lote_completo_resultados_por_fala(self):
        r = self.by_key(self.run_batch())
        self.assertEqual(r['A_OK']['outcome'], 'OK')
        self.assertEqual(r['B_LONG']['outcome'], 'KEPT_ORIGINAL')
        self.assertIn('TAKE_TOO_LONG', r['B_LONG']['final_reasons'])
        self.assertIn('TAKE_SILENT', r['C_SILENT']['final_reasons'])
        self.assertIn('GENERATION_FAILED', r['G_FAIL']['final_reasons'])
        self.assertEqual(r['G_FAIL']['error'], 'motor caiu')
        self.assertEqual(r['H_SHORT_TAKE']['outcome'], 'WARN')
        self.assertEqual(r['D_SHORT']['outcome'], 'KEPT_ORIGINAL')
        self.assertNotIn('D_SHORT', self.calls)
        self.assertNotIn('E_SAME', self.calls)

    def test_overlap_policy_reaches_render_draft_and_cached_mix_report(self):
        report = {'records': [rec(1, 'A', 1., 5.), rec(2, 'B', 4., 3.)]}
        policy = batch.Policy(allow_overlap=True, background_mode='preserve_original')
        items = batch.plan_track(report, TRACK, lookup, 24., policy)
        result = self.run_batch(items, policy=policy)
        self.assertEqual(self.calls, ['A', 'B'])
        self.assertTrue(result['render_info']['allow_overlap'])
        self.assertEqual(result['render_info']['replaced_union_frames'], [[RATE, 7 * RATE]])
        draft = json.loads(Path(result['draft']).read_text(encoding='utf-8'))
        self.assertTrue(draft['allow_overlap'])
        self.assertEqual([(s['start_seconds'], s['end_seconds']) for s in draft['segments']], [(1., 6.), (4., 7.)])
        again = self.run_batch(items, policy=policy)
        self.assertEqual(again['render_info'], result['render_info'])

    def test_overlap_default_generates_neither_voice_and_preserves_original(self):
        report = {'records': [rec(1, 'A', 1., 5.), rec(2, 'B', 4., 3.)]}
        result = self.run_batch(batch.plan_track(report, TRACK, lookup, 24., batch.Policy(background_mode='preserve_original')))
        self.assertEqual(self.calls, [])
        self.assertIsNone(result['render'])
        self.assertTrue(all(row['outcome'] == 'KEPT_ORIGINAL' for row in result['items']))

    def test_voice_only_incomplete_overlap_mixes_available_ptbr_and_reports_missing_voice(self):
        report = {'records': [rec(1, 'G_FAIL', 1., 5.), rec(2, 'B', 4., 3.)]}
        policy = batch.Policy(allow_overlap=True)
        result = self.run_batch(batch.plan_track(report, TRACK, lookup, 24., policy), policy=policy)
        rows = self.by_key(result)
        self.assertEqual(rows['G_FAIL']['outcome'], 'MISSING_PTBR')
        self.assertEqual(rows['B']['outcome'], 'WARN')
        self.assertIn('OVERLAP_PARTIAL_PTBR', rows['B']['final_reasons'])
        self.assertEqual(result['missing_ptbr'][0]['key'], 'G_FAIL')
        self.assertEqual(result['render_info']['background_mode'], 'voice_only')
        with wave.open(result['render'], 'rb') as stream:
            pcm = stream.readframes(stream.getnframes())
        self.assertEqual(pcm[:4 * RATE * 4], b'\0' * (4 * RATE * 4))
        self.assertNotEqual(pcm[4 * RATE * 4:6 * RATE * 4], b'\0' * (2 * RATE * 4))
        self.assertEqual(pcm[7 * RATE * 4:], b'\0' * (17 * RATE * 4))
        draft = json.loads(Path(result['draft']).read_text())
        self.assertEqual(draft['background_mode'], 'voice_only')

    def test_voice_only_all_skipped_exports_silence_without_generating_any_take(self):
        skips = [row for row in self.items if row['status'] == 'SKIP']
        result = self.run_batch(skips, policy=batch.Policy())
        self.assertEqual(self.calls, [])
        self.assertTrue(result['missing_ptbr'])
        self.assertTrue(all(row['outcome'] == 'MISSING_PTBR' for row in result['items']))
        with wave.open(result['render'], 'rb') as stream:
            self.assertEqual(stream.readframes(stream.getnframes()), b'\0' * (24 * RATE * 4))

    def test_native_take_extends_end_without_stretching_or_cutting_its_pcm(self):
        report = {'records': [rec(1, 'A', 1., 3.)]}
        policy = batch.Policy()
        items = batch.plan_track(report, TRACK, lookup, 24., policy)
        take = self.root / 'prepared_native_take.wav'
        write_wav(take, 3.3, rate=RATE, channels=2)
        qc = {'duration': 3.3, 'rms': .1, 'clip_ratio': 0}
        synth = Mock(spec=[])
        with patch('studio.audio.cutscene_batch._ensure_take', return_value=(take, qc, True)):
            result = batch.run_track(self.root, {}, self.track, items, synth, policy)
        synth.assert_not_called()
        self.assertEqual(result['items'][0]['end'], 4.3)
        self.assertEqual(result['items'][0]['reference_end'], 4.)
        self.assertIn('TAKE_EXTENDED', result['items'][0]['final_reasons'])
        with wave.open(result['render'], 'rb') as rendered, wave.open(str(take), 'rb') as original:
            rendered.setpos(RATE)
            self.assertEqual(rendered.readframes(round(3.3 * RATE)), original.readframes(original.getnframes()))
        self.assertFalse(json.loads(Path(result['draft']).read_text())['fit_oversized'])

    def test_take_past_absolute_track_end_is_missing_without_trimming(self):
        report = {'records': [rec(1, 'A', 22., 2.)]}
        policy = batch.Policy()
        items = batch.plan_track(report, TRACK, lookup, 24., policy)
        take = self.root / 'too_late.wav'
        write_wav(take, 2.2, rate=RATE, channels=2)
        qc = {'duration': 2.2, 'rms': .1, 'clip_ratio': 0}
        with patch('studio.audio.cutscene_batch._ensure_take', return_value=(take, qc, True)):
            result = batch.run_track(self.root, {}, self.track, items, Mock(spec=[]), policy)
        self.assertEqual(result['items'][0]['outcome'], 'MISSING_PTBR')
        self.assertIn('TAKE_OUT_OF_TRACK', result['items'][0]['final_reasons'])
        with wave.open(result['render'], 'rb') as rendered:
            self.assertEqual(rendered.readframes(rendered.getnframes()), b'\0' * (24 * RATE * 4))

    def test_incomplete_simultaneous_group_keeps_both_voices_original(self):
        report = {'records': [rec(1, 'G_FAIL', 1., 5.), rec(2, 'B', 4., 3.)]}
        policy = batch.Policy(allow_overlap=True, background_mode='preserve_original')
        result = self.run_batch(batch.plan_track(report, TRACK, lookup, 24., policy), policy=policy)
        self.assertIsNone(result['render'])
        self.assertTrue(all(row['outcome'] == 'KEPT_ORIGINAL' for row in result['items']))
        self.assertIn('OVERLAP_GROUP_INCOMPLETE', self.by_key(result)['B']['final_reasons'])

    def test_faixa_montada_preserva_duracao_e_so_troca_as_janelas_aprovadas(self):
        result = self.run_batch()
        rendered = Path(result['render'])
        self.assertTrue(rendered.is_file())
        with wave.open(str(rendered), 'rb') as out, wave.open(str(self.source), 'rb') as src:
            self.assertEqual(out.getnframes(), src.getnframes())
            a, b = out.readframes(out.getnframes()), src.readframes(src.getnframes())
        frame = 4  # 2 canais x 16 bits

        def region(data, start, end):
            return data[int(start * RATE) * frame:int(end * RATE) * frame]
        self.assertNotEqual(region(a, 1.0, 4.0), region(b, 1.0, 4.0))       # A_OK trocada
        self.assertEqual(region(a, 4.5, 8.5), region(b, 4.5, 8.5))          # B_LONG mantida
        self.assertEqual(region(a, 9.0, 11.5), region(b, 9.0, 11.5))        # C_SILENT mantida
        self.assertEqual(region(a, 15.5, 18.5), region(b, 15.5, 18.5))      # G_FAIL mantida
        self.assertEqual(region(a, 0.0, 1.0), region(b, 0.0, 1.0))          # fora das janelas

    def test_rascunho_padrao_do_estudio_e_relatorio(self):
        result = self.run_batch()
        draft = json.loads(Path(result['draft']).read_text(encoding='utf-8'))
        self.assertEqual(draft['schema'], 1)
        self.assertEqual(draft['status'], 'DRAFT')
        self.assertFalse(draft['production_approved'])
        self.assertEqual(sorted(s['gxt']['key'] for s in draft['segments']), ['A_OK', 'H_SHORT_TAKE'])
        self.assertEqual(Path(result['draft']).parent, self.root / 'workspace' / 'cutscene_studio' / 'drafts')
        saved = json.loads((self.root / 'workspace' / 'cutscene_studio' / 'batch' / TRACK / 'report.json').read_text('utf-8'))
        self.assertEqual(saved['track'], TRACK)

    def test_repetir_nao_gera_de_novo_e_resultado_e_identico(self):
        first = self.run_batch()
        generated = len(self.calls)
        self.assertEqual(generated, 5)  # A, B, C, G, H (D/E excluídos no plano)
        second = self.run_batch()
        self.assertEqual(self.calls[generated:], ['G_FAIL'])  # só o que falhou é tentado de novo
        self.assertEqual(first['render'], second['render'])
        self.assertEqual(first['draft'], second['draft'])

    def test_mudar_o_texto_regera_somente_aquela_fala(self):
        self.run_batch()
        before = len(self.calls)
        changed = [dict(i, pt_text='Olá, parceiro, como vai você?') if i['key'] == 'A_OK' else i for i in self.items]
        result = self.run_batch(changed)
        self.assertEqual(self.calls[before:], ['A_OK', 'G_FAIL'])
        self.assertIsNotNone(result['render'])

    def test_rascunho_ajustado_a_mao_nao_e_sobrescrito(self):
        first = self.run_batch()
        path = Path(first['draft'])
        data = json.loads(path.read_text(encoding='utf-8'))
        data['editado_pelo_usuario'] = True
        path.write_text(json.dumps(data), encoding='utf-8')
        self.run_batch()
        self.assertTrue(json.loads(path.read_text(encoding='utf-8')).get('editado_pelo_usuario'))

    def test_excecoes_listam_so_o_que_precisa_de_decisao(self):
        result = self.run_batch()
        keys = sorted(r['key'] for r in batch.exceptions(result))
        self.assertEqual(keys, ['B_LONG', 'C_SILENT', 'D_SHORT', 'G_FAIL', 'H_SHORT_TAKE'])
        self.assertNotIn('E_SAME', keys)  # não traduzida: rotina, não exceção

    def test_cancelar_salva_relatorio_parcial(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(batch.CancelledError):
            self.run_batch(cancel_event=event)
        saved = json.loads((self.root / 'workspace' / 'cutscene_studio' / 'batch' / TRACK / 'report.json').read_text('utf-8'))
        self.assertTrue(saved['cancelled'])

    def test_resumo_csv(self):
        result = self.run_batch()
        path = batch.write_summary_csv(self.root, [result])
        rows = list(csv.reader(path.open(encoding='utf-8-sig'), delimiter=';'))
        self.assertEqual(rows[0][:5], ['faixa', 'chave', 'inicio_s', 'fim_s', 'resultado'])
        self.assertEqual(len(rows), 1 + len(self.items))
        by = {r[1]: r for r in rows[1:]}
        self.assertEqual(by['A_OK'][4], 'OK')
        self.assertIn('Encurte', by['B_LONG'][6])

    def test_faixa_sem_nada_a_gerar_nao_decodifica_nem_monta(self):
        only_skips = [i for i in self.items if i['status'] == 'SKIP']
        result = self.run_batch(only_skips)
        self.assertIsNone(result['render'])
        self.assertEqual(self.calls, [])


class OmniVoiceWiringTests(unittest.TestCase):
    """Usa o build_command REAL com um runner de mentira que só escreve um WAV."""

    def test_pedido_real_chega_ao_runner_com_janela_e_textos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'src.wav'
            write_wav(source, 24.0, rate=RATE, channels=2)
            gxt = root / 'american.gxt'
            gxt.write_bytes(b'gxt original')
            runner = root / 'runner.py'
            runner.write_text(
                'import json, sys, wave\n'
                'req = json.load(open(sys.argv[sys.argv.index("--request")+1], encoding="utf-8"))\n'
                'json.dump({k: req[k] for k in ("spoken_target_text","reference_text","duration","window_seconds","seed","speed")},'
                ' open(req["output_path"] + ".seen.json", "w"))\n'
                'w = wave.open(req["output_path"], "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)\n'
                'w.writeframes(b"\\x10\\x10" * int(24000 * req["window_seconds"] * .9)); w.close()\n', encoding='utf-8')
            report = {'sources': {'gxt': {'path': str(gxt), 'sha256': studio.digest(gxt)}}}
            track_data = {'source_path': str(source), 'source_sha256': studio.digest(source), 'sample_rate': RATE,
                          'frames': 24 * RATE, 'archive_path': None, 'archive_sha256': None, 'track': TRACK}
            reference = root / 'ref.wav'
            write_wav(reference, 3.0)
            synth = batch.omnivoice_synthesizer(root, report, track_data, python_path=sys.executable,
                                                script_path=runner, timeout=60)
            item = {'gid': 'CAS:00000001', 'start': 1.0, 'end': 4.0, 'en_text': '~r~Hello there.', 'pt_text': 'Olá aí.'}
            out = root / 'workspace' / 'cutscene_studio' / 'batch' / TRACK / 'takes' / 'x.part.wav'
            out.parent.mkdir(parents=True)
            synth(item, {'path': str(reference)}, out)
            self.assertTrue(out.is_file())
            seen = json.loads(Path(str(out) + '.seen.json').read_text())
            self.assertEqual(seen['spoken_target_text'], 'Olá aí.')
            self.assertEqual(seen['reference_text'], 'Hello there.')   # tags GTA removidas
            self.assertEqual(seen['duration'], 3.0)                    # pede ao motor o tamanho da janela
            self.assertEqual(seen['seed'], 20261009)
            # modo livre não fixa a duração
            out2 = out.with_name('y.part.wav')
            batch.omnivoice_synthesizer(root, report, track_data, batch.Policy(duration_mode='livre'),
                                        python_path=sys.executable, script_path=runner, timeout=60)(
                item, {'path': str(reference)}, out2)
            self.assertIsNone(json.loads(Path(str(out2) + '.seen.json').read_text())['duration'])

    def test_erro_do_runner_vira_mensagem_legivel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'src.wav'
            write_wav(source, 5.0, rate=RATE, channels=2)
            gxt = root / 'g.gxt'
            gxt.write_bytes(b'x')
            runner = root / 'bad.py'
            runner.write_text('import sys\nprint("GPU fora do limite")\nsys.exit(3)\n', encoding='utf-8')
            report = {'sources': {'gxt': {'path': str(gxt), 'sha256': studio.digest(gxt)}}}
            track_data = {'source_path': str(source), 'source_sha256': studio.digest(source), 'sample_rate': RATE,
                          'frames': 5 * RATE, 'archive_path': None, 'archive_sha256': None, 'track': TRACK}
            ref = root / 'r.wav'
            write_wav(ref, 2.0)
            synth = batch.omnivoice_synthesizer(root, report, track_data, python_path=sys.executable,
                                                script_path=runner, timeout=30)
            item = {'gid': 'CAS:1', 'start': 0.5, 'end': 3.5, 'en_text': 'Hi there.', 'pt_text': 'Oi aí.'}
            with self.assertRaisesRegex(RuntimeError, 'GPU fora do limite'):
                synth(item, {'path': str(ref)}, root / 'o.part.wav')


if __name__ == '__main__':
    unittest.main()


class HelpersTests(unittest.TestCase):
    def test_select_tracks_e_tabela(self):
        plans = batch.plan_all(REPORT, lookup, {TRACK: 24.0})
        # Track_008 não tem tradução: 'todas' só inclui faixas com algo a gerar
        self.assertEqual(batch.select_tracks(plans), [TRACK])
        self.assertEqual(batch.select_tracks(plans, 'todas'), [TRACK])
        self.assertEqual(batch.select_tracks(plans, '7'), [TRACK])
        self.assertEqual(batch.select_tracks(plans, 'Track_007, 8'), [TRACK, 'Track_008'])
        with self.assertRaisesRegex(ValueError, 'Track_099'):
            batch.select_tracks(plans, '99')
        table = batch.format_plan_table(plans)
        self.assertIn('Track_007', table)
        self.assertIn('TOTAL', table.splitlines()[-1])
        self.assertIn('CASINO', table)

    def test_fabrica_recebe_os_dados_da_faixa_decodificada(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / (TRACK + '.wav')
            write_wav(source, 6.0, rate=RATE, channels=2)
            track = {'path': source, 'track': TRACK, 'sha256': studio.digest(source),
                     'archive_sha256': None, 'archive_path': None}
            seen = []

            def factory(track_data):
                seen.append(track_data['frames'])
                return lambda item, ref, out, cancel=None: write_wav(out, (item['end'] - item['start']) * .9, amplitude=.3)
            factory.is_factory = True
            items = batch.plan_track({'records': [rec(1, 'A', 1.0, 3.0)]}, TRACK, lookup, 6.0)
            result = batch.run_track(root, {'sources': {}}, track, items, factory)
            self.assertEqual(seen, [6 * RATE])
            self.assertEqual(result['items'][0]['outcome'], 'OK')

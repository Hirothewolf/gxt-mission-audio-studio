#!/usr/bin/env python3
"""Lote de cutscenes sem interface.

  python tools/cutscene_batch.py plan                    # mostra o plano de todas as faixas
  python tools/cutscene_batch.py run --faixas 16,17      # gera só essas faixas
  python tools/cutscene_batch.py run                     # gera todas as faixas com falas geráveis

Usa o GXT do mod indicado em --gxt (padrão: o mais recente aberto no editor). Não altera o jogo nem o GXT.
"""
import argparse
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio import config  # noqa: E402
from studio.audio import cutscene_batch as batch  # noqa: E402
from studio.audio import cutscene_studio as audio  # noqa: E402
from studio.gxt.model import Gxt  # noqa: E402
from studio.script.native_index import load_current  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('comando', choices=['plan', 'run'])
    ap.add_argument('--gxt', help='GXT do mod com a tradução (padrão: o mais recente do editor)')
    ap.add_argument('--faixas', default='todas', help="'todas' ou lista: 16,17 ou Track_016")
    ap.add_argument('--duracao', choices=['janela', 'livre'], default='janela')
    ap.add_argument('--velocidade', type=float, default=1.0)
    args = ap.parse_args(argv)
    project, game = Path(config.get_project_root()).resolve(), config.CFG.get('game_dir')
    gxt_path = args.gxt or next((r for r in (config.CFG.get('recentes') or []) if Path(r).is_file()), None)
    if not game or not gxt_path:
        ap.error('Informe a pasta do jogo no editor e use --gxt para indicar o GXT do mod.')
    report = load_current(project, game)
    if not report:
        ap.error('Índice nativo indisponível: atualize-o no editor (Jogo → Guia do projeto).')
    native_info = report['sources']['gxt']
    policy = batch.Policy(duration_mode=args.duracao, speed=args.velocidade)
    lookup = batch.make_text_lookup(Gxt(native_info['path']), Gxt(gxt_path))
    plans = batch.plan_all(report, lookup, None, policy, (str(gxt_path), ''))
    print(batch.format_plan_table(plans))
    if args.comando == 'plan':
        return 0
    names = batch.select_tracks(plans, args.faixas)
    ok, reason = batch.omnivoice_disponivel(project)
    if not ok:
        print('OmniVoice indisponível:', reason, file=sys.stderr)
        return 2
    tracks = {t['track']: t for t in audio.list_tracks(project, report)}
    factory, results, cancel = batch.omnivoice_factory(project, report, policy), [], threading.Event()
    try:
        for name in names:
            result = batch.run_track(project, report, tracks[name], plans[name], factory, policy, cancel,
                                     lambda done, total, msg: print('  ' + msg, flush=True))
            results.append(result)
            problems = batch.exceptions(result)
            print('%s: %d substituídas, %d exceções. Faixa: %s' % (
                name, sum(r['outcome'] in ('OK', 'WARN') for r in result['items']), len(problems), result['render']))
    except KeyboardInterrupt:
        cancel.set()
        print('Interrompido. O que já foi gerado é reaproveitado na próxima execução.')
    if results:
        print('Resumo:', batch.write_summary_csv(project, results))
    return 0


if __name__ == '__main__':
    sys.exit(main())

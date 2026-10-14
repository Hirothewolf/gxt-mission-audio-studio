# -*- coding: utf-8 -*-
"""
Cliente de execução do SAAT (San Andreas Audio Toolkit) - Somente leitura e exportação.
"""
import subprocess
import threading
import time
from pathlib import Path
from studio.core.utils import CREATE_FLAGS, matar_arvore, nome_insens


class SaatClient:
    """Único ponto que conhece a linha de comando do SAAT. Só EXPORTA (leitura); nunca importa nada no jogo."""

    def __init__(self, game_dir, exe_sfx=None, exe_stream=None, timeout=3600):
        self.game = Path(game_dir)
        self.exe_sfx = Path(exe_sfx) if exe_sfx else None
        self.exe_stream = Path(exe_stream) if exe_stream else None
        self.timeout = timeout
        self.proc = None

    def comando_export_sfx(self, pak, saida, relativo=False, source=None, bank=None):
        if not self.exe_sfx:
            raise FileNotFoundError('saat_sfx não encontrado')
        if source is not None:
            bank_path = Path(bank) if bank else None
            if not bank_path or not bank_path.is_file():
                audio_dir = nome_insens(self.game, 'audio') or (self.game / 'audio')
                cfg_dir = nome_insens(audio_dir, 'CONFIG') or (audio_dir / 'CONFIG')
                bank_path = nome_insens(cfg_dir, 'BankLkup.dat') or (cfg_dir / 'BankLkup.dat')
            return [str(self.exe_sfx), '-e', str(source), str(saida), str(bank_path)]
        if relativo:
            return [str(self.exe_sfx), '-e', 'audio\\SFX\\%s' % pak, str(saida), 'audio\\CONFIG\\BankLkup.dat']
        audio_dir = nome_insens(self.game, 'audio') or (self.game / 'audio')
        sfx_dir = nome_insens(audio_dir, 'SFX') or (audio_dir / 'SFX')
        cfg_dir = nome_insens(audio_dir, 'CONFIG') or (audio_dir / 'CONFIG')
        script = nome_insens(sfx_dir, pak) or (sfx_dir / pak)
        bank = nome_insens(cfg_dir, 'BankLkup.dat') or (cfg_dir / 'BankLkup.dat')
        return [str(self.exe_sfx), '-e', str(script), str(saida), str(bank)]

    def comando_export_stream(self, nome, saida, relativo=False):
        if not self.exe_stream:
            raise FileNotFoundError('saat_stream não encontrado')
        if relativo:
            return [str(self.exe_stream), '-e', 'audio\\streams\\%s' % nome, str(saida)]
        audio_dir = nome_insens(self.game, 'audio') or (self.game / 'audio')
        strm_dir = nome_insens(audio_dir, 'streams') or (audio_dir / 'streams')
        return [str(self.exe_stream), '-e', str(strm_dir / nome), str(saida)]

    def _executar(self, cmd, cwd, on_line, cancel):
        linhas = []
        t0 = time.time()
        self.proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', creationflags=CREATE_FLAGS
        )
        proc = self.proc

        def vigia():
            while proc.poll() is None:
                if (cancel is not None and cancel.is_set()) or time.time() - t0 > self.timeout:
                    matar_arvore(proc)
                    return
                time.sleep(0.2)

        threading.Thread(target=vigia, daemon=True).start()
        for l in proc.stdout:
            l = l.rstrip()
            if l:
                linhas.append(l)
                if on_line:
                    on_line(l)
        proc.wait()
        return proc.returncode, linhas

    def exportar_sfx(self, pak, saida, on_line=None, cancel=None, source=None, bank=None):
        """Tenta relativo com cwd=jogo e absoluto com cwd=pasta do SAAT."""
        saida = Path(saida)
        saida.mkdir(parents=True, exist_ok=True)
        if source is not None:
            tentativas = [(self.comando_export_sfx(pak, saida, source=source, bank=bank), self.exe_sfx.parent)]
        else:
            tentativas = [
                (self.comando_export_sfx(pak, saida, relativo=True), self.game),
                (self.comando_export_sfx(pak, saida, relativo=False), self.exe_sfx.parent)
            ]
        ultimo = {'ok': False, 'rc': None, 'cmd': [], 'log': [], 'wavs': 0}
        for cmd, cwd in tentativas:
            if cancel is not None and cancel.is_set():
                break
            if on_line:
                on_line('$ (%s)> %s' % (cwd, ' '.join('"%s"' % c if ' ' in c else c for c in cmd)))
            try:
                rc, log = self._executar(cmd, cwd, on_line, cancel)
            except OSError as e:
                ultimo = {'ok': False, 'rc': None, 'cmd': cmd, 'log': [str(e)], 'wavs': 0}
                continue
            n = sum(1 for _ in saida.rglob('*') if _.suffix.lower() == '.wav')
            ultimo = {'ok': rc == 0 and n > 0, 'rc': rc, 'cmd': cmd, 'log': log[-12:], 'wavs': n,
                      'cancelado': bool(cancel is not None and cancel.is_set())}
            if ultimo['ok']:
                break
            if ultimo['cancelado']:
                break
        return ultimo

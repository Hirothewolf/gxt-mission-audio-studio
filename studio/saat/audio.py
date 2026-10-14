# -*- coding: utf-8 -*-
"""
Gerenciamento de cache do áudio original do jogo e integração segura com o ModLoader.
"""
import shutil
import hashlib
import json
import uuid
import os
import tempfile
import subprocess
import configparser
import wave
import contextlib
from datetime import datetime
from pathlib import Path

from studio.config import PROJ_ROOT  # Compatibility export used by existing test and UI patches.
from studio.core.utils import sha256_arquivo, escrever_atomico, json_atomico, matar_arvore
from studio.audio.wav import wav_info, converter_wav_pcm16_mono
from studio.script.mapping import ref_do_registro
from studio.saat.detection import PAKS, validar_pasta_jogo, achar_saat, varrer_sfx, caminho_relativo_sfx
from studio.saat.client import SaatClient

MOD_PADRAO = 'PTBR_Dublagem'


class GameAudio:
    """Coordena o áudio original do jogo via SAAT (somente leitura) e a instalação no ModLoader."""

    def __init__(self, cfg, cfg_save, cache_root, backup_root, manifest_path, log=None):
        self.cfg, self.cfg_save = cfg, cfg_save
        self.cache_root, self.backup_root = Path(cache_root), Path(backup_root)
        self.manifest_path = Path(manifest_path)
        self.log = log or (lambda *a, **k: None)
        self.info = validar_pasta_jogo(cfg.get('game_dir'))
        self.indice = {}  # (PAK, b, s) -> WAV original extraído do jogo
        self.indice_mod = {}
        self.instalados = {}  # (PAK, b, s) -> [(mod, Path)]
        self._ml_escaneado = False
        if self.info['ok']:
            self.carregar_indice()

    @property
    def game_dir(self):
        return self.info['pasta'] if self.info['ok'] else None

    def definir_pasta(self, p):
        self.info = validar_pasta_jogo(p)
        if self.info['ok']:
            self.cfg['game_dir'] = str(p)
            self.cfg_save()
            self.indice, self.instalados, self._ml_escaneado = {}, {}, False
            self.carregar_indice()
        return self.info

    def saat(self):
        ex = achar_saat(self.game_dir, [self.cfg.get('saat_dir')])
        if self.cfg.get('saat_sfx') and Path(self.cfg['saat_sfx']).is_file():
            ex['sfx'] = Path(self.cfg['saat_sfx'])
        if self.cfg.get('saat_stream') and Path(self.cfg['saat_stream']).is_file():
            ex['stream'] = Path(self.cfg['saat_stream'])
        return ex

    def cache_dir(self):
        slug = hashlib.sha1(str(self.game_dir).lower().encode('utf-8')).hexdigest()[:10] if self.game_dir else 'sem_jogo'
        return self.cache_root / slug

    def cache_dir_mod(self, mod=None):
        mod = mod or self.mod_ativo()
        slug = hashlib.sha1(str(mod).lower().encode('utf-8')).hexdigest()[:10] if mod else 'sem_mod'
        return self.cache_root.parent / 'saat_mod' / slug

    def _arquivo_pak_jogo(self, pak):
        from studio.core.utils import nome_insens
        audio = nome_insens(self.game_dir, 'audio') or (self.game_dir / 'audio')
        sfx = nome_insens(audio, 'SFX') or (audio / 'SFX')
        return nome_insens(sfx, pak) or (sfx / pak)

    def _arquivo_pak_mod(self, mod, pak):
        from studio.core.utils import nome_insens
        mod_dir = self.modloader_dir() / mod
        audio = nome_insens(mod_dir, 'audio') or (mod_dir / 'audio')
        sfx = nome_insens(audio, 'SFX') or (audio / 'SFX')
        return nome_insens(sfx, pak) or (sfx / pak)

    def _bank_mod(self, mod):
        from studio.core.utils import nome_insens
        mod_dir = self.modloader_dir() / mod
        audio = nome_insens(mod_dir, 'audio') or (mod_dir / 'audio')
        cfg = nome_insens(audio, 'CONFIG') or (audio / 'CONFIG')
        bank = nome_insens(cfg, 'BankLkup.dat') if cfg.is_dir() else None
        return bank if bank and bank.is_file() else self.info.get('banklkup')

    def _assinatura_fonte(self, pak='SCRIPT'):
        sig = {}
        archive = self._arquivo_pak_jogo(pak) if self.game_dir else self.info.get('script')
        for nome, p in ((str(pak).upper(), archive), ('BankLkup.dat', self.info.get('banklkup'))):
            try:
                st = Path(p).stat()
                sig[nome] = [st.st_size, st.st_mtime_ns]
            except (OSError, TypeError):
                sig[nome] = None
        return sig

    def _assinatura_fonte_mod(self, mod, pak='SCRIPT'):
        """Signature for the exact ModLoader archive and lookup table we export."""
        if not self.game_dir:
            return None
        script_mod = self._arquivo_pak_mod(mod, pak)
        bank_mod = self._bank_mod(mod)
        if not script_mod.is_file():
            return None

        def file_signature(path):
            try:
                path = Path(path).resolve()
                st = path.stat()
                return [str(path), st.st_size, st.st_mtime_ns]
            except (OSError, TypeError):
                return None

        return {
            'mod': str(mod),
            'pak': str(pak).upper(),
            str(pak).upper(): file_signature(script_mod),
            'BankLkup.dat': file_signature(bank_mod),
        }

    def _cache_meta_path(self, pak, mod=None):
        base = self.cache_dir_mod(mod) if mod else self.cache_dir()
        return base / ('cache_meta_%s.json' % str(pak).upper())

    def _ler_meta(self, pak, mod=None):
        paths = [self._cache_meta_path(pak, mod)]
        if str(pak).upper() == 'SCRIPT':
            paths.append((self.cache_dir_mod(mod) if mod else self.cache_dir()) / 'cache_meta.json')
        for path in paths:
            try:
                meta = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(meta, dict):
                    return meta
            except (OSError, ValueError):
                pass
        return None

    def _cache_original_atual(self, pak='SCRIPT'):
        expected = self._assinatura_fonte(pak)
        if expected.get(str(pak).upper()) is None:
            return False
        meta = self._ler_meta(pak)
        return bool(meta and str(meta.get('pak', '')).upper() == str(pak).upper()
                    and meta.get('fonte') == expected)

    def _cache_mod_atual(self, mod, pak='SCRIPT'):
        expected = self._assinatura_fonte_mod(mod, pak)
        if expected is None:
            return False
        try:
            meta = self._ler_meta(pak, mod)
        except (OSError, ValueError):
            return False
        return (isinstance(meta, dict) and meta.get('mod') == str(mod)
                and str(meta.get('pak', '')).upper() == str(pak).upper()
                and meta.get('fonte') == expected)

    def estado_cache(self, pak='SCRIPT'):
        meta = self._ler_meta(pak)
        indexado = bool(meta and str(meta.get('pak', 'SCRIPT' if pak == 'SCRIPT' else '')).upper() == str(pak).upper())
        wavs = sum(1 for ref in self.indice if ref[0] == str(pak).upper()) if self._cache_original_atual(pak) else 0
        return {'indexado': indexado, 'desatualizado': bool(indexado and meta.get('fonte') != self._assinatura_fonte(pak)),
                'wavs': wavs,
                'wavs_mod': len(self.indice_mod)}

    def carregar_indice(self, mod_nome=None, pak='SCRIPT'):
        self.indice = {}
        for archive in PAKS:
            if self._cache_original_atual(archive):
                self.indice.update(varrer_sfx(self.cache_dir() / 'export' / archive))
        self.indice_mod = {}
        mod = mod_nome or self.mod_ativo()
        for archive in PAKS:
            if self._cache_mod_atual(mod, archive):
                self.indice_mod.update(varrer_sfx(self.cache_dir_mod(mod) / 'export' / archive))
        return len(self.indice)

    def available_archives(self, target='original'):
        target = str(target).lower()
        if target == 'original':
            base = self._arquivo_pak_jogo('SCRIPT').parent if self.game_dir else None
        elif target == 'mod':
            mod = self.mod_ativo()
            base = self._arquivo_pak_mod(mod, 'SCRIPT').parent if self.game_dir else None
        else:
            raise ValueError("target deve ser 'original' ou 'mod'")
        if not base or not base.is_dir():
            return []
        from studio.core.utils import nome_insens
        return [pak for pak in PAKS if (nome_insens(base, pak) or base / pak).is_file()]

    @staticmethod
    def _dentro(base, path):
        base, path = Path(base).resolve(), Path(path).resolve()
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False

    def _publicar_pak(self, stage_pak, export_root, pak, meta_path, meta):
        """Replace one archive and its provenance together, restoring both on failure."""
        export_root, stage_pak = Path(export_root), Path(stage_pak)
        base = export_root.parent.resolve()
        dest = export_root / str(pak).upper()
        meta_path = Path(meta_path)
        if (not self._dentro(base, export_root) or not self._dentro(base, dest)
                or not self._dentro(base, stage_pak) or not self._dentro(base, meta_path)):
            raise ValueError('Caminho de publicação fora do cache permitido.')
        if not stage_pak.is_dir() or not any(p.is_file() and p.suffix.lower() == '.wav' for p in stage_pak.rglob('*')):
            raise ValueError('A exportação não contém WAVs válidos para este PAK.')
        export_root.mkdir(parents=True, exist_ok=True)
        backup = export_root / ('.%s.old-%s' % (pak, uuid.uuid4().hex))
        old_meta = meta_path.read_bytes() if meta_path.is_file() else None
        had_dest = dest.exists()
        if had_dest:
            if not self._dentro(base, dest):
                raise ValueError('Destino existente fora do cache permitido.')
            dest.rename(backup)
        try:
            stage_pak.rename(dest)
            json_atomico(meta_path, meta)
        except Exception:
            if dest.exists():
                shutil.rmtree(dest)
            if had_dest and backup.exists():
                backup.rename(dest)
            if old_meta is None:
                try:
                    meta_path.unlink()
                except FileNotFoundError:
                    pass
            else:
                escrever_atomico(meta_path, old_meta)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    def indexar(self, pak='SCRIPT', on_line=None, cancel=None, forcar=False):
        """Exporta um PAK do jogo para cache isolado por arquivo de origem."""
        return self._indexar_um(str(pak).upper(), 'original', on_line, cancel, forcar)

    def _indexar_um(self, pak, alvo, on_line=None, cancel=None, forcar=False, mod_nome=None):
        if pak not in PAKS:
            return {'ok': False, 'motivo': 'PAK desconhecido: %s' % pak}
        if not self.info['ok']:
            return {'ok': False, 'motivo': 'Pasta do jogo inválida: ' + ', '.join(self.info['faltando'])}
        ex = self.saat()
        if not ex['sfx']:
            return {'ok': False, 'motivo': 'saat_sfx.exe não encontrado.'}
        mod = mod_nome or self.mod_ativo()
        source = self._arquivo_pak_jogo(pak) if alvo == 'original' else self._arquivo_pak_mod(mod, pak)
        if not source.is_file():
            return {'ok': False, 'motivo': ('Arquivo base' if alvo == 'original' else 'Arquivo do mod') +
                    ' audio/SFX/%s não encontrado.' % pak, 'ausente': True}
        if alvo == 'original':
            cache_base = self.cache_dir()
            fresh = self._cache_original_atual(pak)
            meta_path = self._cache_meta_path(pak)
            bank = self.info.get('banklkup')
        else:
            cache_base = self.cache_dir_mod(mod)
            fresh = self._cache_mod_atual(mod, pak)
            meta_path = self._cache_meta_path(pak, mod)
            bank = self._bank_mod(mod)
        current = varrer_sfx(cache_base / 'export' / pak)
        if fresh and current and not forcar:
            if alvo == 'original':
                self.carregar_indice()
            else:
                self.carregar_indice(mod)
            return {'ok': True, 'motivo': 'cache já está atualizado', 'wavs': len(current), 'pulado': True}
        run_root = cache_base / 'runs' / uuid.uuid4().hex
        stage_export = run_root / 'export'
        stage_export.mkdir(parents=True, exist_ok=True)
        cli = SaatClient(self.game_dir, ex['sfx'], ex['stream'])
        self._cli = cli
        try:
            r = cli.exportar_sfx(pak, stage_export, on_line=on_line, cancel=cancel,
                                 source=source if alvo == 'mod' else None, bank=bank if alvo == 'mod' else None)
            stage_pak = stage_export / pak
            if not stage_pak.is_dir() and varrer_sfx(stage_export):
                stage_pak.mkdir()
                for wav in list(stage_export.rglob('*.wav')):
                    relative = wav.relative_to(stage_export)
                    if relative.parts[0].upper() == pak:
                        continue
                    target = stage_pak / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(wav), str(target))
            staged = varrer_sfx(stage_pak)
            valid = [p for p in stage_pak.rglob('*.wav') if wav_info(p)]
            if r.get('cancelado') or (cancel is not None and cancel.is_set()):
                return {'ok': False, 'cancelado': True, 'motivo': 'Operação cancelada'}
            if not r.get('ok') or r.get('rc') != 0 or not staged or not valid:
                return dict(r, ok=False, motivo=r.get('motivo') or 'SAAT falhou ou não gerou WAVs válidos.')
            signature = self._assinatura_fonte(pak) if alvo == 'original' else self._assinatura_fonte_mod(mod, pak)
            meta = {'schema_version': 2, 'jogo': str(self.game_dir), 'pak': pak,
                    'fonte': signature, 'criado_em': datetime.now().isoformat(timespec='seconds'),
                    'comando': r.get('cmd'), 'wavs': len(staged)}
            if alvo == 'mod':
                meta['mod'] = str(mod)
            self._publicar_pak(stage_pak, cache_base / 'export', pak, meta_path, meta)
            if alvo == 'original':
                self.carregar_indice()
            else:
                self.carregar_indice(mod)
            return dict(r, ok=True, wavs=len(staged))
        except Exception as e:
            return {'ok': False, 'motivo': str(e)}
        finally:
            if self._dentro(cache_base, run_root) and run_root.exists():
                shutil.rmtree(run_root, ignore_errors=True)

    def indexar_mod(self, mod_nome=None, pak='SCRIPT', on_line=None, cancel=None, forcar=False):
        """Exporta um PAK do mod via SAAT; WAVs soltos continuam aceitos."""
        mod = mod_nome or self.mod_ativo()
        source = self._arquivo_pak_mod(mod, str(pak).upper()) if self.game_dir else Path()
        if not source.is_file():
            loose = varrer_sfx(source.parent)
            if loose:
                self.indice_mod.update(loose)
                return {'ok': True, 'motivo': 'Mod já possui WAVs soltos', 'wavs': len(loose), 'pulado': True}
        return self._indexar_um(str(pak).upper(), 'mod', on_line, cancel, forcar, mod)

    def indexar_arquivos(self, paks, alvo='jogo', on_line=None, cancel=None, forcar=False):
        """Indexa PAKs selecionados e publica separadamente cada arquivo de origem."""
        alvo_map = {'jogo': 'original', 'mod': 'mod', 'ambos': 'ambos'}
        if alvo not in alvo_map:
            return {'ok': False, 'motivo': 'alvo deve ser jogo, mod ou ambos', 'resultados': {}}
        requested = list(dict.fromkeys(str(p).upper() for p in paks))
        if any(p not in PAKS for p in requested):
            return {'ok': False, 'motivo': 'A lista contém PAK desconhecido.', 'resultados': {}}
        results = {}
        for i, pak in enumerate(requested, 1):
            if cancel is not None and cancel.is_set():
                break
            if on_line:
                on_line('[%d/%d] %s — %s' % (i, len(requested), pak, alvo))
            row = {}
            if alvo_map[alvo] in ('original', 'ambos'):
                row['jogo'] = self.indexar(pak, on_line, cancel, forcar)
            if alvo_map[alvo] in ('mod', 'ambos') and not (cancel is not None and cancel.is_set()):
                row['mod'] = self.indexar_mod(self.mod_ativo(), pak, on_line, cancel, forcar)
                if row['mod'].get('ausente'):
                    row['mod']['pulado'] = True
            results[pak] = row
        self.carregar_indice()
        ok = not (cancel is not None and cancel.is_set()) and all(
            all(x.get('ok') or (x.get('ausente') and x.get('pulado')) for x in row.values())
            for row in results.values())
        return {'ok': ok, 'cancelado': bool(cancel is not None and cancel.is_set()), 'resultados': results,
                'wavs_jogo': len(self.indice), 'wavs_mod': len(self.indice_mod)}

    def indexar_ambos(self, on_line=None, cancel=None, forcar=False):
        """Extrai tanto o áudio do jogo base quanto o áudio do mod ativo usando o SAAT."""
        if on_line:
            on_line('=======================================================================\n')
            on_line(' [1/2] EXTRAINDO ÁUDIO ORIGINAL DO JOGO BASE (SOMENTE LEITURA)\n')
            on_line('=======================================================================\n')
        r1 = self.indexar('SCRIPT', on_line=on_line, cancel=cancel, forcar=forcar)
        if cancel and cancel.is_set():
            return {'ok': False, 'motivo': 'Operação cancelada'}
        if on_line:
            on_line('\n=======================================================================\n')
            on_line(f' [2/2] EXTRAINDO ÁUDIO DO MOD NO MODLOADER: {self.mod_ativo()}\n')
            on_line('=======================================================================\n')
        r2 = self.indexar_mod(self.mod_ativo(), 'SCRIPT', on_line=on_line, cancel=cancel, forcar=forcar)
        self.carregar_indice()
        return {
            'ok': r1.get('ok') and r2.get('ok'),
            'r_jogo': r1,
            'r_mod': r2,
            'wavs_jogo': len(self.indice),
            'wavs_mod': len(self.indice_mod),
        }

    def cancelar(self):
        matar_arvore(getattr(getattr(self, '_cli', None), 'proc', None))
        matar_arvore(getattr(self, '_cli_proc', None))

    def ref_para(self, ainfo):
        return ref_do_registro(ainfo)

    def original_para(self, ainfo):
        ref = self.ref_para(ainfo)
        if not ref or not self._cache_original_atual(ref[0]):
            return None
        return self.indice.get(ref)

    def modloader_dir(self):
        return (self.game_dir / 'modloader') if self.game_dir else None

    def mods(self):
        ml = self.modloader_dir()
        if not ml or not ml.is_dir():
            return []
        return sorted(d.name for d in ml.iterdir() if d.is_dir() and not d.name.startswith('.'))

    def destino_mod(self, mod, pak, banco, som):
        return self.modloader_dir() / mod / caminho_relativo_sfx(pak, banco, som)

    def escanear_modloader(self):
        self.instalados = {}
        for mod in self.mods():
            base = self.modloader_dir() / mod
            sfx = base / 'audio' / 'SFX'
            if not sfx.is_dir():
                from studio.core.utils import nome_insens
                audio = nome_insens(base, 'audio')
                sfx = nome_insens(audio, 'SFX') if audio else None
            if not sfx or not Path(sfx).is_dir():
                continue
            for k, p in varrer_sfx(sfx).items():
                self.instalados.setdefault(k, []).append((mod, p))
        self._ml_escaneado = True
        return len(self.instalados)

    def mod_ativo(self):
        m = self.cfg.get('modloader_mod')
        mods = self.mods()
        if m and m in mods:
            return m
        for mod in mods:
            if self.modloader_dir() and (self.modloader_dir() / mod / 'text' / 'american.gxt').is_file():
                return mod
        return m or (mods[0] if mods else MOD_PADRAO)

    def instalados_para_mod(self, mod=None):
        mod = mod or self.mod_ativo()
        res = []
        for k, lst in self.instalados.items():
            for m, p in lst:
                if m == mod:
                    res.append((k, p))
        return res

    def instalados_para(self, ainfo):
        if not self._ml_escaneado:
            self.escanear_modloader()
        ref = self.ref_para(ainfo)
        return self.instalados.get(ref, []) if ref else []

    def instalado_nosso(self, ainfo):
        mod = self.mod_ativo()
        if any(m == mod for m, _ in self.instalados_para(ainfo)):
            return True
        mod_dir = self.modloader_dir() / mod if self.modloader_dir() else None
        if mod_dir and (mod_dir / 'audio' / 'SFX' / 'SCRIPT').is_file():
            return True
        return False

    def instalado_para(self, ainfo, mod=None):
        lst = self.instalados_para(ainfo)
        mod = mod or self.mod_ativo()
        for m, p in lst:
            if m == mod:
                return p
        ref = self.ref_para(ainfo)
        if ref and self._cache_mod_atual(mod, ref[0]):
            cache_wav = (self.cache_dir_mod(mod) / 'export' / ref[0]
                         / ('Bank_%03d' % ref[1]) / ('sound_%03d.wav' % ref[2]))
            if cache_wav.is_file():
                return cache_wav
        return None

    def _manifest(self):
        try:
            import json
            m = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            if isinstance(m, dict) and isinstance(m.get('mods'), dict):
                return m
        except (OSError, ValueError):
            pass
        return {'schema_version': 1, 'mods': {}}

    def estado_instalacao(self, ainfo, dub_wav=None):
        lst = self.instalados_para(ainfo)
        mod = self.mod_ativo()
        ref = self.ref_para(ainfo)
        if ref and ref in self.indice_mod and Path(self.indice_mod[ref]).is_file():
            return f'extraído/disponível do mod ({mod}) ✓'
        if not lst:
            mod_dir = self.modloader_dir() / mod if self.modloader_dir() else None
            if mod_dir and (mod_dir / 'audio' / 'SFX' / 'SCRIPT').is_file():
                return f'embutido no SCRIPT do mod ({mod})'
            return f'não instalado em {mod}'
        nosso = [(m, p) for m, p in lst if m == mod]
        outros = [m for m, _ in lst if m != mod]
        if not nosso:
            return 'outro mod: ' + ', '.join(outros)
        ref = self.ref_para(ainfo)
        rel = str(caminho_relativo_sfx(*ref)).replace('\\', '/')
        ent = self._manifest()['mods'].get(mod, {}).get('arquivos', {}).get(rel)
        try:
            atual = sha256_arquivo(nosso[0][1])
        except OSError:
            return 'instalado (ilegível)'
        if ent and atual != ent.get('sha256_instalado'):
            return 'modificado fora do programa ⚠'
        if ent and dub_wav and Path(dub_wav).is_file() and sha256_arquivo(dub_wav) != ent.get('sha256_fonte'):
            return 'instalado, mas a dublagem mudou depois ⚠'
        return 'instalado ✓' + (' (+ também em: %s)' % ', '.join(outros) if outros else '')

    def info_linha(self, ainfo, dub_wav=None):
        if not ainfo:
            return []
        ref = self.ref_para(ainfo)
        partes = []
        if ref:
            partes.append('Jogo: %s/Bank_%03d/sound_%03d%s' % (
                ref[0], ref[1], ref[2], '' if ref in self.indice else ' (não extraído)'))
        else:
            partes.append('Jogo: banco/som não identificado no mapeamento')
        if self.game_dir and ref:
            partes.append('ModLoader: ' + self.estado_instalacao(ainfo, dub_wav))
        return partes

    def planejar(self, itens, mod=None):
        mod = mod or self.cfg.get('modloader_mod') or MOD_PADRAO
        plano = []
        for it in itens:
            ref, dub = it.get('ref'), it.get('dub')
            e = {'rotulo': it.get('rotulo', ''), 'ref': ref, 'dub': dub, 'problemas': [], 'acao': None, 'destino': None}
            if not ref:
                e['problemas'].append('banco/som não identificado')
            if not dub or not Path(dub).is_file():
                e['problemas'].append('sem WAV dublado')
            if not self.game_dir:
                e['problemas'].append('pasta do jogo não configurada')
            if not e['problemas']:
                orig = self.indice.get(ref)
                info = wav_info(dub)
                if not info:
                    e['problemas'].append('WAV dublado ilegível')
                else:
                    e['taxa_orig'] = (wav_info(orig) or {}).get('taxa') if orig else None
                    e['precisa_converter'] = not (info['fmt'] == 1 and info['bits'] == 16 and info['canais'] == 1
                                                  and (not e['taxa_orig'] or e['taxa_orig'] == info['taxa']))
                    e['dur_dub'] = info['dur']
                    e['dur_orig'] = (wav_info(orig) or {}).get('dur') if orig else None
                    archive = self._arquivo_pak_mod(mod, ref[0])
                    if archive.is_file():
                        # A packed PAK shadows this path. Rebuild the archive from
                        # its own current contents instead of creating a loose overlay.
                        e['destino'] = archive
                        e['acao'] = 'reempacotar_nativo'
                    elif archive.is_dir():
                        dest = self.destino_mod(mod, *ref)
                        e['destino'] = dest
                        e['acao'] = 'substituir' if dest.exists() else 'novo'
                    else:
                        e['problemas'].append('PAK nativo do mod não encontrado; não será criado WAV solto')
                    if e['dur_orig'] and e['dur_dub'] > max(e['dur_orig'] * 1.5, e['dur_orig'] + 1.5):
                        e['avisos'] = ['dublagem %.2fs vs original %.2fs (muito mais longa)' % (e['dur_dub'], e['dur_orig'])]
            plano.append(e)
        return plano

    def instalar(self, plano, mod=None, on_progress=None):
        mod = mod or self.cfg.get('modloader_mod') or MOD_PADRAO
        if not self.game_dir:
            return {'ok': False, 'motivo': 'pasta do jogo não configurada'}
        native = [e for e in plano if not e['problemas'] and e.get('acao') == 'reempacotar_nativo']
        loose = [e for e in plano if not e['problemas'] and e.get('acao') != 'reempacotar_nativo']
        if native:
            result = self._instalar_native(native, mod, on_progress)
            if not result.get('ok') or not loose:
                return result
        if not loose:
            return {'ok': True, 'instalados': 0, 'mod': mod}
        plano = loose
        carimbo = datetime.now().strftime('%Y%m%d_%H%M%S')
        man = self._manifest()
        entrada_mod = man['mods'].setdefault(mod, {'arquivos': {}})
        feitos = []
        try:
            for i, e in enumerate(plano):
                if e['problemas'] or not e.get('destino'):
                    continue
                dest = Path(e['destino'])
                dados = converter_wav_pcm16_mono(e['dub'], e.get('taxa_orig')) if e.get('precisa_converter') \
                    else Path(e['dub']).read_bytes()
                rel = str(dest.relative_to(self.modloader_dir() / mod)).replace('\\', '/')
                backup = None
                if dest.exists():
                    if sha256_arquivo(dest) == hashlib.sha256(dados).hexdigest():
                        continue
                    backup = self.backup_root / carimbo / mod / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, backup)
                antigo = entrada_mod['arquivos'].get(rel)
                escrever_atomico(dest, dados)
                feitos.append((dest, backup, rel, antigo))
                entrada_mod['arquivos'][rel] = {
                    'rotulo': e['rotulo'], 'sha256_fonte': sha256_arquivo(e['dub']),
                    'sha256_instalado': hashlib.sha256(dados).hexdigest(),
                    'backup': str(backup) if backup else None, 'instalado_em': datetime.now().isoformat(timespec='seconds')}
                if on_progress:
                    on_progress(i + 1, len(plano), e['rotulo'])
            json_atomico(self.manifest_path, man)
        except Exception as ex:
            for dest, backup, rel, antigo in reversed(feitos):
                try:
                    if backup and backup.exists():
                        shutil.copy2(backup, dest)
                    elif dest.exists():
                        dest.unlink()
                except OSError:
                    pass
                if antigo is None:
                    entrada_mod['arquivos'].pop(rel, None)
                else:
                    entrada_mod['arquivos'][rel] = antigo
            return {'ok': False, 'motivo': 'falhou e foi desfeito: %s' % ex, 'instalados': 0}
        self._ml_escaneado = False
        return {'ok': True, 'instalados': len(feitos), 'mod': mod, 'backup': str(self.backup_root / carimbo)}

    @staticmethod
    def _pcm_signature(path):
        with wave.open(str(path), 'rb') as wav:
            params = wav.getparams()
            pcm = wav.readframes(params.nframes)
        if len(pcm) != params.nframes * params.nchannels * params.sampwidth:
            raise ValueError('WAV truncado: %s' % path)
        return (params.framerate, params.nchannels, params.sampwidth, params.nframes,
                hashlib.sha256(pcm).hexdigest())

    @contextlib.contextmanager
    def _native_install_lock(self):
        project = Path(self.cfg.get('project_root') or self.manifest_path.parent.parent)
        lock_path = project / 'reports' / 'native_audio_install.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open('a+b')
        stream.seek(0)
        if not stream.read(1):
            stream.seek(0); stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield lock_path
        finally:
            stream.close()

    def _game_is_running(self):
        if os.name != 'nt':
            raise RuntimeError('A verificação segura do GTA requer Windows.')
        result = subprocess.run(['powershell', '-NoProfile', '-Command',
            'if (Get-Process gta_sa -ErrorAction SilentlyContinue) { exit 3 }'],
            capture_output=True, timeout=15)
        if result.returncode == 0:
            return False
        if result.returncode == 3:
            return True
        detail = (result.stderr or result.stdout or b'').decode('utf-8', 'replace')[-1000:]
        raise RuntimeError('Não foi possível confirmar se o GTA está fechado (PowerShell %d): %s' %
                           (result.returncode, detail))

    def _safe_mod_dir(self, mod):
        modloader = Path(self.modloader_dir()).resolve()
        name = str(mod)
        candidate = Path(name)
        if (not name or candidate.is_absolute() or candidate.name != name or
                name in ('.', '..') or '/' in name or '\\' in name or ':' in name):
            raise ValueError('O nome do mod deve identificar uma pasta direta dentro de ModLoader.')
        resolved = (modloader / name).resolve()
        if resolved.parent != modloader or not resolved.is_dir():
            raise ValueError('O destino precisa ser uma pasta existente diretamente dentro de ModLoader.')
        return resolved

    def _run_native_saat(self, args, cwd, label):
        exe = self.saat().get('sfx')
        if not exe:
            raise FileNotFoundError('saat_sfx.exe não encontrado')
        result = subprocess.run([str(exe), *[str(x) for x in args]], cwd=str(cwd),
                                capture_output=True, timeout=3600)
        if result.returncode:
            detail = (result.stdout + b'\n' + result.stderr).decode('utf-8', 'replace')[-3000:]
            raise RuntimeError('SAAT %s falhou (código %d): %s' % (label, result.returncode, detail))

    @staticmethod
    def _publish_native_pair(staged_archive, archive, staged_bank, bank, snapshot_dir):
        """Replace a native archive/lookup pair and restore both on any partial failure."""
        staged_archive, archive = Path(staged_archive), Path(archive)
        staged_bank, bank = Path(staged_bank), Path(bank)
        snapshot_dir = Path(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snap_archive = snapshot_dir / 'archive-before'
        snap_bank = snapshot_dir / 'bank-before'
        shutil.copy2(archive, snap_archive)
        shutil.copy2(bank, snap_bank)
        before_archive, before_bank = sha256_arquivo(snap_archive), sha256_arquivo(snap_bank)
        try:
            os.replace(staged_archive, archive)
            os.replace(staged_bank, bank)
        except Exception as publish_error:
            try:
                restore_archive = snapshot_dir / 'archive-restore'
                restore_bank = snapshot_dir / 'bank-restore'
                shutil.copy2(snap_archive, restore_archive)
                shutil.copy2(snap_bank, restore_bank)
                os.replace(restore_archive, archive)
                os.replace(restore_bank, bank)
                if sha256_arquivo(archive) != before_archive or sha256_arquivo(bank) != before_bank:
                    raise IOError('Restauração não conferiu os hashes anteriores.')
            except Exception as restore_error:
                raise RuntimeError('Publicação parcial falhou (%s) e rollback também falhou (%s). Snapshots: %s' %
                                   (publish_error, restore_error, snapshot_dir)) from publish_error
            for path in (snap_archive, snap_bank, snapshot_dir / 'archive-restore', snapshot_dir / 'bank-restore'):
                path.unlink(missing_ok=True)
            raise
        for path in (snap_archive, snap_bank):
            path.unlink(missing_ok=True)

    def _instalar_native(self, plano, mod, on_progress=None):
        """Rebuild packed SFX from the active mod archive; preserve and verify every slot."""
        groups = {}
        refs_seen = set()
        for entry in plano:
            ref = tuple(entry.get('ref') or ())
            if (len(ref) != 3 or ref[0] not in PAKS or int(ref[1]) < 1 or int(ref[2]) < 1):
                return {'ok': False, 'motivo': 'Referência SFX inválida; streams não podem ser instalados como SFX.'}
            if ref in refs_seen:
                return {'ok': False, 'motivo': 'O plano contém mais de um take para o mesmo slot SFX: %s.' % (ref,)}
            refs_seen.add(ref)
            groups.setdefault(ref[0], []).append(entry)
        if len(groups) > 1:
            return {'ok': False, 'motivo': 'Aplique um PAK por operação para permitir a verificação e o rollback do par archive/BankLkup.'}
        try:
            mod_dir = self._safe_mod_dir(mod)
            if self._game_is_running():
                return {'ok': False, 'motivo': 'Feche o GTA San Andreas antes de aplicar áudio ao mod.'}
            with self._native_install_lock():
                if self._game_is_running():
                    return {'ok': False, 'motivo': 'O GTA foi aberto durante a preparação; nada foi publicado.'}
                ex = self.saat()
                if not ex.get('sfx'):
                    return {'ok': False, 'motivo': 'saat_sfx.exe não encontrado.'}
                self.cache_root.mkdir(parents=True, exist_ok=True)
                backup_root = self.backup_root / datetime.now().strftime('%Y%m%d_%H%M%S_%f') / mod
                published = []
                for index, (pak, entries) in enumerate(groups.items(), 1):
                    archive = self._arquivo_pak_mod(mod, pak).resolve()
                    bank = self._bank_mod(mod)
                    if not archive.is_file() or not self._dentro(mod_dir, archive):
                        raise ValueError('Arquivo nativo do PAK fora do mod ativo: %s' % pak)
                    if not bank or not Path(bank).is_file() or not self._dentro(mod_dir, bank):
                        raise ValueError('BankLkup.dat do mod ausente; não será usado o lookup do jogo base.')
                    archive_hash, bank_hash = sha256_arquivo(archive), sha256_arquivo(bank)
                    with tempfile.TemporaryDirectory(prefix='native_sfx_', dir=str(self.cache_root)) as temp:
                        work = Path(temp); native = work / pak; lookup = work / 'BankLkup.dat'
                        shutil.copy2(archive, native); shutil.copy2(bank, lookup)
                        baseline_export = work / 'baseline'; baseline_export.mkdir()
                        cli = SaatClient(self.game_dir, ex['sfx'], ex.get('stream'))
                        exported = cli.exportar_sfx(pak, baseline_export, source=native, bank=lookup)
                        if not exported.get('ok'):
                            raise RuntimeError('SAAT não exportou o PAK atual do mod: %s' % pak)
                        base = baseline_export / pak
                        ini_path = base / 'sfx_import.ini'
                        if not ini_path.is_file():
                            raise ValueError('Exportação SAAT sem sfx_import.ini; repack bloqueado.')
                        ini = configparser.ConfigParser(interpolation=None)
                        ini.optionxform = str
                        ini.read(ini_path, encoding='utf-8-sig')
                        lookup_index = ini.getint('Archive', 'lookup_index')
                        lookup_before = lookup.read_bytes()
                        expected, total = {}, 0
                        for section in ini.sections():
                            if not section.lower().startswith('bank_'):
                                continue
                            bank_num = int(section.rsplit('_', 1)[1])
                            for sound_num in range(1, ini.getint(section, 'num_sounds') + 1):
                                key = 'sound_%03d.filename' % sound_num
                                if not ini.has_option(section, key):
                                    raise ValueError('Receita SAAT incompleta: %s/%s' % (section, key))
                                relative = Path(ini.get(section, key).replace('\\', '/'))
                                wav = (base / relative).resolve()
                                if not self._dentro(base, wav) or not wav.is_file():
                                    raise ValueError('Slot SAAT ausente/fora do export: %s' % wav)
                                ref = (pak, bank_num, sound_num)
                                if ref in expected:
                                    raise ValueError('Slot duplicado na receita SAAT: %s' % (ref,))
                                expected[ref] = self._pcm_signature(wav)
                                ini.set(section, key, str(wav))
                                total += 1
                        if not total:
                            raise ValueError('Receita SAAT sem slots.')
                        replacements = {tuple(e['ref']): e for e in entries}
                        if any(ref not in expected for ref in replacements):
                            raise ValueError('Um ou mais slots não existem no PAK nativo atual.')
                        for ref, entry in replacements.items():
                            dest = Path(ini.get('Bank_%03d' % ref[1], 'sound_%03d.filename' % ref[2]))
                            dest.write_bytes(converter_wav_pcm16_mono(entry['dub'], expected[ref][0]))
                            expected[ref] = self._pcm_signature(dest)
                        recipe = work / 'sfx_import.ini'
                        with recipe.open('w', encoding='utf-8') as stream:
                            ini.write(stream)
                        self._run_native_saat(['-i', native, recipe, lookup], base, 'import')
                        lookup_after = lookup.read_bytes()
                        if len(lookup_before) != len(lookup_after) or len(lookup_before) % 12:
                            raise ValueError('BankLkup.dat mudou de tamanho ou não contém registros de 12 bytes.')
                        for offset in range(0, len(lookup_before), 12):
                            before_record = lookup_before[offset:offset + 12]
                            after_record = lookup_after[offset:offset + 12]
                            if (before_record[0] != after_record[0] or
                                    (before_record[0] != lookup_index and before_record != after_record)):
                                raise ValueError('SAAT alterou um registro BankLkup fora do PAK reconstruído (offset %d).' % offset)
                        roundtrip = work / 'roundtrip'; roundtrip.mkdir()
                        checked = cli.exportar_sfx(pak, roundtrip, source=native, bank=lookup)
                        if not checked.get('ok'):
                            raise RuntimeError('SAAT não reexportou o PAK reconstruído: %s' % pak)
                        wavs = varrer_sfx(roundtrip / pak)
                        if len(wavs) != total:
                            raise ValueError('Quantidade de slots mudou no roundtrip (%d != %d).' % (len(wavs), total))
                        for ref, signature in expected.items():
                            if ref not in wavs or self._pcm_signature(wavs[ref]) != signature:
                                raise ValueError('Roundtrip PCM divergente no slot %s.' % (ref,))
                        if self._game_is_running():
                            raise RuntimeError('O GTA abriu antes da publicação; nada foi alterado.')
                        if sha256_arquivo(archive) != archive_hash or sha256_arquivo(bank) != bank_hash:
                            raise RuntimeError('Archive/BankLkup do mod mudou; repack cancelado para proteger trabalho novo.')
                        backup_archive = backup_root / 'audio' / 'SFX' / pak
                        backup_bank = backup_root / 'audio' / 'CONFIG' / 'BankLkup.dat'
                        backup_archive.parent.mkdir(parents=True, exist_ok=True)
                        backup_bank.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(archive, backup_archive); shutil.copy2(bank, backup_bank)
                        if sha256_arquivo(backup_archive) != archive_hash or sha256_arquivo(backup_bank) != bank_hash:
                            raise IOError('Backup do archive/lookup não passou na verificação de hash.')
                        temp_archive = archive.with_name(archive.name + '.studio-new')
                        temp_bank = Path(bank).with_name(Path(bank).name + '.studio-new')
                        shutil.copy2(native, temp_archive); shutil.copy2(lookup, temp_bank)
                        # Keep only the replaced, PCM-roundtripped slots for audition.
                        # Archive/lookup pins make stale copies ineligible after edits.
                        slot_proofs = []
                        audition_root = self.cache_root / 'native_verified_slots' / sha256_arquivo(native)
                        for ref, entry in replacements.items():
                            audition = audition_root / ('%s/Bank_%03d/sound_%03d.wav' % ref)
                            audition.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(wavs[ref], audition)
                            slot_proofs.append(dict(ref=list(ref), source_take_path=str(Path(entry['dub']).resolve()),
                                source_take_sha256=sha256_arquivo(entry['dub']), audition_path=str(audition),
                                audition_sha256=sha256_arquivo(audition), pcm_signature=list(expected[ref])))
                        self._publish_native_pair(temp_archive, archive, temp_bank, bank,
                                                  backup_root / '.publish-rollback')
                        published.append({'pak': pak, 'slots': len(replacements), 'archive': str(archive),
                                          'slots_verified': slot_proofs,
                                          'archive_sha256': sha256_arquivo(archive),
                                          'banklkup_sha256': sha256_arquivo(bank), 'backup': str(backup_root)})
                        if on_progress:
                            on_progress(index, len(groups), pak)
                manifest = self._manifest()
                manifest.setdefault('native_archives', {}).setdefault(mod, []).extend(published)
                try:
                    json_atomico(self.manifest_path, manifest)
                except Exception:
                    for item in published:
                        shutil.copy2(Path(item['backup']) / 'audio' / 'SFX' / item['pak'], item['archive'])
                        shutil.copy2(Path(item['backup']) / 'audio' / 'CONFIG' / 'BankLkup.dat',
                                     self._bank_mod(mod))
                    raise
                self._ml_escaneado = False
                return {'ok': True, 'instalados': sum(x['slots'] for x in published),
                        'mod': mod, 'arquivos_nativos': published, 'backup': str(backup_root)}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            return {'ok': False, 'motivo': str(exc), 'instalados': 0}

    def desinstalar(self, mod=None):
        mod = mod or self.cfg.get('modloader_mod') or MOD_PADRAO
        man = self._manifest()
        ent = man['mods'].get(mod)
        native_rows = man.get('native_archives', {}).get(mod, [])
        if (not ent and not native_rows) or not self.game_dir:
            return {'ok': True, 'removidos': 0, 'restaurados': 0, 'pulados': []}
        base = self.modloader_dir() / mod
        rem = rest = 0
        pulados = []
        for rel, d in list((ent or {}).get('arquivos', {}).items()):
            dest = base / rel
            if not dest.exists():
                ent['arquivos'].pop(rel, None)
                continue
            if sha256_arquivo(dest) != d.get('sha256_instalado'):
                pulados.append(rel)
                continue
            bk = Path(d['backup']) if d.get('backup') else None
            if bk and bk.exists():
                shutil.copy2(bk, dest)
                rest += 1
            else:
                dest.unlink()
                rem += 1
                p = dest.parent
                while p != base and p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
                    p = p.parent
            ent['arquivos'].pop(rel, None)
        restored_native = []
        if native_rows:
            if self._game_is_running():
                return {'ok': False, 'motivo': 'Feche o GTA San Andreas antes de restaurar os archives nativos.',
                        'removidos': rem, 'restaurados': rest, 'pulados': pulados}
            try:
                with self._native_install_lock():
                    if self._game_is_running():
                        raise RuntimeError('O GTA foi aberto durante a restauração.')
                    for row in reversed(native_rows):
                        archive = self._arquivo_pak_mod(mod, row['pak']).resolve()
                        bank = self._bank_mod(mod)
                        backup_root = Path(row['backup']).resolve()
                        backup_archive = backup_root / 'audio' / 'SFX' / row['pak']
                        backup_bank = backup_root / 'audio' / 'CONFIG' / 'BankLkup.dat'
                        if (not self._dentro(base, archive) or not self._dentro(base, bank)
                                or not self._dentro(self.backup_root, backup_root)
                                or not backup_archive.is_file() or not backup_bank.is_file()):
                            pulados.append('%s: backup ou destino inválido' % row['pak'])
                            continue
                        if (sha256_arquivo(archive) != row.get('archive_sha256')
                                or sha256_arquivo(bank) != row.get('banklkup_sha256')):
                            pulados.append('%s: archive/BankLkup mudou após instalação' % row['pak'])
                            continue
                        tmp_archive = archive.with_name(archive.name + '.studio-restore')
                        tmp_bank = Path(bank).with_name(Path(bank).name + '.studio-restore')
                        shutil.copy2(backup_archive, tmp_archive); shutil.copy2(backup_bank, tmp_bank)
                        self._publish_native_pair(tmp_archive, archive, tmp_bank, bank,
                                                  backup_root / '.restore-rollback')
                        restored_native.append(row)
                        rest += row.get('slots', 0)
                    man['native_archives'][mod] = [r for r in native_rows if r not in restored_native]
                    if not man['native_archives'][mod]:
                        del man['native_archives'][mod]
            except (OSError, RuntimeError) as exc:
                return {'ok': False, 'motivo': str(exc), 'removidos': rem,
                        'restaurados': rest, 'pulados': pulados}
        json_atomico(self.manifest_path, man)
        self._ml_escaneado = False
        return {'ok': True, 'removidos': rem, 'restaurados': rest, 'pulados': pulados}

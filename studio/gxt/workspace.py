# -*- coding: utf-8 -*-
"""
Workspace de anotações, favoritos e status de revisão por fala.
"""
import os
import json
from pathlib import Path
from studio.core.utils import json_atomico


class Workspace:
    """Favoritos, notas e 'revisada' por fala. Fica FORA do GXT (JSON atômico no projeto)."""

    def __init__(self, path):
        self.path = Path(path)
        self.d = {'schema_version': 1, 'linhas': {}}
        try:
            d = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(d, dict) and isinstance(d.get('linhas'), dict):
                self.d = d
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:  # arquivo corrompido: guarda de lado em vez de perder/sobrescrever
                os.replace(self.path, self.path.with_suffix('.corrompido'))
            except OSError:
                pass

    def get(self, key):
        return self.d['linhas'].get(key, {})

    def set(self, key, **kw):
        e = dict(self.d['linhas'].get(key, {}))
        e.update(kw)
        e = {k: v for k, v in e.items() if v}
        if e:
            self.d['linhas'][key] = e
        else:
            self.d['linhas'].pop(key, None)
        self.salvar()

    def salvar(self):
        try:
            json_atomico(self.path, self.d)
        except OSError:
            pass

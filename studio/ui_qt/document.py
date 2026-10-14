"""Qt-facing document state for classic GXT text editing.

This module contains no widgets. It reuses the classic GXT codec, validator,
atomic save path, name recovery and navigation catalog.
"""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from studio.config import PROJ_ROOT
from studio.gxt.codec import decode_text, encode_text
from studio.gxt.jamcrc import (
    carregar_cache_nomes,
    recuperar_nomes,
    salvar_cache_nomes,
)
from studio.gxt.model import Gxt
from studio.ui.navigation_catalog import NavigationCatalog


from functools import lru_cache


@lru_cache(maxsize=512)
def _recover_table_names(recover, table, missing):
    """Reuse deterministic misses when the main window creates a new document."""
    return tuple(recover(table, list(missing)).items())


class QtDocument:
    """Editable current GXT plus an optional read-only comparison GXT."""

    CSV_FIELDS = ('tabela', 'id', 'chave', 'audio_id', 'comparacao', 'atual')

    def __init__(self, navigation_catalog=None):
        self.gxt = None
        self.comparison = None
        self.path = None
        self._names = {}
        self._catalog_names = None
        self._recovery_cache = {}
        self._name_cache = None
        self._undo = []
        self._redo = []
        self._navigation_catalog = navigation_catalog
        self._saved_digest = None

    @property
    def g(self):
        """Compatibility-friendly short name for the current GXT."""
        return self.gxt

    @property
    def c(self):
        """Compatibility-friendly short name for the comparison GXT."""
        return self.comparison

    @property
    def dirty(self):
        return bool(self.gxt and self.gxt.total_edited())

    def load(self, path):
        """Load and validate the editable GXT, clearing document history."""
        replacement = Gxt(str(path))
        self.gxt = replacement
        self.path = Path(path)
        self._saved_digest = hashlib.sha256(self.path.read_bytes()).digest()
        self._names.clear()
        self._name_cache = None
        self._undo.clear()
        self._redo.clear()
        return replacement

    def load_comparison(self, path):
        """Load a read-only comparison GXT."""
        self.comparison = Gxt(str(path))
        return self.comparison

    def names(self, table):
        """Return resolved names for a table, doing expensive recovery once."""
        if not self.gxt or table not in self.gxt.t:
            return {}
        if table in self._names:
            return self._names[table]

        if self._name_cache is None:
            self._name_cache = carregar_cache_nomes()
        hashes = self.gxt.t[table]['hashes']
        names = {}
        # Catalogued names are curated, stable evidence and take precedence.
        catalog = self._get_navigation_catalog()
        if self._catalog_names is None:
            self._catalog_names = {}
            for key, value in getattr(catalog, 'key_names', {}).items():
                try:
                    key_table, key_hash = key.rsplit('|', 1)
                    self._catalog_names.setdefault(key_table, {})[int(key_hash, 16)] = value
                except (AttributeError, ValueError):
                    continue
        names.update(self._catalog_names.get(table, {}))

        cached = {}
        for key_hash in hashes:
            value = self._name_cache.get(str(key_hash))
            if value and key_hash not in names:
                cached[key_hash] = value

        missing = [value for value in hashes if value not in names and value not in cached]
        recovery_key = (table, tuple(missing))
        if missing and recovery_key not in self._recovery_cache:
            # Include the exact unresolved identities: a replacement GXT may add keys.
            if len(self._recovery_cache) >= 512:
                self._recovery_cache.clear()
            self._recovery_cache[recovery_key] = dict(_recover_table_names(recuperar_nomes, table, tuple(missing)))
        recovered = self._recovery_cache.get(recovery_key, {})
        changed = False
        for key_hash, value in recovered.items():
            if key_hash not in names and key_hash not in cached:
                cached[key_hash] = value
                cache_key = str(key_hash)
                if cache_key not in self._name_cache:
                    self._name_cache[cache_key] = value
                    changed = True
        if changed:
            salvar_cache_nomes(self._name_cache)

        names.update(cached)
        self._names[table] = names
        return names

    def rows(self, pairs=None):
        """Return row dictionaries, preserving supplied (table, hash) order."""
        if not self.gxt:
            return []
        identities = pairs if pairs is not None else [
            (table, key_hash)
            for table in self.gxt.order
            for key_hash in self.gxt.t[table]['hashes']
        ]
        result = []
        names_by_table = {}
        for table, key_hash in identities:
            current_table = self.gxt.t.get(table)
            if not current_table or key_hash not in current_table['cur']:
                continue
            if table not in names_by_table:
                names_by_table[table] = self.names(table)
            names = names_by_table[table]
            text = decode_text(current_table['cur'][key_hash])
            reference = None
            flags = set()
            if self.comparison and table in self.comparison.t:
                comparison_table = self.comparison.t[table]
                if key_hash in comparison_table['cur']:
                    reference = decode_text(comparison_table['cur'][key_hash])
                    if reference != text:
                        flags.add('different')
            if current_table['cur'][key_hash] != current_table['orig'][key_hash]:
                flags.add('edited')
            result.append({
                'table': table,
                'hash': key_hash,
                'name': names.get(key_hash, '0x%08X' % key_hash),
                'text': text,
                'comparison': reference,
                'flags': flags,
            })
        return result

    def set_text(self, table, key_hash, text):
        """Set one identity after strict validation by the original GXT codec."""
        target = self._target(table, key_hash)
        data = self._encode_valid(text)
        before = target['cur'][key_hash]
        if before == data:
            return False
        target['cur'][key_hash] = data
        self._undo.append([(table, key_hash, before, data)])
        self._redo.clear()
        return True

    def undo(self):
        if not self._undo or not self.gxt:
            return False
        command = self._undo.pop()
        for table, key_hash, before, _after in reversed(command):
            self.gxt.t[table]['cur'][key_hash] = before
        self._redo.append(command)
        return True

    def redo(self):
        if not self._redo or not self.gxt:
            return False
        command = self._redo.pop()
        for table, key_hash, _before, after in command:
            self.gxt.t[table]['cur'][key_hash] = after
        self._undo.append(command)
        return True

    def save(self, path=None):
        if not self.gxt:
            raise RuntimeError('Nenhum GXT aberto para salvar.')
        destination = Path(path) if path is not None else self.path
        if destination is None:
            raise RuntimeError('O GXT atual não possui caminho de destino.')
        if self.path and destination.resolve() == self.path.resolve() and self._saved_digest is not None:
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).digest() != self._saved_digest:
                raise ValueError('Este GXT foi alterado fora desta janela. Suas edições continuam aqui. '
                    'Use Salvar como para preservá-las em outro arquivo ou reabra a versão externa antes de continuar.')
        self.gxt.save(destination, backups_dir=PROJ_ROOT / 'backups')
        self.path = destination
        self._saved_digest = hashlib.sha256(destination.read_bytes()).digest()
        return destination

    def navigation(self):
        if not self.gxt:
            return {}
        return self._get_navigation_catalog().for_gxt(self.gxt)

    def export_csv(self, destination, pairs=None):
        """Export classic-compatible CSV rows for the selected identities."""
        if not self.gxt:
            raise RuntimeError('Nenhum GXT aberto para exportar.')
        selected = set(pairs) if pairs is not None else None
        with Path(destination).open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.writer(stream, delimiter=';')
            writer.writerow(self.CSV_FIELDS)
            for table in self.gxt.order:
                table_names = self.names(table)
                for key_hash in self.gxt.t[table]['hashes']:
                    if selected is not None and (table, key_hash) not in selected:
                        continue
                    comparison = None
                    if self.comparison and table in self.comparison.t:
                        comparison = self.comparison.t[table]['cur'].get(key_hash)
                    writer.writerow((
                        table,
                        '0x%08X' % key_hash,
                        table_names.get(key_hash, ''),
                        '',
                        decode_text(comparison) if comparison is not None else '',
                        decode_text(self.gxt.t[table]['cur'][key_hash]),
                    ))
        return Path(destination)

    def import_csv(self, source, preview=False):
        """Validate a CSV fully, then apply all compatible identity changes."""
        if not self.gxt:
            raise RuntimeError('Nenhum GXT aberto para importar.')
        with Path(source).open('r', newline='', encoding='utf-8-sig') as stream:
            reader = csv.DictReader(stream, delimiter=';')
            fields = reader.fieldnames or []
            if 'tabela' not in fields or 'id' not in fields or not ({'atual', 'texto'} & set(fields)):
                raise ValueError('CSV deve conter tabela, id e atual (ou texto), separados por ponto e vírgula.')
            rows = list(reader)

        parsed = []
        invalid = missing = incompatible = unchanged = 0
        seen = set()
        for row in rows:
            try:
                table = (row.get('tabela') or '').strip()
                raw_hash = (row.get('id') or '').strip()
                key_hash = int(raw_hash, 16)
                text = row.get('atual') if 'atual' in row else row.get('texto')
                if not table or text is None:
                    raise ValueError
            except (TypeError, ValueError):
                invalid += 1
                continue
            identity = (table, key_hash)
            if identity in seen:
                invalid += 1
                continue
            seen.add(identity)
            target_table = self.gxt.t.get(table)
            if not target_table or key_hash not in target_table['cur']:
                missing += 1
                continue
            try:
                data = self._encode_valid(text)
            except ValueError:
                incompatible += 1
                continue
            before = target_table['cur'][key_hash]
            if before == data:
                unchanged += 1
            else:
                parsed.append((table, key_hash, before, data))

        if parsed and not preview:
            for table, key_hash, _before, after in parsed:
                self.gxt.t[table]['cur'][key_hash] = after
            self._undo.append(parsed)
            self._redo.clear()
        return {
            'changes': parsed,
            'changed': len(parsed),
            'unchanged': unchanged,
            'invalid': invalid,
            'missing': missing,
            'incompatible': incompatible,
        }

    def _get_navigation_catalog(self):
        if self._navigation_catalog is None:
            self._navigation_catalog = NavigationCatalog()
        return self._navigation_catalog

    def _target(self, table, key_hash):
        if not self.gxt:
            raise RuntimeError('Nenhum GXT aberto para editar.')
        target = self.gxt.t.get(table)
        if not target or key_hash not in target['cur']:
            raise KeyError('Chave GXT inexistente: %s/%08X' % (table, key_hash))
        return target

    @staticmethod
    def _encode_valid(text):
        if not isinstance(text, str):
            raise TypeError('O texto GXT deve ser uma string.')
        data, incompatible = encode_text(text)
        if incompatible:
            raise ValueError('Caracteres não compatíveis com a codificação GXT: %s' % ''.join(incompatible))
        if b'\0' in data:
            raise ValueError('O texto GXT não pode conter o terminador NUL.')
        return data

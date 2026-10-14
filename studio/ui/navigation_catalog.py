"""Read-only offline navigation: identities are always real (table, JAMCRC) pairs."""
import json
from pathlib import Path

class NavigationCatalog:
    def __init__(self, path=None):
        path = path or Path(__file__).parents[1] / 'data' / 'navigation_catalog.json'
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        self.groups = data['groups']
        self.key_names = data.get('key_names', {})
        self.sources = data.get('sources', {})
        self.coverage = data.get('coverage', {})

    @staticmethod
    def members_for(group):
        return set((t, int(h)) for t, h in group.get('keys', []))

    def for_gxt(self, gxt):
        rows = [(t,h) for t in gxt.order for h in gxt.t[t]['hashes']]
        allrows = set(rows)
        result = {'all': dict(label='Todos os textos',section='',members=rows,
                             evidence='Todas as identidades do GXT aberto, sem alterar dados.')}
        covered = set()
        for id, group in sorted(self.groups.items(), key=lambda item: item[1]['label'].casefold()):
            members = self.members_for(group) & allrows
            for t in group.get('tables', []):
                if t in gxt.t:
                    members.update((t,h) for h in gxt.t[t]['hashes'])
            if not members:
                continue
            covered.update(members)
            result[id] = dict(group, members=[pair for pair in rows if pair in members])
        result['other'] = dict(label='Outros / não classificados', section='Categorias',
            members=[pair for pair in rows if pair not in covered],
            evidence='Sem associação no catálogo local. Nenhuma categoria foi inferida do texto hipertraduzido.')
        return result

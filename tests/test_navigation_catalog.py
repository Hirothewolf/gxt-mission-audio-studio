import unittest
from types import SimpleNamespace
from studio.ui.navigation_catalog import NavigationCatalog
from studio.gxt.jamcrc import jamcrc

class CatalogTests(unittest.TestCase):
    def test_real_titles_and_exact_main_membership(self):
        c = NavigationCatalog()
        mission = next(x for x in c.groups.values() if 'tagging up turf' in x['label'].casefold())
        self.assertIn('SWEET1', mission['tables'])
        self.assertIn(('MAIN', jamcrc('SWEET_1')), c.members_for(mission))
        self.assertNotIn(('MAIN', jamcrc('LANDSTK')), c.members_for(mission))
    def test_unknown_remains_visible_and_overlap_deduplicates(self):
        c = NavigationCatalog()
        g = SimpleNamespace(order=['MAIN', 'UNKNOWN'], t={
            'MAIN': {'hashes': [jamcrc('LANDSTK'), 123]}, 'UNKNOWN': {'hashes': [456]}})
        groups = c.for_gxt(g)
        allrows = {(t,h) for t in g.order for h in g.t[t]['hashes']}
        self.assertEqual(set().union(*(set(x['members']) for x in groups.values())), allrows)
        self.assertIn(('UNKNOWN',456), groups['other']['members'])
        self.assertIn(('MAIN',123), groups['other']['members'])
        self.assertEqual(groups['vehicles']['members'], [('MAIN',jamcrc('LANDSTK'))])
        self.assertEqual(len(groups['all']['members']), 3)
    def test_no_matching_keys_never_fabricated(self):
        c = NavigationCatalog()
        g = SimpleNamespace(order=['MAIN'], t={'MAIN': {'hashes': [123]}})
        self.assertEqual(c.for_gxt(g)['other']['members'], [('MAIN',123)])

    def test_prologue_has_native_intro_tables_assets_and_exact_main_keys(self):
        c = NavigationCatalog()
        prologue = c.groups['mission:prologue']
        self.assertEqual(prologue['tables'], ['INTRO1'])
        self.assertIn(('MAIN', jamcrc('VO_AA')), c.members_for(prologue))
        self.assertIn(('MAIN', jamcrc('VO_AG')), c.members_for(prologue))
        scenes = {asset['scene'] for asset in prologue['assets'] if asset['kind'] == 'cutscene'}
        self.assertEqual(scenes, {'PROLOG1', 'PROLOG3'})
        beats = next(asset for asset in prologue['assets'] if asset['kind'] == 'continuous_dialogue_stream')
        self.assertEqual((beats['container'], beats['track']), ('BEATS', 'Track_010'))
        self.assertEqual(len(beats['gxt_pairs']), 24)
        self.assertIn({'audio_id': 'POLICE:PRO@24', 'table': 'INTRO1',
                       'hash': '%08X' % jamcrc('PRO@24'), 'timing_status': 'SAVED_EXACT_UTTERANCE'},
                      beats['gxt_pairs'])

    def test_every_native_mission_segment_is_accounted_for(self):
        c = NavigationCatalog()
        coverage = c.coverage
        self.assertEqual(coverage['mission_script_segments_scanned'], 135)
        self.assertEqual(coverage['named_title_groups'], 104)
        self.assertEqual(coverage['context_only_groups'], 10)
        rows = coverage['segments']
        self.assertEqual(len(rows), coverage['mission_script_segments_scanned'])
        self.assertEqual(len({row['segment'] for row in rows}), len(rows))
        self.assertEqual(sum(row['status'] == 'no_mission_text_table' for row in rows), 4)
        self.assertEqual(sum(row['status'] == 'activity_table_category' for row in rows), 12)
        self.assertEqual(sum(row['status'] == 'mission_group' for row in rows), 119)
        for segment in ('MISSION_041', 'MISSION_042', 'MISSION_043', 'MISSION_044'):
            row = next(row for row in rows if row['segment'] == segment)
            self.assertEqual(row['group'], 'mission:40')
        context = c.groups['mission-context:117']
        self.assertIn('sem título', context['label'])
        self.assertEqual(context['tables'], ['TRUCK'])

    def test_navigation_preserves_group_assets_and_coverage(self):
        c = NavigationCatalog()
        prologue = c.groups['mission:prologue']
        g = SimpleNamespace(order=['MAIN', 'INTRO1'], t={
            'MAIN': {'hashes': [jamcrc('VO_AA')]},
            'INTRO1': {'hashes': [jamcrc('PRO@24'), 123]}})
        groups = c.for_gxt(g)
        self.assertEqual(groups['mission:prologue']['assets'], prologue['assets'])
        self.assertIn(('MAIN', jamcrc('VO_AA')), groups['mission:prologue']['members'])
        self.assertIn(('INTRO1', jamcrc('PRO@24')), groups['mission:prologue']['members'])

if __name__ == '__main__': unittest.main()

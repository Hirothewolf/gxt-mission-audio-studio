import unittest
from types import SimpleNamespace
from unittest.mock import patch


class FakeRoot:
    def __init__(self):
        self.callbacks = {}
        self.cancelled = set()
        self.next_id = 0

    def after(self, delay, callback):
        self.next_id += 1
        self.callbacks[self.next_id] = callback
        return self.next_id

    def after_cancel(self, callback_id):
        self.cancelled.add(callback_id)
        self.callbacks.pop(callback_id, None)


class FakeTree:
    def __init__(self):
        self.rows = {}
        self.selected = ()

    def delete(self, *iids):
        self.rows.clear()

    def get_children(self):
        return tuple(self.rows)

    def exists(self, iid):
        return iid in self.rows

    def insert(self, parent, where, iid, values, tags=()):
        self.rows[iid] = (values, tags)

    def selection_set(self, iid):
        self.selected = (iid,)

    def see(self, iid):
        pass


class FakeLabel:
    def __init__(self):
        self.text = ''

    def config(self, text):
        self.text = text

    def winfo_exists(self):
        return True


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class AppSearchResponsivenessTests(unittest.TestCase):
    def make_app(self, count=3):
        from studio.ui.app import App

        app = App.__new__(App)
        app.root = FakeRoot()
        app.closed = False
        app.busca_job = None
        app.busca_token = 0
        app.busca_contexto = None
        app.busca_cache_linhas = {}
        app.busca_cache_filtro = None
        app.MAX_LINHAS = 20
        app.tv = FakeTree()
        app.lbl_stats = FakeLabel()
        app.var_busca = FakeVar('')
        app.var_filtro = FakeVar('Todas')
        app.var_todas = FakeVar(False)
        app.g = SimpleNamespace(order=['MAIN'], t={'MAIN': {'hashes': list(range(count))}})
        app.c = None
        app.sel = None
        app.row_info = {}
        app.ignorando_sel = False
        app.nomes_tabela = lambda tab, forcar=False: {}
        app.dados_calls = []

        def dados(tab, h, nome=None):
            app.dados_calls.append((tab, h))
            return (('', '', tab, str(h), '', '', '', '', f'text {h}', ''), set())

        app.dados_linha = dados
        app.ws = SimpleNamespace(get=lambda key: {})
        app.tabela_atual = lambda: 'MAIN'
        app.tags_linha = lambda flags: ()
        app.atualizar_stats = lambda: None
        return app

    def drain(self, app):
        while app.root.callbacks:
            callback_id = next(iter(app.root.callbacks))
            callback = app.root.callbacks.pop(callback_id)
            callback()

    def test_latest_debounced_query_supersedes_older_request(self):
        app = self.make_app()
        app.agendar_atualizacao()
        first_id = app.busca_job
        app.var_busca.value = '2'
        app.agendar_atualizacao()
        self.assertIn(first_id, app.root.cancelled)
        self.drain(app)
        self.assertEqual(list(app.tv.rows), ['MAIN|00000002'])
        self.assertEqual(len(app.dados_calls), 3)

    def test_rows_are_inserted_across_tk_callbacks_and_cached_for_new_query(self):
        app = self.make_app(4)
        app.busca_token = 1
        app._executar_busca(1)

        # Force each scheduled callback to yield after a single row, including
        # during both scanning and Treeview insertion.
        clock = [0.0]

        def tick():
            clock[0] += 0.006
            return clock[0]

        with patch('studio.ui.app.time.perf_counter', side_effect=tick):
            first = app.root.callbacks.pop(app.busca_job)
            first()
            self.assertEqual(app.dados_calls, [('MAIN', 0)])
            self.assertEqual(app.tv.rows, {})
            self.drain(app)

        self.assertEqual(len(app.tv.rows), 4)
        self.assertEqual(len(app.dados_calls), 4)

        app.var_busca.value = '3'
        app.busca_token += 1
        app._executar_busca(app.busca_token)
        self.drain(app)
        self.assertEqual(list(app.tv.rows), ['MAIN|00000003'])
        self.assertEqual(len(app.dados_calls), 4)

    def test_row_render_uses_raw_mapping_without_speaker_enrichment(self):
        from studio.ui.app import App

        app = self.make_app(1)
        h = 123
        app.g.t['MAIN'].update(hashes=[h], cur={h: b'Current text'}, orig={h: b'Current text'})
        app.c = None
        app.var_tags = FakeVar(False)
        app.script_db = {('MAIN', h): {'event_name': 'MISSION_LINE_123'}}
        app.review_store = None
        app.dub_cache = {'MAIN': {}}
        app.original_audio_cache = {}
        app.jogo = None
        app.info_script = lambda *args: self.fail('row rendering should not enrich speaker identity')

        values, flags = App.dados_linha(app, 'MAIN', h)

        self.assertEqual(values[3], 'MISSION_LINE_123')
        self.assertEqual(values[8], 'Current text')
        self.assertIn('script', flags)


    def test_beats_dialogue_visible_before_original_clip_extraction(self):
        from studio.ui.app import App, FILTRO_PRED, PREDICADOS
        app = self.make_app(1)
        h = 0x7F49AF6F
        app.g.t['MAIN'].update(hashes=[h], cur={h: b'Current text'}, orig={h: b'Current text'})
        app.var_tags = FakeVar(False)
        app.script_db = {('MAIN', h): {'event_name': 'PRO@24', 'stable_audio_id': 'POLICE:PRO@24', 'pak': 'BEATS'}}
        app.review_store = None
        app.dub_cache = {'MAIN': {}}
        app.original_audio_cache = {}
        app.jogo = None
        values, flags = App.dados_linha(app, 'MAIN', h)
        self.assertIn('BEATS/Track_010', values[4])
        self.assertNotIn('audio', flags)  # No WAV ready to play yet.
        self.assertTrue(FILTRO_PRED['Com áudio original'](flags))
        self.assertFalse(FILTRO_PRED['Sem áudio original'](flags))
        self.assertTrue(FILTRO_PRED['Sem dublagem (tem original)'](flags))
        self.assertTrue(PREDICADOS['semdub'](flags))
        app.script_db[('MAIN', h)] = {'event_name': 'TEXT_ONLY'}
        _, text_flags = App.dados_linha(app, 'MAIN', h)
        self.assertFalse(FILTRO_PRED['Com áudio original'](text_flags))


if __name__ == '__main__':
    unittest.main()

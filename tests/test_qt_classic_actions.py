import unittest

from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QVBoxLayout, QWidget

from studio.ui_qt.classic_actions import install_classic_actions


class _Player:
    def stop(self):
        pass


class QtClassicActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self):
        window = QMainWindow()
        central = QWidget()
        layout = QVBoxLayout(central)
        toolbar = QVBoxLayout()
        toolbar.addWidget(QWidget())
        layout.addLayout(toolbar)
        window.setCentralWidget(central)
        window.toolbar = toolbar

        callbacks = (
            'open_file', 'compare_file', 'save_file', 'export_csv', 'import_csv',
            'commit', 'history', 'copy_comparison', 'restore_text', 'mark', 'note',
            'next_row', 'play', 'compare_audio', 'review', 'bulk_generate',
            'bulk_review', 'timeline', 'timeline_scene', 'timeline_selected',
            'keep_loaded', 'settings', 'extract', 'reindex', 'open_mod', 'undo_csv',
            'adjacent_row', 'command_palette', 'help_dialog', 'classic_tool',
            'may_close_document', 'choose_scene', 'close', 'next_undubbed', 'refresh_audio_lists',
        )
        for name in callbacks:
            setattr(window, name, lambda *args, **kwargs: None)
        window.editor = type('Editor', (), {'setPlainText': lambda *_args: None})()
        window.context = type('Context', (), {'player': _Player(), 'project_root': '.'})()
        window.document = type('Document', (), {'path': None, 'comparison': None})()
        window.selected = None
        window.statusBar().showMessage('ready')
        return window

    def test_rebuilds_classic_menu_groups_and_keeps_qt_commands(self):
        window = self.make_window()

        install_classic_actions(window)

        menus = [action.menu() for action in window.menuBar().actions()]
        self.assertEqual(
            [menu.title() for menu in menus],
            ['Arquivo', 'Editar', 'Navegar', 'Áudio', 'Dublagem', 'Opções', 'Ferramentas', 'Ajuda'],
        )
        actions = {action.text() for menu in menus
                   for action in menu.actions()}
        self.assertIn('Revisar / gerar fala selecionada…', actions)
        self.assertIn('Redublar lote…', actions)
        self.assertIn('Confirmar e aplicar áudios AI em lote…', actions)
        self.assertIn('Escolher cena de cutscene…', actions)
        self.assertIn('Cena completa da fala selecionada…', actions)

    def test_shortcuts_use_qt_actions_and_unavailable_workflows_are_recorded(self):
        window = self.make_window()

        install_classic_actions(window)

        menus = [action.menu() for action in window.menuBar().actions()]
        shortcuts = {action.text(): action.shortcut().toString()
                     for menu in menus
                     for action in menu.actions()}
        self.assertEqual(shortcuts['Abrir GXT…'], 'Ctrl+O')
        self.assertEqual(shortcuts['Salvar'], 'Ctrl+S')
        self.assertEqual(shortcuts['Desfazer edição do GXT'], 'Ctrl+Alt+Z')
        self.assertEqual(shortcuts['Tocar original'], 'F5')
        self.assertFalse(window.classic_actions_pending)


if __name__ == '__main__':
    unittest.main()

"""Classic menu and toolbar discoverability for the Qt editor.

All installed routes use native Qt dialogs and maintained backend services.
The module name is retained for compatibility with the classic menu layout.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import QMessageBox, QMenu, QPushButton, QToolButton

from studio import config


PENDING_CLASSIC_ACTIONS = {}


CLASSIC_TOOL_KEYS = {
    'Biblioteca de áudio…': 'audio_library',
    'Identificar personagens por voz…': 'voice_identity',
    'Análise nativa: SCM, cutscenes e legendas…': 'native_sources',
    'Gerenciador de modelos de IA…': 'models',
    'Gerenciador SAAT…': 'saat',
    'Lote automático de cutscenes…': 'cutscene_batch',
    'Estúdio manual de cutscenes…': 'cutscene_manual',
    'Pastas de áudio base (AutoSearch)…': 'audio_folders',
    'Extrair arquivos de áudio…': 'archive_extract',
}


def _launch_classic_tool(window, key):
    return window.classic_tool(key)


def _action(window, menu, title, callback, shortcut=None, checkable=False):
    action = QAction(title, window)
    action.setCheckable(checkable)
    if shortcut:
        action.setShortcut(QKeySequence(shortcut))
    action.triggered.connect(lambda _checked=False, fn=callback: fn())
    menu.addAction(action)
    return action


def _recent_menu(window, menu):
    menu.clear()
    paths = [str(path) for path in (config.CFG.get('recentes') or []) if str(path).strip()]
    if not paths:
        action = QAction('Nenhum arquivo recente', window)
        action.setEnabled(False)
        menu.addAction(action)
        return
    for path in paths[:10]:
        action = QAction(path, window)
        action.setToolTip(path)
        action.triggered.connect(lambda _checked=False, recent=path: _open_recent(window, recent))
        menu.addAction(action)


def _open_recent(window, path):
    if not Path(path).is_file():
        QMessageBox.information(window, 'Arquivo não encontrado', f'O arquivo não existe mais:\n{path}')
        return
    if window.may_close_document():
        window.load(path)


def _play_adjacent(window, direction):
    window.adjacent_row(direction)
    window.play('original')


def _launch_tool_action(window, menu, label, key):
    _action(window, menu, label, lambda: _launch_classic_tool(window, key))


def _replace_toolbar(window):
    toolbar = getattr(window, 'toolbar', None)
    if toolbar is None:
        central_layout = window.centralWidget().layout()
        first = central_layout.itemAt(0) if central_layout else None
        toolbar = first.layout() if first else None
    if toolbar is None:
        return

    while toolbar.count():
        item = toolbar.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
        child_layout = item.layout()
        if child_layout is not None:
            while child_layout.count():
                nested = child_layout.takeAt(0)
                if nested.widget() is not None:
                    nested.widget().deleteLater()

    def button(label, callback, tooltip=None):
        control = QPushButton(label, window.centralWidget())
        control.clicked.connect(lambda _checked=False, fn=callback: fn())
        if tooltip:
            control.setToolTip(tooltip)
        toolbar.addWidget(control)
        return control

    button('Abrir GXT…', window.open_file)
    button('Comparar…', window.compare_file)
    button('Salvar', window.save_file)
    button('Salvar como…', lambda: window.save_file(True))

    csv_button = QToolButton(window.centralWidget())
    csv_button.setText('CSV ▾')
    csv_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    csv_menu = QMenu(csv_button)
    _action(window, csv_menu, 'Exportar CSV visível…', window.export_csv)
    _action(window, csv_menu, 'Importar CSV…', window.import_csv)
    _action(window, csv_menu, 'Desfazer última importação CSV', window.undo_csv)
    csv_button.setMenu(csv_menu)
    toolbar.addWidget(csv_button)

    button('Áudios da missão', lambda: window.classic_tool('audio_library'))
    button('Cutscenes…', window.choose_scene, 'Escolher por missão/cena e carregar o áudio completo automaticamente')
    button('Redublar lote…', window.bulk_generate)
    button('Áudios AI…', window.bulk_review)

    more = QToolButton(window.centralWidget())
    more.setText('Mais ▾')
    more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    more_menu = QMenu(more)
    _action(window, more_menu, 'Biblioteca de áudio…', lambda: _launch_classic_tool(window, 'audio_library'))
    _action(window, more_menu, 'Modelos de IA…', lambda: _launch_classic_tool(window, 'models'))
    _action(window, more_menu, 'Extrair arquivos de áudio…', lambda: _launch_classic_tool(window, 'archive_extract'))
    more.setMenu(more_menu)
    toolbar.addWidget(more)
    toolbar.addStretch(1)
    button('Guia', window.help_dialog)
    button('Comandos', window.command_palette)


def install_classic_actions(window):
    """Replace Qt's starter menus and toolbar with backed classic actions."""
    window.classic_actions_pending = {menu: list(items) for menu, items in PENDING_CLASSIC_ACTIONS.items()}
    bar = window.menuBar()
    bar.clear()

    files = bar.addMenu('Arquivo')
    _action(window, files, 'Abrir GXT…', window.open_file, 'Ctrl+O')
    _action(window, files, 'Comparar com…', window.compare_file)
    recent = files.addMenu('Recentes')
    recent.aboutToShow.connect(lambda: _recent_menu(window, recent))
    recent_menu = recent.menuAction().menu()
    _recent_menu(window, recent_menu)
    files.addSeparator()
    _action(window, files, 'Salvar', window.save_file, 'Ctrl+S')
    _action(window, files, 'Salvar como…', lambda: window.save_file(True), 'Ctrl+Shift+S')
    files.addSeparator()
    _action(window, files, 'Exportar CSV visível…', window.export_csv)
    _action(window, files, 'Importar CSV…', window.import_csv)
    _action(window, files, 'Desfazer última importação CSV', window.undo_csv)
    files.addSeparator()
    _action(window, files, 'Sair', window.close, 'Alt+F4')

    edit = bar.addMenu('Editar')
    _action(window, edit, 'Aplicar texto', window.commit, 'Ctrl+Return')
    _action(window, edit, 'Desfazer edição do GXT', lambda: window.history(False), 'Ctrl+Alt+Z')
    _action(window, edit, 'Refazer edição do GXT', lambda: window.history(True), 'Ctrl+Alt+Y')
    edit.addSeparator()
    _action(window, edit, 'Restaurar texto salvo', window.restore_text)
    _action(window, edit, 'Copiar texto da comparação', window.copy_comparison)
    _action(window, edit, 'Tirar espaços das pontas',
            lambda: window.editor.setPlainText(window.editor.toPlainText().strip()))
    edit.addSeparator()
    _action(window, edit, '★ Favorita', lambda: window.mark('fav'), 'Ctrl+B')
    _action(window, edit, '✓ Marcar revisada', lambda: window.mark('rev'), 'Ctrl+Shift+M')
    _action(window, edit, '📝 Nota da fala…', window.note, 'Ctrl+Shift+N')

    navigation = bar.addMenu('Navegar')
    _action(window, navigation, 'Próxima diferença',
            lambda: window.next_row('different', True, direction=1), 'Ctrl+Down')
    _action(window, navigation, 'Diferença anterior',
            lambda: window.next_row('different', True, direction=-1), 'Ctrl+Up')
    navigation.addSeparator()
    _action(window, navigation, 'Próximo sem dublagem', window.next_undubbed, 'Ctrl+Shift+Down')
    _action(window, navigation, 'Anterior sem dublagem', lambda: window.next_undubbed(-1), 'Ctrl+Shift+Up')
    _action(window, navigation, 'Próxima editada', lambda: window.next_row('edited', True))
    _action(window, navigation, 'Próxima favorita', lambda: window.next_row('fav', True))
    _action(window, navigation, 'Próxima não revisada', lambda: window.next_row('rev', False))
    navigation.addSeparator()
    _action(window, navigation, 'Linha seguinte', lambda: window.adjacent_row(1), 'Alt+Down')
    _action(window, navigation, 'Linha anterior', lambda: window.adjacent_row(-1), 'Alt+Up')

    audio = bar.addMenu('Áudio')
    _action(window, audio, 'Tocar original', lambda: window.play('original'), 'F5')
    _action(window, audio, 'Tocar take escolhido', lambda: window.play('approved'), 'F6')
    _action(window, audio, 'Original → aprovado (A/B)', window.compare_audio, 'F7')
    _action(window, audio, 'A/B em loop', lambda: window.compare_audio(loop=True), 'Shift+F7')
    _action(window, audio, 'Fala anterior (original)', lambda: _play_adjacent(window, -1), 'Shift+F5')
    _action(window, audio, 'Fala seguinte (original)', lambda: _play_adjacent(window, 1), 'Ctrl+F5')
    _action(window, audio, 'Tocar instalado no mod', lambda: window.play('installed'), 'F8')
    _action(window, audio, 'A/B com áudio instalado', lambda: window.compare_audio(installed=True), 'Ctrl+F7')
    _action(window, audio, 'Parar', window.context.player.stop, 'Escape')
    audio.addSeparator()
    _action(window, audio, 'Revisar / gerar fala selecionada…', window.review)
    _action(window, audio, 'Revisar voz, takes e efeitos…', window.review)
    _launch_tool_action(window, audio, 'Biblioteca de áudio (com ou sem legenda)…', 'audio_library')
    _launch_tool_action(window, audio, 'Identificar personagens por voz…', 'voice_identity')
    _launch_tool_action(window, audio, 'Análise nativa: SCM, cutscenes e legendas…', 'native_sources')
    _launch_tool_action(window, audio, 'Lote automático de cutscenes…', 'cutscene_batch')
    audio.addSeparator()
    _action(window, audio, 'Trecho na timeline…', window.timeline_selected)
    _action(window, audio, 'Escolher cena de cutscene…', window.choose_scene)
    _action(window, audio, 'Cena completa da fala selecionada…', window.timeline_scene)
    _launch_tool_action(window, audio, 'Extrair/atualizar áudio do jogo (SAAT)…', 'saat')
    _launch_tool_action(window, audio, 'Extrair arquivos de áudio…', 'archive_extract')

    dubbing = bar.addMenu('Dublagem')
    _launch_tool_action(window, dubbing, 'Gerenciador de modelos de IA…', 'models')
    _action(window, dubbing, 'Gerar/revisar fala selecionada…', window.review)
    _action(window, dubbing, 'Redublar lote…', window.bulk_generate)
    _action(window, dubbing, 'Confirmar e aplicar áudios AI em lote…', window.bulk_review)
    _action(window, dubbing, 'Atualizar vínculos de faixas (BEATS / CUTSCENE)…', window.reindex)
    _action(window, dubbing, 'Abrir pasta de áudios dublados', lambda: QDesktopServices.openUrl(
        QUrl.fromLocalFile(str(window.context.project_root / 'workspace/studio_review/takes'))))
    _action(window, dubbing, 'Atualizar fontes e takes', window.refresh_audio_lists)
    if hasattr(window, 'keep_model'):
        dubbing.addAction(window.keep_model)
    else:
        keep = QAction('Manter OmniVoice carregado enquanto o editor estiver aberto', window)
        keep.setCheckable(True)
        keep.setChecked(bool(config.CFG.get('omnivoice_keep_model_loaded', False)))
        keep.toggled.connect(window.keep_loaded)
        dubbing.addAction(keep)
        window.keep_model = keep

    options = bar.addMenu('Opções')
    auto = options.addMenu('Ao selecionar uma fala')
    auto_group = QActionGroup(window)
    auto_group.setExclusive(True)
    for label, value in (('Nada', 'none'), ('Tocar original', 'original'),
                         ('Tocar dublado/aprovado', 'dub'), ('Original → aprovado', 'ab')):
        action = QAction(label, window)
        action.setCheckable(True)
        auto_group.addAction(action)
        action.setChecked(config.CFG.get('autoplay', 'none') == value)
        action.triggered.connect(lambda checked=False, selected=value: _set_option('autoplay', selected))
        auto.addAction(action)
    pause = options.addMenu('Pausa no A/B')
    pause_group = QActionGroup(window)
    pause_group.setExclusive(True)
    for milliseconds in (0, 150, 300, 600, 1000):
        action = QAction('%d ms' % milliseconds, window)
        action.setCheckable(True)
        pause_group.addAction(action)
        action.setChecked(int(config.CFG.get('pausa_ab_ms', 300)) == milliseconds)
        action.triggered.connect(lambda checked=False, selected=milliseconds: _set_option('pausa_ab_ms', selected))
        pause.addAction(action)
    _action(window, options, 'Configurar jogo e mod…', window.settings)
    _launch_tool_action(window, options, 'Pastas de áudio base (AutoSearch)…', 'audio_folders')
    _launch_tool_action(window, options, 'Gerenciador SAAT…', 'saat')

    tools = bar.addMenu('Ferramentas')
    _action(window, tools, 'Abrir pasta do projeto',
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(window.context.project_root))))
    _action(window, tools, 'Abrir pasta do mod', window.open_mod)
    _action(window, tools, 'Extrair / reindexar áudio…', window.extract)
    _action(window, tools, 'Reconstruir vínculos GXT, SCRIPT, CUTSCENE e BEATS', window.reindex)
    _launch_tool_action(window, tools, 'Diagnóstico / detectar instalações…', 'diagnostics')
    command_palette = _action(window, tools, 'Paleta de comandos…', window.command_palette, 'Ctrl+P')
    _action(window, tools, 'Atalhos de teclado / ajuda', window.help_dialog, 'F1')

    help_menu = bar.addMenu('Ajuda')
    _action(window, help_menu, 'Como salvar texto e aplicar áudio', window.help_dialog)

    _replace_toolbar(window)
    return {'menus': bar.actions(), 'command_palette': command_palette,
            'pending': window.classic_actions_pending}


def _set_option(key, value):
    config.CFG[key] = value
    config.cfg_save()

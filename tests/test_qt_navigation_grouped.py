import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from studio.gxt.jamcrc import jamcrc
from studio.ui.navigation_catalog import NavigationCatalog
from studio.ui_qt.navigation import GroupedNavigation


app = QApplication.instance() or QApplication([])


def real_categories():
    catalog = NavigationCatalog()
    weapon_table, weapon_hash = catalog.members_for(catalog.groups['weapons']).pop()
    help_table, help_hash = catalog.members_for(catalog.groups['help']).pop()
    tables = {
        'MAIN': {'hashes': [jamcrc('LANDSTK'), jamcrc('VO_AA'), jamcrc('SWEET_1'), 0x12345678]},
        'INTRO1': {'hashes': [jamcrc('PRO@24')]},
        'RACETOR': {'hashes': [0x11111111]},
        'POOL': {'hashes': [0x22222222]},
        'UNKNOWN': {'hashes': [0x33333333]},
    }
    tables.setdefault(weapon_table, {'hashes': []})['hashes'].append(weapon_hash)
    tables.setdefault(help_table, {'hashes': []})['hashes'].append(help_hash)
    gxt = SimpleNamespace(order=list(tables), t=tables)
    return catalog.for_gxt(gxt)


def item_by_id(tree, category_id):
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        if item.data(0, Qt.ItemDataRole.UserRole) == category_id:
            return item
        stack.extend(item.child(i) for i in range(item.childCount()))
    raise AssertionError('missing navigation item: %s' % category_id)


def group_by_title(tree, title):
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item.text(0) == title:
            return item
    raise AssertionError('missing navigation group: %s' % title)


def test_real_navigation_categories_are_grouped_without_changing_identity():
    categories = real_categories()
    tree = GroupedNavigation()
    tree.set_categories(categories)

    mission = item_by_id(tree, 'mission:13')
    vehicle = item_by_id(tree, 'vehicles')
    weapon = item_by_id(tree, 'weapons')
    race = item_by_id(tree, 'races')
    mini = item_by_id(tree, 'minigames')
    common = item_by_id(tree, 'common')
    other = item_by_id(tree, 'other')

    assert mission.parent().text(0) == 'Missões'
    assert vehicle.parent().text(0) == 'Veículos'
    assert weapon.parent().text(0) == 'Armas'
    assert race.parent().text(0) == 'Minijogos/Eventos'
    assert mini.parent().text(0) == 'Minijogos/Eventos'
    assert common.parent().text(0) == 'Outros textos'
    assert other.parent().text(0) == 'Outros textos'
    assert item_by_id(tree, 'help').parent().text(0) == 'Menus e dicas'
    assert group_by_title(tree, 'Dicas/tutoriais').isHidden()
    assert len([tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).text(0) in {
                    'Missões', 'Menus e dicas', 'Dicas/tutoriais', 'Veículos', 'Armas',
                    'Minijogos/Eventos', 'Outros textos'}]) == 7
    assert vehicle.data(0, Qt.ItemDataRole.UserRole) == 'vehicles'
    tree.select_category('vehicles')
    assert tree.selected_member_ids() == categories['vehicles']['members']


def test_selecting_group_emits_none_and_exposes_union_of_members():
    categories = real_categories()
    tree = GroupedNavigation()
    tree.set_categories(categories)
    emitted = []
    tree.categorySelected.connect(emitted.append)

    tree.select_category('vehicles')
    assert tree.selected_category_id() == 'vehicles'
    assert emitted[-1] == 'vehicles'
    assert tree.selected_member_ids() == categories['vehicles']['members']

    group = group_by_title(tree, 'Minijogos/Eventos')
    tree.setCurrentItem(group)
    assert tree.selected_category_id() is None
    assert emitted[-1] is None
    expected = list(dict.fromkeys(categories['races']['members'] + categories['minigames']['members']))
    assert tree.selected_member_ids() == expected


def test_filter_keeps_matching_ancestors_and_preserves_category_selection():
    categories = real_categories()
    tree = GroupedNavigation()
    tree.set_categories(categories)
    tree.show()
    tree.select_category('mission:13')
    selected = tree.currentItem()

    tree.set_filter('Tagging up Turf')
    mission_group = group_by_title(tree, 'Missões')
    assert not mission_group.isHidden()
    assert not selected.isHidden()
    assert selected.parent() is mission_group
    assert tree.selected_category_id() == 'mission:13'
    assert mission_group.isExpanded()

    tree.set_filter('no category matches this')
    assert tree.selected_category_id() == 'mission:13'
    assert tree.topLevelItemCount() > 0
    assert all(tree.topLevelItem(i).isHidden() for i in range(tree.topLevelItemCount()))


def test_groups_are_collapsible_and_selectable_by_clicking_the_header():
    tree = GroupedNavigation()
    tree.set_categories(real_categories())
    tree.resize(420, 520)
    tree.show()
    group = group_by_title(tree, 'Armas')
    assert group.isExpanded()
    tree.setCurrentItem(group)
    QTest.keyClick(tree, Qt.Key.Key_Minus)
    assert not group.isExpanded()
    QTest.keyClick(tree, Qt.Key.Key_Plus)
    assert group.isExpanded()
    assert tree.select_category('weapons')
    tree.scrollToItem(group)
    app.processEvents()
    QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                     pos=tree.visualItemRect(group).center())
    assert tree.selected_category_id() is None


def test_select_category_survives_refill_by_stable_id_and_all_remains_available():
    categories = real_categories()
    tree = GroupedNavigation()
    tree.set_categories(categories)
    assert tree.select_category('vehicles')
    tree.set_categories(dict(categories))
    assert tree.selected_category_id() == 'vehicles'
    assert tree.select_category('all')
    assert tree.selected_category_id() == 'all'
    assert tree.selected_member_ids() == categories['all']['members']


def test_large_mission_group_starts_folded_and_search_restores_its_state():
    categories = {str(i): dict(label='Missão %02d' % i, section='Missões',
                              members=[('MAIN', i)]) for i in range(20)}
    tree = GroupedNavigation()
    tree.set_categories(categories)
    group = group_by_title(tree, 'Missões')
    assert not group.isExpanded()
    tree.set_filter('Missão 17')
    assert group.isExpanded()
    assert not item_by_id(tree, '17').isHidden()
    tree.set_filter('')
    assert not group.isExpanded()
    assert tree.select_category('17')
    assert group.isExpanded()


def test_explicit_tutorial_category_remains_separate_and_selectable():
    tree = GroupedNavigation()
    tree.set_categories({'menu': dict(label='Pausa', kind='menu', members=[('MAIN', 1)]),
                         'tutorial': dict(label='Bicicleta', kind='tutorial', members=[('MAIN', 2)])})
    assert item_by_id(tree, 'menu').parent().text(0) == 'Menus'
    assert not group_by_title(tree, 'Dicas/tutoriais').isHidden()
    assert tree.select_category('tutorial')
    assert tree.selected_member_ids() == [('MAIN', 2)]

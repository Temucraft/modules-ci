#!/usr/bin/env python3
"""
Questie (3.3.5 backport): zone data for the zones of our WorldMapArea.dbc that the Temucraft/Questie fork doesn't know.

Questie shows a spawn {x, y} of zone (AreaTable id) Z like this:
  ZoneDB.private.areaIdToUiMapId[Z]   -> uiMapId                         (Database/Zones/zoneTables.lua)
  QuestieCompat.UiMapData[uiMapId]    -> { width, height, left, top, mapType, parentMapID, mapID, instance, name }
                                                                          (Compat/UiMapData.lua)
mapID is what the 3.3.5 client's GetCurrentMapAreaID() returns for the map (WorldMapArea id + 1; SetMapByID takes the
WorldMapArea id), instance is the map id (Map.dbc), and width/height/left/top come from the WorldMapArea bounds:
  width = locLeft - locRight, height = locTop - locBottom, left = locLeft, top = locTop
Zone names (l10n.zoneLookup in Localization/lookups/lookupZones.lua) let the fork find the map from the zone text and
list the zone in the Journey; ZoneDB.private.uiMapIdToAreaId, subZoneToParentZone and ZoneDB.instanceIdToAreaId are
the other id tables.

This script reads those fork files (they stay untouched) and our DBCs and writes one Lua file that adds what the fork
lacks, for every WorldMapArea row with an area and bounds:
  - maps the fork has no UiMapData for (no entry with mapID = WorldMapArea id + 1) get a new uiMapId,
    30000 + WorldMapArea id, with our bounds; zones on maps 0, 1, 530 and 571 hang under their continent
  - maps the fork has, but with other bounds than ours (more than 1 yard off), get our bounds, because the spawns
    (gen_questie_db.py) and the client's world map both use our WorldMapArea.dbc
  - area -> uiMapId, uiMapId -> area, map id -> area (new instance maps), zone names, and (with AreaTable.dbc) the
    sub areas of the new zones with their parent zone, all only where the fork has no row yet
The file must load after Localization/lookups/lookupZones.lua (zoneTables.lua and lookupZones.lua make their tables
anew); see the Questie-335.toc line in the patch for the fork.

Usage:
  gen_questie_zones.py --dbc <folder with WorldMapArea.dbc [AreaTable.dbc Map.dbc]> --questie <fork checkout>
                       [--out <file>]   (default <fork>/Database/Zones/temucraftZones.lua)
The fork checkout needs Compat/UiMapData.lua, Compat/Map.lua, Database/Zones/zoneTables.lua and
Localization/lookups/lookupZones.lua.
"""
import argparse, os, re, struct

UI_MAP_BASE = 30000          # new uiMapIds: 30000 + WorldMapArea id (the fork's own ids stay below 12000)
CONTINENT_UI_MAP = {0: 1415, 1: 1414, 530: 1945, 571: 113}
WORLD_UI_MAP = 947
# Journey categories (l10n.zoneCategoryLookup): 1-4 are the continents' zoneLookup tables themselves
CATEGORY_DUNGEONS, CATEGORY_BATTLEGROUNDS = 5, 6
# Enum.UIMapType as the fork uses it: 3 zone (also battlegrounds), 4 dungeon, 6 raid
MAP_TYPE_ZONE, MAP_TYPE_DUNGEON, MAP_TYPE_RAID = 3, 4, 6
BOUNDS_TOLERANCE = 1.0


# ---- DBCs -------------------------------------------------------------------------------------------------------

def dbc(path):
    b = open(path, 'rb').read()
    magic, n, fields, size, ssize = struct.unpack('<4s4I', b[:20])
    if magic != b'WDBC':
        raise ValueError('%s is not a DBC' % path)
    strings = b[20 + n * size:]

    def string(off):
        end = strings.find(b'\0', off)
        return strings[off:end].decode('utf-8', 'replace')
    return [b[20 + i * size:20 + (i + 1) * size] for i in range(n)], fields, string


def world_map_areas(path):
    rows, fields, string = dbc(path)
    out = []
    for r in rows:
        wid, mapid, area, name_off = struct.unpack_from('<4I', r, 0)
        left, right, top, bottom = struct.unpack_from('<4f', r, 16)
        parent = struct.unpack_from('<i', r, 40)[0] if fields >= 11 else 0
        out.append({'id': wid, 'map': mapid, 'area': area, 'file': string(name_off), 'left': left, 'right': right,
                    'top': top, 'bottom': bottom, 'parent': parent})
    return out


def area_table(path):
    """area id -> (map, parent area, enUS name)"""
    rows, fields, string = dbc(path)
    out = {}
    for r in rows:
        aid, mapid, parent = struct.unpack_from('<3I', r, 0)
        out[aid] = (mapid, parent, string(struct.unpack_from('<I', r, 11 * 4)[0]))
    return out


def map_types(path):
    """map id -> InstanceType (0 world, 1 dungeon, 2 raid, 3 battleground, 4 arena)"""
    rows, fields, string = dbc(path)
    return {struct.unpack_from('<I', r, 0)[0]: struct.unpack_from('<I', r, 8)[0] for r in rows}


def split_name(file_name):
    """WorldMapArea internal name -> readable name, when there is no AreaTable.dbc ("TheLostIsles" -> "The Lost Isles")"""
    return re.sub(r'(?<=[a-z])(?=[A-Z0-9])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', file_name)


# ---- the fork's tables ------------------------------------------------------------------------------------------

NUM = r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?'


def lua_block(text, start_re):
    """the body of the first "<start_re> {" ... "\n}" block"""
    m = re.search(start_re + r'\s*\{', text)
    if not m:
        return ''
    end = text.find('\n}', m.end())
    return text[m.end():end if end >= 0 else len(text)]


def int_map(body):
    return {int(k): int(v) for k, v in re.findall(r'\[(\d+)\]\s*=\s*(-?\d+)', body)}


def ui_map_data(text):
    """uiMapId -> {'mapID', 'instance', 'bounds'} of every UiMapData entry (both of the file's styles)"""
    out = {}
    for key, body in re.findall(r'\[(\d+)\]\s*=\s*\{([^{}]*)\}', text):
        m = re.search(r'(?:\["mapID"\]|\bmapID)\s*=\s*(%s)' % NUM, body)
        if not m:
            continue
        named = dict(re.findall(r'\[([1-4])\]\s*=\s*(%s)' % NUM, body))
        if len(named) == 4:
            bounds = [float(named[str(i)]) for i in range(1, 5)]
        else:
            positional = [p.strip() for p in body.split(',') if p.strip() and '=' not in p]
            bounds = [float(p) for p in positional[:4]] if len(positional) >= 4 else None
        inst = re.search(r'(?:\["instance"\]|\binstance)\s*=\s*(-?\d+)', body)
        out[int(key)] = {'mapID': float(m.group(1)), 'instance': int(inst.group(1)) if inst else None,
                         'bounds': bounds, 'floors': 'wdmInstanceMap' in body}
    return out


def zone_lookup(text):
    """(map ids with a zoneLookup table, area ids with a name)"""
    body = lua_block(text, r'l10n\.zoneLookup\s*=')
    maps = {int(k) for k in re.findall(r'^    \[(\d+)\]\s*=\s*\{', body, re.M)}
    areas = {int(k) for k in re.findall(r'\[(\d+)\]\s*=\s*"', body)}
    return maps, areas


class Fork:
    def __init__(self, root):
        def read(*p):
            return open(os.path.join(root, *p), encoding='utf-8', errors='replace').read()
        self.ui_maps = ui_map_data(read('Compat', 'UiMapData.lua'))
        tables = read('Database', 'Zones', 'zoneTables.lua')
        self.area_to_ui = int_map(lua_block(tables, r'ZoneDB\.private\.areaIdToUiMapId\s*='))
        self.special_to_ui = int_map(lua_block(tables, r'ZoneDB\.private\.specialZoneIdToUiMapId\s*='))
        self.ui_to_area = int_map(lua_block(tables, r'ZoneDB\.private\.uiMapIdToAreaId\s*='))
        self.sub_to_parent = int_map(lua_block(tables, r'ZoneDB\.private\.subZoneToParentZone\s*='))
        self.sub_to_parent.update({int(k): int(v) for k, v in re.findall(
            r'ZoneDB\.private\.subZoneToParentZone\[(\d+)\]\s*=\s*(\d+)', tables)})
        self.instance_to_area = set(int(k) for k in re.findall(
            r'\[(\d+)\]\s*=', lua_block(tables, r'ZoneDB\.instanceIdToAreaId\s*=')))
        self.starter_to_ui = int_map(lua_block(read('Compat', 'Map.lua'), r'local starterAreaIdToUiMapId\s*='))
        lookups = read('Localization', 'lookups', 'lookupZones.lua')
        self.lookup_maps, self.lookup_areas = zone_lookup(lookups)
        self.lookup_names = set(re.findall(r'\[\d+\]\s*=\s*"((?:[^"\\]|\\.)*)"', lua_block(lookups, r'l10n\.zoneLookup\s*=')))
        # GetCurrentMapAreaID -> uiMapId; dungeon floors are "mapID.floor" (530.1): those maps count as known too
        self.by_map_id = {}
        for ui, d in sorted(self.ui_maps.items(), key=lambda t: (t[1]['mapID'] != int(t[1]['mapID']), t[1]['mapID'], t[0])):
            if d['mapID'] > 0:
                self.by_map_id.setdefault(int(d['mapID']), ui)
        if not self.ui_maps or not self.area_to_ui or not self.lookup_maps:
            raise SystemExit('%s does not look like the Questie fork (no UiMapData, areaIdToUiMapId or zoneLookup)'
                             % root)

    def area_ui(self, area):
        return self.starter_to_ui.get(area) or self.area_to_ui.get(area) or self.special_to_ui.get(area)


# ---- building ---------------------------------------------------------------------------------------------------

def num(v):
    s = '%.4f' % v
    s = s.rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def lua_str(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ') + '"'


def build(wma, fork, areas=None, types=None):
    usable = [z for z in wma if z['area'] and z['left'] != z['right'] and z['top'] != z['bottom']]
    by_wid = {z['id']: z for z in wma}

    def name_of(z):
        if areas and z['area'] in areas and areas[z['area']][2]:
            return areas[z['area']][2]
        return split_name(z['file'])

    def map_type(mapid):
        t = types.get(mapid) if types else None
        if mapid in CONTINENT_UI_MAP:
            return MAP_TYPE_ZONE
        if t is None:
            return MAP_TYPE_DUNGEON
        return {1: MAP_TYPE_DUNGEON, 2: MAP_TYPE_RAID}.get(t, MAP_TYPE_ZONE)

    # uiMapId of every usable row: the fork's (same GetCurrentMapAreaID) or a new one
    ui_of = {}
    for z in usable:
        ui_of[z['id']] = fork.by_map_id.get(z['id'] + 1) or UI_MAP_BASE + z['id']
    taken = set(fork.ui_maps)
    for z in usable:
        if ui_of[z['id']] >= UI_MAP_BASE and ui_of[z['id']] in taken:
            raise SystemExit('uiMapId %d for WorldMapArea %d is taken in the fork' % (ui_of[z['id']], z['id']))

    def parent_ui(z):
        if z['map'] in CONTINENT_UI_MAP:
            return CONTINENT_UI_MAP[z['map']]
        p = by_wid.get(z['parent'])        # instance maps: the zone the WorldMapArea row hangs under
        if p and p['id'] in ui_of:
            return ui_of[p['id']]
        if p and fork.by_map_id.get(p['id'] + 1):
            return fork.by_map_id[p['id'] + 1]
        return WORLD_UI_MAP

    new_maps, bounds, area_ui, ui_area, names, instance_area, subs = [], [], {}, {}, {}, {}, {}
    zone_areas, labels = set(), {}
    for z in usable:
        ui = ui_of[z['id']]
        b = [z['left'] - z['right'], z['top'] - z['bottom'], z['left'], z['top']]
        if ui >= UI_MAP_BASE:
            new_maps.append((ui, b, z, name_of(z)))
        else:
            # only a map of another size: the fork moves some zones on purpose (the map 530 zones it shows in the
            # instance 0/1 space), and dungeon floors use DungeonMap.dbc bounds, not ours
            d = fork.ui_maps[ui]
            have = d['bounds']
            if not d['floors'] and d['mapID'] == int(d['mapID']) and (
                    have is None or any(abs(x - y) > BOUNDS_TOLERANCE for x, y in zip(have[:2], b[:2]))):
                bounds.append((ui, b, z, have))
        if not fork.area_ui(z['area']) and z['area'] not in area_ui:
            area_ui[z['area']] = (ui, z)
        if ui not in fork.ui_to_area and ui not in ui_area:
            ui_area[ui] = z['area']
        if z['area'] not in fork.lookup_areas and z['area'] not in names:
            names[z['area']] = (z['map'], name_of(z))
        if ui >= UI_MAP_BASE and z['map'] not in CONTINENT_UI_MAP and z['map'] not in fork.instance_to_area \
                and z['map'] not in instance_area:
            instance_area[z['map']] = z['area']
        zone_areas.add(z['area'])
        labels[z['area']] = name_of(z)

    # sub areas of the zones we add (AreaTable): name, and the parent zone for the zone text lookups
    if areas:
        added = set(area_ui)

        def owner(aid):
            seen = set()
            while aid and aid not in seen:
                seen.add(aid)
                parent = areas.get(aid, (0, 0, ''))[1]
                if parent in zone_areas:
                    return parent
                aid = parent
            return None
        for aid, (mapid, parent, name) in sorted(areas.items()):
            # a sub area named like a zone the fork has (generic seas like "The Great Sea", walls on two zones)
            # would send that name's zone text to our zone: left out
            if aid in zone_areas or not name or name in fork.lookup_names:
                continue
            zone = owner(aid)
            if zone not in added:
                continue
            if aid not in fork.sub_to_parent:
                subs[aid] = zone
            labels[aid] = name
            if aid not in fork.lookup_areas and aid not in names:
                names[aid] = (mapid, name)
    return {'new_maps': new_maps, 'bounds': bounds, 'area_ui': area_ui, 'ui_area': ui_area, 'names': names,
            'instance_area': instance_area, 'subs': subs, 'parent_ui': parent_ui, 'map_type': map_type,
            'types': types, 'labels': labels}


def render(res, fork, sources):
    L = []
    w = L.append
    w('-- Made by modules-ci ci/gen_questie_zones.py from the Temucraft %s. Do not edit: the next run writes it anew.'
      % ', '.join(sources))
    w('-- The zones of our WorldMapArea.dbc that the fork lacks. Loads after Localization/lookups/lookupZones.lua.')
    w('---@type ZoneDB')
    w('local ZoneDB = QuestieLoader:ImportModule("ZoneDB")')
    w('---@type l10n')
    w('local l10n = QuestieLoader:ImportModule("l10n")')
    w('local private = ZoneDB.private')
    w('local UiMapData = QuestieCompat.UiMapData')
    w('')
    w('-- [uiMapId] = { width, height, left, top, mapType, parentMapID, mapID (WorldMapArea id + 1), instance (map id), name }')
    w('local uiMaps = {')
    for ui, b, z, name in sorted(res['new_maps']):
        w('    [%d] = { %s, %s, %s, %s, mapType = %d, parentMapID = %d, mapID = %d, instance = %d, name = %s }, '
          '-- WorldMapArea %d %s, area %d'
          % (ui, num(b[0]), num(b[1]), num(b[2]), num(b[3]), res['map_type'](z['map']), res['parent_ui'](z),
             z['id'] + 1, z['map'], lua_str(name), z['id'], z['file'], z['area']))
    w('}')
    w('for uiMapId, data in pairs(uiMaps) do')
    w('    UiMapData[uiMapId] = data')
    w('end')
    w('')
    w('-- maps the fork has, with the bounds of our WorldMapArea.dbc: [uiMapId] = { width, height, left, top }')
    w('local bounds = {')
    for ui, b, z, have in sorted(res['bounds'], key=lambda t: t[0]):
        w('    [%d] = { %s, %s, %s, %s }, -- WorldMapArea %d %s (fork: %s)'
          % (ui, num(b[0]), num(b[1]), num(b[2]), num(b[3]), z['id'], z['file'],
             ', '.join(num(x) for x in have) if have else 'none'))
    w('}')
    w('for uiMapId, b in pairs(bounds) do')
    w('    local data = UiMapData[uiMapId]')
    w('    if data then')
    w('        data[1], data[2], data[3], data[4] = b[1], b[2], b[3], b[4]')
    w('    end')
    w('end')
    w('')

    def table(var, comment, items):
        w('-- %s' % comment)
        w('local %s = {' % var)
        for k, v, c in items:
            w('    [%d] = %d,%s' % (k, v, ' -- ' + c if c else ''))
        w('}')

    def merge(var, target):
        w('for k, v in pairs(%s) do' % var)
        w('    if %s[k] == nil then' % target)
        w('        %s[k] = v' % target)
        w('    end')
        w('end')
        w('')
    names = res['names']

    def nm(a):
        return res['labels'].get(a, '')
    table('areaIdToUiMapId', '[areaId] = uiMapId', [(a, ui, '%s (WorldMapArea %d)' % (nm(a) or z['file'], z['id']))
                                                   for a, (ui, z) in sorted(res['area_ui'].items())])
    merge('areaIdToUiMapId', 'private.areaIdToUiMapId')
    table('uiMapIdToAreaId', '[uiMapId] = areaId', [(ui, a, nm(a)) for ui, a in sorted(res['ui_area'].items())])
    merge('uiMapIdToAreaId', 'private.uiMapIdToAreaId')
    table('subZoneToParentZone', '[subAreaId] = zone areaId (AreaTable.dbc)',
          [(a, p, '%s -> %s' % (nm(a), nm(p))) for a, p in sorted(res['subs'].items())])
    merge('subZoneToParentZone', 'private.subZoneToParentZone')
    table('instanceIdToAreaId', '[map id] = areaId', [(m, a, nm(a)) for m, a in sorted(res['instance_area'].items())])
    merge('instanceIdToAreaId', 'ZoneDB.instanceIdToAreaId')

    # names, by map like lookupZones.lua; new maps go in the Journey's dungeon or battleground category
    by_map = {}
    for a, (m, n) in names.items():
        by_map.setdefault(m, []).append((a, n))
    w('-- [map id] = { [areaId] = "name" }')
    w('local zoneNames = {')
    for m in sorted(by_map):
        w('    [%d] = {' % m)
        for a, n in sorted(by_map[m]):
            w('        [%d] = %s,' % (a, lua_str(n)))
        w('    },')
    w('}')
    w('-- [map id] = Journey category of maps the fork has no zone list for')
    w('local categories = {')
    types = res['types'] or {}
    for m in sorted(by_map):
        if m in fork.lookup_maps or m in CONTINENT_UI_MAP:
            continue
        t = types.get(m)
        cat = CATEGORY_BATTLEGROUNDS if t in (3, 4) else CATEGORY_DUNGEONS if t in (1, 2) or t is None else None
        if cat:
            w('    [%d] = %d,' % (m, cat))
    w('}')
    w('for mapId, zones in pairs(zoneNames) do')
    w('    local lookup = l10n.zoneLookup[mapId]')
    w('    if not lookup then')
    w('        lookup = {}')
    w('        l10n.zoneLookup[mapId] = lookup')
    w('    end')
    w('    local category = categories[mapId] and l10n.zoneCategoryLookup[categories[mapId]]')
    w('    for areaId, name in pairs(zones) do')
    w('        if lookup[areaId] == nil then')
    w('            lookup[areaId] = name')
    w('        end')
    w('        if category and category[areaId] == nil and uiMaps[areaIdToUiMapId[areaId] or 0] then')
    w('            category[areaId] = name')
    w('        end')
    w('    end')
    w('end')
    return '\n'.join(L) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dbc', required=True, help='folder with WorldMapArea.dbc; AreaTable.dbc and Map.dbc when there')
    ap.add_argument('--questie', required=True, help='checkout of the Questie fork')
    ap.add_argument('--out', help='output file (default <questie>/Database/Zones/temucraftZones.lua)')
    a = ap.parse_args()

    fork = Fork(a.questie)
    wma = world_map_areas(os.path.join(a.dbc, 'WorldMapArea.dbc'))
    sources = ['WorldMapArea.dbc']
    apath, mpath = os.path.join(a.dbc, 'AreaTable.dbc'), os.path.join(a.dbc, 'Map.dbc')
    areas = area_table(apath) if os.path.exists(apath) else None
    types = map_types(mpath) if os.path.exists(mpath) else None
    if areas:
        sources.append('AreaTable.dbc')
    if types:
        sources.append('Map.dbc')
    res = build(wma, fork, areas, types)
    out = a.out or os.path.join(a.questie, 'Database', 'Zones', 'temucraftZones.lua')
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    with open(out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(render(res, fork, sources))
    print('zones: %d new maps, %d bounds fixed, %d area ids, %d names, %d sub areas -> %s'
          % (len(res['new_maps']), len(res['bounds']), len(res['area_ui']), len(res['names']), len(res['subs']), out))
    for ui, b, z, name in sorted(res['new_maps']):
        print('  new  %5d  WorldMapArea %4d  map %4d  area %5d  %s' % (ui, z['id'], z['map'], z['area'], name))
    for ui, b, z, have in sorted(res['bounds'], key=lambda t: t[0]):
        print('  bounds %5d  WorldMapArea %4d %s' % (ui, z['id'], z['file']))


if __name__ == '__main__':
    main()

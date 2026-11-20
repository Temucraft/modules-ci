#!/usr/bin/env python3
"""
Questie (3.3.5 backport): quest, NPC, object and item data from the Temucraft world database.

Questie keeps its data in Database/AzerothCore/acore{Quest,Npc,Object,Item}DB.lua: QuestieDB.<kind>Data is a table of
chunks, each chunk a string "return { [id]={field, field, ...}, ... }" (field order: QuestieDB.questKeys / npcKeys /
objectKeys / itemKeys in Database/{quest,npc,object,item}DB.lua). This script makes the same rows from our world
database:

  quests   quest_template (+ _addon), creature/gameobject_queststarter and _questender, items that start quests,
           objectives (creatures, objects, items, reputation, kill credit), prerequisites, exclusive groups,
           breadcrumbs, reputation rewards, exploration triggers (AreaTrigger.dbc, if given)
  npcs     creature_template and creature spawns, quest starts and ends, friendliness (FactionTemplate.dbc, if given)
  objects  gameobject_template and gameobject spawns, quest starts and ends
  items    items the quests need, give or start, with the creatures, objects and items that drop them and the
           vendors that sell them

Spawn positions become zone coordinates (0-100) with our WorldMapArea.dbc (so the new zones work too):
  x = (locLeft - worldY) / (locLeft - locRight) * 100,  y = (locTop - worldX) / (locTop - locBottom) * 100
Spawns inside instances (no map area covers them) are {-1, -1} in the instance's zone, as Questie does.

Output:
  --out DIR              write the four acore*DB.lua files into DIR (the fork's Database/AzerothCore)
  --mode add (default)   the files in DIR already exist: keep their rows (they carry Questie's own corrections) and
                         add ours for every id they don't have (our quests, NPCs, objects and items); rows of their
                         NPCs, objects and items that start, end or are needed by one of our new quests are replaced
                         by ours, so the new quests show. Everything else in the files stays.
  --mode replace         QuestieDB.<kind>Data becomes ours alone (the rest of an existing file stays).
  --zones 12,14,...      only quests given, ended or sorted in these zones (AreaTable ids) and what they need:
                         for tests, to keep the output small.

Usage:
  gen_questie_db.py --db <world database> --dbc <folder with WorldMapArea.dbc [FactionTemplate.dbc AreaTrigger.dbc]>
                    --out <Questie>/Database/AzerothCore [--mysql "mysql -uroot -p..."] [--mode add|replace]
                    [--zones 12,14]
"""
import argparse, os, re, shlex, struct, subprocess, sys
from collections import defaultdict

FILES = {'quest': 'acoreQuestDB.lua', 'npc': 'acoreNpcDB.lua', 'object': 'acoreObjectDB.lua', 'item': 'acoreItemDB.lua'}

QUEST_KEYS = ['name', 'startedBy', 'finishedBy', 'requiredLevel', 'questLevel', 'requiredRaces', 'requiredClasses',
              'objectivesText', 'triggerEnd', 'objectives', 'sourceItemId', 'preQuestGroup', 'preQuestSingle',
              'childQuests', 'inGroupWith', 'exclusiveTo', 'zoneOrSort', 'requiredSkill', 'requiredMinRep',
              'requiredMaxRep', 'requiredSourceItems', 'nextQuestInChain', 'questFlags', 'specialFlags',
              'parentQuest', 'reputationReward', 'breadcrumbForQuestId', 'breadcrumbs', 'extraObjectives',
              'requiredSpell', 'requiredSpecialization', 'requiredMaxLevel', 'availableUntilCompleted',
              'availableStartingWith', 'requiredRanks', 'disabledByQuest', 'requiredItemConditions']
NPC_KEYS = ['name', 'minLevelHealth', 'maxLevelHealth', 'minLevel', 'maxLevel', 'rank', 'spawns', 'waypoints',
            'zoneID', 'questStarts', 'questEnds', 'factionID', 'friendlyToFaction', 'subName', 'npcFlags']
OBJECT_KEYS = ['name', 'questStarts', 'questEnds', 'spawns', 'zoneID', 'factionID']
ITEM_KEYS = ['name', 'npcDrops', 'objectDrops', 'itemDrops', 'startQuest', 'questRewards', 'flags', 'foodType',
             'itemLevel', 'requiredLevel', 'ammoType', 'class', 'subClass', 'vendors', 'relatedQuests', 'teachesSpell']
KEYS = {'quest': QUEST_KEYS, 'npc': NPC_KEYS, 'object': OBJECT_KEYS, 'item': ITEM_KEYS}
TABLE = {'quest': 'questData', 'npc': 'npcData', 'object': 'objectData', 'item': 'itemData'}

# QuestFactionReward.dbc (3.3.5): reputation for RewardFactionValue 1-9 (negative: the same, lost)
FACTION_REWARD = [0, 10, 25, 75, 150, 250, 350, 500, 1000, 5]
CHUNK_TOKENS = 120000      # numbers and strings per chunk (Lua 5.1 allows 262143 constants per function)


# ---- input ------------------------------------------------------------------------------------------------------

class DB:
    def __init__(self, mysql, db):
        self.cmd = shlex.split(mysql) + ['-N', '-B', '--default-character-set=utf8mb4', db]

    def q(self, sql):
        out = subprocess.run(self.cmd + ['-e', sql], check=True, capture_output=True).stdout.decode('utf-8', 'replace')
        rows = []
        for line in out.split('\n'):
            if line:
                rows.append([None if v == 'NULL' else v.replace('\\n', '\n').replace('\\t', '\t').replace('\\\\', '\\')
                             for v in line.split('\t')])
        return rows

    def columns(self, table):
        return {r[0] for r in self.q('SHOW COLUMNS FROM `%s`' % table)}


def dbc_rows(path):
    b = open(path, 'rb').read()
    magic, n, fields, size, ssize = struct.unpack('<4s4I', b[:20])
    strings = b[20 + n * size:]
    return [b[20 + i * size:20 + (i + 1) * size] for i in range(n)], strings


def world_map_areas(path):
    rows, strings = dbc_rows(path)
    out = []
    for r in rows:
        wid, mapid, area, name_off = struct.unpack_from('<4I', r, 0)
        left, right, top, bottom = struct.unpack_from('<4f', r, 16)
        if area and left != right and top != bottom:
            out.append({'id': wid, 'map': mapid, 'area': area, 'left': left, 'right': right, 'top': top,
                        'bottom': bottom})
    return out


def faction_friendliness(path):
    """FactionTemplate id -> "A", "H", "AH" or None (hostile to both), as Questie's friendlyToFaction."""
    rows, _ = dbc_rows(path)
    out = {}
    for r in rows:
        f = struct.unpack_from('<14I', r, 0)
        tid, faction, flags, group, friend_group, enemy_group = f[:6]
        enemies, friends = f[6:10], f[10:14]
        s = ''
        for letter, mask, fac in (('A', 2, 469), ('H', 4, 67)):
            hostile = bool(enemy_group & (mask | 1)) or fac in enemies
            if bool(friend_group & mask) or fac in friends:
                hostile = False
            if not hostile:
                s += letter
        out[tid] = s or None
    return out


def area_triggers(path):
    rows, _ = dbc_rows(path)
    out = {}
    for r in rows:
        tid, mapid = struct.unpack_from('<2I', r, 0)
        x, y, z = struct.unpack_from('<3f', r, 8)
        out[tid] = (mapid, x, y)
    return out


class Zones:
    def __init__(self, wma):
        self.by_map = defaultdict(list)
        for z in wma:
            self.by_map[z['map']].append(z)

    def place(self, mapid, zone_id, x, y):
        """(zone, (x, y)): the spawn's own zone if its map area holds the point, else the biggest area holding it
        (a zone, not the small sub-maps some patches add, like Northshire inside Elwynn Forest, which Questie's zone
        list doesn't know). Instances (no area holds the point): (zone id, (-1, -1))."""
        best = None
        for z in self.by_map.get(mapid, ()):
            if z['right'] <= y <= z['left'] and z['bottom'] <= x <= z['top']:
                size = (z['left'] - z['right']) * (z['top'] - z['bottom'])
                key = (0 if z['area'] == zone_id else 1, -size)
                if best is None or key < best[0]:
                    best = (key, z)
        if best:
            z = best[1]
            px = (z['left'] - y) / (z['left'] - z['right']) * 100
            py = (z['top'] - x) / (z['top'] - z['bottom']) * 100
            return z['area'], (round(px, 2), round(py, 2))
        if zone_id:
            return zone_id, (-1, -1)
        return None, None


# ---- Lua output -------------------------------------------------------------------------------------------------

def lua(v):
    if v is None:
        return 'nil'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v == int(v):
            return str(int(v)) if abs(v) < 1e15 else repr(v)
        return repr(round(v, 2))
    if isinstance(v, str):
        s = v.replace('\\', '\\\\').replace('"', '\\"').replace('\r', '').replace('\n', '\\n')
        return '"' + s + '"'
    if isinstance(v, (list, tuple)):
        return '{' + ','.join('[%d]=%s' % (i + 1, lua(x)) for i, x in enumerate(v) if x is not None) + '}'
    if isinstance(v, dict):
        return '{' + ','.join('[%s]=%s' % (lua(k), lua(x)) for k, x in sorted(v.items()) if x is not None) + '}'
    raise TypeError(type(v))


def row(fields):
    while fields and fields[-1] is None:
        fields = fields[:-1]
    return '{' + ','.join(lua(f) for f in fields) + '}'


def chunks(rows):
    """rows: {id: lua row} -> list of chunk sources"""
    out, cur, tokens = [], [], 0
    for rid in sorted(rows):
        line = '[%d]=%s,' % (rid, rows[rid])
        if ']====]' in line:
            raise ValueError('row %d holds the long string end marker' % rid)
        t = line.count(',') + line.count('"') // 2 + 1
        if cur and tokens + t > CHUNK_TOKENS:
            out.append(cur)
            cur, tokens = [], 0
        cur.append(line)
        tokens += t
    if cur:
        out.append(cur)
    return ['[====[\nreturn {\n' + '\n'.join(c) + '\n}]====],' for c in out]


def keys_block(kind):
    name = kind + 'Keys'
    return 'QuestieDB.%s = {\n%s}\n' % (name, ''.join('    ["%s"] = %d,\n' % (k, i + 1) for i, k in enumerate(KEYS[kind])))


DATA_RE = r'QuestieDB\.%s = \{\n    __questieSourceChunks = true,\n'


def find_data(text, kind):
    """(start, end) of the QuestieDB.<kind>Data = {...} block in an existing file, or None."""
    m = re.search(DATA_RE % TABLE[kind], text)
    if not m:
        return None
    end = text.find(']====],\n}', m.end())
    if end < 0:
        return None
    return m.start(), end + len(']====],\n}')


def existing_rows(text, span):
    """id -> line of the rows in an existing data block"""
    out = {}
    for m in re.finditer(r'^\[(\d+)\]=.*$', text[span[0]:span[1]], re.M):
        out[int(m.group(1))] = m.group(0)
    return out


def data_block(kind, chunk_list):
    return 'QuestieDB.%s = {\n    __questieSourceChunks = true,\n%s\n}' % (TABLE[kind], '\n'.join(chunk_list))


def write(path, kind, rows, mode, touched):
    """rows: {id: lua row}; touched: ids whose existing rows ours replace (add mode)"""
    old = open(path, encoding='utf-8').read() if os.path.exists(path) else None
    span = find_data(old, kind) if old else None
    added = replaced = 0
    if mode == 'add' and span:
        have = existing_rows(old, span)
        new = {rid: r for rid, r in rows.items() if rid not in have or rid in touched}
        added = sum(1 for rid in new if rid not in have)
        replaced = len(new) - added
        block = old[span[0]:span[1]]
        if replaced:
            # take the replaced rows out of their chunks
            drop = {rid for rid in new if rid in have}
            block = '\n'.join(line for line in block.split('\n')
                              if not (line.startswith('[') and re.match(r'\[(\d+)\]=', line)
                                      and int(re.match(r'\[(\d+)\]=', line).group(1)) in drop))
        if new:
            body = block[:-len('\n}')]
            block = body + '\n' + '\n'.join(chunks(new)) + '\n}'
        text = old[:span[0]] + block + old[span[1]:]
    else:
        block = data_block(kind, chunks(rows) or ['[====[\nreturn {\n}]====],'])
        added = len(rows)
        if span:
            text = old[:span[0]] + block + old[span[1]:]
        else:
            text = ('-- Made by modules-ci ci/gen_questie_db.py from the Temucraft world database.\n'
                    'local QuestieDB = QuestieLoader:ImportModule("QuestieDB")\n\n'
                    + keys_block(kind) + '\n' + block + '\n')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    return added, replaced


# ---- building ---------------------------------------------------------------------------------------------------

def ints(v):
    try:
        return int(float(v)) if v not in (None, '') else 0
    except ValueError:
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True)
    ap.add_argument('--dbc', required=True, help='folder with WorldMapArea.dbc (ours); FactionTemplate.dbc and '
                    'AreaTrigger.dbc are used when there')
    ap.add_argument('--mysql', default='mysql')
    ap.add_argument('--out', required=True, help='folder for the acore*DB.lua files (Questie Database/AzerothCore)')
    ap.add_argument('--mode', choices=('add', 'replace'), default='add')
    ap.add_argument('--zones', default='', help='AreaTable zone ids, comma separated: only their quests')
    a = ap.parse_args()

    db = DB(a.mysql, a.db)
    zones = Zones(world_map_areas(os.path.join(a.dbc, 'WorldMapArea.dbc')))
    fpath = os.path.join(a.dbc, 'FactionTemplate.dbc')
    friendly = faction_friendliness(fpath) if os.path.exists(fpath) else None
    tpath = os.path.join(a.dbc, 'AreaTrigger.dbc')
    triggers = area_triggers(tpath) if os.path.exists(tpath) else {}
    only_zones = {int(z) for z in a.zones.split(',') if z.strip()}

    def pairs(table):
        d = defaultdict(set)
        for k, v in db.q('SELECT id, quest FROM `%s`' % table):
            d[int(k)].add(int(v))
        return d

    c_start, c_end = pairs('creature_queststarter'), pairs('creature_questender')
    g_start, g_end = pairs('gameobject_queststarter'), pairs('gameobject_questender')

    # spawns
    cid = 'id1' if 'id1' in db.columns('creature') else 'id'
    spawns = {'npc': defaultdict(lambda: defaultdict(list)), 'object': defaultdict(lambda: defaultdict(list))}
    for kind, sql in (('npc', 'SELECT `%s`, map, zoneId, position_x, position_y FROM creature' % cid),
                      ('object', 'SELECT id, map, zoneId, position_x, position_y FROM gameobject')):
        for eid, mapid, zone, x, y in db.q(sql):
            z, xy = zones.place(int(mapid), ints(zone), float(x), float(y))
            if z is None:
                continue
            lst = spawns[kind][int(eid)][z]
            if xy not in lst:
                lst.append(xy)

    # quests
    qcols = ('ID, LogTitle, MinLevel, QuestLevel, AllowableRaces, LogDescription, AreaDescription, StartItem, '
             'QuestSortID, Flags, RewardNextQuest, RequiredFactionId1, RequiredFactionValue1, '
             + ', '.join('RequiredNpcOrGo%d' % i for i in range(1, 5)) + ', '
             + ', '.join('RequiredItemId%d' % i for i in range(1, 7)) + ', '
             + ', '.join('ItemDrop%d' % i for i in range(1, 5)) + ', '
             + ', '.join('ObjectiveText%d' % i for i in range(1, 5)) + ', '
             + ', '.join('RewardFactionID%d, RewardFactionValue%d, RewardFactionOverride%d' % (i, i, i) for i in range(1, 6)) + ', '
             + ', '.join('RewardItem%d' % i for i in range(1, 5)) + ', '
             + ', '.join('RewardChoiceItemID%d' % i for i in range(1, 7)))
    acols = db.columns('quest_template_addon')
    addon_fields = ['MaxLevel', 'AllowableClasses', 'PrevQuestID', 'NextQuestID', 'ExclusiveGroup',
                    'BreadcrumbForQuestId', 'RequiredSkillID', 'RequiredSkillPoints', 'RequiredMinRepFaction',
                    'RequiredMaxRepFaction', 'RequiredMinRepValue', 'RequiredMaxRepValue', 'SpecialFlags']
    addon = {}
    for r in db.q('SELECT ID, %s FROM quest_template_addon' % ', '.join(
            f if f in acols else '0' for f in addon_fields)):
        addon[int(r[0])] = dict(zip(addon_fields, (ints(v) for v in r[1:])))
    names = [c.strip() for c in qcols.split(',')]
    quests = {}
    for r in db.q('SELECT %s FROM quest_template' % qcols):
        q = dict(zip(names, r))
        q['ad'] = addon.get(int(q['ID']), dict.fromkeys(addon_fields, 0))
        quests[int(q['ID'])] = q

    item_start = defaultdict(set)
    for iid, qid in db.q('SELECT entry, startquest FROM item_template WHERE startquest > 0'):
        item_start[int(qid)].add(int(iid))
    area_trigger = defaultdict(set)
    for tid, qid in db.q('SELECT id, quest FROM areatrigger_involvedrelation'):
        area_trigger[int(qid)].add(int(tid))
    kill_credit = defaultdict(set)
    ctemplate = {}
    for r in db.q('SELECT entry, name, subname, minlevel, maxlevel, `rank`, faction, npcflag, unit_class, exp, '
                  'HealthModifier, KillCredit1, KillCredit2, lootid FROM creature_template'):
        e = int(r[0])
        ctemplate[e] = r
        for kc in (ints(r[11]), ints(r[12])):
            if kc:
                kill_credit[kc].add(e)

    starts_c, ends_c, starts_g, ends_g = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    for c, s in c_start.items():
        for qid in s:
            starts_c[qid].add(c)
    for c, s in c_end.items():
        for qid in s:
            ends_c[qid].add(c)
    for o, s in g_start.items():
        for qid in s:
            starts_g[qid].add(o)
    for o, s in g_end.items():
        for qid in s:
            ends_g[qid].add(o)

    # which quests
    def quest_zones(qid, q):
        zs = {ints(q['QuestSortID'])}
        for cre in starts_c[qid] | ends_c[qid]:
            zs |= set(spawns['npc'].get(cre, {}))
        for obj in starts_g[qid] | ends_g[qid]:
            zs |= set(spawns['object'].get(obj, {}))
        return zs

    # quests nobody can take are left out, as Questie does: no quest giver (creature, object or item), disabled, or
    # a placeholder title
    disabled = {int(r[0]) for r in db.q('SELECT entry FROM disables WHERE sourceType = 1')} \
        if db.q("SHOW TABLES LIKE 'disables'") else set()
    placeholder = re.compile(r'<\s*(NYI|UNUSED|TXT|TEST)\s*>|\[(UNUSED|PH|DEPRECATED)\]|^\s*(ZZ|Z_|TEST\b)', re.I)
    quests = {qid: q for qid, q in quests.items()
              if (starts_c[qid] or starts_g[qid] or item_start[qid]) and qid not in disabled
              and not placeholder.search(q['LogTitle'] or '')}
    if only_zones:
        quests = {qid: q for qid, q in quests.items() if quest_zones(qid, q) & only_zones}

    # prerequisites (as the core reads them: NextQuestID of a quest is a prerequisite of the next one; a negative
    # ExclusiveGroup makes the whole group needed; a negative PrevQuestID needs that quest active)
    pre_group, pre_single, children, breadcrumbs, excl = (defaultdict(set) for _ in range(5))
    for qid, q in quests.items():
        ad = q['ad']
        if ad['PrevQuestID'] > 0:
            pre_single[qid].add(ad['PrevQuestID'])
        elif ad['PrevQuestID'] < 0:
            children[-ad['PrevQuestID']].add(qid)
        if ad['NextQuestID'] > 0:
            (pre_group if ad['ExclusiveGroup'] < 0 else pre_single)[ad['NextQuestID']].add(qid)
        if ad['ExclusiveGroup'] > 0:
            excl[ad['ExclusiveGroup']].add(qid)
        if ad['BreadcrumbForQuestId']:
            breadcrumbs[ad['BreadcrumbForQuestId']].add(qid)

    def lst(s):
        return sorted(s) if s else None

    needed_npcs, needed_objects, needed_items = set(), set(), set()
    item_related = defaultdict(set)
    item_rewards = defaultdict(set)
    quest_rows = {}
    for qid, q in quests.items():
        ad = q['ad']
        started = [lst(starts_c[qid]), lst(starts_g[qid]), lst(item_start[qid])]
        finished = [lst(ends_c[qid]), lst(ends_g[qid])]
        needed_npcs |= starts_c[qid] | ends_c[qid]
        needed_objects |= starts_g[qid] | ends_g[qid]
        needed_items |= item_start[qid]
        for i in item_start[qid]:
            item_related[i].add(qid)

        cre_obj, obj_obj, item_obj, credit_obj = [], [], [], []
        for i in range(1, 5):
            v = ints(q['RequiredNpcOrGo%d' % i])
            text = q['ObjectiveText%d' % i] or None
            if v > 0:
                others = sorted(kill_credit.get(v, ()))
                if others:
                    ids = sorted(set(others) | ({v} if v in spawns['npc'] else set()))
                    credit_obj.append([ids, v, text or ctemplate.get(v, [0, ''])[1]])
                    needed_npcs |= set(ids)
                else:
                    cre_obj.append([v, text])
                needed_npcs.add(v)
            elif v < 0:
                obj_obj.append([-v, text])
                needed_objects.add(-v)
        req_items = []
        for i in range(1, 7):
            v = ints(q['RequiredItemId%d' % i])
            if v:
                item_obj.append([v])
                req_items.append(v)
                needed_items.add(v)
                item_related[v].add(qid)
        rep_obj = None
        if ints(q['RequiredFactionId1']) and ints(q['RequiredFactionValue1']):
            rep_obj = [ints(q['RequiredFactionId1']), ints(q['RequiredFactionValue1'])]
        objectives = None
        if cre_obj or obj_obj or item_obj or rep_obj or credit_obj:
            objectives = [cre_obj or None, obj_obj or None, item_obj or None, rep_obj, credit_obj or None]

        source_items = []
        start_item = ints(q['StartItem'])
        for i in range(1, 5):
            v = ints(q['ItemDrop%d' % i])
            if v and v not in req_items and v != start_item and v not in source_items:
                source_items.append(v)
        for v in source_items + ([start_item] if start_item else []):
            needed_items.add(v)
            item_related[v].add(qid)
        for i in list(range(1, 5)):
            v = ints(q['RewardItem%d' % i])
            if v:
                item_rewards[v].add(qid)
        for i in range(1, 7):
            v = ints(q['RewardChoiceItemID%d' % i])
            if v:
                item_rewards[v].add(qid)

        rep = []
        for i in range(1, 6):
            fac, idx, override = ints(q['RewardFactionID%d' % i]), ints(q['RewardFactionValue%d' % i]), ints(q['RewardFactionOverride%d' % i])
            if not fac:
                continue
            value = override // 100 if override else (FACTION_REWARD[abs(idx)] * (1 if idx > 0 else -1) if 0 < abs(idx) < 10 else 0)
            if value:
                rep.append([fac, value])

        trigger = None
        for tid in sorted(area_trigger.get(qid, ())):
            t = triggers.get(tid)
            if not t:
                continue
            z, xy = zones.place(t[0], 0, t[1], t[2])
            if z is not None:
                if trigger is None:
                    trigger = [q['AreaDescription'] or q['LogDescription'] or q['LogTitle'], {}]
                trigger[1].setdefault(z, []).append(list(xy))

        group = ad['ExclusiveGroup']
        exclusive = lst(excl[group] - {qid}) if group > 0 else None
        fields = [
            q['LogTitle'] or '',
            started if any(started) else None,
            finished if any(finished) else None,
            ints(q['MinLevel']),
            ints(q['QuestLevel']),
            ints(q['AllowableRaces']),
            ad['AllowableClasses'] or None,
            [q['LogDescription']] if q['LogDescription'] else None,
            trigger,
            objectives,
            start_item or None,
            lst(pre_group[qid]),
            lst(pre_single[qid]),
            lst(children[qid]),
            None,
            exclusive,
            ints(q['QuestSortID']),
            [ad['RequiredSkillID'], ad['RequiredSkillPoints']] if ad['RequiredSkillID'] else None,
            [ad['RequiredMinRepFaction'], ad['RequiredMinRepValue']] if ad['RequiredMinRepFaction'] else None,
            [ad['RequiredMaxRepFaction'], ad['RequiredMaxRepValue']] if ad['RequiredMaxRepFaction'] else None,
            source_items or None,
            ad['NextQuestID'] or ints(q['RewardNextQuest']) or None,
            ints(q['Flags']),
            ad['SpecialFlags'] or None,
            -ad['PrevQuestID'] if ad['PrevQuestID'] < 0 else None,
            rep or None,
            ad['BreadcrumbForQuestId'] or None,
            lst(breadcrumbs[qid]),
            None, None, None,
            ad['MaxLevel'] or None,
        ]
        quest_rows[qid] = row(fields)

    # item sources
    items = {}
    if needed_items or not only_zones:
        idlist = ','.join(str(i) for i in sorted(needed_items | (set(item_rewards) if not only_zones else set()))) or '0'
        for r in db.q('SELECT entry, name, Flags, ItemLevel, RequiredLevel, ammo_type, class, subclass, startquest, '
                      'spellid_1, spelltrigger_1, spellid_2, spelltrigger_2 FROM item_template WHERE entry IN (%s)' % idlist):
            items[int(r[0])] = r

        def loot_holders(table, ids):
            """loot entry -> items in it, references followed one level"""
            out = defaultdict(set)
            refs = defaultdict(set)
            for entry, item in db.q('SELECT Entry, Item FROM reference_loot_template WHERE Item IN (%s)' % ids):
                refs[int(entry)].add(int(item))
            for entry, item, ref in db.q('SELECT Entry, Item, Reference FROM `%s` WHERE Item IN (%s) OR Reference IN (%s)'
                                         % (table, ids, ','.join(str(x) for x in refs) or '0')):
                if ints(ref) and ints(ref) in refs:
                    out[int(entry)] |= refs[ints(ref)]
                elif not ints(ref):
                    out[int(entry)].add(int(item))
            return out

        ids = ','.join(str(i) for i in items) or '0'
        npc_drops, obj_drops, item_drops, vendors = (defaultdict(set) for _ in range(4))
        creature_loot = loot_holders('creature_loot_template', ids)
        for e, r in ctemplate.items():
            lid = ints(r[13])
            if lid and lid in creature_loot:
                for i in creature_loot[lid]:
                    npc_drops[i].add(e)
        go_loot = loot_holders('gameobject_loot_template', ids)
        for e, gtype, data1 in db.q('SELECT entry, type, Data1 FROM gameobject_template WHERE type IN (3, 25)'):
            lid = ints(data1)
            if lid and lid in go_loot:
                for i in go_loot[lid]:
                    obj_drops[i].add(int(e))
        for entry, item in db.q('SELECT Entry, Item FROM item_loot_template WHERE Item IN (%s)' % ids):
            item_drops[int(item)].add(int(entry))
        for npc, item in db.q('SELECT entry, item FROM npc_vendor WHERE item IN (%s)' % ids):
            vendors[int(item)].add(int(npc))

    item_rows = {}
    for iid, r in items.items():
        teach = None
        if ints(r[9]) == 483 and ints(r[10]) == 0 and ints(r[11]):   # learning a recipe
            teach = ints(r[11])
        sq = ints(r[8])
        related = item_related.get(iid, set()) | ({sq} if sq else set())
        nd, od = npc_drops.get(iid, set()), obj_drops.get(iid, set())
        if iid in needed_items:   # only the sources of items the quests need go on the map
            needed_npcs |= nd | vendors.get(iid, set())
            needed_objects |= od
        item_rows[iid] = row([r[1] or '', lst(nd), lst(od), lst(item_drops.get(iid)), sq or None,
                              lst(item_rewards.get(iid)), ints(r[2]) or None, None, ints(r[3]), ints(r[4]),
                              ints(r[5]), ints(r[6]), ints(r[7]), lst(vendors.get(iid)), lst(related), teach])

    # npcs
    if not only_zones:
        needed_npcs |= set(spawns['npc'])
    hp = {}
    for level, cls, h0, h1, h2 in db.q('SELECT level, class, basehp0, basehp1, basehp2 FROM creature_classlevelstats'):
        hp[(int(level), int(cls))] = (ints(h0), ints(h1), ints(h2))

    def health(level, cls, exp, mod):
        h = hp.get((level, cls)) or hp.get((level, 1))
        if not h:
            return None
        return max(1, int(round(h[min(max(exp, 0), 2)] * mod)))

    npc_quest_starts, npc_quest_ends = defaultdict(set), defaultdict(set)
    for qid in quests:
        for c in starts_c[qid]:
            npc_quest_starts[c].add(qid)
        for c in ends_c[qid]:
            npc_quest_ends[c].add(qid)
    npc_rows = {}
    for e in needed_npcs:
        r = ctemplate.get(e)
        if not r:
            continue
        sp = spawns['npc'].get(e)
        zone = max(sp, key=lambda z: len(sp[z])) if sp else 0
        lo, hi, cls, exp, mod = ints(r[3]), ints(r[4]), ints(r[8]) or 1, ints(r[9]), float(r[10] or 1)
        fac = ints(r[6])
        npc_rows[e] = row([r[1] or '', health(lo, cls, exp, mod), health(hi, cls, exp, mod), lo, hi, ints(r[5]),
                           {z: [list(p) for p in pts] for z, pts in sp.items()} if sp else None, None, zone,
                           lst(npc_quest_starts.get(e)), lst(npc_quest_ends.get(e)), fac,
                           friendly.get(fac) if friendly is not None else None, r[2] or None, ints(r[7])])

    # objects
    obj_quest_starts, obj_quest_ends = defaultdict(set), defaultdict(set)
    for qid in quests:
        for o in starts_g[qid]:
            obj_quest_starts[o].add(qid)
        for o in ends_g[qid]:
            obj_quest_ends[o].add(qid)
    if not only_zones:
        needed_objects |= set(g_start) | set(g_end)
    obj_rows = {}
    if needed_objects:
        for e, name, faction in db.q('SELECT t.entry, t.name, IFNULL(a.faction, 0) FROM gameobject_template t '
                                     'LEFT JOIN gameobject_template_addon a ON a.entry = t.entry WHERE t.entry IN (%s)'
                                     % ','.join(str(o) for o in sorted(needed_objects))):
            e = int(e)
            sp = spawns['object'].get(e)
            zone = max(sp, key=lambda z: len(sp[z])) if sp else 0
            obj_rows[e] = row([name or '', lst(obj_quest_starts.get(e)), lst(obj_quest_ends.get(e)),
                               {z: [list(p) for p in pts] for z, pts in sp.items()} if sp else None, zone,
                               ints(faction) or None])

    # write; in add mode, rows of the fork's NPCs/objects/items that our new quests use are replaced
    os.makedirs(a.out, exist_ok=True)
    touched = {'quest': set(), 'npc': set(), 'object': set(), 'item': set()}
    if a.mode == 'add':
        qpath = os.path.join(a.out, FILES['quest'])
        old = open(qpath, encoding='utf-8').read() if os.path.exists(qpath) else ''
        span = find_data(old, 'quest') if old else None
        have = set(existing_rows(old, span)) if span else set()
        new_quests = set(quests) - have
        for qid in new_quests:
            touched['npc'] |= starts_c[qid] | ends_c[qid]
            touched['object'] |= starts_g[qid] | ends_g[qid]
            touched['item'] |= item_start[qid] | {i for i, s in item_related.items() if qid in s}
        print('quests the fork does not have: %d' % len(new_quests))
    for kind, rows in (('quest', quest_rows), ('npc', npc_rows), ('object', obj_rows), ('item', item_rows)):
        added, replaced = write(os.path.join(a.out, FILES[kind]), kind, rows, a.mode, touched[kind])
        print('%-7s %6d rows made, %6d written new, %5d replaced' % (kind, len(rows), added, replaced))


if __name__ == '__main__':
    main()

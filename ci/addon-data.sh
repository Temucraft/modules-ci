#!/usr/bin/env bash
# Builds the Temucraft world database from SQL alone (core fork + every module in modulesList.txt), then makes the
# data files of our addons from it and commits them to the addon repositories when they changed.
#
# Needs: a MySQL server reachable as $MYSQL (default "mysql -h127.0.0.1 -uroot -proot"), git, python3.
# Optional: DBC_ZIP_URL, a zip of the client DBCs (Spell, SpellDuration, Talent, FactionTemplate, SkillLine,
# SkillLineAbility, SkillRaceClassInfo, WorldMapArea, AreaTrigger; Leatrix Maps and AtlasLoot also use Map, AreaTable
# and TaxiNodes when they are there) and, under stock/, the stock 3.3.5a ones (Spell, Talent, TalentTab, SkillLine,
# SkillLineAbility, SkillRaceClassInfo, SpellIcon, SpellDuration, SpellRadius, SpellRange, SpellCastTimes,
# SpellRuneCost): without it the generators that need DBCs are skipped.
# Optional: PUSH=true and a token git can use (gh auth setup-git), to push the changes.
set -euo pipefail
CI_DIR=$(cd "$(dirname "$0")" && pwd)

MYSQL=${MYSQL:-"mysql -h127.0.0.1 -uroot -proot"}
DB=${DB:-temucraft_world}
WORK=${WORK:-$PWD/work}
CORE_REPO=${CORE_REPO:-https://github.com/Temucraft/azerothcore-wotlk.git}
CORE_BRANCH=${CORE_BRANCH:-Playerbot}
mkdir -p "$WORK"
cd "$WORK"
summary=()

sql() { $MYSQL --force "$DB" < "$1" > /dev/null 2>> "$WORK/sql-errors.log" || true; }

echo "::group::Core SQL"
rm -rf core
git clone -q --depth 1 --filter=blob:none --sparse -b "$CORE_BRANCH" "$CORE_REPO" core
git -C core sparse-checkout set data/sql/base/db_world data/sql/updates/db_world data/sql/updates/pending_db_world data/sql/custom/db_world
$MYSQL -e "DROP DATABASE IF EXISTS $DB; CREATE DATABASE $DB DEFAULT CHARACTER SET utf8mb4;"
for dir in base/db_world updates/db_world updates/pending_db_world custom/db_world; do
  [ -d "core/data/sql/$dir" ] || continue
  for f in $(find "core/data/sql/$dir" -name '*.sql' | sort); do sql "$f"; done
done
# the ids the core SQL has (stock): the AoWoW expansion tags leave these alone
mkdir -p stock-ids
for t in creature_template:entry gameobject_template:entry item_template:entry quest_template:ID spell_dbc:ID; do
  $MYSQL -N -B "$DB" -e "SELECT \`${t#*:}\` FROM \`${t%%:*}\`" > "stock-ids/${t%%:*}.txt" 2>> "$WORK/sql-errors.log" || true
done
echo "::endgroup::"

echo "::group::Module SQL"
rm -rf modules && mkdir modules
grep -vE '^\s*(#|$)' ../modulesList.txt | while read -r url branch; do
  name=$(basename "$url" .git)
  if git clone -q --depth 1 ${branch:+-b "$branch"} "$url" "modules/$name" 2>/dev/null; then
    # the module SQL AzerothCore loads: data/sql/<anything with world in it>, base before updates, by name
    for f in $(find "modules/$name/data/sql" -path '*world*' -name '*.sql' -not -path '*optional*' 2>/dev/null | sort); do sql "$f"; done
  else
    echo "Couldn't clone $url (private without a token?)"
  fi
done
echo "::endgroup::"
echo "SQL statements that failed (other tables or engines; usually harmless): $(grep -c ERROR "$WORK/sql-errors.log" 2>/dev/null || true)"
$MYSQL -N -e "SELECT CONCAT('trainer spells: ', COUNT(*)) FROM $DB.trainer_spell"

if [ -n "${DBC_ZIP_URL:-}" ]; then
  # our DBCs at the top of the zip, the stock 3.3.5a ones (for the AoWoW changes page) under stock/
  curl -fsSL "$DBC_ZIP_URL" -o dbc.zip && rm -rf dbc dbc-stock && mkdir dbc dbc-stock
  unzip -q -j dbc.zip -x 'stock/*' -d dbc
  unzip -q -j dbc.zip 'stock/*' -d dbc-stock 2>/dev/null || true
fi

# commits the folder if it changed: commit <repo dir> <message>
commit() {
  if [ -n "$(git -C "$1" status --porcelain)" ]; then
    git -C "$1" add -A
    git -C "$1" -c user.name="temucraft-bot" -c user.email="temucraft-bot@users.noreply.github.com" commit -q -m "$2"
    if [ "${PUSH:-false}" = true ]; then git -C "$1" push -q; fi
    summary+=("$(basename "$1"): updated")
  else
    summary+=("$(basename "$1"): no change")
  fi
}

# AoWoW: expansion tags from the module SQL (no DBCs needed); the changes page below adds to the same clone
rm -rf aowow
git clone -q --depth 1 --filter=blob:none --sparse https://github.com/Temucraft/aowow.git aowow
git -C aowow sparse-checkout set setup/temucraft setup/tools
if [ -f aowow/setup/tools/temucraft-expansions.py ]; then
  python3 -I aowow/setup/tools/temucraft-expansions.py --modules modules --stock-ids stock-ids \
    -o aowow/setup/temucraft/expansions.json
else
  summary+=("AoWoW expansion tags skipped: aowow has no setup/tools/temucraft-expansions.py yet")
fi

# AtlasLoot: loot tables of our dungeons and raids (no DBCs needed; Map.dbc, when the zip has it, for instance names)
ATLASLOOT_REPO=${ATLASLOOT_REPO:-https://github.com/Temucraft/AtlasLoot_Temucraft.git}
if git ls-remote --exit-code -q "$ATLASLOOT_REPO" HEAD > /dev/null 2>&1; then
  rm -rf AtlasLoot_Temucraft
  git clone -q "$ATLASLOOT_REPO" AtlasLoot_Temucraft
  python3 -I AtlasLoot_Temucraft/tools/gen_atlasloot_data.py --db "$DB" --mysql "$MYSQL" --modules modules \
    $( [ -d dbc ] && echo --dbc dbc )
  commit AtlasLoot_Temucraft "Loot tables from the Temucraft world database"
else
  summary+=("AtlasLoot skipped: $ATLASLOOT_REPO doesn't exist")
fi

# What's Training: class trainer spells
if [ -d dbc ]; then
  rm -rf WhatsTraining-Temucraft
  git clone -q https://github.com/Temucraft/WhatsTraining-Temucraft.git
  python3 -I WhatsTraining-Temucraft/tools/gen_wt_data.py --db "$DB" --dbc dbc --classes WhatsTraining-Temucraft/Classes --mysql "$MYSQL"
  commit WhatsTraining-Temucraft "Spell lists from the Temucraft database"

  # Loss of Control: control spells and interrupts from Spell.dbc
  rm -rf TemucraftLossOfControl
  git clone -q https://github.com/Temucraft/TemucraftLossOfControl.git
  python3 -I TemucraftLossOfControl/tools/gen_loc_data.py TemucraftLossOfControl/TemucraftLossOfControl/Data.lua dbc/Spell.dbc --duration dbc/SpellDuration.dbc
  commit TemucraftLossOfControl "Spell list from the Temucraft Spell.dbc"

  # GatherMate: herb, ore, gas, fishing and treasure spawns, zones from our WorldMapArea.dbc
  if [ -f dbc/WorldMapArea.dbc ]; then
    rm -rf GatherMate-Temucraft
    git clone -q https://github.com/Temucraft/GatherMate-Temucraft.git
    python3 -I GatherMate-Temucraft/tools/gen_gm_data.py --db "$DB" --dbc dbc --mysql "$MYSQL"
    commit GatherMate-Temucraft "Node data from the Temucraft world database"
  else
    summary+=("GatherMate skipped: the DBC zip has no WorldMapArea.dbc")
  fi

  # Leatrix Maps: zone levels and icons (entrances, flight masters, spirit healers, portals) of our new zones
  if [ -f dbc/WorldMapArea.dbc ]; then
    rm -rf Leatrix_Maps-Temucraft
    git clone -q https://github.com/Temucraft/Leatrix_Maps-Temucraft.git
    python3 -I Leatrix_Maps-Temucraft/tools/gen_ltm_data.py --db "$DB" --dbc dbc --mysql "$MYSQL"
    commit Leatrix_Maps-Temucraft "Zone levels and icons from the Temucraft world database"
  else
    summary+=("Leatrix Maps skipped: the DBC zip has no WorldMapArea.dbc")
  fi

  # Questie: quests, NPCs, objects and items from this database, into the Temucraft/Questie fork (only if the fork
  # exists). Rows the fork already has stay (they carry Questie's own corrections); ours are added for our quests.
  QUESTIE_REPO=${QUESTIE_REPO:-https://github.com/Temucraft/Questie.git}
  if [ ! -f dbc/WorldMapArea.dbc ]; then
    summary+=("Questie skipped: the DBC zip has no WorldMapArea.dbc")
  elif git ls-remote --exit-code -q "$QUESTIE_REPO" HEAD > /dev/null 2>&1; then
    rm -rf Questie
    git clone -q --depth 1 --filter=blob:none --sparse "$QUESTIE_REPO" Questie
    git -C Questie sparse-checkout set Database/AzerothCore
    python3 -I "$CI_DIR/gen_questie_db.py" --db "$DB" --dbc dbc --mysql "$MYSQL" --out Questie/Database/AzerothCore
    commit Questie "Quest, NPC, object and item data from the Temucraft world database"
  else
    summary+=("Questie skipped: $QUESTIE_REPO doesn't exist")
  fi

  # AoWoW changes page: stock DBCs against ours (the zip's, then every module's client patch) and this database
  if [ -n "$(ls dbc-stock 2>/dev/null)" ]; then
    python3 -I aowow/setup/tools/temucraft-changes.py --stock dbc-stock --layer dbc --modules modules --db "$DB" \
      --mysql "$MYSQL" --notes aowow/setup/temucraft/changes-notes.json -o aowow/setup/temucraft/changes.json
  else
    summary+=("AoWoW changes skipped: the DBC zip has no stock/ folder")
  fi
else
  summary+=("DBC generators skipped: set the DBC_ZIP_URL secret")
fi
commit aowow "Changes page and expansion tag data from the Temucraft DBCs and database"

printf '%s\n' "${summary[@]}"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then printf -- '- %s\n' "${summary[@]}" >> "$GITHUB_STEP_SUMMARY"; fi

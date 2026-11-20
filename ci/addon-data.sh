#!/usr/bin/env bash
# Builds the Temucraft world database from SQL alone (core fork + every module in modulesList.txt), then makes the
# data files of our addons from it and commits them to the addon repositories when they changed.
#
# Needs: a MySQL server reachable as $MYSQL (default "mysql -h127.0.0.1 -uroot -proot"), git, python3.
# Optional: DBC_ZIP_URL, a zip of the client DBCs (Spell, SpellDuration, Talent, FactionTemplate, SkillLine,
# SkillLineAbility, SkillRaceClassInfo, WorldMapArea) and, under stock/, the stock 3.3.5a ones (Spell, Talent, TalentTab, SkillLine,
# SkillLineAbility, SkillRaceClassInfo, SpellIcon, SpellDuration, SpellRadius, SpellRange, SpellCastTimes,
# SpellRuneCost): without it the generators that need DBCs are skipped.
# Optional: PUSH=true and a token git can use (gh auth setup-git), to push the changes.
set -euo pipefail

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

  # AoWoW changes page: stock DBCs against ours (the zip's, then every module's client patch) and this database
  if [ -n "$(ls dbc-stock 2>/dev/null)" ]; then
    rm -rf aowow
    git clone -q --depth 1 --filter=blob:none --sparse https://github.com/Temucraft/aowow.git aowow
    git -C aowow sparse-checkout set setup/temucraft setup/tools
    python3 -I aowow/setup/tools/temucraft-changes.py --stock dbc-stock --layer dbc --modules modules --db "$DB" \
      --mysql "$MYSQL" --notes aowow/setup/temucraft/changes-notes.json -o aowow/setup/temucraft/changes.json
    commit aowow "Changes page data from the Temucraft DBCs and database"
  else
    summary+=("AoWoW changes skipped: the DBC zip has no stock/ folder")
  fi
else
  summary+=("DBC generators skipped: set the DBC_ZIP_URL secret")
fi

printf '%s\n' "${summary[@]}"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then printf -- '- %s\n' "${summary[@]}" >> "$GITHUB_STEP_SUMMARY"; fi

# modules-ci

Weekly compile check for the Temucraft AzerothCore server modules.

Every Monday (04:00 UTC, or by hand under **Actions → weekly-module-build → Run workflow**) it builds the
latest core that `Temucraft/azerothcore-wotlk` is forked from (`Playerbot` branch) together with the latest
version of every module in `modulesList.txt`, taken from each module's original author.

- A module whose latest version compiles: its Temucraft fork is moved forward to exactly the tested commit.
- A module whose latest version fails: its fork stays where it is (the last version that compiled).
- A module that fails even at its fork version: it is reported as broken, and nothing else moves until it is fixed.
- The core fork only moves forward when every module compiles with the new core.

The server builds from the forks, so a broken upstream change never reaches it. Results are in
[STATUS.md](STATUS.md); a run that holds anything back fails, which sends a GitHub notification mail.

`modulesList.txt` is a copy of `/root/AzerothCore/Source/modulesList.txt` on the server, kept up to date by
`forkModules.sh` there. Updating the forks needs the org secret `FORK_SYNC_TOKEN`: a fine-grained token with
**Contents** and **Workflows** read & write on the Temucraft repositories. Without it the check still runs but
only reports.

To get the tested versions onto the server: `cd /root/AzerothCore/Source && ./cloneModules.sh --pull`, then compile.

## Addon data (`addon-data` workflow)

Daily (03:23 UTC), after every weekly build, or by hand, it builds the Temucraft world database from SQL alone: the
core fork's `data/sql` plus every module in `modulesList.txt` (no compile, MySQL 8.4 in the runner). Then each of our
addons makes its data from it and the changed files are committed to the addon's repository:

| Addon | Generator | Data |
| --- | --- | --- |
| What's Training (Temucraft) | `tools/gen_wt_data.py` | class trainer spells, level, cost, faction, talent prerequisites |
| Temucraft Loss of Control | `tools/gen_loc_data.py` | control spells and interrupt lockouts from Spell.dbc |
| Questie (Temucraft fork) | `ci/gen_questie_db.py` (here) | quests, quest givers and enders, NPC and object spawns in zone coordinates (our WorldMapArea.dbc), quest items and what drops or sells them. Runs only if Temucraft/Questie exists. The fork's own rows stay; ours are added for quests it doesn't have (our content), and its NPC / object / item rows those quests use are replaced. `--mode replace` makes the data ours alone. AreaTrigger.dbc in the zip adds exploration objectives, FactionTemplate.dbc the friendly / hostile marks. |

Secrets (org or repository):
- `FORK_SYNC_TOKEN` (already used by the weekly build): reads private modules and pushes to the addon repositories.
  Without it the data is made but not pushed.
- `DBC_ZIP_URL`: the address of `addon-data/dbc.zip` on our server (it comes with `4 - Upload release`). The client
  DBCs the generators read; without it those generators are skipped.

The SQL statements that failed (statements for tables that only exist with other modules, and the like) are kept as
the `sql-errors` artifact. AtlasLoot loot is the next generator to add.

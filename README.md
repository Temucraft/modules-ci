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

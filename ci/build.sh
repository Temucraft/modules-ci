#!/usr/bin/env bash
# Weekly compile check, run by .github/workflows/weekly-build.yml (can also be run by hand on a Linux box).
# Builds the latest core (the repo Temucraft/azerothcore-wotlk is forked from) with the latest version of every
# module in modulesList.txt, taken from the module's original author (the parent of the Temucraft fork).
#   - a module that fails to compile is put back to its Temucraft fork version (the last one that worked)
#   - a module that also fails at the fork version is disabled
#   - a failure no module is to blame for (core, link error) stops the check, nothing gets updated
# A fork with commits of its own (like the core with merged PRs) is tested as the fork merged with the latest original.
# It repeats until a build is clean and writes results.tsv for ci/sync.sh:
#   <name> <fork> <branch> <parent> <fork sha> <tested sha> <status> <ff|merge>
# status: pass (latest original compiled, fork can move to it), current (fork is already there),
#         held (latest original fails, fork kept), broken (fails even at the fork version), own (not a fork),
#         untested (the core build failed), waiting (compiled, but held back with the core because a module is broken),
#         conflict (the fork's own commits don't merge with the latest original, fork kept)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIST="$HERE/modulesList.txt"
WORK="${WORK:-$HERE/work}"
CORE_FORK="${CORE_FORK:-Temucraft/azerothcore-wotlk}"
CORE_BRANCH="${CORE_BRANCH:-Playerbot}"
# the core is compiled with these, a failure in them is a core failure
REQUIRED_MODULES=(mod-playerbots)
JOBS="${JOBS:-$(nproc)}"
RESULTS="$HERE/results.tsv"
export GIT_TERMINAL_PROMPT=0

# fetch_at <dir> <owner/repo> <sha>: shallow checkout of one commit
fetch_at() {
    rm -rf "$1" && git init -q "$1" \
        && git -C "$1" fetch -q --depth 1 "https://github.com/$2.git" "$3" \
        && git -C "$1" -c advice.detachedHead=false checkout -q FETCH_HEAD
}

# merge_at <dir> <fork> <branch> <original sha>: the fork's branch merged with a commit of its original. GitHub serves
# every commit of the fork network through the fork's URL. Leaves the fork's version checked out on a conflict.
merge_at() {
    rm -rf "$1" && git clone -q --filter=blob:none --single-branch -b "$3" "https://github.com/$2.git" "$1" \
        && git -C "$1" fetch -q --filter=blob:none origin "$4" || return 2
    git -C "$1" -c user.name=modules-ci -c user.email=modules-ci@users.noreply.github.com merge -q --no-edit FETCH_HEAD \
        || { git -C "$1" merge --abort; return 1; }
}

sha_of() { gh api "repos/$1/commits/$2" --jq .sha; }

# status_of <fork> <original> <branch> <fork sha> <original sha>: "<status> <mode>"
#   current ff: the fork already has every commit of the original    pass ff: the fork can fast-forward to it
#   pass merge: both have commits the other doesn't, so the original gets merged into the fork
status_of() {
    [[ "$4" == "$5" ]] && { echo "current ff"; return; }
    local ahead behind
    read -r ahead behind < <(gh api "repos/$1/compare/${2%%/*}:$3...${1%%/*}:$3" --jq '"\(.ahead_by) \(.behind_by)"')
    if [[ "$behind" == 0 ]]; then echo "current ff"
    elif [[ "$ahead" == 0 ]]; then echo "pass ff"
    else echo "pass merge"; fi
}

declare -A FORK BRANCH PARENT FORK_SHA UP_SHA STATUS USING MODE
names=()

echo "::group::Resolve versions"
core_parent="$(gh api "repos/$CORE_FORK" --jq '.parent.full_name // ""')"
[[ -n "$core_parent" ]] || core_parent="$CORE_FORK"
FORK[CORE]="$CORE_FORK"; BRANCH[CORE]="$CORE_BRANCH"; PARENT[CORE]="$core_parent"
FORK_SHA[CORE]="$(sha_of "$CORE_FORK" "$CORE_BRANCH")"
UP_SHA[CORE]="$(sha_of "$core_parent" "$CORE_BRANCH")"
if [[ "$core_parent" == "$CORE_FORK" ]]; then STATUS[CORE]=own; MODE[CORE]=ff
else read -r STATUS[CORE] MODE[CORE] < <(status_of "$CORE_FORK" "$core_parent" "$CORE_BRANCH" "${FORK_SHA[CORE]}" "${UP_SHA[CORE]}"); fi
# a fork that already has everything is tested (and kept) at its own commit
[[ "${STATUS[CORE]}" == pass ]] || UP_SHA[CORE]="${FORK_SHA[CORE]}"
echo "core: $core_parent@$CORE_BRANCH ${UP_SHA[CORE]:0:7} (fork ${FORK_SHA[CORE]:0:7}, ${STATUS[CORE]} ${MODE[CORE]})"

while IFS= read -r line || [[ -n "$line" ]]; do
    read -r url branch _ <<< "${line%%#*}" || true
    # local folders (like /root/AzerothCore/mod-temucraft) only exist on the server
    [[ -z "${url:-}" || "$url" == /* ]] && continue
    [[ "$url" =~ github\.com[:/]([^/]+)/([^/]+)$ ]] || { echo "::warning::not a GitHub URL, skipped: $url"; continue; }
    fork="${BASH_REMATCH[1]}/${BASH_REMATCH[2]%.git}"
    name="${fork#*/}"
    IFS=$'\t' read -r default parent < <(gh api "repos/$fork" --jq '[.default_branch, (.parent.full_name // "")] | @tsv')
    names+=("$name")
    FORK[$name]="$fork"; BRANCH[$name]="${branch:-$default}"; PARENT[$name]="$parent"
    FORK_SHA[$name]="$(sha_of "$fork" "${BRANCH[$name]}")"
    if [[ -n "$parent" ]]; then
        UP_SHA[$name]="$(sha_of "$parent" "${BRANCH[$name]}")" || UP_SHA[$name]="${FORK_SHA[$name]}"
    else
        UP_SHA[$name]="${FORK_SHA[$name]}"
    fi
    if [[ -z "$parent" ]]; then STATUS[$name]=own; MODE[$name]=ff
    else read -r STATUS[$name] MODE[$name] < <(status_of "$fork" "$parent" "${BRANCH[$name]}" "${FORK_SHA[$name]}" "${UP_SHA[$name]}"); fi
    [[ "${STATUS[$name]}" == pass ]] || UP_SHA[$name]="${FORK_SHA[$name]}"
    printf '%-32s %-8s %-5s %s\n' "$name" "${STATUS[$name]}" "${MODE[$name]}" "${parent:-$fork}@${BRANCH[$name]} ${UP_SHA[$name]:0:7}"
done < "$LIST"
echo "::endgroup::"

write_results() {
    local n
    : > "$RESULTS"
    for n in CORE "${names[@]}"; do
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$n" "${FORK[$n]}" "${BRANCH[$n]}" "${PARENT[$n]:--}" \
            "${FORK_SHA[$n]}" "${UP_SHA[$n]}" "${STATUS[$n]}" "${MODE[$n]}" >> "$RESULTS"
    done
}

echo "::group::Checkout"
mkdir -p "$WORK"
# checkout <name> <dir>: the version to test. The latest original (merged with the fork's own commits in merge mode),
# or the fork itself when it has no original, is already up to date, or doesn't merge.
checkout() {
    local n="$1" dir="$2" rc
    if [[ "${STATUS[$n]}" != pass ]]; then
        USING[$n]=fork; fetch_at "$dir" "${FORK[$n]}" "${FORK_SHA[$n]}"; return
    fi
    USING[$n]=upstream
    [[ "${MODE[$n]}" == merge ]] || { fetch_at "$dir" "${PARENT[$n]}" "${UP_SHA[$n]}"; return; }
    merge_at "$dir" "${FORK[$n]}" "${BRANCH[$n]}" "${UP_SHA[$n]}"; rc=$?
    if (( rc == 1 )); then
        echo "::warning::$n: the fork's own commits conflict with the latest ${PARENT[$n]}, testing the fork version"
        STATUS[$n]=conflict; USING[$n]=fork; UP_SHA[$n]="${FORK_SHA[$n]}"; return 0
    fi
    return $rc
}
checkout CORE "$WORK/core" || { echo "::error::could not check out the core"; STATUS[CORE]=untested; write_results; exit 1; }
for n in "${names[@]}"; do
    checkout "$n" "$WORK/core/modules/$n" || { echo "::error::could not check out $n"; exit 1; }
done
echo "::endgroup::"

is_required() { [[ " ${REQUIRED_MODULES[*]} " == *" $1 "* ]]; }

# configure: same module switches as compile.sh on the server (static modules are disabled with MODULE_<NAME>)
configure() {
    local -a flags=(-U'MODULE_*') dac=()
    local n
    for n in "${names[@]}"; do
        [[ "${STATUS[$n]}" == broken ]] || continue
        flags+=("-DMODULE_${n^^}=disabled")
        # mod-ale must stay out of DISABLED_AC_MODULES: the loader always expects its lualib target
        [[ "$n" == mod-ale ]] || dac+=("$n")
    done
    local IFS=';'
    cmake -S "$WORK/core" -B "$WORK/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
        -DWITH_WARNINGS=0 -DTOOLS_BUILD=none -DSCRIPTS=static -DMODULES=static \
        -DDISABLED_AC_MODULES="${dac[*]}" "${flags[@]}"
}

core_failed() {
    echo "::error::the build failed and no module is to blame (core, required module or link error), see the build log"
    grep -m15 -E 'error|undefined reference|multiple definition' "$1" | cut -c1-300
    STATUS[CORE]=untested
    local n; for n in "${names[@]}"; do [[ "${STATUS[$n]}" == own ]] || STATUS[$n]=untested; done
    write_results
    exit 1
}

mkdir -p "$HERE/logs"
for round in 1 2 3 4 5 6; do
    log="$HERE/logs/round$round.log"
    echo "::group::Round $round"
    { configure && cmake --build "$WORK/build" -j "$JOBS" -- -k; } > "$log" 2>&1
    rc=$?
    echo "::endgroup::"
    if (( rc == 0 )); then echo "Round $round: build OK"; break; fi

    # a module is to blame when an error line points into its folder
    bad=()
    for n in "${names[@]}"; do
        [[ "${STATUS[$n]}" == broken ]] && continue
        grep -E 'error:|CMake Error|\*\*\* \[' "$log" | grep -q -F "/$n/" && bad+=("$n")
    done
    (( ${#bad[@]} )) || core_failed "$log"

    for n in "${bad[@]}"; do
        echo "::group::$n failed in round $round"
        grep -E 'error:|CMake Error' "$log" | grep -F "/$n/" | head -10 | cut -c1-300
        echo "::endgroup::"
        if [[ "${USING[$n]}" == upstream ]]; then
            echo "Round $round: $n fails at the latest ${PARENT[$n]}, back to the fork version ${FORK_SHA[$n]:0:7}"
            STATUS[$n]=held; USING[$n]=fork; UP_SHA[$n]="${FORK_SHA[$n]}"
            fetch_at "$WORK/core/modules/$n" "${FORK[$n]}" "${FORK_SHA[$n]}" || { echo "::error::could not check out $n"; exit 1; }
        elif is_required "$n"; then
            core_failed "$log"
        else
            echo "Round $round: $n fails even at the fork version, disabled"
            STATUS[$n]=broken
        fi
    done
done
(( rc == 0 )) || core_failed "$log"

# the core may only move forward when every module still compiles with it
if [[ "${STATUS[CORE]}" != conflict ]]; then
    STATUS[CORE]=pass
    [[ "${UP_SHA[CORE]}" == "${FORK_SHA[CORE]}" ]] && STATUS[CORE]=current
fi
for n in "${names[@]}"; do [[ "${STATUS[$n]}" == broken ]] && STATUS[CORE]=held; done
if [[ "${STATUS[CORE]}" == held ]]; then
    # the passing modules were tested against the newer core, so they wait for it too
    UP_SHA[CORE]="${FORK_SHA[CORE]}"
    for n in "${names[@]}"; do
        [[ "${STATUS[$n]}" == pass ]] && STATUS[$n]=waiting && UP_SHA[$n]="${FORK_SHA[$n]}"
    done
fi
write_results
echo "Done, see $RESULTS"

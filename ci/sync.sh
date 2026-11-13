#!/usr/bin/env bash
# Moves every fork that passed ci/build.sh forward to exactly the commit that was tested (fast-forward only,
# GitHub refuses anything else), then writes STATUS.md and the job summary. A fork with commits of its own (merge mode)
# gets the original merged in, but only when the original is still at the tested commit (else it waits a week).
# Needs GH_TOKEN with write access to the Temucraft repos (Contents + Workflows), unless SYNC=false.
# Exits 1 when a module was held back, broken or not tested, or an update failed, so GitHub sends a failure mail.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS="$HERE/results.tsv"
SYNC="${SYNC:-true}"
[[ -s "$RESULTS" ]] || { echo "::error::no results.tsv, the build step did not get far enough"; exit 1; }

declare -A label=(
    [pass]="✅ updated" [current]="✅ up to date" [own]="➖ not a fork" [held]="⚠️ latest fails, kept fork version"
    [broken]="❌ fails even at fork version" [untested]="❔ not tested (core failed)" [waiting]="⏸️ compiles, waiting for the core"
    [conflict]="⚠️ own commits conflict with the latest original, kept fork version" [moved]="⏭️ original moved during the build, next week"
)
[[ "$SYNC" == true ]] || label[pass]="🆕 compiles (not updated, sync off)"

problems=0
rows=()
while IFS=$'\t' read -r name fork branch parent fork_sha sha status mode; do
    [[ "$parent" == - ]] && parent=""  # no original (own repo)
    note=""
    if [[ "$status" == pass && "$SYNC" == true && "$mode" == merge ]]; then
        if [[ "$(gh api "repos/$parent/commits/$branch" --jq .sha 2>/dev/null)" != "$sha" ]]; then
            status=moved
        elif out="$(gh api -X POST "repos/$fork/merge-upstream" -f branch="$branch" --jq .merge_type 2>&1)"; then
            echo "merged $parent ${sha:0:7} into $fork $branch ($out)"
            note=" (merged into the fork)"
        else
            echo "::error::could not merge $parent into $fork: $out"
            status=failed; note=" ($out)"; problems=1
        fi
    elif [[ "$status" == pass && "$SYNC" == true ]]; then
        if out="$(gh api -X PATCH "repos/$fork/git/refs/heads/$branch" -f sha="$sha" -F force=false --jq .object.sha 2>&1)"; then
            echo "updated $fork $branch -> ${sha:0:7}"
        else
            echo "::error::could not update $fork: $out"
            status=failed; note=" ($out)"; problems=1
        fi
    fi
    case "$status" in held|broken|untested|waiting|failed|conflict) problems=1 ;; esac
    if [[ "$fork_sha" == "$sha" ]]; then
        change="\`${sha:0:7}\`"
    elif [[ "$mode" == merge ]]; then
        # the fork has commits the original doesn't, so show what the original adds
        change="[\`${fork_sha:0:7}\` + \`${sha:0:7}\`](https://github.com/$fork/compare/$branch...${parent%%/*}:$branch)"
    else
        change="[\`${fork_sha:0:7}\` → \`${sha:0:7}\`](https://github.com/${parent:-$fork}/compare/$fork_sha...$sha)"
    fi
    rows+=("| ${name/CORE/**core**} | [$fork](https://github.com/$fork) | ${parent:+[$parent](https://github.com/$parent)} | $change | ${label[$status]:-❌ update failed}$note |")
done < "$RESULTS"

# forks of the core without commits of their own follow it (the Modded realm builds from its own fork of the core)
CORE_FOLLOWERS="${CORE_FOLLOWERS:-Temucraft/azerothcore-wotlk-modded}"
if [[ "$SYNC" == true ]]; then
    for f in $CORE_FOLLOWERS; do
        if out="$(gh repo sync "$f" 2>&1)"; then
            echo "synced $f with the core: ${out##*$'\n'}"
        else
            echo "::error::could not sync $f with the core: $out"
            rows+=("| $f | [$f](https://github.com/$f) | | | ❌ could not follow the core: ${out##*$'\n'} |"); problems=1
        fi
    done
fi

{
    echo "# Weekly module build"
    echo
    echo "Last run: $(date -u '+%Y-%m-%d %H:%M UTC')${GITHUB_RUN_ID:+, [run log](${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/$GITHUB_RUN_ID)}"
    echo
    echo "| Module | Temucraft fork | Original | Version | Result |"
    echo "|---|---|---|---|---|"
    printf '%s\n' "${rows[@]}"
} > "$HERE/STATUS.md"
[[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && cat "$HERE/STATUS.md" >> "$GITHUB_STEP_SUMMARY"
cat "$HERE/STATUS.md"
exit $problems

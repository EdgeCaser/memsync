#!/usr/bin/env bash
# Fast-forward a memsync checkout to its tracking branch, and restart the daemon
# only if HEAD actually moved.
#
# Why this exists: the harvest runs as a fresh process on every schedule, so it
# always picks up whatever is on disk. The daemon does not. It is long-lived and
# Python does not hot-reload, so a daemon started before an update keeps running
# the old code indefinitely. One was found still executing week-old code while
# the fixes it needed sat on disk beside it.
#
# Safety properties, roughly in order of how much they matter:
#   1. Never clobbers local work. Updates only when HEAD is an ancestor of the
#      remote branch, so a diverged, ahead, or dirty checkout is left alone.
#   2. Never deploys a commit CI has not passed, and fails CLOSED: if the status
#      cannot be determined at all, nothing is updated.
#   3. Never updates while a harvest is running. Python imports lazily, so
#      swapping module files under a 45-minute harvest can mix pre- and
#      post-update code inside a single run.
#   4. Restarts the daemon only when HEAD moved, so an idle tick is silent and
#      the web UI is not bounced on every interval.
#
# Known gap, stated rather than papered over: the nightly refresh job does not
# take the advisory lock, so a restart landing inside one drops that refresh's
# work. It cannot corrupt the store, because the backup is written before the
# memory file, and the session notes remain, so the next run redoes it.
#
# Environment, all optional:
#   MEMSYNC_REPO          checkout to update       (default $HOME/github/memsync)
#   MEMSYNC_BRANCH        branch to track          (default main)
#   MEMSYNC_SERVICE       user unit to restart     (default memsync.service)
#   MEMSYNC_HARVEST_UNIT  harvest unit to check    (default memsync-harvest.service)
#   MEMSYNC_REQUIRE_CI    set to 0 to skip the CI gate (default 1)

# No -e: every failure path below is handled explicitly and exits 0, so a
# transient network blip does not turn into a failed unit and an alert.
set -uo pipefail

REPO="${MEMSYNC_REPO:-$HOME/github/memsync}"
BRANCH="${MEMSYNC_BRANCH:-main}"
SERVICE="${MEMSYNC_SERVICE:-memsync.service}"
HARVEST_UNIT="${MEMSYNC_HARVEST_UNIT:-memsync-harvest.service}"
REQUIRE_CI="${MEMSYNC_REQUIRE_CI:-1}"

# Not `date -Is`: that is GNU-only and this also has to run on macOS, where BSD
# date rejects it and every log line would carry an error instead of a time.
log() { echo "$(date +%Y-%m-%dT%H:%M:%S%z) $*"; }

# CI gate. Returns non-zero for "not verified green", which includes every
# unknown: no gh, no network, no runs recorded, runs still in progress. The
# caller treats that as "do not update", so an unreachable API stalls the
# rollout instead of waving an unverified commit through.
ci_passed() {
    local sha="$1" slug json total pending failed

    slug="$(git config --get remote.origin.url 2>/dev/null \
        | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')"
    if [ -z "$slug" ]; then
        log "cannot derive repo slug from origin; treating CI as unverified"
        return 1
    fi

    if ! command -v gh >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
        log "gh or jq missing; cannot verify CI"
        return 1
    fi

    json="$(gh api "repos/$slug/commits/$sha/check-runs" 2>/dev/null)"
    if [ -z "$json" ]; then
        log "CI lookup failed for ${sha:0:7}"
        return 1
    fi

    total="$(printf '%s' "$json" | jq -r '.total_count // 0' 2>/dev/null)"
    if ! [ "${total:-0}" -gt 0 ] 2>/dev/null; then
        log "no CI runs recorded for ${sha:0:7}"
        return 1
    fi

    pending="$(printf '%s' "$json" \
        | jq -r '[.check_runs[] | select(.status != "completed")] | length' 2>/dev/null)"
    if [ "${pending:-1}" -ne 0 ]; then
        log "CI still in progress for ${sha:0:7} (${pending} pending)"
        return 1
    fi

    failed="$(printf '%s' "$json" | jq -r '
        [ .check_runs[]
          | select(.conclusion != "success"
               and .conclusion != "neutral"
               and .conclusion != "skipped") ] | length' 2>/dev/null)"
    if [ "${failed:-1}" -ne 0 ]; then
        log "CI not green for ${sha:0:7} (${failed} not passing)"
        return 1
    fi

    return 0
}

cd "$REPO" 2>/dev/null || { log "no checkout at $REPO"; exit 0; }

git fetch --quiet origin "$BRANCH" 2>/dev/null || { log "fetch failed"; exit 0; }

before="$(git rev-parse HEAD 2>/dev/null)"
target="$(git rev-parse "origin/$BRANCH" 2>/dev/null)"
if [ -z "$before" ] || [ -z "$target" ]; then
    log "cannot resolve HEAD or origin/$BRANCH"
    exit 0
fi

if [ "$before" = "$target" ]; then
    log "already current at ${before:0:7}"
    exit 0
fi

# Only ever move forward. An ahead or diverged checkout means someone is working
# here, and their commits are worth more than this update.
if ! git merge-base --is-ancestor HEAD "origin/$BRANCH" 2>/dev/null; then
    log "HEAD not behind origin/$BRANCH (diverged or ahead); skipping"
    exit 0
fi

if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    log "checkout has uncommitted changes; skipping"
    exit 0
fi

if systemctl --user is-active --quiet "$HARVEST_UNIT" 2>/dev/null; then
    log "$HARVEST_UNIT is active; deferring to the next tick"
    exit 0
fi

# memsync's own advisory lock, which is the authoritative signal. Deliberately
# not a pgrep on the process name: any shell, editor or log tailer whose command
# line merely mentions "memsync harvest" matches that, and the update then defers
# forever with nothing actually running. Found exactly that way in testing.
#
# A lock older than the staleness window is one memsync itself would steal, so
# honouring it here would stall updates indefinitely after a killed harvest.
lock_held() {
    local lock="${MEMSYNC_LOCK:-}" root age

    if [ -z "$lock" ]; then
        root="$(sed -nE 's/^[[:space:]]*sync_root[[:space:]]*=[[:space:]]*"(.*)"[[:space:]]*$/\1/p' \
            "${MEMSYNC_CONFIG:-$HOME/.config/memsync/config.toml}" 2>/dev/null | head -1)"
        [ -n "$root" ] || return 1
        lock="$root/.claude-memory/.harvest.lock"
    fi

    [ -f "$lock" ] || return 1

    age="$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || echo 0) ))"
    if [ "$age" -ge "${MEMSYNC_LOCK_STALE:-3600}" ]; then
        log "harvest lock is stale (${age}s); ignoring it"
        return 1
    fi
    return 0
}

if lock_held; then
    log "a harvest holds the store lock; deferring to the next tick"
    exit 0
fi

if [ "$REQUIRE_CI" = "1" ] && ! ci_passed "$target"; then
    log "not updating to ${target:0:7}"
    exit 0
fi

git merge --ff-only --quiet "origin/$BRANCH" 2>/dev/null || { log "ff-merge failed"; exit 0; }

after="$(git rev-parse HEAD 2>/dev/null)"
if [ "$before" = "$after" ]; then
    log "no change after merge, still ${after:0:7}"
    exit 0
fi

log "updated ${before:0:7} -> ${after:0:7}"

# The restart is the point of the exercise: without it the daemon keeps running
# the code it started with, however current the checkout now is.
if systemctl --user restart "$SERVICE" 2>/dev/null; then
    log "restarted $SERVICE"
else
    log "restart of $SERVICE FAILED"
fi

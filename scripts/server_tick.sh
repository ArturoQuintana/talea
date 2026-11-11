#!/usr/bin/env bash
# The server tick: pull (the Mac still pushes code/docs), then run the daily
# pass. Invoked by systemd (esios-tick.timer, 11:00 + 17:00 Europe/Madrid,
# Persistent=true). The tick itself owns all safety (leak guard, retries).
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
# ff-only first; if local history has DIVERGED (an earlier push was rejected
# while ERCOT/JP Actions pushed to main), rebase our unpushed Data commits onto
# origin rather than staying stuck forever (incident 2026-11-03..09: a bare
# --ff-only failed every tick for 6 days; ES/DE/GB never reached origin and
# ERCOT never reached the mirror). Writers own disjoint Data/<slug>/ paths; on
# any conflict the rebase is aborted and the tick continues on local state.
# Never a force-push; published history is never rewritten. Deliberately NOT
# --autostash: on a dirty tree it can exit 0 with conflict markers left in a
# data file (which `git add Data` would then commit) — a dirty tree makes the
# rebase refuse instead, and git_backup retries once the tick has committed.
git pull --ff-only \
  || git pull --rebase \
  || { git rebase --abort 2>/dev/null; echo "[talea] pre-tick pull failed (continuing on local state)"; }
# Silent shadow markets FIRST (best-effort, never affect the ES tick): their
# Data/<slug>/ changes ride the ES tick's git_backup. They DO OTS-anchor (each
# gets its own Data/<slug>/ots Bitcoin proof — see cmd_tick + the test
# test_cmd_tick_silent_market_skips_heartbeat_email_git_ots); they skip only the
# heartbeat, email, and their OWN git push. They accumulate privately (not
# mirrored). The 11:00 Madrid slot = 11:00 Berlin/Paris (<12:00 DE/FR gate)
# = 04:00 CT (<10:00 ERCOT deadline): pre-publication for all.
# The server-driven shadow set comes from the REGISTRY (markets with
# driver="server", non-primary) — not a hardcoded list (Phase 0). ERCOT is
# driver="actions" (ercot.com 403s this Helsinki server — geo-block, incident
# 2026-08-23), so it's excluded here and runs on GitHub Actions instead; adding
# or moving a market never touches this script.
for m in $(uv run --no-dev python -m talea markets --driver server); do
  uv run --no-dev python -m talea tick --market "$m" \
    || echo "[talea] $m tick failed (non-fatal, silent market)"
done
uv run --no-dev python -m talea tick          # ES: commits Data/ (all markets)
rc=$?
# Pillar B — liveness monitor (2026-08-28): flag if any REGISTERED market has
# silently stopped committing (>48h since its last receipt) — the one harm
# verify_ledger cannot see (a dark market writes nothing to verify). Runs after
# all ticks so it sees the freshest state; best-effort, never affects the tick exit.
if ! LIVENESS=$(uv run --no-dev python scripts/check_liveness.py --quiet 2>&1); then
  # Journal only. The operator is told by EMAIL: the daily digest's ALERTS line
  # carries this same assessment. The ntfy phone push that used to be sent here
  # was retired 2026-11-11 (it reached the app but the phone never notified;
  # docs/incidents.md, register D-17).
  echo "[talea] LIVENESS ALERT:"; echo "$LIVENESS"
fi
scripts/publish_mirror.sh || echo "[talea] mirror publish failed (non-fatal)"
exit $rc

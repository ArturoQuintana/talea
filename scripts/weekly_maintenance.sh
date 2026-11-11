#!/usr/bin/env bash
# Weekly maintenance (systemd esios-ots-upgrade.timer, Sun 12:00 Madrid):
#   1) refresh the token-route dataset — the independent source
#   2) cross-validate the two routes (a failure lands in git, loudly)
#   3) upgrade pending OpenTimestamps proofs to Bitcoin-anchored ones
#   4) commit whatever changed, then sync with origin and push (retrying)
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
# Same pre-run pull as server_tick.sh: ff-only first; if the clone has DIVERGED
# (an earlier push never landed while ERCOT/JP Actions pushed to main), rebase
# our unpushed Data commits onto origin instead of silently staying stuck — the
# old `git pull --ff-only || true` hid six days of that (incident 2026-11-03..09).
# On any conflict the rebase is aborted and the run continues on local state.
# No --autostash (see server_tick.sh): a dirty tree must make the rebase refuse.
git pull --ff-only \
  || git pull --rebase \
  || { git rebase --abort 2>/dev/null; echo "[weekly] pre-run pull failed (continuing on local state)"; }

PYTHONPATH=tools/esios-fetcher uv run --no-dev python -m esios_fetcher \
  || echo "[weekly] token-route refresh FAILED"

if ! uv run --no-dev python scripts/crosscheck_routes.py; then
  echo "$(date -u +%FT%TZ) cross-check failed or inconclusive" \
    >> Data/CROSSCHECK-ALERTS.log
fi

if ! uv run --no-dev python scripts/audit_ots_manifests.py; then
  echo "$(date -u +%FT%TZ) unstamped ots manifest(s) found" \
    >> Data/OTS-GAPS.log
fi

# Every market's OTS proofs (ES now at Data/es/ots; all markets covered uniformly,
# fixing the old es+de-only list). Data/<slug>/ots/*.txt.ots.
for f in Data/*/ots/*.txt.ots; do
  [ -e "$f" ] || continue
  uvx --from opentimestamps-client==0.7.2 ots upgrade "$f" || true   # pinned (see __main__.OTS_CLIENT)
done
rm -f Data/*/ots/*.bak

if [ -n "$(git status --porcelain Data)" ]; then
  git add Data
  git commit -m "data: weekly maintenance (route refresh + cross-check + ots upgrade)"
fi

# Sync, then push — the rebase-retry the ERCOT/JP Actions writers use (minus
# their --autostash: a fresh Actions checkout is never dirty, this clone can be). This
# run holds the checkout for ~7 minutes (one `ots upgrade` per proof) starting
# 11:00 UTC, inside the ERCOT ladder's usual push window, so "origin moved since
# our pull" is the EXPECTED case here, not a rare race: a bare `git push` is
# rejected, and the unit failed exactly that way on 2026-11-08. Runs even when
# this pass committed nothing, so an earlier unpushed commit is flushed too.
# Writers own disjoint Data/<slug>/ paths (conflict-free); a conflict aborts the
# rebase and leaves local state intact. Never a force-push.
for attempt in 1 2 3; do
  git pull --rebase && git push && exit 0
  git rebase --abort 2>/dev/null   # no-op unless a conflict left one half-applied
  echo "[weekly] sync+push failed (attempt $attempt/3)"
  sleep "${WEEKLY_PUSH_RETRY_SLEEP:-5}"
done
echo "[weekly] PUSH-FAILED after 3 attempts (commits stay local; the next tick's git_backup retries)"
exit 1

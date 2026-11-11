#!/usr/bin/env bash
# Publish the Talea public mirror: render the site, sync the allowlist, push.
# Called after each tick on the server. Best-effort: never fails the tick. The
# mirror checkout lives at $MIRROR_DIR (default ~/ledger-mirror) with its own
# deploy key. ALLOWLIST ONLY — ops files (CLAUDE.md, plans, server
# scripts) never leave the private repo.
#
# ONE project, many markets: this ONE mirror hosts every public market
# (es de gb ercot …) under Data/<slug>/, the primary (ES) at index.html and each
# other market a first-class <slug>.html sibling, tied by a neutral Talea nav
# (render_dashboard --site). Markets stay independent units — no market is
# subordinated and none gets its own separate repo (see
# docs/talea-migration-plan.md). Replaces the separate ES-only + DE-only mirrors.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
export PATH="$HOME/.local/bin:$PATH"
MIRROR="${MIRROR_DIR:-$HOME/ledger-mirror}"
[ -d "$MIRROR/.git" ] || { echo "[mirror] $MIRROR not initialized; skipping"; exit 0; }

# The public-market set is REGISTRY-DERIVED (the single source of truth) — a new
# public market is published automatically; a private one is impossible to leak
# (it is never on the include list AND the terminal --exclude='*' drops it). If the
# lookup fails, publish NOTHING rather than wipe the mirror (fail-safe).
SLUGS="$(uv run --no-dev python -m talea markets --public 2>/dev/null)"
if [ -z "$SLUGS" ]; then
  echo "[mirror] could not resolve public markets; skipping (fail-safe)"; exit 0
fi

# Render the whole consolidated site (index landing + one page per public market).
uv run --no-dev python scripts/render_dashboard.py --site "$MIRROR"
mkdir -p "$MIRROR/docs"

# POSITIVE ALLOWLIST — deny-by-default. Only the public markets' Data/<slug>/ trees
# (plus shared public artifacts) are published; the terminal --exclude='*' drops
# everything else, so a private market subdir or a stray root file CANNOT leak —
# leaking now requires a market being flagged public in the registry, not a
# forgotten exclude. (Incident 2026-08-28: FR leaked through a deny-LIST gap; this
# flips the posture to fail-safe.) --delete-excluded purges anything now disallowed.
INCLUDES=()
for s in $SLUGS; do INCLUDES+=(--include="/$s/***"); done
rsync -a --delete --delete-excluded \
  --exclude='__pycache__' --exclude='.tick.lock' \
  "${INCLUDES[@]}" \
  --include='/esios_prices.json' \
  --include='/calibration/***' \
  --include='/README-MOVED.md' \
  --exclude='*' \
  Data/ "$MIRROR/Data/"
rsync -a --delete --exclude __pycache__ src/ "$MIRROR/src/"
rsync -a --delete --exclude __pycache__ tests/ "$MIRROR/tests/"
rsync -a --delete --exclude __pycache__ scripts/ "$MIRROR/scripts/"
cp VERIFY.md GOVERNANCE.md DATA-SOURCES.md pyproject.toml uv.lock .python-version "$MIRROR/"
[ -f LICENSE ] && cp LICENSE "$MIRROR/"
for d in backtest-baselines-2015-2026.md backtest-markets-2026-08.md ARCHITECTURE.md \
         gate-analysis-plan.md gate-verdict-2026-08.md incidents.md \
         research-track-records-2026-08.md knowledge-map.md; do
  cp "docs/$d" "$MIRROR/docs/" 2>/dev/null || true
done
cp README-public.md "$MIRROR/README.md"
# The mirror runs its OWN verify workflow so the README badge is a real,
# anonymously-verifiable check on GitHub's infra (deploy keys may push workflow
# files; PATs without `workflow` scope may not — we push via the mirror's key).
# The consolidated workflow runs verify_ledger.py --all (every public market).
mkdir -p "$MIRROR/.github/workflows"
cp mirror/verify.yml "$MIRROR/.github/workflows/verify.yml"

cd "$MIRROR"
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -q -m "Talea update $(date -u +%FT%TZ) [$SLUGS]"
  git push -q && echo "[mirror] published [$SLUGS]" || echo "[mirror] PUSH-FAILED (rides next publish)"
else
  echo "[mirror] up to date"
fi

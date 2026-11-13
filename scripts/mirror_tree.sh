#!/usr/bin/env bash
# Build the Talea public mirror's FILE SET into a folder. Nothing else: no git,
# no push. This is the one place that says what the mirror carries.
#
#   scripts/mirror_tree.sh <folder>
#
# Two callers, one list:
#   - scripts/publish_mirror.sh builds into the mirror checkout, then commits
#     and pushes it (the server, after each tick);
#   - CI builds into a temporary folder and runs the public README's command
#     there (`uv sync && uv run pytest`), so that what the README promises is
#     tested before it is published.
#
# Prints the public market slugs on stdout (one line) and everything else on
# stderr. Exits 3, having created and written nothing AT THE DESTINATION, if the
# public markets cannot be resolved (fail-safe: never publish from a broken
# lookup). Exits 2, having created and written nothing there, if the folder is
# one it must never build into: this repository, a folder inside it or
# containing it, any checkout of the private repository, the home folder, the
# root folder; or if the folder already HOLDS anything but folders and regular
# files with one link each (a write aimed at a link, a second hard link, a pipe
# or a device lands somewhere else, or never returns).
# (The lookup itself, `uv run`, works in THIS repository: it may create .venv
# here, as it does for every other command.)
#
# ALLOWLIST ONLY: ops files (CLAUDE.md, INFRA.md, the workflows, plans,
# PRIVATE-REPO) are not named here and so never leave the private repository.
#
# ONE project, many markets: this ONE mirror hosts every public market
# (es de gb ercot …) under Data/<slug>/, the primary (ES) at index.html and each
# other market a first-class <slug>.html sibling, tied by a neutral Talea nav
# (render_dashboard --site). Markets stay independent units — no market is
# subordinated and none gets its own separate repo (see
# docs/talea-migration-plan.md).
set -uo pipefail
unset CDPATH
[ $# -eq 1 ] || { echo "usage: scripts/mirror_tree.sh <folder>" >&2; exit 2; }

# ---- WHERE it may build. Everything down to "the list" only reads and refuses.
# The copies below DELETE whatever is in the folder and not on the list. Built
# into this repository that removes every private market's Data/ and replaces
# README.md with the public one; built into a folder that contains it, it empties
# that folder's src/, tests/ and scripts/. So the folder is judged first, by what
# it IS on disk (symlinks followed, `..` resolved, compared by inode and not by
# spelling), and a refusal exits 2 having created and written nothing.
TARGET="$1"
refuse() { echo "[mirror] refusing to build into '$TARGET': $1" >&2; exit 2; }
[ -n "$TARGET" ] || refuse "no folder given"
REPO="$(cd -P -- "$(dirname -- "$0")/.." && pwd -P)"
[ -n "$REPO" ] || refuse "cannot find this repository"
if [ -d "$TARGET" ]; then
  MIRROR="$(cd -P -- "$TARGET" && pwd -P)"
  FROM="$MIRROR"
else
  # not there yet: its parent must be, so that where it will be is known now
  FROM="$(cd -P -- "$(dirname -- "$TARGET")" 2>/dev/null && pwd -P)"
  [ -n "$FROM" ] || refuse "its parent folder does not exist"
  MIRROR="${FROM%/}/$(basename -- "$TARGET")"
  [ ! -e "$MIRROR" ] && [ ! -L "$MIRROR" ] || refuse "it exists and is not a folder"
fi
case "$MIRROR" in /*) ;; *) refuse "it could not be resolved to a folder";; esac
[ "$MIRROR" -ef / ] && refuse "it is the root folder"
[ -n "${HOME:-}" ] || refuse "HOME is not set, or is empty"
[ "$MIRROR" -ef "$HOME" ] && refuse "it is the home folder"
d="$FROM"                      # the folder, then every folder above it
while :; do
  [ "$d" -ef "$REPO" ] && refuse "it is this repository, or inside it"
  [ -e "$d/PRIVATE-REPO" ] && refuse "$d is a checkout of the private repository"
  up="$(dirname -- "$d")"; [ "$up" = "$d" ] && break; d="$up"   # stops at / (and at .)
done
d="$REPO"                      # every folder above this repository
while :; do
  up="$(dirname -- "$d")"; [ "$up" = "$d" ] && break; d="$up"
  [ "$d" -ef "$MIRROR" ] && refuse "it contains this repository"
done
# WHAT IS ALREADY IN IT. A folder outside the repository can still lead back
# into it: with `Data` in it a link to the checkout's Data/, the Data copy below
# would delete the private markets through that link; with README.md or
# index.html a link, or a second hard link to a file elsewhere, `cp` and the
# page renderer would overwrite the file behind it. So nothing in the folder
# may be a link, and no file in it may have a second hard link: every name the
# list writes, at any depth, and every other name too. A pipe, a socket or a
# device is refused the same way: `cp` onto a pipe never returns. So the folder
# may hold folders and regular files with one link each, and nothing else.
# Only the folder's own `.git` is not looked into, and only if it is a REAL
# folder: the builder never writes there, and a `.git` that is a link is
# reported like any other link (the publisher's git commands would follow it).
if [ -d "$MIRROR" ]; then
  found="$(cd -P -- "$MIRROR" && find . \( -path ./.git -type d \) -prune -o ! -type f ! -type d -print 2>/dev/null)" \
    || refuse "what it holds could not be read"
  found="${found%%$'\n'*}"
  if [ -n "$found" ]; then
    [ -L "$MIRROR/$found" ] && refuse "it holds a link: ${found#./}"
    refuse "it holds something that is neither a folder nor a regular file: ${found#./}"
  fi
  found="$(cd -P -- "$MIRROR" && find . \( -path ./.git -type d \) -prune -o -type f -links +1 -print 2>/dev/null)" \
    || refuse "what it holds could not be read"
  found="${found%%$'\n'*}"
  [ -z "$found" ] || refuse "it holds a file with more than one hard link: ${found#./}"
fi

# ---- the list ----
cd "$REPO" || exit 2
export PATH="$HOME/.local/bin:$PATH"

# The public-market set is REGISTRY-DERIVED (the single source of truth) — a new
# public market is published automatically; a private one is impossible to leak
# (it is never on the include list AND the terminal --exclude='*' drops it). If the
# lookup fails, build NOTHING rather than wipe the mirror (fail-safe).
SLUGS="$(uv run --no-dev python -m talea markets --public 2>/dev/null)"
if [ -z "$SLUGS" ]; then
  echo "[mirror] could not resolve public markets" >&2; exit 3
fi
mkdir -p "$MIRROR" || exit 2

# Render the whole consolidated site (index landing + one page per public market).
uv run --no-dev python scripts/render_dashboard.py --site "$MIRROR" >&2
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
  Data/ "$MIRROR/Data/" >&2
rsync -a --delete --exclude __pycache__ src/ "$MIRROR/src/" >&2
rsync -a --delete --exclude __pycache__ tests/ "$MIRROR/tests/" >&2
rsync -a --delete --exclude __pycache__ scripts/ "$MIRROR/scripts/" >&2
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

echo "$SLUGS"

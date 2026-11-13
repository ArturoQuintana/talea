#!/usr/bin/env bash
# Publish the Talea public mirror: build its file set into the mirror checkout
# (scripts/mirror_tree.sh: render the site, sync the allowlist), then commit and
# push. Called after each tick on the server. Best-effort: never fails the tick
# (every refusal below prints its reason and exits 0).
# The mirror checkout lives at $MIRROR_DIR (default ~/ledger-mirror) with its own
# deploy key. ALLOWLIST ONLY — ops files (CLAUDE.md, plans, the workflows) never
# leave the private repo; the list is in scripts/mirror_tree.sh.
set -uo pipefail

# BEFORE ANYTHING ELSE TOUCHES A FOLDER OR GIT. This script is started by the
# tick, from the private checkout, and whatever that process left in the
# environment comes with it.
#   - CDPATH makes `cd name` look in other folders first: the `cd` just below
#     could land in another checkout and run ITS scripts.
#   - Git's repository-local variables tie every git command to a repository,
#     an index, an object store or a working tree chosen by the caller, whatever
#     folder the command is run in: GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE,
#     GIT_COMMON_DIR, GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES and
#     the rest of the list git itself gives (`git rev-parse --local-env-vars`).
#     With one of them inherited, the commit below would land in, or be built
#     from, a repository that is not the mirror's.
# Both are cleared. Git is asked for its list in an emptied environment, so that
# nothing inherited can change the answer; if it gives none, or one without the
# five named here, nothing is published.
# ONE MORE, NAMED BY HAND: GIT_NAMESPACE. It is not on git's list, and with it
# set the push lands under refs/namespaces/<name>/ on the mirror's origin:
# origin's main does not move, and this script would still print "published".
# WHAT SURVIVES, on purpose: every other GIT_ variable. The push may need
# GIT_SSH_COMMAND or GIT_SSH for the mirror's deploy key; the commit takes its
# author from GIT_AUTHOR_* / GIT_COMMITTER_* or from the user's git settings
# (GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM). Those are trusted, as on main.
# ONE PATH FOR EVERY CALL: $HOME/.local/bin is put in front first, so the git
# that gives the list is the git that does the work.
unset CDPATH
export PATH="$HOME/.local/bin:$PATH"
LOCAL_VARS="$(env -i PATH="$PATH" git rev-parse --local-env-vars 2>/dev/null)"
for needed in GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY; do
  case " $(echo $LOCAL_VARS) " in *" $needed "*) ;; *)
    echo "[mirror] git did not list $needed among its repository variables; nothing built or published (fail-safe)"; exit 0;;
  esac
done
unset $LOCAL_VARS GIT_NAMESPACE

cd "$(dirname "$0")/.." || { echo "[mirror] cannot find this repository; nothing built or published"; exit 0; }

# THE MIRROR FOLDER, RESOLVED ONCE to the folder it is on disk (absolute, links
# followed). That one value goes to the builder and to every git command below,
# and no command ever changes directory into it: a relative name, or a link
# swapped later, cannot make two commands mean two folders.
MIRROR="$(cd -P -- "${MIRROR_DIR:-$HOME/ledger-mirror}" 2>/dev/null && pwd -P)"
case "$MIRROR" in /?*) ;; *)
  echo "[mirror] ${MIRROR_DIR:-$HOME/ledger-mirror} not initialized (no such folder); skipping"; exit 0;;
esac
[ -d "$MIRROR/.git" ] || { echo "[mirror] $MIRROR not initialized; skipping"; exit 0; }

# WHAT IS ESTABLISHED before the build, and again before anything is staged,
# by asking git itself about the resolved mirror folder, with the variables
# above cleared:
#   1. `.git` there is a real folder, not a link;
#   2. git's working tree is that folder (not one a `core.worktree` names);
#   3. git's own folder is that folder's `.git`;
#   4. git's COMMON folder, where the branches and the objects live, is that
#      same `.git` (a `commondir` file can name another repository's while
#      questions 2 and 3 still answer "here").
# If any answer is otherwise, or git cannot answer (the questions need git 2.13
# or later), nothing is staged and nothing is pushed.
# WHAT IS TRUSTED, as on main, and not checked here: the git program that PATH
# finds (one PATH serves every call above and below); what the mirror's own
# `.git` holds (its hooks and core.hooksPath, fsmonitor, its remotes, pushurl
# and insteadOf rules, included settings files, alternates, sparse settings, a
# special file inside it); the rest of the tick's environment; and anything
# that changes between the second asking and the push.
mirror_is_its_own_repository() {
  local top gitdir common
  [ -d "$MIRROR/.git" ] && [ ! -L "$MIRROR/.git" ] \
    || { echo "[mirror] $MIRROR/.git is a link, not the mirror's own git folder"; return 1; }
  top="$(git -C "$MIRROR" rev-parse --show-toplevel 2>/dev/null)" && [ -n "$top" ] && [ "$top" -ef "$MIRROR" ] \
    || { echo "[mirror] git's working tree for $MIRROR is not that folder (it says: '${top:-nothing}')"; return 1; }
  gitdir="$(git -C "$MIRROR" rev-parse --absolute-git-dir 2>/dev/null)" && [ -n "$gitdir" ] && [ "$gitdir" -ef "$MIRROR/.git" ] \
    || { echo "[mirror] git's folder for $MIRROR is not its own .git (it says: '${gitdir:-nothing}')"; return 1; }
  common="$(git -C "$MIRROR" rev-parse --git-common-dir 2>/dev/null)" && [ -n "$common" ] \
    || { echo "[mirror] git gave no common folder for $MIRROR"; return 1; }
  case "$common" in /*) ;; *) common="$MIRROR/$common";; esac    # git answers relative to the folder it was run in
  [ "$common" -ef "$MIRROR/.git" ] \
    || { echo "[mirror] git's common folder for $MIRROR is not its own .git (it says: '$common'): its branches and objects belong to another repository"; return 1; }
}
mirror_is_its_own_repository || { echo "[mirror] nothing built or published; skipping (fail-safe)"; exit 0; }

# WHAT the mirror carries is decided in one place, scripts/mirror_tree.sh, which
# builds the file set into a folder and prints the public market slugs. CI runs
# the same script into a temporary folder and runs the public README's command
# there, so the list published here and the list tested there cannot differ.
# If the public markets cannot be resolved (its exit 3), or it refuses the folder
# (its exit 2: this repository, a folder inside or around it, the home folder,
# or a folder that already holds anything but folders and regular files with
# one link each),
# it writes nothing and prints nothing: publish NOTHING rather than wipe the
# mirror (fail-safe). Its own line, just above this one in the log, says why.
SLUGS="$(bash scripts/mirror_tree.sh "$MIRROR")"; BUILT=$?
if [ -z "$SLUGS" ]; then
  echo "[mirror] nothing built (mirror_tree.sh exit $BUILT, its reason is on the line above); skipping (fail-safe)"; exit 0
fi

mirror_is_its_own_repository || { echo "[mirror] built but NOT published; skipping (fail-safe)"; exit 0; }
CHANGED="$(git -C "$MIRROR" status --porcelain)" \
  || { echo "[mirror] git status failed in $MIRROR; built but NOT published"; exit 0; }
if [ -n "$CHANGED" ]; then
  git -C "$MIRROR" add -A
  git -C "$MIRROR" commit -q -m "Talea update $(date -u +%FT%TZ) [$SLUGS]"
  git -C "$MIRROR" push -q && echo "[mirror] published [$SLUGS]" || echo "[mirror] PUSH-FAILED (rides next publish)"
else
  echo "[mirror] up to date"
fi

#!/usr/bin/env bash
# Publish the Talea public mirror: build its file set into a temporary folder
# (scripts/mirror_tree.sh: render the site, sync the allowlist), judge it with
# the private-content check (the gate, below), make the mirror checkout equal to
# it, check that what is staged is byte for byte what was judged, then commit
# and push. Called after each tick on the server. Never fails the tick
# (scripts/server_tick.sh goes on whatever this exits).
# HOW IT ENDS. Published (the push succeeded), or already up to date (GitHub's
# main already holds the judged tree): exit 0. Every other ending prints its
# reason, emails the operator in fixed words (`python -m talea alert`) and
# exits 1. The few endings that cannot email are listed, with the reason, in
# INFRA.md ("The publisher's gate").
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
# The checkout is entered before git is asked (cd uses no git): the emails
# below are this checkout's own command, `uv run … python -m talea alert`,
# and are always sent from it ($REPO), wherever the publisher is by then.
unset CDPATH
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.." || { echo "[mirror] cannot find this repository; nothing built or published (no email: the alert is this repository's command)"; exit 1; }
REPO="$(pwd -P)"

# THE EMAILS: fixed words only. Every reason is a sentence written in this
# script, with integers (exit codes, seconds, a count); the check's own lines,
# paths and folders go to the journal only, never into an email. No temporary
# file stands between a failure and its email: the body is piped.
# WOULD_REFUSE holds the gate's reason when, in observe mode, it would have
# refused and the publish went on; the email that says how the publish ended
# repeats it. STEP names what the publisher is doing, for a signal's email.
WOULD_REFUSE=""
STEP="the start"
send_alert() {   # $1: the subject; the body on stdin
  ( cd -- "$REPO" && uv run --no-dev python -m talea alert --subject "$1" --body-file /dev/stdin ) \
    || echo "[mirror] the email could not be sent (see the line above); the journal is the only record"
}
common_lines() {
  echo "The receipts are unaffected: the tick commits and pushes them before the publish."
  echo "The details are in the server's journal: journalctl -u esios-tick."
}
# THE ONE WAY A PUBLISH ENDS WITHOUT PUBLISHING (once the checkout is found):
# the reason in the journal, the email, exit 1. In either mode.
fail() {   # $1: the reason (fixed words and integers); $2: the subject's end (default: the reason)
  trap - TERM INT HUP
  echo "[mirror] NOT published: $1"
  { echo "The public mirror was NOT published: nothing new was pushed to it by this publish."
    echo "Reason: $1."
    [ -z "$WOULD_REFUSE" ] || echo "Before that, the private-content gate would have refused this publish (observe mode): $WOULD_REFUSE."
    common_lines
  } | send_alert "talea mirror NOT published: ${2:-$1}"
  exit 1
}
# THE ONE ENDING WHOSE OUTCOME IS NOT KNOWN: the push did not succeed (it
# failed, or a signal stopped it). GitHub may have taken the commit before the
# connection broke, so "NOT published" would not be known to be true either.
# The next publish fetches GitHub's main again and pushes only if the judged
# tree is not already there.
not_known() {   # $1: what happened (fixed words and integers)
  trap - TERM INT HUP
  echo "[mirror] NOT KNOWN whether published: $1"
  { echo "Whether the public mirror was published is not known: $1."
    echo "Whether anything reached GitHub is not known. The next publish retries: it fetches GitHub's main and pushes only if the judged tree is not there."
    [ -z "$WOULD_REFUSE" ] || echo "Before that, the private-content gate would have refused this publish (observe mode): $WOULD_REFUSE."
    common_lines
  } | send_alert "talea mirror: not known whether published ($1)"
  exit 1
}
# A SIGNAL (TERM, INT, HUP) at any point up to the end: nothing more is done.
# (bash runs this only once the command running at that moment has returned;
# see INFRA.md.)
stopped() {
  trap - TERM INT HUP
  [ "$STEP" != "the push" ] || not_known "the publisher was stopped by a signal during the push"
  fail "the publisher was stopped by a signal during $STEP"
}
trap stopped TERM INT HUP

LOCAL_VARS="$(env -i PATH="$PATH" git rev-parse --local-env-vars 2>/dev/null)"
for needed in GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY; do
  case " $(echo $LOCAL_VARS) " in *" $needed "*) ;; *)
    echo "[mirror] git did not list $needed among its repository variables (fail-safe)"
    fail "git did not list the repository variables it must clear; nothing was built";;
  esac
done
unset $LOCAL_VARS GIT_NAMESPACE

# THE MIRROR FOLDER, RESOLVED ONCE to the folder it is on disk (absolute, links
# followed). That one value goes to the builder, to git's first questions and
# to the judgement of the destination; the publisher then enters that folder
# once, checks that it entered the folder that was judged, and does everything
# after (the sync, git) there, in "." (THE DESTINATION, below).
MIRROR="$(cd -P -- "${MIRROR_DIR:-$HOME/ledger-mirror}" 2>/dev/null && pwd -P)"
case "$MIRROR" in /?*) ;; *)
  echo "[mirror] ${MIRROR_DIR:-$HOME/ledger-mirror} not initialized (no such folder)"
  fail "the mirror folder does not exist; nothing was built";;
esac
[ -d "$MIRROR/.git" ] || { echo "[mirror] $MIRROR not initialized (no .git)"; fail "the mirror folder holds no git repository; nothing was built"; }

# WHAT IS ESTABLISHED before the build, and again before the sync, by asking
# git itself about the resolved mirror folder, with the variables above
# cleared:
#   1. `.git` there is a real folder, not a link;
#   2. git's working tree is that folder (not one a `core.worktree` names);
#   3. git's own folder is that folder's `.git`;
#   4. git's COMMON folder, where the branches and the objects live, is that
#      same `.git` (a `commondir` file can name another repository's while
#      questions 2 and 3 still answer "here").
# If any answer is otherwise, or git cannot answer (the questions need git 2.13
# or later), nothing is synced, staged or pushed.
# WHAT IS TRUSTED, as on main, and not checked here: the git program that PATH
# finds (one PATH serves every call above and below); what the mirror's own
# `.git` holds (its hooks and core.hooksPath, fsmonitor, its remotes, pushurl
# and insteadOf rules, included settings files, alternates, sparse settings, a
# special file inside it); the rest of the tick's environment; and anything
# that changes between the second asking and the push. (What is staged is
# compared with the judged tree whatever the mirror's settings: below.)
mirror_is_its_own_repository() {   # $1: the folder ($MIRROR, or "." once inside it)
  local at="$1" top gitdir common
  [ -d "$at/.git" ] && [ ! -L "$at/.git" ] \
    || { echo "[mirror] $MIRROR/.git is a link, not the mirror's own git folder"; return 1; }
  top="$(git -C "$at" rev-parse --show-toplevel 2>/dev/null)" && [ -n "$top" ] && [ "$top" -ef "$at" ] \
    || { echo "[mirror] git's working tree for $MIRROR is not that folder (it says: '${top:-nothing}')"; return 1; }
  gitdir="$(git -C "$at" rev-parse --absolute-git-dir 2>/dev/null)" && [ -n "$gitdir" ] && [ "$gitdir" -ef "$at/.git" ] \
    || { echo "[mirror] git's folder for $MIRROR is not its own .git (it says: '${gitdir:-nothing}')"; return 1; }
  common="$(git -C "$at" rev-parse --git-common-dir 2>/dev/null)" && [ -n "$common" ] \
    || { echo "[mirror] git gave no common folder for $MIRROR"; return 1; }
  case "$common" in /*) ;; *) common="$at/$common";; esac    # git answers relative to the folder it was run in
  [ "$common" -ef "$at/.git" ] \
    || { echo "[mirror] git's common folder for $MIRROR is not its own .git (it says: '$common'): its branches and objects belong to another repository"; return 1; }
}
mirror_is_its_own_repository "$MIRROR" \
  || fail "the mirror folder is not its own git repository (the journal says which question failed); nothing was built"

# THE GATE'S MODE (register D-37). `observe`: the private-content check runs on
# every publish, its result and elapsed time go to the journal, the operator is
# emailed whenever it WOULD refuse, the publish goes on, and a second email says
# how it ended. `enforce`: only a pass publishes. It ships in observe; enforce
# is a one-line reviewed pull request (a reserved file) after days of observe
# runs on real ticks with no would-refuse and a known runtime. Never read from
# the environment or .env.
GATE_MODE=observe
GATE_TIMEOUT=600          # seconds; GNU timeout, then KILL 30 s later

# A FAIL OF THE GATE. In enforce mode the publish ends (fail, exit 1). In
# observe mode the operator is told now that it would have refused and that
# the publish goes on, and told at the end how it ended.
gate_fails() {   # $1: the reason (fixed words and integers)
  if [ "$GATE_MODE" = enforce ]; then
    echo "[mirror] REFUSED by the private-content gate: $1"
    fail "the private-content gate refused: $1" "the private-content gate refused"
  fi
  WOULD_REFUSE="$1"
  echo "[mirror] the private-content gate WOULD refuse (observe mode; continuing): $1"
  { echo "The private-content gate would have refused this publish. It is in observe mode, so the publish continues."
    echo "Reason: $1."
    echo "A second email says how the publish ended. If none arrives, either the publisher stopped before it could send it or that email could not be sent: the journal says which."
    common_lines
  } | send_alert "talea mirror: the private-content gate would have refused (observe mode; continuing)"
}

# WHAT the mirror carries is decided in one place, scripts/mirror_tree.sh, which
# builds the file set into a folder and prints the public market slugs. CI runs
# the same script into a temporary folder and runs the public README's command
# there, so the list published here and the list tested there cannot differ.
# It builds into a NEW temporary folder, never into the mirror itself: that
# folder is judged, and only then is the mirror's working tree made equal to it
# (rsync --delete, the mirror's .git excluded). So a stray file in the mirror
# folder, a file the builder no longer makes, or a reader's .pytest_cache there,
# is removed, never judged by its absence and never staged; and a refusal
# before the sync leaves the mirror folder exactly as it was.
# If the public markets cannot be resolved (its exit 3), or it refuses the folder
# (its exit 2), it writes nothing and prints nothing: publish NOTHING rather than
# wipe the mirror (fail-safe). Its own line, just above this one in the log, says why.
# ONE RENDER TIME PER PUBLISH. Every page prints the minute it was rendered (and
# the stale-data banner is judged at that time); the private-content check
# rebuilds the tree and requires it to equal this one byte for byte, so both
# builds must render the same time (scripts/render_dashboard.py reads
# TALEA_RENDER_EPOCH): the pages show the minute the publish started. Set here,
# never inherited: a stale value would publish an old time.
STEP="the build"
BUILD_ROOT="$(mktemp -d)" && [ -d "$BUILD_ROOT" ] || fail "no temporary folder could be made for the build; nothing was built"
trap 'rm -rf -- "$BUILD_ROOT"' EXIT
TREE="$BUILD_ROOT/tree"
TALEA_RENDER_EPOCH="$(date -u +%s)"
case "$TALEA_RENDER_EPOCH" in ''|*[!0-9]*) fail "the clock could not be read for the render time; nothing was built";; esac
export TALEA_RENDER_EPOCH
SLUGS="$(bash scripts/mirror_tree.sh "$TREE")"; BUILT=$?
if [ -z "$SLUGS" ]; then
  echo "[mirror] nothing built (mirror_tree.sh exit $BUILT, its reason is on the line above) (fail-safe)"
  fail "nothing was built (mirror_tree.sh exit $BUILT; its reason is in the journal)"
fi

# THE PRIVATE-CONTENT GATE (register D-37). The built tree is judged by
# team/private_content_check.py against MIRROR_BASE: main as the tick fetched
# it (scripts/server_tick.sh), the trust anchor for what may be public. A pass
# is exit 0 and nothing else: a finding (1), a check that cannot run (2), a
# check that does not finish (124 from timeout, 137 after the KILL), uv or
# timeout missing (127), any other exit, or no MIRROR_BASE, is a fail. The
# receipts are already committed and pushed by then (the tick runs before this
# script), so the gate can hold back a publish, never a receipt.
STEP="the private-content check"
if [ -z "${MIRROR_BASE:-}" ]; then
  gate_fails "no MIRROR_BASE, the trust anchor (a hand run, or a tick script without the gate)"
else
  GATE_START="$(date +%s)"
  GATE_OUT="$(timeout -k 30 "$GATE_TIMEOUT" uv run --no-project python -I -S team/private_content_check.py \
      --repo . --tree "$TREE" --base "$MIRROR_BASE" 2>&1)"
  GATE=$?
  GATE_SECS=$(( $(date +%s) - GATE_START ))
  [ -z "$GATE_OUT" ] || printf '%s\n' "$GATE_OUT"
  FOUND="$(printf '%s\n' "$GATE_OUT" | sed -n 's/^private-content check: \([0-9][0-9]*\) finding(s)$/\1/p' | tail -n 1)"
  case "$GATE" in
    0) echo "[mirror] private-content check passed in ${GATE_SECS}s (mode $GATE_MODE, base ${MIRROR_BASE})";;
    1) gate_fails "the check found ${FOUND:-an unknown number of} finding(s) (exit 1, after ${GATE_SECS} s)";;
    2) gate_fails "the check could not run (exit 2, after ${GATE_SECS} s)";;
    124|137) gate_fails "the check did not finish within ${GATE_TIMEOUT} s (exit $GATE, after ${GATE_SECS} s)";;
    127) gate_fails "the check could not be started: uv or timeout is missing (exit 127, after ${GATE_SECS} s)";;
    *) gate_fails "the check exited $GATE after ${GATE_SECS} s";;
  esac
fi

# THE DESTINATION. The sync below deletes whatever in the mirror folder the
# judged tree does not have. So the folder is asked about right before it, and
# the sync is bound to the folder that was asked about:
#   1. the publisher enters the mirror folder (cd -P) and checks that the
#      folder it is in IS the folder at that path, and that the path is not
#      a link (the same inode: `. -ef`; bash's own `pwd -P` re-reads the path
#      and would not see a swap);
#   2. is it still the mirror's own repository (as above, asked in ".");
#   3. may the builder write into it at all (scripts/mirror_tree.sh
#      --judge-only: not this checkout, a folder inside it or around it, any
#      checkout of the private repository, home or root, and holding nothing
#      but folders and regular files with one link each);
#   4. is the folder at that path still the folder the publisher is in.
# Everything after is done in "." (the folder the publisher is in), never
# through the path again: a folder renamed away and replaced, or replaced by a
# link, after these checks is not the one synced or committed. Any refusal:
# nothing is synced, in either mode. THE LIMIT (INFRA.md): a writer on the
# server who swaps the folder at that path during the judgement and back again,
# or changes what is inside the folder after it, is not caught here. The gate
# guards against accidental content, not against a hostile writer on the server.
STEP="the destination check"
in_the_judged_folder() { [ ! -L "$MIRROR" ] && [ . -ef "$MIRROR" ]; }   # the path is no link, and is this folder
cd -P -- "$MIRROR" 2>/dev/null || fail "the mirror folder could not be entered; nothing was synced"
in_the_judged_folder \
  || { echo "[mirror] the folder entered is not the folder at $MIRROR"; fail "the mirror folder was moved or replaced before it was judged; nothing was synced"; }
mirror_is_its_own_repository . \
  || fail "the mirror folder is no longer its own git repository (the journal says which question failed); nothing was synced"
bash "$REPO/scripts/mirror_tree.sh" --judge-only "$MIRROR"; JUDGED=$?
[ "$JUDGED" -eq 0 ] \
  || fail "the mirror folder is one the builder must never write into (mirror_tree.sh exit $JUDGED; its reason is in the journal); nothing was synced"
in_the_judged_folder \
  || { echo "[mirror] after the judgement $MIRROR is no longer the folder the publisher is in"; fail "the mirror folder was moved or replaced while it was judged; nothing was synced"; }

# THE SYNC, into "." (above). The mirror's own .git is excluded, and so never
# written, deleted or read by it: rsync's --delete leaves excluded paths alone
# (no --delete-excluded).
# --checksum: files are compared by their content, so a file whose size and time
# match the judged one but whose bytes differ is replaced too.
# rsync is already the builder's own tool (scripts/mirror_tree.sh); missing
# here (127) or failing, it is loud. A sync that stops half way leaves the
# mirror folder half synced and nothing staged: the next publish builds,
# judges and syncs again.
STEP="the sync"
rsync -a --checksum --delete --exclude=/.git "$TREE/" ./
SYNCED=$?
if [ "$SYNCED" -ne 0 ]; then
  if [ "$SYNCED" -eq 127 ]; then fail "rsync is missing (exit 127)"
  else fail "the built tree could not be put into the mirror folder (rsync exit $SYNCED)"; fi
fi

# WHAT IS STAGED IS THE JUDGED TREE, byte for byte, or nothing is committed.
# After `git add -A` (in "."), git's index (what the commit will hold) is read
# entry by entry and compared with the judged tree: the same paths, each one a
# regular file there, and each blob the hash of that file's bytes as they are
# (`git hash-object --no-filters`: no setting of the mirror's can make the two
# agree by changing both). A file git did not stage (an ignore rule, a file
# marked unchanged), a file changed after the sync, a filter that rewrites
# content, or anything in the judged tree that is neither a regular file nor a
# folder: a difference, and nothing is committed. File modes are not compared.
staged_is_the_judged_tree() {
  local entry meta path mode blob stage odd count files=() blobs=()   # an entry: "<mode> <blob> <stage>\t<path>"
  git ls-files -s -z > "$BUILD_ROOT/staged" \
    || { echo "[mirror] git could not list what is staged"; return 1; }
  while IFS= read -r -d '' entry; do
    meta="${entry%%$'\t'*}"; path="${entry#*$'\t'}"
    mode="${meta%% *}"; meta="${meta#* }"; blob="${meta%% *}"; stage="${meta#* }"
    case "$mode" in 100644|100755) ;; *) echo "[mirror] staged with mode $mode: $path"; return 1;; esac
    [ "$stage" = 0 ] || { echo "[mirror] staged in conflict: $path"; return 1; }
    [ -f "$TREE/$path" ] && [ ! -L "$TREE/$path" ] || { echo "[mirror] staged but not a file of the judged tree: $path"; return 1; }
    files+=("$TREE/$path"); blobs+=("$blob")
  done < "$BUILD_ROOT/staged"
  [ "${#files[@]}" -gt 0 ] || { echo "[mirror] nothing is staged"; return 1; }
  odd="$(cd -P -- "$TREE" && find . ! -type f ! -type d -print | head -n 1)"
  [ -z "$odd" ] || { echo "[mirror] the judged tree holds something that is neither a regular file nor a folder: $odd"; return 1; }
  count="$(cd -P -- "$TREE" && find . -type f -print0 | tr -dc '\000' | wc -c | tr -d ' ')"
  [ "$count" = "${#files[@]}" ] || { echo "[mirror] the judged tree has $count files and git staged ${#files[@]}"; return 1; }
  [ "$(printf '%s\n' "${files[@]}" | git hash-object --no-filters --stdin-paths)" = "$(printf '%s\n' "${blobs[@]}")" ] \
    || { echo "[mirror] the bytes git staged for a file are not the judged file's"; return 1; }
}
STEP="staging"
git add -A; ADDED=$?
[ "$ADDED" -eq 0 ] || fail "git could not stage the synced tree (git exit $ADDED)"
STEP="the comparison"
staged_is_the_judged_tree \
  || fail "what git staged is not byte for byte the judged tree (the journal says where they differ)"

# WHAT IS PUSHED: ONE NEW COMMIT, holding exactly the compared index, whose only
# parent is GitHub's main as fetched now. Nothing else. A commit an earlier
# publish left unpushed was not compared by THIS run, and is never pushed (it
# stays in the mirror folder's reflog only). So:
#   1. the mirror's branch must track main on GitHub (its upstream), on a
#      remote: not "." (git's name for this same repository, whose main is
#      no published state);
#   2. the compared index is written as a tree (`git write-tree`: exactly the
#      entries compared, no hook, no filter);
#   3. GitHub's main is fetched (into FETCH_HEAD); if that fails, nothing is
#      pushed;
#   4. if GitHub's main already holds that tree, it is up to date and nothing
#      is pushed; the branch is set to GitHub's main (local only: reset --soft);
#   5. otherwise one commit of that tree is made on GitHub's main (`git
#      commit-tree`: no hook runs), the branch is set to it (local only), and
#      THAT commit, by its id, is pushed to main. Never a force-push: if main
#      moved since the fetch, GitHub refuses the push, and the next publish
#      fetches again. AND NOTHING WITH IT: no tag. With `push.followTags` set,
#      git would also push every annotated tag in the mirror folder that
#      points into the pushed history (GitHub's main included) and that
#      GitHub lacks; a tag's message was never judged. So the push is told
#      `--no-follow-tags`, and the setting is turned off for that one command
#      (`-c push.followTags=false`) as well.
STEP="fetching GitHub's main"
BRANCH="$(git symbolic-ref -q --short HEAD)" || fail "the mirror folder is not on a branch; nothing was pushed"
REMOTE="$(git config --get "branch.$BRANCH.remote")"; UPSTREAM="$(git config --get "branch.$BRANCH.merge")"
[ -n "$REMOTE" ] && [ "$REMOTE" != . ] && [ "$UPSTREAM" = refs/heads/main ] \
  || { echo "[mirror] the mirror's branch $BRANCH tracks '${UPSTREAM:-nothing}' on '${REMOTE:-nothing}'"
       fail "the mirror's branch does not track main on GitHub; nothing was pushed"; }
JUDGED_TREE="$(git write-tree)"; WROTE=$?
[ "$WROTE" -eq 0 ] && [ -n "$JUDGED_TREE" ] || fail "git could not write the compared index as a tree (git exit $WROTE); nothing was pushed"
git fetch -q "$REMOTE" refs/heads/main; FETCHED=$?
[ "$FETCHED" -eq 0 ] || fail "GitHub's main could not be fetched (git exit $FETCHED); nothing was pushed"
BASE="$(git rev-parse -q --verify 'FETCH_HEAD^{commit}')" && [ -n "$BASE" ] \
  || fail "the fetch of GitHub's main gave no commit; nothing was pushed"
STEP="the commit"
NEW_COMMIT=""
if [ "$(git rev-parse -q --verify "$BASE^{tree}")" = "$JUDGED_TREE" ]; then
  git reset -q --soft "$BASE"; MOVED=$?
  [ "$MOVED" -eq 0 ] || fail "git could not set the mirror's branch to GitHub's main (git exit $MOVED); nothing was pushed"
else
  NEW_COMMIT="$(git commit-tree "$JUDGED_TREE" -p "$BASE" -m "Talea update $(date -u +%FT%TZ) [$SLUGS]")"; MADE=$?
  [ "$MADE" -eq 0 ] && [ -n "$NEW_COMMIT" ] || fail "git could not commit the judged tree (git exit $MADE); nothing was pushed"
  git reset -q --soft "$NEW_COMMIT"; MOVED=$?
  [ "$MOVED" -eq 0 ] || fail "git could not set the mirror's branch to the new commit (git exit $MOVED); nothing was pushed"
  STEP="the push"
  git -c push.followTags=false push -q --no-follow-tags "$REMOTE" "$NEW_COMMIT:refs/heads/main"; PUSHED=$?
  [ "$PUSHED" -eq 0 ] || not_known "the push failed (git exit $PUSHED)"
  trap - TERM INT HUP      # pushed: a signal from here on must not say "not known"
fi
trap - TERM INT HUP
if [ -z "$NEW_COMMIT" ]; then
  echo "[mirror] up to date [$SLUGS] (GitHub's main already holds the judged tree; nothing was pushed)"
  if [ -n "$WOULD_REFUSE" ]; then
    { echo "The public mirror was already up to date (observe mode): GitHub's main already holds exactly the judged tree, so nothing was pushed."
      echo "The private-content gate would have refused that tree. The gate's reason: $WOULD_REFUSE."
      common_lines
    } | send_alert "talea mirror already up to date (observe mode) after the gate would have refused"
  fi
else
  echo "[mirror] published [$SLUGS]"
  if [ -n "$WOULD_REFUSE" ]; then
    { echo "The public mirror was published (observe mode), although the private-content gate would have refused it."
      echo "The gate's reason: $WOULD_REFUSE."
      common_lines
    } | send_alert "talea mirror published (observe mode) after the gate would have refused"
  fi
fi
exit 0

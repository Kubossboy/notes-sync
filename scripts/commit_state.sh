#!/usr/bin/env bash
# Commit the given files (paths under var/) onto the `state` branch without touching other files there.
# Retries on a push race (the trade loop and the weekly learning job both write to `state`).
set -euo pipefail
MSG="$1"; shift
git config user.name "sync"
git config user.email "atlas@localhost"
for attempt in 1 2 3 4; do
  WT="${RUNNER_TEMP:-/tmp}/st$attempt"; rm -rf "$WT"; mkdir -p "$WT/var"
  for f in "$@"; do if [ -f "$f" ]; then cp -f "$f" "$WT/$f"; fi; done
  export GIT_INDEX_FILE="${RUNNER_TEMP:-/tmp}/state.index.$attempt" GIT_WORK_TREE="$WT"
  rm -f "$GIT_INDEX_FILE"
  PARENT=""
  if git fetch -q origin state:state 2>/dev/null || git fetch -q -f origin state:state 2>/dev/null; then
    git read-tree state; PARENT=$(git rev-parse --verify state)
  fi
  for f in "$@"; do if [ -f "$WT/$f" ]; then git add -f "$f"; fi; done
  TREE=$(git write-tree)
  if [ -n "$PARENT" ] && [ "$(git rev-parse "$PARENT^{tree}")" = "$TREE" ]; then echo "state unchanged"; exit 0; fi
  if [ -n "$PARENT" ]; then COMMIT=$(git commit-tree "$TREE" -p "$PARENT" -m "$MSG")
  else COMMIT=$(git commit-tree "$TREE" -m "$MSG"); fi
  unset GIT_INDEX_FILE GIT_WORK_TREE
  if git push -q origin "$COMMIT":refs/heads/state; then echo "pushed $MSG"; exit 0; fi
  echo "push race, retrying ($attempt)"; sleep $((attempt * 3))
done
echo "could not push to state" >&2; exit 1

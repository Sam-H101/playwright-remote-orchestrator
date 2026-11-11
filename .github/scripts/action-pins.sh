#!/usr/bin/env bash
# Fails when a workflow uses an action that isn't pinned to a full commit SHA, for release.yml's script
# tests. A tag (@v7, @v7.0.1) or a branch can be moved to other code after it was reviewed; a commit
# can't. Dependabot moves the pins, and the version comments beside them (.github/dependabot.yml).
#
# Passes:
#   OWNER/REPO@<40 hex digits>, and with a path after REPO (an action in a folder, a reusable workflow)
#   ./...                                   this repository's own, at the commit that runs
#   docker://IMAGE@sha256:<64 hex digits>
# Everything else fails, and so does a uses: this can't read: one with its value on the next line, or
# inside { }. Comment lines are skipped.
#
# Args: workflow files, or folders of them (default: .github/workflows). Prints each uses: that isn't
# pinned as FILE:LINE: and what it is, then the counts.
set -euo pipefail
shopt -s inherit_errexit nullglob

files=()
for arg in "${@:-.github/workflows}"; do
  if [ -d "$arg" ]; then files+=("$arg"/*.yml "$arg"/*.yaml); else files+=("$arg"); fi
done
[ "${#files[@]}" -gt 0 ] || { echo "no workflow files in ${*:-.github/workflows}" >&2; exit 1; }

q="[\"']?" # YAML lets the key and the value be quoted
comment='^[[:space:]]*#'
key="^[[:space:]]*(-[[:space:]]+)?${q}uses${q}[[:space:]]*:[[:space:]]*(.*)\$"
flow="[{,][[:space:]]*${q}uses${q}[[:space:]]*:"
commit='^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[^@]+)?@[0-9a-f]{40}$'
digest='^docker://[^@]+@sha256:[0-9a-f]{64}$'

pinned=0; own=0; bad=0
fail() { echo "$file:$n: $1"; bad=$((bad + 1)); }
for file in "${files[@]}"; do
  n=0
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    if [[ $line =~ $comment ]]; then continue; fi
    if [[ $line =~ $key ]]; then
      ref=${BASH_REMATCH[2]#[\"\']}
      ref=${ref%%[[:space:]\"\']*} # up to the closing quote, the comment or the line ending
      if [[ $ref == ./* ]]; then own=$((own + 1))
      elif [[ $ref =~ $commit || $ref =~ $digest ]]; then pinned=$((pinned + 1))
      else fail "${ref:-a uses: with no value on its line} isn't pinned to a full commit SHA"
      fi
    elif [[ $line =~ $flow ]]; then
      fail "a uses: inside { }: put it on a line of its own"
    fi
  done < "$file"
done
echo "workflow files: ${#files[@]}; uses: $pinned pinned, $own local, $bad not pinned"
if [ "$bad" != 0 ]; then
  echo "Pin each as OWNER/REPO@<commit SHA> # vX.Y.Z (gh api repos/OWNER/REPO/commits/tags/vX.Y.Z --jq .sha)."
  exit 1
fi

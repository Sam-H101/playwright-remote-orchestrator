#!/usr/bin/env bash
# The version the next release gets, for release.yml: on a push to master, for that merge; on a pull
# request, for merging it. Prints version=, previous= and why= lines for $GITHUB_OUTPUT. version is
# empty when there's nothing to release. Appends what each counted merge asked for to $MERGES, a
# Markdown list for the run's summary, when that's set.
#
# vX.Y.Z (released here) and pwrunner/vX.Y.Z (the runner binaries, tagged by hand) share one line
# of numbers: both publish the images as :X.Y.Z, so no two releases may have the same one.
#
# The bump is the largest that any pull request merged since the last release asks for with a label:
# release:major, release:minor, and otherwise a patch. A release queued behind another can be
# replaced by a later one, so a later one counts the labels of every merge it ships. release:skip
# (docs, CI) makes no release only while every merge waiting to go out is a skip.
#
# A commit that a release already contains is never released again, whatever re-runs or arrives late.
# On a re-run (GITHUB_RUN_ATTEMPT > 1), a commit carrying its own vX.Y.Z tag gives that version back,
# so that re-running all of a release whose images failed publishes them again.
#
# Env: EVENT (push or pull_request); SHA (the commit to release: master's tip; for a pull request,
# its base's head); PR_LABELS (pull request only: its labels, space-separated); GITHUB_REPOSITORY;
# GH_TOKEN; GITHUB_RUN_ATTEMPT.
set -euo pipefail
shopt -s inherit_errexit # a failure inside $(...) counts too

# The labels of the merged pull request a commit on master came from (none for a direct push). The
# commit's link to its pull request is lost if master is rewritten; then the "(#N)" GitHub puts at
# the end of a squash merge's subject finds it.
labels_of() {
  local found n
  found=$(gh api "repos/$GITHUB_REPOSITORY/commits/$1/pulls" --jq '[.[] | select(.merged_at != null)] | if length > 0 then "#\(.[0].number) \([.[0].labels[].name] | join(" "))" else "" end') || return 1
  if [ -z "$found" ]; then
    n=$(git log -1 --format=%s "$1" | sed -nE 's/.*\(#([0-9]+)\)$/\1/p')
    if [ -n "$n" ]; then
      found=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$n" --jq 'if .merged_at != null then "#\(.number) \([.labels[].name] | join(" "))" else "" end') || return 1
    fi
  fi
  echo "$found"
}

rank() { # 0 skip, 1 patch, 2 minor, 3 major
  case " $1 " in
    *" release:skip "*) echo 0 ;;
    *" release:major "*) echo 3 ;;
    *" release:minor "*) echo 2 ;;
    *) echo 1 ;;
  esac
}

release_tags() { grep -E '^(pwrunner/)?v[0-9]+\.[0-9]+\.[0-9]+$' || true; }
names=(skip patch minor major)
note() { if [ -n "${MERGES:-}" ]; then echo "$1" >> "$MERGES"; fi; }

none() {
  echo "version="
  echo "previous=$previous"
  echo "why=$1"
  exit 0
}

previous=$(git tag -l | release_tags | sed -E 's#^(pwrunner/)?v(.*)$#\2 &#' | sort -V | tail -n 1 | cut -d ' ' -f 2)
base=${previous#pwrunner/}
base=${base#v}
IFS=. read -r major minor patch <<<"${base:-0.0.0}"

if [ "$EVENT" = push ]; then
  own=$(git tag --points-at "$SHA" | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)
  if [ -n "$own" ] && [ "${GITHUB_RUN_ATTEMPT:-1}" -gt 1 ]; then
    echo "version=${own#v}"
    echo "previous=$previous"
    echo "why=this commit is $own already, and this is a re-run: its images are published again"
    exit 0
  fi
  shipped=$(git tag --contains "$SHA" | release_tags | sort -V | sed -n 1p)
  [ -z "$shipped" ] || none "already released in $shipped"
  bump=0 # the merge itself is in the range below
else
  bump=$(rank "${PR_LABELS:-}")
  note "- this pull request: ${names[$bump]}${PR_LABELS:+ ($PR_LABELS)}"
fi

# Merges since the last release that haven't gone out (the last 200 at most).
for commit in $(git rev-list --first-parent --max-count=200 "${previous:+$previous..}$SHA"); do
  found=$(labels_of "$commit") # on its own, so a failed lookup stops the script (set -e)
  r=$(rank "${found#* }")
  note "- \`${commit:0:7}\` ${found%% *}${found:+: }${names[$r]}$(git log -1 --format=' — %s' "$commit")"
  if [ "$r" -gt "$bump" ]; then bump=$r; fi
done
[ "$bump" != 0 ] || none "release:skip, and nothing else is waiting to go out"

case $bump in
  3) next="$((major + 1)).0.0" ;;
  2) next="$major.$((minor + 1)).0" ;;
  *) next="$major.$minor.$((patch + 1))" ;;
esac
echo "version=$next"
echo "previous=$previous"
echo "why=a ${names[$bump]} release after ${previous:-no release yet}"

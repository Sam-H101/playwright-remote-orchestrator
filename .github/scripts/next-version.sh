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
# A merge or direct push that changes only documentation (docs_file below: docs/, *.md, mkdocs.yml,
# .gitlab-ci.yml and the docs workflow) is a skip whatever its labels, with or without a pull request:
# nothing in the images or the runner changes, and the site is published by docs.yml on its own.
#
# A commit that a release already contains is never released again, whatever re-runs or arrives late.
# On a re-run (GITHUB_RUN_ATTEMPT > 1), a commit carrying its own vX.Y.Z tag gives that version back,
# so that re-running all of a release whose images failed publishes them again.
#
# Env: EVENT (push or pull_request); SHA (the commit to release: master's tip; for a pull request,
# its base's head); PR_LABELS and PR_HEAD (pull request only: its labels, space-separated, and its head
# commit); GITHUB_REPOSITORY; GH_TOKEN; GITHUB_RUN_ATTEMPT.
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

docs_file() {
  case $1 in
    docs/* | *.md | mkdocs.yml | .gitlab-ci.yml | .github/workflows/docs.yml) return 0 ;;
    *) return 1 ;;
  esac
}

# Whether the file names on stdin (one per line) are all documentation. No files at all is not.
docs_only() {
  local f any=
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    docs_file "$f" || return 1
    any=1
  done
  [ -n "$any" ]
}

# What a commit on master changed against its first parent (nothing for the first commit).
changed_files() { git diff --name-only "$1^1" "$1" 2>/dev/null || true; }

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
  docs=
  if [ -n "${PR_HEAD:-}" ] && { git diff --name-only "$SHA...$PR_HEAD" || true; } | docs_only; then
    bump=0 docs=" (documentation only)"
  fi
  note "- this pull request: ${names[$bump]}${docs}${PR_LABELS:+ ($PR_LABELS)}"
fi

# Merges since the last release that haven't gone out (the last 200 at most).
for commit in $(git rev-list --first-parent --max-count=200 "${previous:+$previous..}$SHA"); do
  found=$(labels_of "$commit") # on its own, so a failed lookup stops the script (set -e)
  r=$(rank "${found#* }")
  docs=
  if changed_files "$commit" | docs_only; then r=0 docs=" (documentation only)"; fi
  note "- \`${commit:0:7}\` ${found%% *}${found:+: }${names[$r]}${docs}$(git log -1 --format=' — %s' "$commit")"
  if [ "$r" -gt "$bump" ]; then bump=$r; fi
done
[ "$bump" != 0 ] || none "release:skip or documentation only, and nothing else is waiting to go out"

case $bump in
  3) next="$((major + 1)).0.0" ;;
  2) next="$major.$((minor + 1)).0" ;;
  *) next="$major.$minor.$((patch + 1))" ;;
esac
echo "version=$next"
echo "previous=$previous"
echo "why=a ${names[$bump]} release after ${previous:-no release yet}"

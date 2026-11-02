#!/usr/bin/env bash
# Scenario tests for next-version.sh: a throwaway git repo per case, and a stand-in gh that answers its
# two lookups from files. Run: bash .github/scripts/next-version.test.sh
set -u
SCRIPT=$(cd "$(dirname "$0")" && pwd)/next-version.sh
STUB=$(mktemp -d)
trap 'rm -rf "$STUB"' EXIT
mkdir -p "$STUB/bin"
cat > "$STUB/bin/gh" <<'GH'
#!/usr/bin/env bash
# Stand-in for the two lookups next-version.sh makes; prints what their --jq would print.
#   repos/<r>/commits/<sha>/pulls -> $STUB/labels/<sha>   ("#N label label", or nothing: no linked PR)
#   repos/<r>/pulls/<n>           -> $STUB/pulls/<n>      (the same, for a merged PR)
[ -z "${GH_FAIL:-}" ] || { echo "gh: HTTP 502" >&2; exit 1; }
echo "$2" >> "$STUB/calls"
case $2 in
  */commits/*/pulls) sha=$(echo "$2" | sed -E 's#.*/commits/([0-9a-f]+)/pulls#\1#'); cat "$STUB/labels/$sha" 2>/dev/null || true ;;
  */pulls/*) cat "$STUB/pulls/${2##*/}" 2>/dev/null || true ;;
esac
GH
chmod +x "$STUB/bin/gh"
export STUB PATH="$STUB/bin:$PATH" GITHUB_REPOSITORY=o/r GH_TOKEN=x
fails=0; n=0
repo() { rm -rf "$STUB/repo" "$STUB/labels" "$STUB/pulls" "$STUB/calls" "$STUB"/*.md; mkdir -p "$STUB/repo" "$STUB/labels" "$STUB/pulls"; cd "$STUB/repo" || exit 1; git init -q -b master; git config user.email t@t; git config user.name t; }
pr=0
commit() { pr=$((pr+1)); git commit -q --allow-empty -m "$1"; local s; s=$(git rev-parse HEAD); echo "#$pr ${2:-}" > "$STUB/labels/$s"; }
direct() { git commit -q --allow-empty -m "$1"; }
label() { echo "#$pr $1" > "$STUB/labels/$(git rev-parse HEAD)"; }
check() { # name, expected version, [expected substring in output]
  n=$((n+1)); local got; got=$(GITHUB_RUN_ATTEMPT=${GITHUB_RUN_ATTEMPT:-1} EVENT=${EVENT:-push} SHA=${SHA:-$(git rev-parse HEAD)} PR_LABELS=${PR_LABELS:-} MERGES="$STUB/merges.md" bash "$SCRIPT" 2>&1)
  local v; v=$(echo "$got" | sed -n 's/^version=//p')
  if [ "$v" = "$2" ] && { [ -z "${3:-}" ] || echo "$got" | grep -qF -- "$3"; }; then echo "ok    $1 -> ${v:-<none>}"; else echo "FAIL  $1: want '$2' ${3:+($3)} got: $(echo "$got" | tr '\n' '|')"; fails=$((fails+1)); fi
  rm -f "$STUB/merges.md"
}
repo; commit a; git tag -a pwrunner/v1.1.0 -m x; commit b; commit c; git tag -a v1.2.0 -m x
commit d; check "unlabelled merge after v1.2.0 is a patch" 1.2.1 "previous=v1.2.0"
label release:minor; check "release:minor" 1.3.0
label "documentation release:major"; check "release:major among other labels" 2.0.0
label "release:skip release:major"; check "release:skip wins on one merge" "" "release:skip"
repo; commit a; git tag v1.2.0; commit b release:minor; commit c release:skip; commit d; check "an earlier unreleased minor counts" 1.3.0
repo; commit a; git tag v1.2.0; commit b release:skip; commit c; check "an earlier skip doesn't lower it" 1.2.1
repo; commit a; git tag v1.2.0; commit b release:minor; commit c release:skip; check "skip after a waiting minor ships the minor" 1.3.0
repo; commit a; git tag v1.2.0; commit b release:skip; commit c release:skip; check "only skips waiting: no release" "" "nothing else is waiting"
repo; commit a; git tag v1.2.0; check "own v-tag: already released" "" "already released in v1.2.0"
repo; commit a; git tag v1.2.0; GITHUB_RUN_ATTEMPT=2 check "own v-tag on a re-run: publish it again" 1.2.0 "is v1.2.0 already, and this is a re-run"
repo; commit a; git tag v1.2.0; A=$(git rev-parse HEAD); commit b; git tag v1.2.1; SHA=$A GITHUB_RUN_ATTEMPT=2 check "own v-tag that isn't the newest, on a re-run" 1.2.0 "is v1.2.0 already"
repo; commit a; git tag v1.2.0; commit b; B=$(git rev-parse HEAD); commit c; git tag v1.2.1; SHA=$B check "a late or re-run older commit isn't released again" "" "already released in v1.2.1"
repo; commit a; git tag v1.2.0; commit b; git tag -a pwrunner/v1.2.1 -m x; check "own pwrunner tag: already released, no images" "" "already released in pwrunner/v1.2.1"
repo; commit a; git tag v1.2.0; commit b; git tag -a pwrunner/v1.3.0 -m x; commit c; check "a newer runner tag sets the base" 1.3.1 "previous=pwrunner/v1.3.0"
repo; commit a; git tag v1.2.0; commit b; git tag v1.10.0-rc1; git tag v1.9; git tag vfoo; commit c; check "non-release tags ignored" 1.2.1
repo; commit a; git tag v1.9.0; commit b; git tag v1.10.0; commit c; check "1.10 sorts after 1.9" 1.10.1
repo; commit a; git tag pwrunner/v1.2.0; git tag v1.2.0; commit b; check "same version on both kinds" 1.2.1
repo; commit a; commit b; check "no release yet" 0.0.1 "no release yet"
repo; commit a; git tag v1.2.0; direct "pushed straight to master"; check "a direct push is a patch" 1.2.1
repo; commit a; git tag v1.2.0; git commit -q --allow-empty -m "Grid UI: something (#42)"; echo "#42 release:minor" > "$STUB/pulls/42"; check "lost PR link: found by (#N)" 1.3.0
repo; commit a; git tag v1.2.0; commit b; base=$(git rev-parse HEAD)
EVENT=pull_request SHA=$base PR_LABELS="" check "PR, no label" 1.2.1
EVENT=pull_request SHA=$base PR_LABELS="enhancement release:minor" check "PR, release:minor" 1.3.0
EVENT=pull_request SHA=$base PR_LABELS="release:skip" check "PR, skip, but a merge is waiting" 1.2.1
label release:major; EVENT=pull_request SHA=$base PR_LABELS="" check "PR behind an unreleased major" 2.0.0
repo; commit a; git tag v1.2.0; base=$(git rev-parse HEAD); EVENT=pull_request SHA=$base PR_LABELS="release:skip" check "PR, skip, nothing waiting" "" "nothing else is waiting"
repo; commit a; git tag v1.2.0; git switch -q -c side; commit s1 release:major; git switch -q master; commit m1; git merge -q --no-ff side -m merge
check "a merge commit's second parent isn't walked" 1.2.1
repo; commit a; git tag v1.2.0; for i in $(seq 1 250); do direct "c$i"; done; check "at most 200 lookups" 1.2.1; n=$((n+1)); if [ "$(grep -c commits "$STUB/calls")" -le 201 ]; then echo "ok    ...with at most 201 lookups"; else echo "FAIL  lookups: $(grep -c commits "$STUB/calls")"; fails=$((fails+1)); fi
repo; commit a; git tag v1.2.0; commit b release:minor; GH_FAIL=1 check "a failed lookup fails the script" "" "HTTP 502"
repo; commit a; git tag v1.2.0; commit "Grid UI: thing (#7)" release:minor
MERGES="$STUB/m.md" EVENT=push SHA=$(git rev-parse HEAD) bash "$SCRIPT" > /dev/null
n=$((n+1)); # shellcheck disable=SC2016 # backticks are Markdown here
if grep -qE '^- `[0-9a-f]{7}` #[0-9]+: minor — Grid UI: thing \(#7\)$' "$STUB/m.md"; then echo "ok    the summary lists each merge counted"; else echo "FAIL  summary: $(cat "$STUB/m.md")"; fails=$((fails+1)); fi
echo "$n checks, $fails failed"
[ "$fails" = 0 ]

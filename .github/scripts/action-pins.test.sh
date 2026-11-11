#!/usr/bin/env bash
# Cases for action-pins.sh: a throwaway .github/workflows per case, read from the folder above it as the
# script is in CI. Run: bash .github/scripts/action-pins.test.sh
set -u
SCRIPT=$(cd "$(dirname "$0")" && pwd)/action-pins.sh
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
SHA=0123456789abcdef0123456789abcdef01234567 # made up
DIGEST=sha256:$SHA${SHA:0:24}
fails=0; n=0
wf() { # the lines of w.yml from line 5 on: a job's steps, or another job
  rm -rf "$TMP/.github" "$TMP/other"; mkdir -p "$TMP/.github/workflows"
  printf '%s\n' 'on: push' 'jobs:' '  job:' '    steps:' "$@" > "$TMP/.github/workflows/w.yml"
}
check() { # name, pass or fail, [expected substring in the output], [the script's arguments]
  n=$((n+1)); local got code=pass; got=$(cd "$TMP" && bash "$SCRIPT" "${@:4}" 2>&1) || code=fail
  if [ "$code" = "$2" ] && { [ -z "${3:-}" ] || echo "$got" | grep -qF -- "$3"; }; then echo "ok    $1"; else echo "FAIL  $1: want $2 ${3:+($3)} got: $(echo "$got" | tr '\n' '|')"; fails=$((fails+1)); fi
}
wf "      - uses: actions/checkout@$SHA # v7.0.1"; check "a commit SHA and its version comment" pass "uses: 1 pinned, 0 local, 0 not pinned"
wf "      - uses: actions/checkout@$SHA"; check "a commit SHA alone" pass "uses: 1 pinned"
wf "      - name: log in" "        uses: docker/login-action@$SHA # v4.8.0"; check "under a step's name" pass "uses: 1 pinned"
wf "      - uses: actions/checkout@v7"; check "a major tag" fail "w.yml:5: actions/checkout@v7 isn't pinned"
wf "      - uses: actions/checkout@v7.0.1"; check "an exact version tag" fail "w.yml:5: actions/checkout@v7.0.1 isn't pinned"
wf "      - uses: actions/checkout@main"; check "a branch" fail "w.yml:5: actions/checkout@main isn't pinned"
wf "      - uses: actions/checkout@${SHA:0:7}"; check "a short SHA" fail "uses: 0 pinned, 0 local, 1 not pinned"
wf "      - uses: actions/checkout@${SHA}0"; check "41 hex digits" fail
wf "      - uses: actions/checkout"; check "no ref at all" fail "w.yml:5: actions/checkout isn't pinned"
wf "      - uses: actions/checkout@v7 # $SHA"; check "the SHA only in the comment" fail "w.yml:5: actions/checkout@v7 isn't pinned"
wf "      - name: log in" "        uses: docker/login-action@v4"; check "a tag under a step's name, by its line" fail "w.yml:6: docker/login-action@v4 isn't pinned"
wf "      - uses: \"actions/checkout@v7\" # v7"; check "a tag in quotes" fail "w.yml:5: actions/checkout@v7 isn't pinned"
wf "      - uses: 'actions/checkout@$SHA' # v7.0.1"; check "a commit SHA in quotes" pass "uses: 1 pinned"
wf "      - \"uses\": actions/checkout@v7"; check "a quoted key" fail "w.yml:5: actions/checkout@v7 isn't pinned"
wf "      - uses: github/codeql-action/init@$SHA # v4.31.2"; check "an action in a folder of its repository" pass "uses: 1 pinned"
wf "      - uses: github/codeql-action/init@v4"; check "...by tag" fail "w.yml:5: github/codeql-action/init@v4 isn't pinned"
wf "      - uses: ./.github/actions/setup"; check "a local action" pass "uses: 0 pinned, 1 local, 0 not pinned"
wf "  images:" "    uses: ./.github/workflows/docker-images.yml"; check "this repository's reusable workflow" pass "uses: 0 pinned, 1 local"
wf "  build:" "    uses: octo-org/shared/.github/workflows/build.yml@$SHA # v1.2.3"; check "another repository's reusable workflow, by commit" pass "uses: 1 pinned"
wf "  build:" "    uses: octo-org/shared/.github/workflows/build.yml@v1"; check "...by tag" fail "w.yml:6: octo-org/shared/.github/workflows/build.yml@v1 isn't pinned"
wf "      - uses: docker://ghcr.io/octo-org/tool@$DIGEST"; check "a docker image by digest" pass "uses: 1 pinned"
wf "      - uses: docker://ghcr.io/octo-org/tool:1.2@$DIGEST"; check "...with a tag before the digest" pass "uses: 1 pinned"
wf "      - uses: docker://alpine:3.20"; check "a docker image by tag" fail "w.yml:5: docker://alpine:3.20 isn't pinned"
wf "      - uses: docker://alpine@sha256:$SHA"; check "a short digest" fail
wf "      # - uses: actions/checkout@v7"; check "a commented-out step is skipped" pass "uses: 0 pinned, 0 local, 0 not pinned"
wf "      - run: echo 'uses: actions/checkout@v7'"; check "uses: inside a command isn't one" pass "uses: 0 pinned, 0 local, 0 not pinned"
wf "      - uses:" "          actions/checkout@$SHA"; check "a value on the next line can't be read" fail "w.yml:5: a uses: with no value on its line"
wf "      - { uses: actions/checkout@$SHA }"; check "nor can one inside { }" fail "w.yml:5: a uses: inside { }"
wf "      - { name: x, uses: actions/checkout@$SHA }"; check "...wherever it is in them" fail "w.yml:5: a uses: inside { }"
wf "      - uses: actions/checkout@$SHA # v7.0.1"$'\r' "      - uses: actions/setup-go@$SHA"$'\r'; check "CRLF line endings" pass "uses: 2 pinned"
wf "      - uses: actions/checkout@$SHA"$'\r' "      - uses: actions/setup-go@v7"$'\r'; check "...and a tag among them" fail "w.yml:6: actions/setup-go@v7 isn't pinned"
wf "      - uses: actions/checkout@$SHA"; printf '%s' "      - uses: actions/cache@v6" >> "$TMP/.github/workflows/w.yml"; check "a last line without a line ending" fail "w.yml:6: actions/cache@v6 isn't pinned"
wf "      - uses: actions/checkout@$SHA" "      - uses: actions/setup-go@v7" "      - uses: ./local" "      - uses: actions/cache@v6"
cp "$TMP/.github/workflows/w.yml" "$TMP/.github/workflows/z.yaml"; echo "uses: actions/checkout@v7" > "$TMP/.github/workflows/notes.txt"
check "every one is listed, not only the first" fail "w.yml:8: actions/cache@v6 isn't pinned"
check "...in .yml and .yaml files, and no others" fail "workflow files: 2; uses: 2 pinned, 2 local, 4 not pinned"
check "...and how to pin them" fail "Pin each as OWNER/REPO@<commit SHA> # vX.Y.Z"
mv "$TMP/.github" "$TMP/other"
check "a folder and a file as arguments" fail "workflow files: 3; uses: 3 pinned, 3 local, 6 not pinned" other/workflows other/workflows/w.yml
check "without them, .github/workflows has to be there" fail
mkdir -p "$TMP/.github/workflows"; check "a folder with no workflows" fail "no workflow files in .github/workflows"
check "a file that isn't there" fail "" other/workflows/missing.yml
echo "$n checks, $fails failed"
[ "$fails" = 0 ]

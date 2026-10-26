#!/usr/bin/env bash
# Pre-tag sanity check. Run it before creating a release tag:
#
#     ./check-release.sh v1.12.7
#
# CI (release.yml) runs it as `./check-release.sh --ci "$GITHUB_REF_NAME"`: only
# the version.py and Changelog.txt checks, since the tag already exists there and
# the checkout is detached. Prerelease tags (v1.3.0-beta.2) are accepted; their
# changelog header is the base version (v1.3.0), as release.yml looks it up.
#
# Fails (exit 1) unless the tag you are about to create matches what is
# committed: src/version.py, the Changelog.txt header, a clean tree, and a
# HEAD that is already pushed. v1.12.6 shipped still reporting v1.12.5 because
# version.py was never bumped, so the updater offered it again after every update.
set -uo pipefail

cd "$(dirname "$0")"

CI=0
if [[ "${1:-}" == "--ci" ]]; then
    CI=1
    shift
fi
TAG="${1:-}"
if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; then
    echo "usage: $0 [--ci] vMAJOR.MINOR.PATCH[-pre]" >&2
    exit 2
fi
BASE="${TAG%%-*}"

fail=0
ok()  { echo "ok    $*"; }
bad() { echo "FAIL  $*"; fail=1; }

ver=$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' src/version.py | head -1)
if [[ "$ver" == "$TAG" ]]; then
    ok "src/version.py is $ver"
else
    bad "src/version.py says '${ver:-?}', expected $TAG"
fi

# release.yml parses this header exactly; any extra text breaks the notes.
if grep -qxF -- "- $BASE" Changelog.txt; then
    ok "Changelog.txt has the '- $BASE' header"
else
    bad "Changelog.txt has no line that is exactly '- $BASE'"
fi

if (( CI )); then
    if (( fail )); then
        echo "Tag $TAG does not match the committed version/changelog."
        exit 1
    fi
    echo "Tag $TAG matches version.py and Changelog.txt."
    exit 0
fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    bad "tag $TAG already exists"
else
    ok "tag $TAG is not used yet"
fi

if [[ -z "$(git status --porcelain)" ]]; then
    ok "working tree is clean"
else
    bad "uncommitted changes in the working tree"
fi

git fetch -q origin main 2>/dev/null || echo "warn  could not fetch origin/main"
if [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main 2>/dev/null)" ]]; then
    ok "HEAD $(git rev-parse --short HEAD) is origin/main"
else
    bad "HEAD is not origin/main (unpushed or behind)"
fi

if (( fail )); then
    echo "Not ready to tag $TAG."
    exit 1
fi
echo "Ready to tag $TAG on $(git rev-parse --short HEAD)."

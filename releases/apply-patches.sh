#!/usr/bin/env bash
# Clone upstream Gitea WITH ITS HISTORY and apply this repository's changes on top
# of it as ordinary git commits, so what is left is a Gitea repository whose
# `git log` shows upstream's history followed by the gitea-patches commits.
#
#   apply-patches.sh <patches-root> <dest-dir> [ref]
#
#   patches-root  where THIS repository is checked out
#   dest-dir      the Gitea repository to create; must not exist yet. The release
#                 workflows use "gitea", which is the top directory of both
#                 gitea-patches-source-*.zip files.
#   ref           upstream branch, tag or commit (full or abbreviated) to build;
#                 empty = upstream main, including unreleased changes.
#
# The commits, in this order, on a branch named gitea-patches:
#   1. dist/all/*.patch, each verified against its .sha256sum and applied with
#      `git am --3way` - a `git format-patch` file keeps its own author and message.
#   2. dist/translations/<lang>.json, merged by key into options/locale/ by
#      apply-translations.py (why not a .patch: see that script), one commit per
#      language with the message from <lang>.md.
#
# The commits are REPRODUCIBLE: author and committer dates are upstream's commit
# date, so the same upstream commit and the same patches give the same commit
# hashes on every run. That is what lets Release All Missing rebuild a release's
# source from its tag and get the source the release already had.
#
# Prints, and under GitHub Actions also writes to GITHUB_ENV and GITHUB_OUTPUT:
#   SOURCE_REF         the ref that was asked for
#   UPSTREAM_COMMIT    the upstream commit it resolved to
#   UPSTREAM_SHORT     its first 10 characters
#   UPSTREAM_VERSION   the newest upstream v<x.y.z> tag it descends from, without
#                      the "v" and the "-dev" (v29.0.0-dev -> 29.0.0)
#   PATCHED_COMMIT     the top commit of the gitea-patches branch
#   PATCHES_APPLIED    how many commits were added
#
# UPSTREAM_URL overrides where upstream is cloned from (the tests use a local
# fixture repository, so they run with no network). PATCH_AUTHOR is the author
# and committer of the translation commits, and the committer of every commit.
set -euo pipefail

PATCHES="$(cd "${1:?usage: apply-patches.sh <patches-root> <dest-dir> [ref]}" && pwd)"
DEST="${2:?usage: apply-patches.sh <patches-root> <dest-dir> [ref]}"
REF="${3:-}"
UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/go-gitea/gitea.git}"
PATCH_AUTHOR="${PATCH_AUTHOR:-Lauri Ojansivu <x@xet7.org>}"
NAME="${PATCH_AUTHOR% <*}"
EMAIL="${PATCH_AUTHOR##*<}"; EMAIL="${EMAIL%>}"

[ ! -e "$DEST" ] || { echo "apply-patches.sh: $DEST already exists" >&2; exit 1; }

# The whole history and every tag: the zip "with history" is this repository, and
# the version comes from the newest upstream tag below the commit. Every branch
# comes along as well, so a release branch or an older commit is a valid ref.
echo "Cloning ${UPSTREAM_URL} with its whole history..."
git clone --quiet --no-checkout "$UPSTREAM_URL" "$DEST"
cd "$DEST"
if [ -z "$REF" ]; then
  COMMIT="$(git rev-parse --verify 'HEAD^{commit}')"
  REF="$(git symbolic-ref --short HEAD)"
elif COMMIT="$(git rev-parse --verify -q "origin/$REF^{commit}")"; then
  :
elif COMMIT="$(git rev-parse --verify -q "$REF^{commit}")"; then
  :
else
  git fetch --quiet origin "$REF"
  COMMIT="$(git rev-parse --verify 'FETCH_HEAD^{commit}')"
fi
git checkout --quiet -B gitea-patches "$COMMIT"
SHORT="${COMMIT:0:10}"
BASE_TAG="$(git describe --tags --abbrev=0 --match 'v[0-9]*' "$COMMIT" 2>/dev/null || echo v0.0.0)"
UPSTREAM_VERSION="${BASE_TAG#v}"; UPSTREAM_VERSION="${UPSTREAM_VERSION%%-*}"
echo "Upstream ${REF} is ${COMMIT} (version ${UPSTREAM_VERSION}, from tag ${BASE_TAG})."

# Who and when, for every commit made below.
DATE="$(git log -1 --format=%cI "$COMMIT")"
export GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL" GIT_COMMITTER_DATE="$DATE"
git config user.name "$NAME"
git config user.email "$EMAIL"
applied=0

# 1. Code patches, checksum first, in name order.
for p in "$PATCHES"/dist/all/*.patch; do
  [ -e "$p" ] || continue
  n="$(basename "$p")"
  ( cd "$PATCHES/dist/all" && sha256sum --quiet -c "${n%.patch}.sha256sum" )
  echo "Applying all/${n}"
  before="$(git rev-list --count HEAD)"
  git am --quiet --3way --committer-date-is-author-date "$p" || {
    git am --abort || true
    echo "::error::dist/all/${n} does not apply to upstream ${REF} (${COMMIT}). Re-port it; see docs/Design/Patch-format.md." >&2
    exit 1
  }
  applied=$((applied + $(git rev-list --count HEAD) - before))
done

# 2. Translations, one commit per language.
LANGS="$(python3 -I "$PATCHES/releases/apply-translations.py" "$PATCHES" . | tee /dev/stderr | sed -n 's/^LANGS=//p')"
for lang in $LANGS; do
  f="options/locale/locale_${lang}.json"
  git add -- "$f"
  if git diff --cached --quiet; then
    echo "${lang}: upstream already has every string; no commit."
    continue
  fi
  md="$PATCHES/dist/translations/${lang}.md"
  # The .md is the commit message: its first paragraph after the "# <lang>" heading
  # is the subject line, the rest is the body.
  msg="$(mktemp)"
  sed '1{/^# /d}' "$md" | sed '/./,$!d' > "$msg"
  GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" GIT_AUTHOR_DATE="$DATE" \
    git commit --quiet --cleanup=strip -F "$msg"
  rm -f "$msg"
  applied=$((applied + 1))
done

PATCHED="$(git rev-parse HEAD)"
echo "Applied ${applied} commit(s) onto upstream ${REF} at ${COMMIT}: gitea-patches is ${PATCHED}."
git log --oneline --no-decorate "${COMMIT}..HEAD"

out="$(printf 'SOURCE_REF=%s\nUPSTREAM_COMMIT=%s\nUPSTREAM_SHORT=%s\nUPSTREAM_VERSION=%s\nPATCHED_COMMIT=%s\nPATCHES_APPLIED=%s\n' \
  "$REF" "$COMMIT" "$SHORT" "$UPSTREAM_VERSION" "$PATCHED" "$applied")"
printf '%s\n' "$out"
[ -z "${GITHUB_ENV:-}" ] || printf '%s\n' "$out" >> "$GITHUB_ENV"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf '%s\n' "$out" | while IFS='=' read -r k v; do
    printf '%s=%s\n' "$(printf '%s' "$k" | tr 'A-Z' 'a-z')" "$v"
  done >> "$GITHUB_OUTPUT"
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Source"
    echo ""
    echo "| Upstream | Ref | Commit | Version | gitea-patches commits |"
    echo "|----------|-----|--------|---------|-----------------------|"
    echo "| go-gitea/gitea | ${REF} | \`${COMMIT}\` | ${UPSTREAM_VERSION} | ${applied} |"
  } >> "$GITHUB_STEP_SUMMARY"
fi

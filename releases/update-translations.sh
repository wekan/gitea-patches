#!/usr/bin/env bash
# update-translations.sh [ref] - the newest Gitea translations from upstream,
# with this repository's own translations merged in, into
# dist/translations/gitea-locale/.
#
#   ./build.sh translations [ref]     ref: a branch or tag of go-gitea/gitea, default main
#
# Why: WeKan's version control prototypes (Omi, Wami, github-to-gitea, ...)
# show Gitea's own things - repositories, issues, pull requests, releases - in
# Gitea's words, and copy their Gitea translations from HERE, not from upstream:
# only this repository has the complete Finnish (dist/translations/fi-FI.json;
# upstream's locale_fi-FI.json has about a third of the strings). So this
# fetches only upstream's options/locale/ (a sparse, shallow clone), merges this
# repository's translations into it by key with releases/apply-translations.py -
# the same merge a Gitea build gets - and keeps the result, with SOURCE naming
# the upstream commit it came from. Run it from the menu whenever upstream's
# translations should be refreshed, then commit dist/translations/gitea-locale/.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
ref="${1:-main}"
upstream="${GITEA_UPSTREAM:-https://github.com/go-gitea/gitea}"
if [[ ! "$ref" =~ ^[A-Za-z0-9._/-]+$ || "$ref" == -* ]]; then echo "Not a branch or tag: $ref" >&2; exit 2; fi
out="$root/dist/translations/gitea-locale"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/gitea-locale.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

echo "Fetching options/locale of $upstream at $ref ..."
git clone --quiet --depth 1 --branch "$ref" --filter=blob:none --sparse -- "$upstream" "$tmp/gitea"
git -C "$tmp/gitea" sparse-checkout set options/locale
commit="$(git -C "$tmp/gitea" rev-parse HEAD)"
[ -f "$tmp/gitea/options/locale/locale_en-US.json" ] || { echo "No options/locale/locale_en-US.json at $ref" >&2; exit 1; }

# This repository's translations, merged by key, as a Gitea build gets them.
python3 -I "$root/releases/apply-translations.py" "$root" "$tmp/gitea" >/dev/null

new="$tmp/new"; mkdir -p "$new"
cp "$tmp/gitea/options/locale/"locale_*.json "$new/"
n="$(ls "$new" | wc -l)"
[ "$n" -ge 2 ] || { echo "Only $n locale files at $ref: refusing to replace the current ones" >&2; exit 1; }
printf 'go-gitea/gitea %s %s\n' "$ref" "$commit" > "$new/SOURCE"
cp "$out/README.md" "$new/README.md" 2>/dev/null || true
# Replace the whole directory only when everything above worked.
rm -rf "$out.old"; [ -d "$out" ] && mv "$out" "$out.old"
mv "$new" "$out"; rm -rf "$out.old"
echo "dist/translations/gitea-locale: $n locale files from $commit, with this repository's translations merged."

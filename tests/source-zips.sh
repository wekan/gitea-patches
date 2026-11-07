#!/usr/bin/env bash
# releases/source-zips.py, with no network: the zip without history has no .git and
# the one with history is a working repository, both unpack to gitea/ exactly as
# the tree was (names in UTF-8 - Info-ZIP's zip aborts on Gitea's "Grüßen" -
# executable bits, symbolic links), and the same tree gives a byte-identical zip without history.
#
#   bash tests/source-zips.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/gitea-patches-zips.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
fail=0
t() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=$((fail + 1)); fi; }

src="$WORK/some-name"
git init -q -b gitea-patches "$src"
git -C "$src" config user.name T; git -C "$src" config user.email t@example.org
mkdir -p "$src/refs/heads" "$src/tools" "$src/empty-later"
printf 'x\n' > "$src/refs/heads/Grüßen"
printf 'y\n' > "$src/refs/heads/ブランチ"
printf '#!/bin/sh\n' > "$src/tools/run.sh"; chmod +x "$src/tools/run.sh"
ln -s tools/run.sh "$src/link"
printf 'z\n' > "$src/empty-later/f"
git -C "$src" add -A; git -C "$src" commit -q -m one

python3 -I "$ROOT/releases/source-zips.py" "$src" "$WORK/out" >/dev/null
W="$WORK/out/gitea-patches-source-with-history.zip"
N="$WORK/out/gitea-patches-source-without-history.zip"
t "both zips and their .sha256 are made" '[ -s "$W" ] && [ -s "$N" ] && (cd "$WORK/out" && sha256sum --quiet -c ./*.sha256)'

mkdir "$WORK/n" "$WORK/w"
( cd "$WORK/n" && unzip -q "$N" ); ( cd "$WORK/w" && unzip -q "$W" )
t "the top directory is gitea/, whatever the source is called" '[ -d "$WORK/n/gitea" ] && [ "$(ls "$WORK/n")" = gitea ]'
t "negative: the zip without history has no .git" '[ ! -e "$WORK/n/gitea/.git" ]'
t "the files are exactly the tree's" 'diff -r --no-dereference --exclude=.git "$src" "$WORK/n/gitea"'
t "UTF-8 names survive" '[ -f "$WORK/n/gitea/refs/heads/Grüßen" ] && [ -f "$WORK/n/gitea/refs/heads/ブランチ" ]'
t "the executable bit survives" '[ -x "$WORK/n/gitea/tools/run.sh" ]'
t "a symbolic link stays a link" '[ -L "$WORK/n/gitea/link" ] && [ "$(readlink "$WORK/n/gitea/link")" = tools/run.sh ]'
t "the zip with history is a working repository" \
  '[ "$(git -C "$WORK/w/gitea" log --format=%s)" = one ] && [ -z "$(git -C "$WORK/w/gitea" status --porcelain)" ]'

sleep 1; touch "$src/tools/run.sh"
python3 -I "$ROOT/releases/source-zips.py" "$src" "$WORK/out2" >/dev/null
t "reproducible: a byte-identical zip without history from the same tree later" \
  'cmp -s "$N" "$WORK/out2/$(basename "$N")"'

echo
echo "$fail failure(s)"
[ "$fail" -eq 0 ]

#!/usr/bin/env bash
# releases/apply-patches.sh against a small local "upstream", with no network:
# the changes become commits on branch gitea-patches above upstream's history,
# the commits are reproducible, and a bad patch or checksum stops it.
#
#   bash tests/apply-patches.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/gitea-patches-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=$((fail + 1)); }
t() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

# --- a fixture upstream: two commits, a dev tag, a locale --------------------------
up="$WORK/upstream"
git init -q -b main "$up"
git -C "$up" config user.name Upstream; git -C "$up" config user.email up@example.org
mkdir -p "$up/options/locale" "$up/tools"
printf '{\n  "a": "Apple",\n  "b": "Banana %%s"\n}\n' > "$up/options/locale/locale_en-US.json"
printf '{\n  "a": "Omena-upstream"\n}\n' > "$up/options/locale/locale_xx-XX.json"
printf 'hello\n' > "$up/README.md"
git -C "$up" add -A; git -C "$up" commit -q -m "first"
git -C "$up" tag v1.2.0-dev
printf 'hello world\n' > "$up/README.md"
git -C "$up" commit -q -am "second"
UP_HEAD="$(git -C "$up" rev-parse HEAD)"

# --- a patches tree: one translation, one git-am patch ----------------------------
pt="$WORK/patches"
mkdir -p "$pt/dist/translations" "$pt/dist/all" "$pt/releases"
cp "$ROOT/releases/apply-patches.sh" "$ROOT/releases/apply-translations.py" "$pt/releases/"
printf '{\n  "b": "Banaani %%s"\n}\n' > "$pt/dist/translations/xx-XX.json"
printf '{\n  "b": "Banana %%s"\n}\n' > "$pt/dist/translations/xx-XX.en-US.json"
printf '# xx-XX\n\nTranslate the fixture into xx.\n\nBody line.\n' > "$pt/dist/translations/xx-XX.md"
( cd "$pt/dist/translations" && sha256sum xx-XX.json xx-XX.en-US.json > xx-XX.sha256sum )
# The git-am patch: made the way docs/Design/Patch-format.md says.
git clone -q "$up" "$WORK/mk"
git -C "$WORK/mk" config user.name 'Patch Author'; git -C "$WORK/mk" config user.email pa@example.org
printf 'patched\n' > "$WORK/mk/PATCHED"; git -C "$WORK/mk" add PATCHED
git -C "$WORK/mk" commit -q -m "Add PATCHED." -m "Why it is needed."
git -C "$WORK/mk" format-patch -q --stdout -1 > "$pt/dist/all/add-patched.patch"
( cd "$pt/dist/all" && sha256sum add-patched.patch > add-patched.sha256sum )

run() { UPSTREAM_URL="$up" GITHUB_ENV= GITHUB_OUTPUT= GITHUB_STEP_SUMMARY= bash "$pt/releases/apply-patches.sh" "$pt" "$@"; }

out="$(run "$WORK/g1" 2>&1)" || { echo "$out"; bad "apply-patches.sh runs"; }
g="$WORK/g1"
t "on branch gitea-patches" '[ "$(git -C "$g" symbolic-ref --short HEAD)" = gitea-patches ]'
t "upstream history is kept" '[ "$(git -C "$g" rev-list --count HEAD)" = 4 ] && git -C "$g" merge-base --is-ancestor "$UP_HEAD" HEAD'
t "the tags came along" 'git -C "$g" rev-parse -q --verify refs/tags/v1.2.0-dev >/dev/null'
t "two commits on top: the patch, then the translation" \
  '[ "$(git -C "$g" log --format=%s "$UP_HEAD..HEAD" | tr "\n" "|")" = "Translate the fixture into xx.|Add PATCHED.|" ]'
t "the patch keeps its own author" '[ "$(git -C "$g" log -1 --format=%an HEAD~1)" = "Patch Author" ]'
t "the translation commit is by PATCH_AUTHOR's default" '[ "$(git -C "$g" log -1 --format="%an <%ae>|%cn <%ce>")" = "Lauri Ojansivu <x@xet7.org>|Lauri Ojansivu <x@xet7.org>" ]'
t "the commit message body comes from the .md" 'git -C "$g" log -1 --format=%b | grep -qx "Body line."'
t "the translation is merged" 'grep -q "\"b\": \"Banaani %s\"" "$g/options/locale/locale_xx-XX.json" && grep -q "Omena-upstream" "$g/options/locale/locale_xx-XX.json"'
t "the working tree is clean" '[ -z "$(git -C "$g" status --porcelain)" ]'
t "prints the upstream version without -dev" 'grep -qx "UPSTREAM_VERSION=1.2.0" <<<"$out"'
t "prints the upstream commit" 'grep -qx "UPSTREAM_COMMIT=$UP_HEAD" <<<"$out" && grep -qx "PATCHES_APPLIED=2" <<<"$out"'

# Reproducible: the same inputs give the same commit, later and by abbreviated ref.
sleep 1
run "$WORK/g2" "${UP_HEAD:0:10}" >/dev/null 2>&1
t "reproducible: a second run gives the same commit hash" '[ "$(git -C "$WORK/g1" rev-parse HEAD)" = "$(git -C "$WORK/g2" rev-parse HEAD)" ]'
run "$WORK/g3" v1.2.0-dev >/dev/null 2>&1
t "a tag works as the ref" '[ "$(git -C "$WORK/g3" rev-list --count HEAD)" = 3 ]'

# Negative: an existing destination, a stale checksum, a patch that does not apply.
t "negative: refuses an existing destination" '! run "$WORK/g1" >/dev/null 2>&1'
echo " " >> "$pt/dist/all/add-patched.patch"
t "negative: refuses a patch whose checksum does not match" '! run "$WORK/g4" >/dev/null 2>&1'
( cd "$pt/dist/all" && sed -i "s/^+patched$/+other/; s/PATCHED/README.md/g" add-patched.patch && sha256sum add-patched.patch > add-patched.sha256sum )
t "negative: stops when a patch does not apply" '! run "$WORK/g5" >/dev/null 2>&1'

echo
echo "$fail failure(s)"
[ "$fail" -eq 0 ]

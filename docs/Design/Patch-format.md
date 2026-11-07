# Patch format

`dist/` has two sections, applied in this order, each as git commits on the
`gitea-patches` branch by [releases/apply-patches.sh](../../releases/apply-patches.sh):

| Section | Applied with | For |
|---------|--------------|-----|
| `dist/all/` | `git am --3way` | changes to Gitea's code, templates, workflows - anything but translations |
| `dist/translations/` | `releases/apply-translations.py`, merged by key | translations of `options/locale/` |

## Code patches: `dist/all/<name>.patch`

Three files sharing a base name, the logical change in kebab-case:

```
dist/all/
  fix-something.patch       git format-patch output: one or more commits
  fix-something.sha256sum   sha256 of the .patch, verified before applying
  fix-something.md          what the patch does and why
```

Make the patch from a Gitea checkout whose commits are on top of upstream:

```sh
git -C gitea format-patch --stdout <upstream commit>..<your commit> > dist/all/fix-something.patch
cd dist/all && sha256sum fix-something.patch > fix-something.sha256sum
```

`git format-patch` output keeps each commit's author and message, so the commit in
the released source is the commit you made. Patches apply in name order; prefix
them `0010-`, `0020-` when the order matters. A patch that no longer applies to
upstream stops the release with an error naming it: re-port it onto current
upstream and regenerate the `.patch` and `.sha256sum`. Prefer a change upstream
would take; the `.md` should link the upstream pull request, so the patch can be
removed when it lands.

## Translations: `dist/translations/<lang>`

```
dist/translations/
  fi-FI.json          the strings this repository translated or corrected - ONLY those
  fi-FI.en-US.json    the English each of them was translated from
  fi-FI.sha256sum     sha256 of both, as `sha256sum fi-FI.json fi-FI.en-US.json` writes it
  fi-FI.md            the commit message: "# fi-FI", then the subject line, then the body
```

The language tag is upstream's: `fi-FI.json` merges into
`options/locale/locale_fi-FI.json`. Both JSON files are flat `key -> string`, in
the key order of upstream's `locale_en-US.json`, 2-space indent, UTF-8, like
upstream's own locale files.

The merge rules are in
[releases/apply-translations.py](../../releases/apply-translations.py). In short:
this repository's string wins while upstream's English is what it was translated
from; when the English has changed, upstream's own translation wins if there is
one; a string whose `%s`-verbs or HTML tags differ from the English is never used;
keys upstream removed are dropped; every other key keeps upstream's string.

To change or add strings, edit both JSON files - the translation and, for a new
key, its English from upstream's `locale_en-US.json` - and regenerate the checksum:

```sh
cd dist/translations && sha256sum fi-FI.json fi-FI.en-US.json > fi-FI.sha256sum
python3 -I releases/apply-translations.py . <a gitea checkout> --check
```

`--check` prints how many strings would come from where, including strings that
need attention: English that has changed since, and mismatched verbs or tags.

**Placeholders are code, not language.** Every `%s`, `%d`, `%[1]s` and HTML tag of
the English must be in the translation exactly; `tests/translations.test.py` fails
when one is not.

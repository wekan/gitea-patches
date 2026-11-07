# dist/ - what is changed in upstream Gitea

| Section | Applied with | Purpose |
|---------|--------------|---------|
| `all/` | `git am --3way`, after cloning upstream | code, templates, anything but translations |
| `translations/` | `releases/apply-translations.py`, merged by key | `options/locale/` |

Both become git commits on the `gitea-patches` branch of the released source, in
that order. See [the patch format](../docs/Design/Patch-format.md).

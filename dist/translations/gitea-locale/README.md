# Gitea translations for the version control prototypes

`locale_<lang>.json` here are upstream Gitea's `options/locale/` files, with
this repository's translations merged in by key - the files a Gitea built
from gitea-patches has. `SOURCE` names the upstream commit.

WeKan's version control prototypes (Omi, Wami, github-to-gitea and the rest
under WeKan's `.tools/w/`) copy their Gitea translations **from here**, in
their own catalog format: only this repository has the complete Finnish
(`../fi-FI.json`; upstream's `locale_fi-FI.json` has about a third of it).
Their other strings come from WeKan's catalogs, `imports/i18n/data/`.

Refresh from upstream with `./build.sh translations [ref]` (menu: "Download
the newest Gitea translations from upstream"), then commit this directory.
Do not edit these files: a correction goes into `../<lang>.json`, and the next
refresh merges it.

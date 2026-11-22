The upcoming gitea-patches release and the 5 newest, in short. Everything
each one changed, and the platforms, are in
[CHANGELOG-big.md](CHANGELOG-big.md).

Written by `python3 releases/changelog-short.py` from CHANGELOG-big.md:
make changes there.

# Upcoming gitea-patches release

Gitea is built from **upstream go-gitea/gitea** with this
repository's changes applied as **git commits**, and released with its source as
**two zips** - with and without git history - and binaries for **every platform
upstream releases**, built from the zip without history. The web UI is
**completely in Finnish**, the local Gitea is **mirrored on to GitLab and
SourceForge**, **long migrations** no longer break off on Gitea's SQLite and
clone time limit, an organization or user can be **backed up, deleted and
restored** - or updated **straight from GitHub** - github-to-gitea recognises
**GitHub's API by its parsed host**, and Gitea carries the **security
hardening** code scanning found.

[All changes so far](CHANGELOG-big.md#upcoming-gitea-patches-release)

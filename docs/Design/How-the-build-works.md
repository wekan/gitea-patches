# How the build works

```
release-all.yml / release-all-missing.yml
        |
        v
release-all-build.yml
  job source (one runner)
    releases/apply-patches.sh   git clone go-gitea/gitea (whole history, tags)
                                checkout <ref> on branch gitea-patches
                                git am dist/all/*.patch          -> commits
                                apply-translations.py + commit  -> one commit per language
    releases/source-zips.py     gitea-patches-source-with-history.zip
                                gitea-patches-source-without-history.zip
    create the release, sign and attach both zips
    hand the zip without history to the build jobs (workflow artifact)
        |
        v
  job build, one per group: linux  windows  darwin  freebsd  src
    unzip gitea-patches-source-without-history.zip at the workspace root
    setup-go (go.mod), upstream's .github/actions/node-setup (pnpm, Node)
    make deps-frontend deps-backend
    make frontend release-<os> release-copy release-compress release-check
      or, for src: make frontend vendor release-sources release-check
    cosign sign-blob every file
    attach to the release - as soon as that group is done
```

## Why the binaries are built from the zip

The release says the binaries were built from
`gitea-patches-source-without-history.zip`, and that is literally true: the build
jobs never see the git clone, only that zip. Whoever downloads it gets the exact
source of the binaries beside it.

A source without `.git` builds because the Makefile is told the version: with
`GITHUB_REF_TYPE=tag` and `GITHUB_REF_NAME=<tag>` it takes both the file names and
the version shown in Gitea from the tag, and runs no git command - the same way
upstream's own `gitea-src-<version>.tar.gz` builds.

## Reproducible source

`apply-patches.sh` dates every commit it makes with upstream's commit date, and
`source-zips.py` sorts the entries and stamps them with that date too. The same
upstream commit and the same `dist/` give the same commit hashes and a
byte-identical `gitea-patches-source-without-history.zip` - the zip the binaries
are built from. (The zip with history has the same commits, but git's index and
packs differ between clones, so not the same bytes.) That is what lets Release
All Missing rebuild a release's missing source zips from the upstream commit its
tag names.

## Release All Missing

1. The release's file list decides which groups are incomplete: a group is
   complete when every binary of its platforms (read from the source's
   `tools/build-release.sh`), its `.xz`, and the `.sha256` and `.sigstore.json` of
   each are there.
2. When both source zips are on the release, the zip without history is downloaded,
   checked against its `.sha256` and built from. Otherwise they are made again from
   the upstream commit at the end of the tag.
3. Only files the release does not have are uploaded; nothing on it is replaced.

## Why translations are not .patch files

See [releases/apply-translations.py](../../releases/apply-translations.py): upstream's
locale files change through Crowdin almost daily, so a line-based patch stops
applying within days. The fork's Finnish commits already conflicted with upstream
`main` 16 commits later. A translation is merged by message key instead, which
cannot conflict.

# gitea-patches

Patches and translations to upstream [Gitea](https://github.com/go-gitea/gitea), and
the build that publishes Gitea for every platform upstream releases, at
[wekan/gitea-patches releases](https://github.com/wekan/gitea-patches/releases).

This repository carries **no Gitea source**. Each release run clones upstream
`main` (or a branch, tag or commit you name) **with its whole history**, applies
this repository's changes on top as ordinary **git commits** on a branch named
`gitea-patches`, and attaches to the release:

| File | What it is |
|------|------------|
| `gitea-patches-source-with-history.zip` | the patched source as a git repository: `gitea/` with `gitea/.git`, upstream's history and the gitea-patches commits |
| `gitea-patches-source-without-history.zip` | the same source without `gitea/.git`, much smaller - **the binaries are built from it** |
| `gitea-<version>-<os>-<arch>[.exe]`, `.xz` | Gitea for every platform in upstream's `tools/build-release.sh` (linux amd64, arm-5, arm-6, arm64, riscv64; windows amd64, arm64; darwin amd64, arm64; freebsd amd64) |
| `gitea-src-<version>.tar.gz` | upstream's own source tarball, made by `make release-sources` |

Every file has a `.sha256` and a keyless cosign signature `.sigstore.json` beside
it. Same model as [wekan/mongo-tools-patches](https://github.com/wekan/mongo-tools-patches)
and [wekan/node-patches](https://github.com/wekan/node-patches); it replaces the
[wekan/gitea](https://github.com/wekan/gitea) source fork, whose only changes were
the Finnish translation and its release workflows - both are here now.

## What is changed

- **Finnish** ([dist/translations/fi-FI.md](dist/translations/fi-FI.md)): the
  Gitea web UI completely in Finnish, where upstream has about a third of it.
- **Release files of any size**
  ([dist/all/release-files-any-size.md](dist/all/release-files-any-size.md)):
  `[repository.release] FILE_MAX_SIZE` is `-1`, no limit, by default, and `-1` works
  in the API's streamed uploads and in the upload form, where upstream refuses every
  file with it. Code patches are in `dist/all/`; see
  [the patch format](docs/Design/Patch-format.md).

## Making a release

From GitHub's Actions tab, or with `gh`:

```sh
gh workflow run release-all.yml -R wekan/gitea-patches                    # upstream main
gh workflow run release-all.yml -R wekan/gitea-patches -f ref=v1.25.3     # an upstream release
gh workflow run release-all-missing.yml -R wekan/gitea-patches            # complete the newest release
```

`./build.sh` has the same as a menu, and also prepares and builds the patched
source locally.

- **Release All** names the release `v<upstream version>-wekan-<YYYYMMDD>-<upstream
  commit>`, e.g. `v29.0.0-wekan-20261107-4f69599981` - a prerelease, unless the
  ref is an upstream release tag. A tag must have no `v` after its first character:
  upstream's Makefile removes every `v` from file names.
- **Release All Missing** rebuilds only the groups (linux, windows, darwin, freebsd,
  src) a release lacks a file of, from the source zip already on that release, and
  uploads only absent files.

See [how the build works](docs/Design/How-the-build-works.md).

## Mirroring

[mirror/](mirror/README.md) copies GitHub to the local Gitea (`github-to-gitea`), and
the local Gitea on to GitLab (`gitea-to-gitlab`) and SourceForge
(`gitea-to-sourceforge`): git, wiki, issues and pull requests with comments, and
releases with their files - with the same menu, and in step with WeKan's
`releases/mirror.sh`.

## Changelog

[CHANGELOG.md](CHANGELOG.md) is the upcoming release and the five newest, in short.
[CHANGELOG-big.md](CHANGELOG-big.md) is the changelog that is written - every entry
in full - and CHANGELOG.md is generated from it with
`python3 releases/changelog-short.py`, never edited by hand: the same two files, and
the same rules, as WeKan's CHANGELOG-big.md and CHANGELOG.md.

## Tests

```sh
python3 -I tests/translations.test.py   # the translation merge rules
bash tests/apply-patches.sh             # clone + commits, against a local fixture
bash tests/source-zips.sh               # the zips: contents, names, reproducibility
python3 -I tests/mirror.test.py         # mirror/: Gitea -> GitLab and SourceForge
python3 -I tests/changelogShort.test.py # CHANGELOG.md is what CHANGELOG-big.md gives
```

None of them needs the network.

## License

This repository's own files (workflows, scripts, docs) are MIT - see
[LICENSE](LICENSE). Gitea is MIT as well; the translations and patches in `dist/`
are modifications of Gitea and are MIT like it.

Maintainer and contributor rules - who commits, as whom, and how the CHANGELOG is
written - are in [WeKan's CLAUDE.md and AGENTS.md](https://github.com/wekan/wekan),
which cover every repository WeKan clones into its `.tools/` directory.

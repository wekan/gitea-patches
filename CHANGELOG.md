# Platforms

Gitea built from upstream + these patches and translations, at these platforms:

- [Releases](https://github.com/wekan/gitea-patches/releases)
- [Upstream Gitea](https://github.com/go-gitea/gitea)
- [How the build works](docs/Design/How-the-build-works.md)
- [Patch format](docs/Design/Patch-format.md)

Every platform in upstream's `tools/build-release.sh`: linux amd64, arm-5, arm-6,
arm64, riscv64; windows amd64, arm64; darwin amd64, arm64; freebsd amd64 - with
the source as `gitea-patches-source-with-history.zip` and
`gitea-patches-source-without-history.zip`, and upstream's
`gitea-src-<version>.tar.gz`.

# Upcoming gitea-patches release

**In short:** Gitea is built from **upstream go-gitea/gitea** with this
repository's changes applied as **git commits**, and released with its source as
**two zips** - with and without git history - and binaries for **every platform
upstream releases**, built from the zip without history. The web UI is
**completely in Finnish**.

This release adds the following new features:

**Releases** - built from upstream plus patches, replacing the wekan/gitea fork.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/88526a4f4efa4b7992e84a0c75b02563a0817208">Build Gitea releases from upstream plus patches, like mongo-tools-patches</a>. Thanks to xet7.</summary>

The wekan/gitea fork's only changes from upstream were the Finnish translation
and its release workflows; both are here now. `releases/apply-patches.sh`
clones upstream with its whole history and applies `dist/all/*.patch` with
`git am --3way` and `dist/translations/` as commits on branch `gitea-patches`,
dated with upstream's commit date so the same inputs give the same commit
hashes. `releases/source-zips.py` makes
`gitea-patches-source-with-history.zip` and
`gitea-patches-source-without-history.zip` - not with `zip`, which aborts on
the UTF-8 file name in Gitea's test fixtures - and the zip without history is
byte-reproducible. Release All builds every platform in upstream's
`tools/build-release.sh` from the content of that zip, one job per group, each
attaching its files with an `.xz`, a `.sha256` and a keyless cosign signature
as soon as it is done; Release All Missing rebuilds only the groups a release
lacks a file of, from the source zip already on it. Built locally from the zip
without history, the linux group gave all five binaries, and the Finnish
locale embedded in the arm64 binary is byte-identical to the patched source.
`tests/` cover the merge rules, the commits against a local fixture upstream,
and the zips.

</details>

**Finnish** - the whole web UI.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/88526a4f4efa4b7992e84a0c75b02563a0817208">Translate the Gitea web UI completely into Finnish, merged by key</a>. Thanks to xet7.</summary>

Upstream has about a third of the strings in Finnish. All 4060 are now, with
the fork's terms (vetopyyntö, ajo, tarkistus, syyllinen, SSH-päätoimija) and
the 12 Terraform module registry strings upstream added since. The translation
is data, not a `.patch`: Crowdin changes upstream's locale files almost daily,
and the fork's Finnish commits already conflicted with upstream `main` 16
commits later. `releases/apply-translations.py` merges by key, keeps
upstream's string for every key this repository does not have, prefers
upstream's own translation when the English has changed since, and never uses
a string whose `%`-verbs or HTML tags differ from the English.

</details>

Thanks to above GitHub users for their contributions and translators for their translations.

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
**completely in Finnish**, the local Gitea is **mirrored on to GitLab and
SourceForge**, **long migrations** no longer break off on Gitea's SQLite and
clone time limit, an organization or user can be **backed up, deleted and
restored**, github-to-gitea recognises **GitHub's API by its parsed host**,
and Gitea carries the **security hardening** code scanning found.

This release fixes the following SECURITY ISSUES found by GitHub CodeQL code scanning:

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/f4a53499f3e4ecb3b66c280166c51b20b4744c8d">github-to-gitea recognises GitHub's API by its parsed host, not by a substring of the URL</a>. Thanks to GitHub CodeQL and xet7.</summary>

`http()` treated a URL as GitHub's API - and waited out GitHub's rate limits for
it - when `"api.github.com" in url`, which also holds for
`https://api.github.com.example.org/`, `https://example.org/?api.github.com` and
`https://api.github.com@example.org/` (code scanning #1,
`py/incomplete-url-substring-sanitization`). `is_github_api()` parses the URL
with `urllib.parse` and requires `https` and the host `api.github.com` exactly.
`tests/githubToGiteaApiHost.test.py` covers GitHub's own URLs and the
look-alikes, and as a negative test that no script under `mirror/` tests a URL
by a substring of it.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/4ffa881dd9ab21d629713fbe05734a0cc798dd5c">Gitea is built with the hardening code scanning found worth making in it</a>. Thanks to GitHub CodeQL and xet7.</summary>

`dist/all/security-hardening.patch` caps result slices at 100 whatever page
size a request asks for (the container registry's `?n=` reserved gigabytes
before reading a row), bounds `int64` totals and issue numbers on 32-bit
platforms, redacts credentials from the access log, keeps session IDs, temp
names and Git hook names inside their directories, binds the job summary
upsert and the milestone keyword as SQL arguments, keeps an external
renderer's request-derived variables out of the program it runs, and computes
the fast hashes that are not password storage through the crypto registry.
The alerts were raised on wekan/caddy-gitea-plugin, whose own patch carried
this; it is Gitea's code, so it is here now. Re-ported onto upstream main,
where the repository adopt feature is gone (go-gitea/gitea#40181). Tests
include `TestNoPageSizeCapacity`, which fails on a page-size capacity anywhere
in Gitea, and `TestTwoFactorEncryptionKeyUnchanged`, which reads a TOTP
secret stored the old way.

</details>

and adds the following new features:

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

**Mirroring** - the local Gitea on to GitLab and SourceForge.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/cbf87d86cf8c185e0e0c28b80f7bdcf9aeb480c3">Back up, delete and restore a Gitea organization or user - into Gitea or WeKan</a>. Thanks to xet7.</summary>

github-to-gitea's menu item 11, Backups (`mirror/common/gitea_backup.py`).
**Back up** writes every repository, issue, pull request, comment, label,
milestone, release, wiki, team, collaborator and file of an organization or
user into `<backup_dir>/<name>/YYYY-MM-DD_HH-MM-SS/<name>.db` - SQLite with
Gitea's own JSON of every record and each file's size and SHA-256 - and
`<name>.zip`, with git and wiki bundles and every file in folders, stored and
not compressed, so a reader can show or replace one entry in place.
**Delete** removes the organization or user from Gitea completely, packages
too, after listing what goes and only when its name is typed. **Restore into
Gitea** keeps the issue and pull request numbers, the authors (Sudo), merged
pull requests, files with their links updated, teams and collaborators;
**restore into WeKan** writes one board .zip a repository and can import it
at once. `backup_dir` can point to another disk. `tests/giteaBackup.test.py`
runs the round trip against a disposable Gitea.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/e994d45ca067704b3b7089407154187cc6262b2f">The users and organizations that take the most space in the local Gitea</a>. Thanks to xet7.</summary>

Gitea's space menu (github-to-gitea's item 10, the mirror scripts' item after
their own) has a third item: every repository of the Gitea the token can see,
summed per user and organization - repositories, git, attachments, together
and share - and the N largest, as many as asked. Release files are counted by
default; `0` counts git sizes only, which Gitea knows without reading
anything. Read-only; `tests/giteaAttachments.test.py` checks the sums, the
order and that git-only reads no attachment list.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/47fdc4ed21fd8f8b6462b4a195b079f0f12dd8c5">Gitea's attachments: space used, the largest repositories, and deleting them; excluded repositories</a>. Thanks to xet7.</summary>

github-to-gitea, gitea-to-gitlab and gitea-to-sourceforge share a new menu
(`mirror/common/gitea_attachments.py`), github-to-gitea's item 10 and the mirror
scripts' item after their own. **Space used**, for an organization or user
(every repository it owns) or one `owner/name`, shows git, attachments
(release files, issue and pull request files, comment files) and both
together, and the N largest repositories - it asks how many - by git and
attachments together; for one repository also its N largest files.
**Delete attachments** removes the chosen kinds of an organization or user,
or of one repository such as `wekan/wekan`: it lists what would go and its
size, and deletes only when the scope's name is typed; a dry run deletes
nothing. github-to-gitea's earlier release-file cleanup of every repository
is an item of the same menu. Sources can leave a repository out - `microsoft`
with `microsoft/verybig` excluded - in both scripts; a repository listed by
name is never left out. `tests/giteaAttachments.test.py` runs it against a
fake Gitea, and `tests/mirror.test.py` checks the mirrors' sources.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/c1678702012830f408cffe43d785468349a3bcf3">Mirror the local Gitea to GitLab and SourceForge, in step with WeKan's mirror</a>. Thanks to xet7.</summary>

`mirror/gitea-to-gitlab` and `mirror/gitea-to-sourceforge` copy git branches and
tags (never forced), the wiki, issues and pull requests with comments, labels,
milestones and closed state, and releases with their files, with the menu of
`github-to-gitea` and the read-only checks and run log of WeKan's
`releases/mirror.sh`. Every copy carries WeKan's `<!-- wekan-mirror:URL -->`
marker with the item's GitHub address when Gitea migrated it from GitHub, so
nothing that mirror already copied is copied twice. GitLab release files go to
the generic package registry; SourceForge uses WeKan's tool names and
`GitHub-releases/` files. Rate limits are waited out, an interrupted run
resumes, and `tests/mirror.test.py` runs both against fake forges.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/8121fae85ba317f2daa619e58a30f94e830814da">Mirror the avatars of organizations, users and repositories</a>. Thanks to xet7.</summary>

A new step, `avatars`, in all three tools. github-to-gitea copies GitHub's
organization and user avatars onto Gitea, and a repository's custom social
preview image as its avatar; gitea-to-gitlab copies them onto GitLab groups and
projects; SourceForge has no API for project icons, which is reported. An avatar
is uploaded again only when the image changes.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/0109b7aee7d8e9992efa2e235d812a69fc74f108">github-to-gitea mirrors the newest release's files by default, deletes older releases' files, one file at a time</a>. Thanks to xet7.</summary>

`release_assets` is `github` by default (it was `none`), and the new
`release_assets_keep` says which releases keep their files: `newest` (the
default), `all`, or a number of the newest releases; the files of older
releases are deleted from Gitea. A file from GitHub is downloaded alone to
WeKan's `.tools/tmp/github-to-gitea/`, uploaded, and deleted - also when that
fails. `tests/githubToGiteaReleases.test.py` covers each setting.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/8220d434cdb5cd88b71b02175b0f93c79d9fb90e">github-to-gitea copies every repository's wiki into its Gitea Wiki tab</a>. Thanks to xet7.</summary>

The migration copied a wiki once, and only when GitHub said `has_wiki` then, so
a wiki written later never reached Gitea. The step `git` now copies each wiki
GitHub has into Gitea's `<name>.wiki.git`, shown in the repository's Wiki tab:
the wiki is turned on when it is off, a wiki Gitea does not have yet is first
initialized by Gitea itself (it takes no push into one that does not exist,
and creates its hooks then), and GitHub's default wiki branch goes to Gitea's
default wiki branch. With the bare repository on this machine the wiki is
fetched into it; otherwise it is pushed through a copy under `.tools/tmp`.
`tests/githubToGiteaWiki.test.py` covers all three cases.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/454bcc08f314b6c4e8e02083b044fd861b226d0c">github-to-gitea syncs a GitHub fork with its upstream, and copies upstream's newest release</a>. Thanks to xet7.</summary>

A fork with no commits of its own, whose upstream is newer, is first brought up
to the upstream on GitHub with `gh repo sync` - gh's own login - and only then
copied to Gitea. GitHub's compare API decides: a fork that is ahead or has
diverged is never synced, and gh runs without `--force`, so it only
fast-forwards (setting `sync_forks`). A fork with no releases of its own gets
its upstream's newest published release, text and files, with the tag and its
commit fetched from the upstream first (setting `fork_upstream_release`).
`tests/githubToGiteaFork.test.py` covers both, with the negative cases.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/0bfa453d7527d4f0aa485ac5a7b9681f9d68234f">github-to-gitea's menu says which repositories keep every release; the others get only the newest</a>. Thanks to xet7.</summary>

The menu's new item 9, Releases kept in full, lists the sources whose every
release is synced, text and files, and keeps them all: `(all)`, `(allpublic)`,
`(allprivate)`, a GitHub organization or user, or `owner/name` (saved as
`[releases_all] list`). With nothing listed, a repository gets only its newest
release, text and files, and the files of older releases are deleted from
Gitea; an older release already there keeps its text.
`tests/githubToGiteaReleases.test.py` covers each form, the menu and the
negative cases.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/b5d42d6634407e1317eca9a5ad420a24342e98e9">gitea-to-gitlab and gitea-to-sourceforge copy the newest release only, unless kept in full</a>. Thanks to xet7.</summary>

The same release rules as github-to-gitea, in the shared engine: by default
only the newest release, notes and files, and the files of older releases are
deleted from the destination (`release_assets_keep`); the menu's item 9,
Releases kept in full, lists the Gitea sources that keep every release. On
GitLab an older release loses its asset links and generic package version, on
SourceForge the files of its directory except `README.md`, its notes. Release
files, avatars and the notes file go through WeKan's `.tools/tmp/<script>/`,
and gitea-to-gitlab turns a project's wiki on when Gitea's wiki has pages.
`tests/mirror.test.py` covers both destinations, the menu and the negative
cases.

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

**Release files of any size** - in Gitea and in the mirror scripts, by default.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/2cb9185b50fbf937bb92861a9e6ee198c7e37789">Gitea takes release files of any size by default</a>. Thanks to xet7.</summary>

The first code patch in `dist/all/`: `[repository.release] FILE_MAX_SIZE` is
`-1`, no limit, by default (it was 2048 MB), and `-1` now works where upstream
refused every file with it - an API upload streamed as
`application/octet-stream`, whose `http.MaxBytesReader` takes a negative limit
as 0, and the upload form, whose Dropzone refuses every file with a negative
`maxFilesize` and applies 256 MiB without one. Go tests and a vitest cover it;
the upload test fails without the fix.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/4c16bbb5ff73bb6b44f53dd0901b1faf282d9d14">The mirror scripts copy release files of any size by default</a>. Thanks to xet7.</summary>

`assets_max_mb` is `0`, no limit, by default in github-to-gitea (it was 2048)
and in gitea-to-gitlab and gitea-to-sourceforge (5000). A settings file with
the old default gets `0` once, recorded under `[migrations]`, so a limit chosen
after that stays. No limit became `-1` in the next entry.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/e9ad778ba62331f40edcb76037440f5cf2f3a307">Release file size limits read as Gitea's: -1 is no limit, 0 is no file</a>. Thanks to xet7.</summary>

`assets_max_mb` reads as Gitea's `FILE_MAX_SIZE`: `-1`, the default, is no
limit, `0` is a largest size of 0 so no file is copied, and a number is the
largest size in MB. A settings file with `0` or an old default gets `-1` once,
and the menus refuse other values. The Gitea patch's upload form gave Dropzone
`maxFilesize` 0 for `FILE_MAX_SIZE = 0`, which Dropzone takes as no limit; it
now gives `maxFiles` 0, and the upload test checks 0 too.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/f7402f10721edaa974b3f1f7bdb5223ced684388">The release file size patch applies to upstream main again</a>. Thanks to xet7.</summary>

Upstream's attachment SHA-256 (go-gitea/gitea#39954) rewrote the imports of
`services/attachment/attachment_test.go`, so the patch stopped applying and a
release from upstream `main` stopped at it. Re-ported: only the test's imports
changed. Every patch in `dist/` applies to upstream `main` again, and the
packages they touch build, vet and pass their tests there.

</details>

**A release of many versions** - rustdesk's nightly style, newest version only.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/488f9603687b59c0900b90bfbeee0dae0015cbfb">The mirror scripts copy only the newest version's files from a release of many versions</a>. Thanks to xet7.</summary>

A release like rustdesk's `nightly` holds the same files with different version
numbers: 352 files of 20 versions, 9.5 GB. When a file name, its version number
taken out, appears with more than one version, only the newest version's files
are copied (24 files, 780 MB), and older versions' files already at the
destination are deleted. Any other release keeps every file, and so does
Releases kept in full. GitLab now deletes single package files rather than the
release's whole package.

</details>

**Freeing disk space** - in the local Gitea.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/51b662900cf0bfe4b1f5423ff5c7fb4b5a71bd56">github-to-gitea's menu frees Gitea's disk by deleting release files</a>. Thanks to xet7.</summary>

Menu item 10 deletes release files from every repository of the local Gitea,
not only the configured sources: keep the newest release's files and delete
the older ones (drafts and Releases kept in full are left alone), or delete all
of them. The releases and their notes stay. It asks for `yes`, deletes nothing
in a dry run and says how much it freed; `--prune-release-files older|all` does
the same from cron. `tests/githubToGiteaReleases.test.py` covers both choices,
the menu and the negative cases.

</details>

and fixes the following bugs:

**Mirroring** - github-to-gitea's errors in runs of 942 and 11,761 repositories.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/60ba0a05ea6a1b54bf840a199fc6947fbbfae618">A non-UTF-8 workflow file is read, and one repository's fault no longer stops the run</a>. Thanks to xet7.</summary>

A run stopped at QB64pe: a workflow file had a Windows-1252 en dash (0x96), and
reading it as UTF-8 raised an error nothing caught. Files are read as UTF-8,
else as Windows-1252. In all three scripts an unexpected fault in one
repository is logged with its traceback and counted as failed, to be tried
again next run, while the run goes on; Ctrl-C still stops it.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/2ab04878bb0213573de51d5c28514f4719e45303">Create organizations whose GitHub website has no https://, and fix two false failures</a>. Thanks to xet7.</summary>

EthKan's organization was refused (`422 [Website]: Url`) because its GitHub
website is `ethkan.io`: it now gets `https://`, and the organization is
created without a website when Gitea still refuses it. `wekan/ldap` was
reported as wrongly imported because GitHub moved its issue #2 to #69; a moved
number is now skipped by the numbering check, as the issue sync already did.
`xet7/Rumble`, whose history has a submodule URL Gitea's git fsck refuses, no
longer waits for a second attempt that fails the same way, and the error says
what to add to Gitea's `app.ini` to accept it.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/7449010cf4730754545a8c8971c0d8cac7b3721b">A draft pull request's WIP: title prefix is not a wrong import</a>. Thanks to xet7.</summary>

`riscv/riscv-attached-matrix-extension` was reported as wrongly imported
because Gitea prefixes an imported draft pull request's title with `WIP:`.
Titles are now compared without Gitea's `WIP:` and `[WIP]` prefixes, in the
numbering check and in the issue sync, which would otherwise have undrafted it.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/3740b6ba40a98d4fc2c4be07126c688abb1d45fb">A new pull request's head commit reaches Gitea even when Gitea's bare repository is not on this machine</a>. Thanks to xet7.</summary>

Run elsewhere than on Gitea's machine, the git step was skipped, and a new pull
request whose head commit Gitea did not have stopped the repository ("PR #6879:
head commit f4f4c7f2cb is missing from the Gitea repository"). That one commit
is now fetched from GitHub's `refs/pull/<N>/head` and pushed to Gitea as branch
`github-pr/<N>` - the branch the PR is made from anyway - after checking it is
the commit GitHub's API names; nothing else is pushed, and the tokens never
appear on a git command line. A commit Gitea does not have is
[pushed at once](https://github.com/wekan/gitea-patches/commit/9d3723a9f9c35532f6762f7fe569f71bfb1dfbc9),
without first asking Gitea for a branch of it - which only wrote a 500 to its
log - and Gitea's "Create a new pull request" push messages are not shown.
`tests/githubToGiteaPush.test.py` runs it against local repositories standing
in for GitHub and Gitea. And Gitea's paging
[ends on a page that brings nothing new](https://github.com/wekan/gitea-patches/commit/ef874664d6e5fdcb3a091c607ac532996c302979):
Gitea ignores page for an issue's comments with `?since=`, and a run went on to
page 83636 of one comment. Gitea's bare repository is
[found by its lower-case path](https://github.com/wekan/gitea-patches/commit/ec05cda01d5f6b42edb199178401f63fcea8e08d),
as Gitea keeps it: the git step skipped every repository with a capital letter
in its owner or name, such as xet7/NEONnoir.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/42dd9f63774550b9050b7fda5fa367303a36746d">Every mirror script writes what the screen shows to WeKan's .tools/log/mirror/</a>. Thanks to xet7.</summary>

`.tools/log/mirror/<script>/YYYY-MM-DD_HH-MM-SS.txt` in WeKan's checkout -
`~/repos/wekan` on Linux, `~/Documents/repos/wekan` on macOS,
`Downloads\repos\wekan` on Windows: the script's lines, the menu's prompts and
the messages of git and the other programs it runs, in the screen's order and
flushed before the program ends. `tests/screenLog.test.py` checks all three
implementations and the place on each operating system.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/7261ea4a4df91aadafc72e5cd5dd872f4ab6f5ec">github-to-gitea's assets_max_mb names Gitea's real release file limit, and 0 is no limit</a>. Thanks to xet7.</summary>

The setting said it matched Gitea's `[attachment] MAX_SIZE`, the limit of files
attached to issues; release files are limited by `[repository.release]
FILE_MAX_SIZE`, where `-1` is no limit. The README says how to lift both, and
that a large file also needs room in Gitea's `TMPDIR` and a reverse proxy's
request limit lifted.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/66d9c35e7a204464bd5ddb66bea091e31b0b7498">github-to-gitea's gh repo sync uses gh's own login, not the settings file's token</a>. Thanks to xet7.</summary>

gh prefers `GITHUB_TOKEN` to its own login, and the script had put `[github]
token` there, so forks whose upstream changed `.github/workflows` were refused
for the `workflow` scope even after `gh auth refresh -s workflow`. gh is now
started without the token variables.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/1674844d06ef573f00ae01aaa357089ad3157349">The mirror scripts' screen log keeps a child's stdout and stderr in order</a>. Thanks to xet7.</summary>

Two pipes read by two threads could swap a child's lines in the log, and
`tests/screenLog.test.py` failed now and then. When stdout and stderr go to the
same place they now share one pipe; the test runs as a terminal does.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/5084850066a3f3dd0dd098771963bd22fc1a27e8">gitea-to-sourceforge uses SourceForge's REST API as its api-docs document it</a>. Thanks to xet7.</summary>

Checked against <https://sourceforge.net/api-docs/>: tickets' `new` and `save`
are now sent as `multipart/form-data`, not urlencoded; `private` and
`discussion_disabled` as `true`/`false`, not Python's `True`/`False`; custom
fields as `ticket_form.custom_fields._<name>`; and the ticket list is read at
`/p/<project>/<tracker>` without a trailing slash. A token that is not
hexadecimal, as the bearer tokens are, is refused before any request, and
Check access asks `has_access?perm=admin` whether the token may administer the
project, which creating Git and Tracker tools needs. Release files stay on
sftp: the API has no file releases. `tests/mirror.test.py` checks each.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/75ff479e939406d50acff9f6f4e8336c35b7585d">gitea-to-sourceforge signs in with an OAuth2 application's Client ID and Client Secret</a>. Thanks to xet7.</summary>

SourceForge's button that creates a bearer token did not work, so the setting
`token` is replaced by `client_id`, `client_secret` and `redirect_uri`
(`https://localhost/` by default; SourceForge takes only `https://`). Menu item
10 signs in with the authorization code flow and PKCE: open the address,
authorize, and paste the address the browser went to. The access and refresh
tokens are saved in `[sourceforge_oauth2]`, and the access token is renewed with
the refresh token when it expires or SourceForge answers 401, so cron runs need
no browser. OAuth2 access tokens are not hexadecimal, so the hex check of the
previous entry is replaced with refusing only a token that could end the header.
The answer's state is checked, and an earlier hand-made token moves to
`[sourceforge_oauth2]`.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/b2a81c76b19a36c12c045d0c4713d0662c5648e3">gitea-to-sourceforge signs in with the OAuth2 application's own redirect URL</a>. Thanks to xet7.</summary>

SourceForge refused the sign-in with "Mismatching redirect URI": the script
sent `https://localhost/`, and SourceForge takes a redirect URL only exactly as
the application registered it. `redirect_uri` is now empty by default and then
not sent, so SourceForge uses the application's first registered redirect URL
in both the authorization and the code exchange; a settings file with the old
default gets the empty value once. Pasting nothing, or the sign-in address
itself, is refused with what to paste instead.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/dea6f5d9f3379ae79f718a752b4872177d90867f">PRs whose commits GitHub's refs do not bring, deleted base branches, and a dropped connection</a>. Thanks to xet7.</summary>

A PR's head commit the repository's git fetch did not bring stopped it with
"object does not exist": cloudflare/moq-rs #317 had no refs/pull/317/head,
storytold/figcraft #2396's ref was a commit behind, and two storytold PRs were
newer than the fetch. The commit is now fetched by its name, into Gitea's bare
repository or the push cache, and the message no longer claims a push that was
not tried. A PR into a base branch GitHub deleted (craft-libs #1185, wordcraft
#6955) gets that branch made again at its base commit. "Remote end closed
connection without response" during microsoft/AEC-Challenge's LFS files no
longer removes a repository Gitea is still migrating, and a leftover is removed
only when it is older than a day. Gitea's issue counter behind its issues
(godotengine/godot #78432, "UNIQUE constraint failed") is named, with the SQL
that sets it.

</details>

**Mirroring on to GitLab and SourceForge** - a branch a tool makes again.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/d43da9185ba70dbb8ffcfa2e1da4d7e62e1539ae">The gitea-actions branch github-to-gitea regenerates reaches GitLab and SourceForge</a>. Thanks to xet7.</summary>

github-to-gitea deletes `gitea-actions` and creates it again on top of the
newest `main`, so each version is a non-fast-forward of the one before, and
gitea-to-sourceforge rejected it as "the destination has commits the source
does not; not forced": the mirror kept the first version for ever. The new
setting `force_branches` (default `gitea-actions`) names the branches a tool
regenerates; only those are pushed with force, in a push of their own, because
a negative refspec excludes its ref from every refspec of a push. Every other
branch and tag is still never forced and never deleted. `tests/mirror.test.py`
regenerates the branch and finds it force-updated, while a `main` that moved
its own way is still not overwritten, and with `force_branches` empty the
branch is not forced.

</details>

**Long migrations** - Gitea's SQLite and clone time limit, which broke them off,
and what a long one is doing.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/49cc43fcd08c7ba037ef148c47003b0743b5ca73">SQLite in WAL mode and a day for a migration's clone, by default; such failures are not retried</a>. Thanks to xet7.</summary>

github-to-gitea's migration of godotengine/godot broke off at 42,535 issues,
after two and a half hours, with `database is locked` (`SQLITE_BUSY`), and
storytold/harfbuzz the same way: in SQLite's default journal mode every reader
blocks the writer, and Gitea's nightly repository health check and its mirror
and Actions queries ran beside the import. LibreOffice/translations and
translations-weblate broke off with `clone timed out`. A migration cannot be
resumed, and each was started again to fail the same way.

`dist/all/sqlite-wal-by-default.patch` makes `[database] SQLITE_JOURNAL_MODE`
`WAL` when it is not set, so readers and the writer work at once; a database
opened with Gitea's own connection string was checked to be in WAL mode.
`dist/all/migrate-timeout-a-day.patch` makes `[git.timeout] MIGRATE` 86400 s
when it is not set (it was 600). A configured value is kept by both, and each
has a Go test that fails without it. github-to-gitea stops a repository whose
migration failed in either way with the `app.ini` settings that fix it, as it
already did for git fsck, instead of a second attempt of hours; any other
failure is still tried once more. Removing the leftover repository no longer
says that nothing is lost: what it imported is discarded.
`tests/githubToGiteaMigrateErrors.test.py` covers it, and 6 of its checks fail
with the old script.

</details>

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/8976ac7cbb24aaa7567a1a43bc073a2ac62f15b1">A migration that downloads Git LFS files says so, instead of only "issues 0, PRs 0"</a>. Thanks to xet7.</summary>

Gitea downloads every Git LFS file of a repository before it reads a single
issue, so microsoft/AEC-Challenge - its `.wav` datasets are in LFS - showed
`issues 0, PRs 0` every five minutes for hours, as if the migration were stuck.
github-to-gitea reads the repository's `.gitattributes` first: when it has LFS
patterns and `lfs` is on, it names them and says that `lfs = no` leaves the
files out, and while nothing has arrived yet each progress line says it is
still cloning, and for how many minutes. `tests/githubToGiteaLfs.test.py`
covers it, a repository without LFS and `lfs = no` included.

</details>

and has the following developer-facing changes:

**Changelog** - two files, as in WeKan.

<details>
<summary><a href="https://github.com/wekan/gitea-patches/commit/58b3df05069bb451ac72d5b1cb218795e657db74">CHANGELOG.md is the upcoming release and each release in short, from CHANGELOG-big.md</a>. Thanks to xet7.</summary>

The changelog that is written is now `CHANGELOG-big.md`, every entry in full.
`CHANGELOG.md` is generated from it by `releases/changelog-short.py`: the
**In short** paragraph of the Upcoming release and of each of the five newest
releases, each linking all of its changes, in plain Markdown. Run on WeKan's
own `CHANGELOG-big.md` with WeKan's headings, the generator writes exactly
WeKan's `CHANGELOG.md`. `tests/changelogShort.test.py` fails when
`CHANGELOG.md` is out of date, and checks `CHANGELOG-big.md`'s headings, its
In short paragraphs and that every summary links its commit.

</details>

**Mirroring tools** - where they are.

- [Move github-to-gitea into mirror/, beside gitea-to-gitlab and gitea-to-sourceforge](https://github.com/wekan/gitea-patches/commit/9713a8ade052985bf5c8e4ef81bac54604ab25d9). Thanks to xet7.

Thanks to above GitHub users for their contributions and translators for their translations.

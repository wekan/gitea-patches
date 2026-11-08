# mirror/ - GitHub -> local Gitea -> GitLab and SourceForge

| Script | From | To |
|--------|------|----|
| [github-to-gitea/github-to-gitea.py](github-to-gitea/github-to-gitea.py) | GitHub | the local Gitea |
| [gitea-to-gitlab/gitea-to-gitlab.py](gitea-to-gitlab/gitea-to-gitlab.py) | the local Gitea | GitLab |
| [gitea-to-sourceforge/gitea-to-sourceforge.py](gitea-to-sourceforge/gitea-to-sourceforge.py) | the local Gitea | SourceForge |

All three have the same menu (run every step, run chosen steps, sources, settings, dry
run) and the same command line (`--all`, `--source`, steps, `--restart`). The two Gitea
scripts also have WeKan's `releases/mirror.sh` checks - **Check access** and **Check
what is not mirrored yet**, both read-only - and a Destinations menu. Every script
here writes what a run shows on the screen - git's messages included - also to
`.tools/log/mirror/<script>/YYYY-MM-DD_HH-MM-SS.txt` in WeKan's checkout:
`~/repos/wekan` on Linux, `~/Documents/repos/wekan` on macOS and
`Downloads\repos\wekan` under the user's profile on Windows (`MIRROR_LOG_DIR`
overrides it).

```sh
cd mirror/gitea-to-gitlab
./gitea-to-gitlab.py                  # the menu
./gitea-to-gitlab.py --check-online   # can Gitea and GitLab be reached
./gitea-to-gitlab.py --check-missing  # what a run would copy, copying nothing
./gitea-to-gitlab.py --all            # every configured step for every source (cron)
```

github-to-gitea's step `avatars` copies each GitHub organization's or user's avatar
onto its Gitea organization (or the token's own Gitea user - Gitea's API sets no
other user's), and a repository's custom social preview image - GitHub's only
repository picture - as its Gitea avatar. Every tool uploads an avatar again only
when the image changes.

## Sources

| Source | What is mirrored |
|--------|------------------|
| `owner/name` | one Gitea repository |
| `org` | every repository of a Gitea organization |
| `user` | every repository a Gitea user owns |
| `(all)` | every repository the Gitea token can read |
| `(allpublic)` / `(allprivate)` | every public / private repository the Gitea token can read |

Each repository goes to the GitLab group or user, or the SourceForge project, named in
the menu's **Destinations** - by default the same name as its Gitea owner.

## What is copied

| | GitLab | SourceForge |
|---|---|---|
| git branches and tags | pushed without force, never deleting | the same, to a Git tool of the project |
| wiki | the project's git wiki | - (SourceForge's wiki is not git) |
| issues, pull requests | issues; a PR is "[PR] ..." | tickets of a Tracker tool |
| comments, labels, milestones, closed state | yes | comments, labels, closed state |
| files attached in Gitea | uploaded to the GitLab project | links to the Gitea public address |
| releases | releases, files in the generic package registry | `<files_dir>[/<repository>]/<tag>/` in the project's Files, notes as README.md |
| private repositories | private projects (`private = no` skips them) | not mirrored: SourceForge projects are public |
| avatars (step `avatars`) | organization or user -> GitLab group (or the token's own user); repository -> project avatar | not possible: SourceForge's API has no project icon; reported once per run |

github-to-gitea mirrors release files by `release_assets` - `github` (the default:
from GitHub), `local` (`assets_dir`) or `none`. By default only a repository's
**newest release** is synced - its text and its files - and the files of older
releases are deleted from Gitea; `release_assets_keep` can make that the N newest
(or `all`). The menu's **9) Releases kept in full** lists what keeps EVERY release,
text and files: `(all)`, `(allpublic)`, `(allprivate)`, a GitHub organization or user
(`wekan`), or one repository (`wekan/wekan`) - saved as `[releases_all] list` in
`github-to-gitea.ini`. An older release already in Gitea - from the migration, or
from before - keeps its text and loses its files. A file from GitHub is downloaded
alone to WeKan's `.tools/tmp/github-to-gitea/`, uploaded to Gitea, and deleted, so at
most one release file is on the disk at a time.

**A release of many versions** - like rustdesk's `nightly`, which holds the same
files over and over with different version numbers (`rustdesk-1.2.0-x86_64.deb` ...
`rustdesk-1.4.3-x86_64.deb`: 352 files, 9.5 GB) - is recognised when a file name, its
version number taken out, appears with more than one version. Then only the newest
version's files are copied (for rustdesk 24 files, 780 MB), with the files that have no
version number, and an older version's files already at the destination are deleted.
Versions compare as numbers (1.10 is newer than 1.9). A release whose files share one
version, or whose differently numbered files have different names, keeps every file,
and so does every repository in Releases kept in full. All three scripts do this; on
GitLab only those files' links and package files are deleted, not the release's
package.

**Freeing the local Gitea's disk.** github-to-gitea's menu item **10) Free Gitea's
disk space** deletes release files from EVERY repository of the local Gitea the token
can see - every one with an admin token - not only the configured sources:

| Choice | What is deleted |
|--------|-----------------|
| 1) Keep newest release attachments, delete older | the files of every release but each repository's newest published one; drafts and the repositories in Releases kept in full are left alone |
| 2) Delete all release attachments | every release file |

The releases and their notes stay. It asks you to type `yes`, deletes nothing in a
dry run, and prints how many files and gigabytes it freed. For cron:
`./github-to-gitea.py --prune-release-files older` (or `all`), without asking.

**Release files of any size into Gitea.** Two limits apply, and both can be lifted:

- The scripts' setting `assets_max_mb`, read as Gitea reads `FILE_MAX_SIZE`: `-1` is
  no limit - the default in every script - `0` is a largest size of 0, so no file is
  copied, and a number is the largest size in MB, a larger file being skipped. (It was
  2048 in github-to-gitea and 5000 in the others, and then for a while `0` meant no
  limit; a settings file with one of those gets `-1` once, and a value chosen after
  that stays.)
- Gitea's own, in its `app.ini` (then restart Gitea). The release files' limit is
  `[repository.release] FILE_MAX_SIZE`, in MB, default 2048 - not `[attachment]
  MAX_SIZE`, which is the limit of files attached to issues and comments. `-1` is no
  limit and `0` lets no file through: Gitea checks the size only when the limit is not
  negative, and the script's upload is multipart, whose size Gitea knows.

  ```ini
  [repository.release]
  FILE_MAX_SIZE = -1
  ```

  A Gitea built from this repository has `-1` as its default
  ([dist/all/release-files-any-size.md](../dist/all/release-files-any-size.md)), with
  the API's streamed uploads and the upload form fixed to take it; upstream Gitea needs
  the line above.

  Gitea writes an uploaded file to its temporary directory before storing it, so the
  `TMPDIR` of the Gitea process needs room for the largest file - `/tmp` is often a
  small tmpfs - and a reverse proxy in front of Gitea needs its own request size
  limit lifted (nginx `client_max_body_size 0;`; Caddy has none by default).

gitea-to-gitlab and gitea-to-sourceforge do the same, from Gitea: the same
`release_assets_keep` setting (default `newest`), and the same menu item **9)
Releases kept in full** with Gitea's organizations, users and repositories. An older
release keeps its notes - on GitLab the release, on SourceForge its `README.md` - and
its files are deleted: on GitLab its asset links and the generic package version that
holds them, on SourceForge the files of its directory. A file is downloaded alone to
WeKan's `.tools/tmp/<script>/` and deleted after it is copied. gitea-to-gitlab turns a
GitLab project's wiki on when Gitea's wiki has pages and the project's wiki is off, so
the copied wiki shows in its Wiki tab.

github-to-gitea's step `git` also copies the wiki of every repository that has one
on GitHub - also a wiki written after the migration, which copies it only once. Gitea
keeps a wiki as a git repository of its own beside the repository,
`gitea-repositories/<owner>/<name>.wiki.git`. Gitea takes no push into a wiki it has
not initialized, so a missing one is first created by Gitea itself (wiki turned on, a
first page written, Gitea's hooks in place), and GitHub's wiki then replaces that page.
GitHub's default wiki branch (`master`) goes to Gitea's default wiki branch (often
`main`), the other branches by their names. As with the repository's branches, GitHub
is the source: an edit made only in Gitea's wiki is overwritten. With Gitea's bare
repository on this machine the wiki is fetched into it; otherwise it is pushed through a
copy in WeKan's `.tools/tmp/github-to-gitea/wiki/`.

A repository that is a **fork on GitHub** gets two more things from github-to-gitea:

- When it has no commits of its own and its upstream is newer, it is first brought up
  to the upstream **on GitHub**, with `gh repo sync <fork> --source <upstream> --branch
  <default branch>` - the `gh` command's own login, which already may write there -
  and only then copied to Gitea. GitHub's compare API decides: a fork that is ahead of
  its upstream or has diverged from it is never synced, and `gh repo sync` runs without
  `--force`, so it only fast-forwards. Setting `sync_forks` (default `yes`).
- When it has no releases of its own, its upstream's newest published release - text
  and files - is copied to Gitea, with the release's tag and commit fetched from the
  upstream (a fork does not get upstream's newer tags). An upstream release copied by
  an earlier run keeps its text, and its files are deleted, as for the fork's own
  older releases. Setting `fork_upstream_release` (default `yes`).

A missing GitLab project is created, in a group that must exist. A SourceForge project
cannot be created through its API: make it at sourceforge.net; its Git and Tracker tools
are created by the script.

## Working with WeKan's releases/mirror.sh

Every copied issue, comment and release carries `<!-- wekan-mirror:URL -->`, the marker
WeKan's mirror writes. For a repository the local Gitea migrated from GitHub, URL is the
**GitHub** address of the item, exactly what WeKan's mirror uses - so what it already
copied from GitHub is recognised and not copied twice, and the other way round.
Migrated comments have no GitHub id; they are also recognised by their text. The
SourceForge layout is WeKan's too: the tool `code` for the repository named like the
project, `repo-<name>-<hash>` for the others, and release files in `GitHub-releases/`
(setting `files_dir`).

Set `[gitea] public_url` when the Gitea has a public address: links into it are written
with that address. Without it they point to `url`, often `http://localhost:2000`.

## Rate limits, retries and resume

- A "too many requests" answer is waited out (`Retry-After`, `RateLimit-Reset`), and
  does not count as a failed attempt.
- Reads and other repeatable requests are retried on a server or network error. A
  request that creates something is not: it may have succeeded before the error, and
  the next run finds it by its marker instead of creating it twice.
- `<script>-state.json` remembers which destination item each source item became, and
  when each repository's issues were last synced: the next run reads only what changed
  since. An interrupted run is resumed by the next run of the same steps and sources;
  `--restart` reads everything again. An issue that failed is read again next run.

Settings with tokens (`*.ini`, mode 600), state, logs and `work/` (git copies, downloads)
are listed in `.gitignore`: they stay on this machine.

## Tests

```sh
python3 -I tests/mirror.test.py
python3 -I tests/githubToGiteaPush.test.py
python3 -I tests/githubToGiteaReleases.test.py
python3 -I tests/githubToGiteaWiki.test.py
python3 -I tests/githubToGiteaFork.test.py
python3 -I tests/screenLog.test.py
```

A fake Gitea, GitLab and SourceForge and local git repositories - no network.

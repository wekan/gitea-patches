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

**Excluding a repository.** Sources' `x) Exclude a repository` leaves one `owner/name`
out of what an organization, a user or `(all)` expands to - `microsoft` as a source
with `microsoft/verybig` excluded mirrors every other microsoft repository; `i)`
includes it again. A repository listed by name as a source is never left out. The
list is `[sources] exclude` in the settings file, the same in github-to-gitea, where
it applies to the GitHub repositories an owner expands to.

## What is copied

| | GitLab | SourceForge |
|---|---|---|
| git branches and tags | pushed without force, never deleting - except `force_branches` | the same, to a Git tool of the project |
| wiki | the project's git wiki | - (SourceForge's wiki is not git) |
| issues, pull requests | issues; a PR is "[PR] ..." | tickets of a Tracker tool |
| comments, labels, milestones, closed state | yes | comments, labels, closed state |
| files attached in Gitea | uploaded to the GitLab project | links to the Gitea public address |
| releases | releases, files in the generic package registry | `<files_dir>[/<repository>]/<tag>/` in the project's Files, notes as README.md |
| private repositories | private projects (`private = no` skips them) | not mirrored: SourceForge projects are public |
| avatars (step `avatars`) | organization or user -> GitLab group (or the token's own user); repository -> project avatar | not possible: SourceForge's API has no project icon; reported once per run |

`force_branches` (default `gitea-actions`) names branches a tool makes again from
scratch: github-to-gitea deletes `gitea-actions` and creates it on top of the newest
`main`, so each version is a non-fast-forward of the one before. Only those are pushed
with force, in a push of their own; every other branch and tag is still never forced.
Empty it to force nothing.

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

**Gitea's space: users, organizations, repositories and attachments.** github-to-gitea's menu item
**10)**, and the mirror scripts' item after their own (11 in gitea-to-sourceforge, 10
in gitea-to-gitlab), are the same menu (`common/gitea_attachments.py`). An
attachment is a release's file or a file attached to an issue, a pull request or a
comment; a repository's git is never deleted here.

| Choice | What it does |
|--------|--------------|
| 1) Space used | for an organization or user (every repository it owns) or one `owner/name`: git, attachments and both together, per kind, and the N largest repositories (it asks how many) by git and attachments together; for one repository also its N largest files. Read-only |
| 2) Delete attachments | the chosen kinds (release files, issue and pull request files, comment files) of an organization or user - its excluded repositories left out - or of one repository. Lists what would go and its size, then deletes only when the scope's name (e.g. `wekan/wekan`) is typed; a dry run deletes nothing |
| 3) The users and organizations that take the most space | every repository of the Gitea the token can see - every one with an admin token - summed per user and organization: repositories, git, attachments, together and share, the N largest (it asks how many). Release files are counted by default, `0` counts git only (instant); issue and comment files take long in a large Gitea. Read-only |

Gitea removes a deleted attachment's file from its storage. The next releases step
brings back the files of the releases it keeps (`release_assets_keep`, Releases kept
in full); `release_assets = none` keeps them away.

github-to-gitea's attachments menu also keeps the earlier item for EVERY repository
of the local Gitea the token can see - every one with an admin token - not only the
configured sources:

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
  <default branch>` - the `gh` command's own login, which already may write there,
  never `[github] token` (the token variables are not passed to gh, which would prefer
  them); when upstream changed `.github/workflows`, that login needs the `workflow`
  scope once: `gh auth refresh -s workflow` -
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

gitea-to-sourceforge signs in to SourceForge with OAuth2, as an application of your
own:

1. At <https://sourceforge.net/auth/oauth/> register an **OAuth2 application** with a
   redirect URL, for example `https://localhost/`. SourceForge takes only `https://`
   addresses.
2. Put the application's **Client ID** and **Client Secret** in Settings
   (`client_id`, `client_secret`; the secret is masked in the menu). Leave
   `redirect_uri` empty: the script then sends none, and SourceForge uses the
   application's first redirect URL, so the two always match. Set it only to choose
   another URL of the same application, written exactly as it is registered. Any
   other address gets "Mismatching redirect URI".
3. Choose **10) Sign in to SourceForge**, open the address it shows, and press
   Authorize. The browser then goes to the redirect URL with `?code=...&state=...` at
   its end. For `https://localhost/` nothing answers, so the page does not load, but
   the code is in its address: copy that whole address from the address bar (not the
   sign-in address) and paste it into the script.

The script exchanges the code (with PKCE) for an access token and a refresh token
and saves them in `gitea-to-sourceforge.ini` (mode 600) under
`[sourceforge_oauth2]`. An access token expires after a while. Before it does, and
whenever SourceForge answers 401, the script gets a new one with the refresh
token, so a cron run never needs the browser again. A run that is not signed in
stops and says so; in a terminal, it offers to sign in. A token made earlier with
the old `[sourceforge] token` setting moves to `[sourceforge_oauth2]` and works
until you sign in.

It uses SourceForge's REST API as
[sourceforge.net/api-docs](https://sourceforge.net/api-docs/) documents it: requests
carry `Authorization: Bearer <access token>`;
tools are created with `admin/install_tool`; tickets are listed at
`/p/<project>/<tracker>`, created with `new` and saved with `<number>/save` as
`multipart/form-data` (booleans `true`/`false`, custom fields
`ticket_form.custom_fields._<name>`), and comments posted to the ticket's
`_discuss/thread/<id>/new`. **Check access** says whether the script is signed in,
and asks `has_access?perm=admin` whether the token may administer the project, which creating tools needs. The API has no file
releases, so release files go over sftp to `frs.sourceforge.net`, the way SourceForge
documents it, and git over SSH to `git.code.sf.net`.

## Backups of an organization or user

github-to-gitea's menu item **11) Backups** (`mirror/common/gitea_backup.py`)
works on one organization or user of the local Gitea:

| Item | What it does |
|------|--------------|
| 1) Back up | every repository, issue, pull request, comment, label, milestone, release, wiki, team, collaborator and file of `wekan`, into its one backup `<backup_dir>/wekan/wekan.db` and `wekan.zip` - made the first time, then **updated in place** with only what is new or changed |
| 2) Delete | removes the organization or user from Gitea completely - every repository with its issues, releases, wiki and files, its packages, and then the owner itself - after listing what goes and only when its name is typed; offers a backup first when there is none |
| 3) Restore into Gitea | the backup into an organization or user (the same or another name; a missing one is created): the same issue and pull request numbers, authors kept with an admin token (Gitea's `Sudo`), merged pull requests merged, files uploaded again with their links in the text updated, teams and collaborators. A repository Gitea already has is left as it is |
| 4) Restore into WeKan | one WeKan import `.zip` a repository (`wekan.json` and `attachments/`), cards in Open, Closed and Merged with their comments, labels and files - imported in WeKan from All Boards / Import, or at once with a WeKan address and sign-in |
| 5) List the backups | one line an owner: when its newest run began, its size, how many runs, from Gitea and/or GitHub |
| 6) Backup directory | the setting `backup_dir`: `github-to-gitea-backup` beside the script by default, or any path, another disk too |
| 7) Update a backup straight from GitHub (no Gitea) | the same one backup, updated in place from GitHub: what is new or changed on GitHub - or a whole one when there is none. Needs no Gitea and no Gitea token; leaves out the Sources excludes and any more repositories it is given. Also `./github-to-gitea.py --backup-from-github wekan [--exclude wekan/big]` |
| 8) Combine dated backups into one (saves disk space) | the dated backups older versions wrote, combined into the one backup, verified, and deleted - one owner or all, a dry run first. Needs no Gitea, no GitHub and no token. Also `--combine-backups wekan\|all [--dry-run] [--delete-partial]` |
| 9) What older backups had that the newest does not | the old data, per repository: issues, pull requests, comments, releases, labels, milestones, files, with counts and the items. Also `--list-old-only wekan` |
| 10) Delete data that does not exist in the newest backup | that list and the space it frees, then - only when the owner's name is typed - the rows and their zip entries removed, the zip written again without them. Also `--delete-old-only wekan [--dry-run] [--confirm wekan]` |
| 11) Migrate all Gitea to backups | see below. Also `--migrate-all-gitea [--dry-run] [--continue-on-error]` |

`wekan.db` is SQLite and keeps Gitea's own JSON of every record (its tables are
listed at the top of `gitea_backup.py`). `wekan.zip` holds the files in folders -
`<repo>/git.bundle`, `<repo>/wiki.bundle`, `<repo>/releases/<tag>/<file>`,
`<repo>/issues/<number>/<id>-<file>`, `<repo>/comments/<number>/<comment>/<id>-<file>`
- each **stored, not compressed** (they are compressed already), so a program such
as Omi can list, show and replace one without unpacking the archive. The `.db`
has each file's size and SHA-256. Not in a backup: Git LFS objects, Actions runs
and secrets, packages, and code review comments on pull request lines.

### One backup an owner, updated in place

```
<backup_dir>/wekan/wekan.db
<backup_dir>/wekan/wekan.zip
```

There is no date in the path: every run - from Gitea (item 1) or from GitHub
(item 7) - updates that backup, and adds only what is new or changed:

- **rows** are written by their primary key; the newest wins;
- **what was deleted at the source is kept**: a backup keeps the issue, comment,
  release or repository that was deleted in Gitea or on GitHub after it was backed
  up. Each row's `seen` is the run that last saw it at the source and `meta runs`
  the newest run, so `seen < runs` is **old data** - items 9 and 10;
- **files** are downloaded only when they are new or changed (a Gitea attachment
  by its id, size and uuid, a GitHub release file by its id, size and
  `updated_at`; a downloaded one that has the same SHA-256 as the old copy is not
  written again);
- **git and wiki bundles** are made again only when the branches, tags or pull
  request heads changed, from a mirror seeded from the old bundle - only the new
  objects come over the network - and a branch deleted at the source stays in
  the bundle;
- **the zip never grows with stale copies**: it is written again into
  `wekan.zip.tmp` - every kept entry copied as it is, from this disk, the new
  ones added, the ones they replace left out - and renamed over `wekan.zip`. A
  run in which nothing in the zip changed does not write it at all. When the disk
  has no room for that copy, the new entries are appended to the zip instead
  (its old length in `wekan.zip.append` until they are whole), and a later run
  with room writes it again;
- the `.db` is updated in a copy, `wekan.db.tmp`. Both are synced and renamed
  into place behind the marker `.backup.commit`: **a run that breaks off leaves
  the previous backup as it was**, and the next run removes what it left - or
  finishes renaming, when it had got that far;
- **one run at a time**: `<backup_dir>/wekan/.backup.lock` (a line `<pid> <host>
  <program> <time>`) is taken by every run and by Omi's edits; a second run
  refuses. A lock whose process is gone on this host is taken over. A `.omi-save`
  journal of an Omi save that broke off is rolled back first, as Omi does it.

The `meta` table says `created` (the first run), `updated` (when the newest run
began), `runs`, `sources` (`gitea`, `github`), `source` (the newest run's),
`kept` (what is kept), `merged` (the dated backups combined into it) and
`deleted_old_only` (the last deletion of old data).

### Combining the dated backups of older versions

Older versions wrote a new directory each run,
`<backup_dir>/wekan/YYYY-MM-DD_HH-MM-SS/wekan.db` and `wekan.zip`, each with
everything - so ten runs took ten times the space. Every backup run, the backups
list (items 3, 4 and 5) and opening a backup combine them first; item 8 and
`--combine-backups` do it on their own, offline, at once:

1. every whole dated backup (a `.partial` one broke off and is never merged;
   item 8 and `--delete-partial` delete it) is merged, **oldest to newest**, into
   `wekan/wekan.db` and `wekan.zip`, with the one backup when there is one: the
   newest value wins for the same key and the newest copy for the same zip path,
   and **every row and file that only an older one has is kept** (as old data).
   Git bundles that differ are merged into one bundle with every ref of all of
   them;
2. the result is **verified**: every row key of every dated `.db` is in the
   combined one, every zip path of every dated `.zip` is in the combined zip at
   the newest copy, and the combined zip tests whole (`zipfile.testzip`);
3. only then are the dated directories deleted, and the space freed is shown.

A damaged dated backup (a zip entry whose CRC does not match, a `.db` that does
not read) stops it before anything is written, and nothing is deleted.
`--dry-run` reads and tests everything and says what would be freed.

### Migrate all Gitea to backups

Item 11 and `--migrate-all-gitea` move **all** of the local Gitea into backups
and free its disk as they go: **one repository at a time, smallest first**, so
what each deletion frees makes room for the next. For each repository:

1. the free space of `backup_dir`'s file system is compared with what its backup
   needs (git twice - the mirror and its bundle - plus its files, 10 % and
   100 MB more); one that does not fit now is skipped and named at the end;
2. it is backed up into its owner's one backup (the other repositories' rows stay
   as current as they were);
3. it is **verified** against Gitea: the counts of its issues, pull requests,
   comments, releases, labels, milestones and files equal Gitea's, every file's
   bytes match their CRC and SHA-256, and its git and wiki bundles pass
   `git bundle verify` with every branch, tag and pull request head at Gitea's
   commit;
4. only then is it deleted from Gitea, with its packages (packages are not in
   backups), and a line says the backup's growth, the space freed and the space
   free now.

After an owner's last repository, its own data (teams, members, labels, avatar)
is backed up and the organization or user is deleted - never the token's own
user, which is backed up and left. A repository whose verification fails is never
deleted; the run stops there (`--continue-on-error` goes on). Excluded
repositories (Sources excludes) stay, and so does their owner. Run it again and it
goes on: what is gone from Gitea is done, a repository backed up but not yet
deleted is updated, verified and deleted. It needs an admin token, asks to type
`migrate all gitea` and then the Gitea address, and warns when `backup_dir` is not
on the disk of Gitea's data (setting `bare_repo`): then deleting from Gitea frees
nothing for the backups.

**A 1 TB disk with 11 GB free**, all of it Gitea: point `backup_dir` to a
directory on the same disk (Settings, or item 6), run `--migrate-all-gitea
--dry-run` and read the order, the sizes and what would not fit, then run the
migration. The small repositories go first, each freeing what it took; the disk
turns from a Gitea disk into a backups disk. Then open the backups with Omi
(`BACKUPS_DIR` in Omi's `settings.txt`).

**Straight from GitHub** (`mirror/common/github_backup.py`) the backup has the
same tables and folders, each row with GitHub's own JSON, and `meta` has
`source = github`, `github_updated` (when the last run from GitHub began) and
`missing_files`. What is downloaded again:

- repositories, labels, milestones, collaborators, teams and members are listed
  again (topics are in GitHub's repository JSON);
- issues, pull requests and comments only when updated since the last run from
  GitHub began (GitHub's `since`), pull request details only for new and changed
  pull requests; everything else is kept as it is, and counts as still at GitHub;
- release files only when the backup lacks that file id with the same size and
  `updated_at` - the others are kept as they are, with no download; a release
  GitHub no longer has is kept, so is a repository - as old data;
- files that issue and comment bodies link to on GitHub
  (`github.com/user-attachments/...`, `github.com/<owner>/<repo>/files|assets/...`,
  `user-images.githubusercontent.com`) into `issues/` and `comments/`, with their
  SHA-256; a link that cannot be fetched is reported and tried again next time;
- git and the wiki: when GitHub's branches, tags and pull request heads are the
  old bundle's, the bundle is kept; otherwise a mirror is seeded from the old
  bundle and fetched from GitHub, so only the new objects come over the network.

A backup made from Gitea and one made from GitHub are the same backup: either
kind of run updates it. The GitHub token goes only to `api.github.com` and
`github.com` over https: a redirect to another host - GitHub sends files to
signed storage addresses - drops it, and other hosts never get it. Each
repository shows its new and changed counts, and the end a summary with the bytes
downloaded and kept and the zip's new entries.

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
python3 -I tests/githubToGiteaRunErrors.test.py
python3 -I tests/screenLog.test.py
python3 -I tests/giteaAttachments.test.py
python3 -I tests/giteaBackup.test.py
python3 -I tests/githubBackup.test.py
```

`tests/giteaBackup.test.py` runs combining, in-place updates, old data and the
migration against a fake Gitea always, and the whole round trip - back up, update
in place, old data, delete, restore, WeKan, migrate - against a disposable Gitea
given by `GITEA_TEST_URL`, `GITEA_TEST_TOKEN` (admin) and `GITEA_TEST_USER` (an
ordinary user). Never point it at a Gitea in use: it creates and deletes the
organizations `backuptestorg` and `migratetestorg`.

A fake Gitea, GitLab and SourceForge and local git repositories - no network.

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
```

A fake Gitea, GitLab and SourceForge and local git repositories - no network.

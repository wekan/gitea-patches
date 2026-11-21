#!/usr/bin/env python3
"""A migration that Gitea's settings break is not started again for hours.

A GitHub migration that broke off with "database is locked" (SQLITE_BUSY) - after
two and a half hours of godotengine/godot - or with "clone timed out" (a history
larger than [git.timeout] MIGRATE allows) breaks off the same way when it is
tried again: github-to-gitea stops that repository with the app.ini settings
that fix it, as it already does for git fsck, instead of a second attempt. Any
other failure is still tried once more. The leftover repository is removed in
every case, and the log no longer says that nothing was lost - except when
Gitea did not answer at all and the repository exists: Gitea goes on migrating
in the background then, and nothing is removed or posted again. A leftover found
at the start of a run is removed only when it is older than a day.

  python3 -I tests/githubToGiteaMigrateErrors.test.py
"""

import configparser
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py")
spec = importlib.util.spec_from_file_location("github_to_gitea", path)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


g.CFG = configparser.ConfigParser(interpolation=None)
g.GITHUB_REPO, g.OWNER, g.GITEA_URL = "o/big", "o", "http://gitea"
os.environ["GITEA_TOKEN"] = "t"
shown, posts, removed = [], [], []
g.log = lambda msg="": shown.append(str(msg))
g.gh = lambda p, params=None: {"size": 100, "private": False, "has_issues": True, "has_wiki": False}
g.github_token = lambda owner: ""
g.gt_total = lambda repo, kind: 0
g.repo_exists = lambda name: True
g.time.sleep = lambda s: None
g.gitea = lambda method, p, body=None, **kw: removed.append((method, p)) or (204, {})


def migrate_failing_with(message):
    shown.clear(); posts.clear(); removed.clear()
    g.http = lambda method, url, token, **kw: posts.append(url) or (500, {"message": message})
    try:
        g.migrate_into("big")
    except g.RepoError as e:
        return str(e)
    return None


err = migrate_failing_with("database is locked (5) (SQLITE_BUSY)")
check("SQLITE_BUSY: the migration is not started a second time", len(posts) == 1)
check("... the repository stops with WAL and a longer SQLITE_TIMEOUT for app.ini",
      err is not None and "SQLITE_JOURNAL_MODE = WAL" in err and "SQLITE_TIMEOUT" in err)
check("... and the nightly health check named as what it collided with",
      "[cron.repo_health_check]" in err)
check("... the leftover repository is removed", ("DELETE", "/repos/o/big") in removed)
check("... and the log does not claim that nothing was lost",
      not any("nothing is lost" in s for s in shown) and any("starts over" in s for s in shown))

err = migrate_failing_with("Migration failed: clone timed out, consider increasing [git.timeout] MIGRATE in app.ini")
check("clone timed out: not started a second time", len(posts) == 1)
check("... the repository stops with a longer [git.timeout] MIGRATE",
      err is not None and "[git.timeout]" in err and "MIGRATE = 86400" in err)

err = migrate_failing_with("some passing network error")
check("negative: any other failure is still tried once more", len(posts) == 2)
check("... and reported without app.ini advice that does not belong to it",
      err is not None and "app.ini" not in err and "some passing network error" in err)

# microsoft/AEC-Challenge: after 70 minutes of LFS files the connection closed with no
# answer ("Remote end closed connection without response", status 0). Gitea goes on
# with the migration when the client goes away, so the repository is not removed and
# the migration is not posted again into it.
exists = {"big": True}
g.repo_exists = lambda name: exists.get(name, False)
shown.clear(); posts.clear(); removed.clear()
g.http = lambda method, url, token, **kw: posts.append(url) or (0, "Remote end closed connection without response")
try:
    g.migrate_into("big"); err = None
except g.RepoError as e:
    err = str(e)
check("no answer, repository there: posted once, not a second time", len(posts) == 1)
check("... and not removed - Gitea is still migrating it", not any(m == "DELETE" for m, _ in removed))
check("... the repository stops saying Gitea goes on in the background",
      err is not None and "in the background" in err and "Do not stop Gitea" in err)
check("... and the log does not call it a failed migration", not any("Migration failed" in s for s in shown))
# Negative: no answer and no repository - Gitea was not reached (refused), nothing started:
# tried once more, as before.
exists = {}
shown.clear(); posts.clear(); removed.clear()
g.http = lambda method, url, token, **kw: posts.append(url) or (0, "[Errno 111] Connection refused")
try:
    g.migrate_into("big")
except g.RepoError as e:
    err = str(e)
check("negative: no answer and no repository is tried once more", len(posts) == 2 and "in the background" not in err)

# A leftover found at the start of a run is removed only when it is old: a young empty
# repository may be the migration Gitea is still running from the run before.
import datetime as _dt


def leftover_of_age(hours):
    removed.clear()
    stamp = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def api(method, p, body=None, **kw):
        removed.append((method, p))
        return (200, {"empty": True, "created_at": stamp}) if method == "GET" else (204, {})
    g.gitea = api
    try:
        g.clear_leftover("big")
        return None
    except g.RepoError as e:
        return str(e)


err = leftover_of_age(1)
check("an empty repository made 1 h ago is not removed", ("DELETE", "/repos/o/big") not in removed)
check("... the repository stops naming the migration that may still run",
      err is not None and "may still be migrating" in err and "60 min ago" in err)
err = leftover_of_age(30)
check("negative: one made 30 h ago is removed as a failed migration's leftover",
      err is None and ("DELETE", "/repos/o/big") in removed)

print(f"githubToGiteaMigrateErrors: {failures} failure(s)")
sys.exit(1 if failures else 0)

#!/usr/bin/env python3
"""github-to-gitea makes a PR whose commits GitHub's refs do not bring, and one into a
base branch GitHub deleted. Local repositories stand in for GitHub and for Gitea's bare
repository; a stand-in Gitea API answers from that bare repository. No network.

- storytold/figcraft #2396: GitHub's refs/pull/2396/head was a commit behind the PR;
- storytold/photocraft #16073: the PR was opened after the repository's git fetch;
- cloudflare/moq-rs #317: GitHub had refs/pull/317/merge and no refs/pull/317/head.
  In all three, with Gitea's bare repository on this machine, POST /branches answered
  "object does not exist". The commit is now fetched into the bare repository by name.
- storytold/craft-libs #1185: its base branch codex/vector-file-imports is gone on GitHub,
  and Gitea refused the PR: 'ref "codex/vector-file-imports" does not exist'. The base is
  made again at the PR's base commit.
- godotengine/godot #78432: "UNIQUE constraint failed: issue.repo_id, issue.index" names
  Gitea's issue counter, and the SQL it prints sets it right (run here on SQLite).

  python3 -I tests/githubToGiteaPrRefs.test.py
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse

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


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(msg, cwd):
    git("-c", "user.name=U", "-c", "user.email=u@example.org", "commit", "-q", "--allow-empty", "-m", msg, cwd=cwd)
    return git("rev-parse", "HEAD", cwd=cwd)


TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
work = os.path.join(TMP, "work")
git("init", "-q", "-b", "main", work)
commit("one", work)
# Gitea's bare repository: main only, as after the repository's git fetch.
bare = os.path.join(TMP, "gitea-repositories", "o", "r.git")
git("clone", "-q", "--bare", "--no-local", "--single-branch", "-b", "main", work, bare)

# GitHub, after that fetch:
git("checkout", "-q", "-b", "fork-branch", cwd=work)
old5 = commit("PR 5, first push", work)
new5 = commit("PR 5, second push", work)          # figcraft: refs/pull/5/head still says old5
git("checkout", "-q", "-b", "pr6", "main", cwd=work)
head6 = commit("PR 6, from a fork; only a merge ref", work)   # moq-rs
git("checkout", "-q", "-b", "stack-base", "main", cwd=work)
base7 = commit("the branch PR 7 is stacked on", work)
head7 = commit("PR 7", work)                       # craft-libs: stack-base is deleted
git("checkout", "-q", "main", cwd=work)
github = os.path.join(TMP, "github.git")
git("clone", "-q", "--bare", work, github)
git("config", "uploadpack.allowReachableSHA1InWant", "true", cwd=github)
git("update-ref", "refs/pull/5/head", old5, cwd=github)
git("update-ref", "refs/pull/6/merge", head6, cwd=github)
git("update-ref", "refs/pull/7/head", head7, cwd=github)
for b in ("fork-branch", "pr6", "stack-base"):
    git("update-ref", "-d", f"refs/heads/{b}", cwd=github)
# The commits are reachable only from the network's refs - here a hidden keep-ref.
for i, c in enumerate((new5, head6, base7, head7)):
    git("update-ref", f"refs/keep/{i}", c, cwd=github)

g.HERE, g.OWNER, g.NAME, g.GITHUB_REPO, g.DRY = TMP, "o", "r", "o/r", False
g.github_token = lambda owner: ""
g.github_git_url = lambda: github
g.find_bare = lambda path=None: bare
shown, calls, made = [], [], {}
g.log = lambda msg="": shown.append(str(msg))
g.label_ids = lambda labels: []
g.milestone_id = lambda ms: 0


def in_bare(*args):
    return subprocess.run(["git", "-C", bare, *args], capture_output=True, text=True)


def gitea_api(method, p, body=None, **kw):
    """Gitea's API, answering from its bare repository as Gitea does."""
    calls.append((method, p, body))
    if p.startswith("/branches/"):
        ref = "refs/heads/" + urllib.parse.unquote(p.split("/branches/", 1)[1])
        return (200 if in_bare("rev-parse", "--verify", "-q", ref).returncode == 0 else 404), None
    if p.startswith("/git/commits/"):
        sha = p.split("/")[3].split("?")[0]
        return (200 if in_bare("cat-file", "-e", sha + "^{commit}").returncode == 0 else 404), None
    if method == "POST" and p == "/branches":
        if in_bare("cat-file", "-e", body["old_ref_name"] + "^{commit}").returncode != 0:
            return 500, {"message": f"object does not exist [id: {body['old_ref_name']}, rel_path: ]"}
        in_bare("update-ref", "refs/heads/" + body["new_branch_name"], body["old_ref_name"])
        return 201, {}
    if method == "POST" and p == "/pulls":
        if in_bare("rev-parse", "--verify", "-q", "refs/heads/" + body["base"]).returncode != 0:
            return 404, {"message": f'ref "{body["base"]}" does not exist'}
        made[body["head"]] = body
        return 201, {"number": int(body["head"].rsplit("/", 1)[1])}
    return 500, {"message": "unexpected " + method + " " + p}


g.gt = gitea_api


def pr(num, head, base_ref="main", base_sha=None):
    return {"title": f"PR {num}", "body": "", "user": {"login": "u"}, "created_at": "2026-11-21T00:00:00Z",
            "html_url": f"https://github.com/o/r/pull/{num}", "head": {"sha": head},
            "base": {"ref": base_ref, "sha": base_sha or git("rev-parse", "main", cwd=work)},
            "labels": [], "milestone": None}


def create(num, data):
    g.gh = lambda path, params=None: data
    calls.clear()
    try:
        g.create_pull(num)
        return None
    except g.RepoError as e:
        return str(e)


err = create(5, pr(5, new5))
check("figcraft: a stale refs/pull/<N>/head - the PR is made", err is None and "github-pr/5" in made)
check("... from the PR's commit, fetched by name into the bare repository",
      git("rev-parse", "refs/heads/github-pr/5", cwd=bare) == new5)
check("... and the stale ref is named in the log", any("is " + old5[:10] + ", not " + new5[:10] in s for s in shown))

err = create(6, pr(6, head6))
check("moq-rs: no refs/pull/<N>/head at all - the PR is made at its commit",
      err is None and git("rev-parse", "refs/heads/github-pr/6", cwd=bare) == head6)
check("negative: no push to Gitea when its bare repository is here",
      not os.path.exists(os.path.join(TMP, "tmp", "github-to-gitea-git")))

err = create(7, pr(7, head7, "stack-base", base7))
check("craft-libs: a base branch GitHub deleted - the PR is made into it",
      err is None and made.get("github-pr/7", {}).get("base") == "stack-base")
check("... the base is made in Gitea at the PR's base commit",
      git("rev-parse", "refs/heads/stack-base", cwd=bare) == base7)
err = create(5, pr(5, new5))
check("negative: a base that exists is not made again",
      not any(c[:2] == ("POST", "/branches") and c[2]["new_branch_name"] == "main" for c in calls))

err = create(8, pr(8, "2" * 40))
check("negative: a commit GitHub does not have stops the PR, not the run, and says why",
      err is not None and "head commit 2222222222 is missing" in err and "github-pr/8" not in made)
check("... and the message no longer claims a push was tried when the bare repository is here",
      "pushing it from GitHub did not work" not in err and "bringing it from GitHub did not work" in err)

# godotengine/godot #78432: Gitea's counter is behind its issues.
g.gt = lambda method, p, body=None, **kw: (200, {"id": 7}) if p == "" else (500, {})
hint = g.index_hint(500, {"message": "newIssue: constraint failed: UNIQUE constraint failed: issue.repo_id, issue.index (2067)"})
check("godot: the UNIQUE issue.index error names Gitea's issue counter",
      "issue counter" in hint and "repo_id = 7" in hint and "group_id = 7" in hint)
check("negative: another error gets no counter hint", g.index_hint(500, {"message": "other"}) == "")
sql = hint.split('sqlite3 gitea.db "', 1)[1].rsplit('"', 1)[0].replace('\\"', '"')
db = sqlite3.connect(":memory:")
db.executescript('CREATE TABLE issue (repo_id INTEGER, "index" INTEGER);'
                 'CREATE TABLE issue_index (group_id INTEGER PRIMARY KEY, max_index INTEGER);'
                 'INSERT INTO issue VALUES (7, 1), (7, 42535), (7, 78431), (8, 900);'
                 'INSERT INTO issue_index VALUES (7, 42535), (8, 900);')
db.executescript(sql)
check("... and its SQL sets the counter to the highest number, for that repository only",
      dict(db.execute("SELECT group_id, max_index FROM issue_index")) == {7: 78431, 8: 900})

print()
print(f"githubToGiteaPrRefs: {failures} failure(s)")
sys.exit(1 if failures else 0)

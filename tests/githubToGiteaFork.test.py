#!/usr/bin/env python3
"""github-to-gitea and GitHub forks: a fork with no commits of its own, behind its
upstream, is first synced on GitHub with `gh repo sync` (never with --force, never when
it has commits of its own), and a fork with no releases gets its upstream's newest
release - text, files and tag - in Gitea. No network: GitHub's API and gh are stubbed,
local repositories stand in for GitHub and Gitea.

  python3 -I tests/githubToGiteaFork.test.py
"""

import configparser
import importlib.util
import os
import subprocess
import sys
import tempfile

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


TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
os.environ["GITEA_TOKEN"], os.environ["GITHUB_TOKEN"] = "gitea-secret", "github-secret"
os.environ["MIRROR_TMP_DIR"] = os.path.join(TMP, "tmp")
g.CFG = configparser.ConfigParser(interpolation=None)
g.DRY = False
shown = []
g.log = lambda msg="": shown.append(str(msg))
g.select_repo("xet7/rumble")

# --- gh repo sync ---------------------------------------------------------------
answers = {}


def fake_gh(p, params=None):
    if p in answers:
        a = answers[p]
        if isinstance(a, Exception):
            raise a
        return a
    raise LookupError(f"GitHub {p}: 404")


g.gh = fake_gh
ran = []


class Done:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


real_run = subprocess.run


def fake_run(cmd, *a, **kw):
    if cmd and str(cmd[0]).endswith("gh"):
        ran.append(cmd)
        return Done()
    return real_run(cmd, *a, **kw)


g.subprocess.run = fake_run
g.shutil.which = lambda name: "/usr/bin/gh" if name == "gh" else None
CMP = "repos/upstream/rumble/compare/master...xet7:master"


def fork(status, ahead, behind, is_fork=True):
    g._info.clear()
    answers.clear()
    answers["repos/xet7/rumble"] = {"fork": is_fork, "default_branch": "master",
                                    "parent": {"full_name": "upstream/rumble"} if is_fork else None}
    answers[CMP] = {"status": status, "ahead_by": ahead, "behind_by": behind}
    ran.clear()
    g.sync_fork_on_github()


fork("behind", 0, 7)
check("a fork behind its upstream with no commits of its own is synced with gh repo sync",
      ran == [["/usr/bin/gh", "repo", "sync", "xet7/rumble", "--source", "upstream/rumble", "--branch", "master"]])
check("negative: never with --force", not any("--force" in c for c in ran))
fork("diverged", 2, 7)
check("negative: a fork with commits of its own (diverged) is not synced", ran == [])
fork("ahead", 3, 0)
check("negative: a fork with commits of its own (ahead) is not synced", ran == [])
fork("identical", 0, 0)
check("negative: a fork already up to date is not synced", ran == [])
fork("behind", 0, 7, is_fork=False)
check("negative: a repository that is not a fork is not synced", ran == [])
g.CFG.read_dict({"options": {"sync_forks": "no"}})
fork("behind", 0, 7)
check("negative: sync_forks = no turns it off", ran == [])
g.CFG.read_dict({"options": {"sync_forks": "yes"}})
g.shutil.which = lambda name: None
fork("behind", 0, 7)
check("negative: without the gh command nothing is run, and it is said",
      ran == [] and any("not installed" in m for m in shown))
g.shutil.which = lambda name: "/usr/bin/gh"
g.DRY = True
fork("behind", 0, 7)
check("negative: a dry run runs no gh", ran == [])
g.DRY = False
g.subprocess.run = real_run

# --- upstream's newest release ----------------------------------------------------
# "upstream": a repository with tags v1 and v2; "Gitea": the fork, from before v2.
work = os.path.join(TMP, "work")
git("init", "-q", "-b", "master", work)
ID = ["-c", "user.name=U", "-c", "user.email=u@example.org"]
git(*ID, "commit", "-q", "--allow-empty", "-m", "one", cwd=work)
git("tag", "v1", cwd=work)
gitea_bare = os.path.join(TMP, "gitea-repositories", "xet7", "rumble.git")
git("clone", "-q", "--bare", work, gitea_bare)
git(*ID, "commit", "-q", "--allow-empty", "-m", "two", cwd=work)
git("tag", "v2", cwd=work)
v2 = git("rev-parse", "v2", cwd=work)
upstream = os.path.join(TMP, "upstream.git")
git("clone", "-q", "--bare", work, upstream)
g.find_bare = lambda path=None: gitea_bare
g.github_token = lambda owner: None

# git fetch from "https://github.com/upstream/rumble.git" goes to the local upstream.
os.environ["GIT_CONFIG_COUNT"] = "1"
os.environ["GIT_CONFIG_KEY_0"] = f"url.{upstream}.insteadOf"
os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/upstream/rumble.git"

answers.clear()
g._info.clear()
answers["repos/xet7/rumble"] = {"fork": True, "default_branch": "master", "parent": {"full_name": "upstream/rumble"}}
answers["repos/upstream/rumble/releases"] = [
    {"tag_name": "v3-draft", "draft": True, "prerelease": False, "assets": []},
    {"tag_name": "v2", "name": "Version 2", "body": "What is new in 2", "draft": False, "prerelease": False,
     "assets": [{"name": "rumble.apk", "url": "https://api.github.com/repos/upstream/rumble/releases/assets/9", "size": 5}]},
    {"tag_name": "v1", "name": "Version 1", "body": "", "draft": False, "prerelease": False, "assets": []}]
answers["repos/upstream/rumble/git/ref/tags/v2"] = {"object": {"type": "commit", "sha": v2}}
g.gh_all = lambda p, params=None: iter([]) if p == "repos/xet7/rumble/releases" else iter(answers[p])

gitea_releases = {"v1": {"id": 1, "tag_name": "v1", "name": "Version 1", "body": "", "draft": False,
                         "prerelease": False, "assets": [{"id": 11, "name": "old.apk"}]}}
api = []


def fake_gt(method, p, body=None, **kw):
    api.append((method, p, body))
    if method == "GET" and p.startswith("/tags/"):
        tag = p.rsplit("/", 1)[1]
        r = subprocess.run(["git", "-C", gitea_bare, "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], capture_output=True)
        return (200 if r.returncode == 0 else 404), {}
    if method == "POST" and p == "/tags":
        r = subprocess.run(["git", "-C", gitea_bare, "cat-file", "-e", body["target"]], capture_output=True)
        if r.returncode:
            return 422, {"message": "commit missing"}
        git("tag", body["tag_name"], body["target"], cwd=gitea_bare)
        return 201, {}
    if method == "POST" and p == "/releases":
        return 201, dict(body, id=2, assets=[])
    return 200, {}


g.gt = fake_gt
g.gt_all = lambda p, repo=None: iter(gitea_releases.values()) if p == "/releases" else iter([])
uploads, deleted = [], []
g.upload_asset = lambda cur, tag, name, path, url, size=0: uploads.append((tag, name, url))
g.delete_assets = lambda cur, tag: deleted.append(tag)
g.sync_releases()
made = [b for m, p, b in api if m == "POST" and p == "/releases"]
check("a fork with no releases gets its upstream's newest release, with its text",
      made == [{"tag_name": "v2", "name": "Version 2", "body": "What is new in 2", "draft": False, "prerelease": False}])
check("upstream's tag and its commit are fetched into Gitea, then the tag made there",
      git("rev-parse", "refs/tags/v2", cwd=gitea_bare) == v2)
check("the release's files come from upstream",
      uploads == [("v2", "rumble.apk", "https://api.github.com/repos/upstream/rumble/releases/assets/9")])
check("negative: the draft and the older upstream releases are not copied", len(made) == 1)
check("negative: the earlier upstream release keeps its text but its files are deleted", deleted == ["v1"])

# A fork WITH releases of its own keeps only its own.
api.clear()
g.gh_all = lambda p, params=None: iter([{"tag_name": "own", "name": "own", "body": "", "draft": False,
                                         "prerelease": False, "assets": []}]) if p == "repos/xet7/rumble/releases" else iter(answers[p])
gitea_releases.clear()
gitea_releases["own"] = {"id": 5, "tag_name": "own", "name": "own", "body": "", "draft": False, "prerelease": False, "assets": []}
g.sync_releases()
check("negative: a fork with releases of its own gets nothing from upstream",
      not any(p == "/releases" and m == "POST" for m, p, b in api))
check("negative: no token in what is shown", not any("secret" in m for m in shown))

print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)

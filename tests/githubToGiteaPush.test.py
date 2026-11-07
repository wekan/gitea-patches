#!/usr/bin/env python3
"""github-to-gitea without Gitea's bare repository on this machine: a new pull request's
head commit is fetched from GitHub and pushed to Gitea as its branch github-pr/<N> - and
only that. Local repositories stand in for GitHub and Gitea; no network.

  python3 -I tests/githubToGiteaPush.test.py
"""

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
# "GitHub": a repository with main and a pull request head under refs/pull/8/head.
work = os.path.join(TMP, "work")
git("init", "-q", "-b", "main", work)
git("-c", "user.name=U", "-c", "user.email=u@example.org", "commit", "-q", "--allow-empty", "-m", "one", cwd=work)
git("checkout", "-q", "-b", "feature", cwd=work)
git("-c", "user.name=U", "-c", "user.email=u@example.org", "commit", "-q", "--allow-empty", "-m", "the PR", cwd=work)
head = git("rev-parse", "HEAD", cwd=work)
github = os.path.join(TMP, "github.git")
git("clone", "-q", "--bare", work, github)
git("update-ref", "refs/pull/8/head", head, cwd=github)
git("update-ref", "-d", "refs/heads/feature", cwd=github)
# "Gitea": the same repository before the PR, without its commit.
gitea = os.path.join(TMP, "gitea.git")
git("clone", "-q", "--bare", "--no-local", "--single-branch", "-b", "main", work, gitea)

g.HERE, g.OWNER, g.NAME, g.GITHUB_REPO, g.DRY = TMP, "wekan", "wekan", "wekan/wekan", False
g.log = lambda msg="": None
os.environ["GITEA_TOKEN"], os.environ["GITHUB_TOKEN"] = "gitea-secret", "github-secret"
g.github_git_url = lambda: github
g.gitea_git_url = lambda: gitea


def branch_in_gitea(method, p, body=None, **kw):
    ref = "refs/heads/" + p.split("/branches/", 1)[1].replace("%2F", "/")
    r = subprocess.run(["git", "-C", gitea, "rev-parse", "--verify", "-q", ref], capture_output=True)
    return (200 if r.returncode == 0 else 404), None


g.gt = branch_in_gitea
argv = []
real_run = subprocess.run


def recording_run(cmd, *a, **kw):
    argv.append(list(cmd))
    return real_run(cmd, *a, **kw)


g.subprocess.run = recording_run

check("the PR's head commit is not in Gitea before",
      subprocess.run(["git", "-C", gitea, "cat-file", "-e", head], capture_output=True).returncode != 0)
check("push_pr_head reports the branch there", g.push_pr_head(8, head, "github-pr/8") is True)
check("Gitea has the commit as branch github-pr/8", git("rev-parse", "refs/heads/github-pr/8", cwd=gitea) == head)
refs = git("for-each-ref", "--format=%(refname)", cwd=gitea).split()
check("negative: nothing else was pushed - only main and github-pr/8", sorted(refs) == ["refs/heads/github-pr/8", "refs/heads/main"])
check("negative: no token on any git command line", not any("secret" in " ".join(c) for c in argv))
check("the cache is under the script's tmp/", os.path.isdir(os.path.join(TMP, "tmp", "github-to-gitea-git", "wekan", "wekan.git")))

# Negative: GitHub's refs/pull/<N>/head is another commit than the API said - not pushed.
check("negative: a head that is not the API's commit is not pushed",
      g.push_pr_head(8, "0" * 40, "github-pr/9") is False and
      subprocess.run(["git", "-C", gitea, "rev-parse", "--verify", "-q", "refs/heads/github-pr/9"], capture_output=True).returncode != 0)
# Negative: a PR GitHub does not have fails, it does not crash the run.
check("negative: a missing PR ref returns False", g.push_pr_head(99, head, "github-pr/99") is False)

print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)

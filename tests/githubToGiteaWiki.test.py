#!/usr/bin/env python3
"""github-to-gitea's git step copies every wiki GitHub has into Gitea's <name>.wiki.git:
into the one on this machine, through Gitea's own initialization when Gitea has no wiki
yet, and by a push when Gitea is on another machine. Local repositories stand in for
GitHub and Gitea; no network.

  python3 -I tests/githubToGiteaWiki.test.py
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


ID = ["-c", "user.name=U", "-c", "user.email=u@example.org"]


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def rev(repo, ref):
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", ref], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def gitea_wiki(where, page="Gitea's own page"):
    """A wiki as Gitea's InitWiki and a first page make it: default branch main."""
    work = where + "-work"
    git("init", "-q", "-b", "main", work)
    open(os.path.join(work, "Home.md"), "w").write(page + "\n")
    git("add", "Home.md", cwd=work)
    git(*ID, "commit", "-q", "-m", "page", cwd=work)
    git("clone", "-q", "--bare", work, where)


TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
# "GitHub": a wiki on master, with a second branch.
work = os.path.join(TMP, "ghwiki-work")
git("init", "-q", "-b", "master", work)
open(os.path.join(work, "Home.md"), "w").write("GitHub's wiki\n")
git("add", "Home.md", cwd=work)
git(*ID, "commit", "-q", "-m", "wiki", cwd=work)
git("branch", "draft", cwd=work)
github_wiki = os.path.join(TMP, "github.wiki.git")
git("clone", "-q", "--bare", work, github_wiki)
master = rev(github_wiki, "refs/heads/master")

g.OWNER, g.NAME, g.GITHUB_REPO, g.DRY = "xet7", "neonnoir", "xet7/NEONnoir", False
shown = []
g.log = lambda msg="": shown.append(str(msg))
os.environ["GITEA_TOKEN"], os.environ["GITHUB_TOKEN"] = "gitea-secret", "github-secret"
os.environ["MIRROR_TMP_DIR"] = os.path.join(TMP, "tmp")
g.github_wiki_url = lambda: github_wiki
calls = []


has_wiki = {"on": True}


def api(method, p, body=None, **kw):
    if method == "GET":
        return 200, {"has_wiki": has_wiki["on"]}
    calls.append((method, p, body))
    return 200, {}


g.gitea = lambda method, p, body=None, repo=None, **kw: api(method, p, body)
g.gt = lambda method, p, body=None, **kw: api(method, p, body)

check("GitHub's default wiki branch and its branches are read",
      g.remote_heads(github_wiki, None) == ("master", {"master": master, "draft": master}))
check("negative: a wiki that does not exist is (None, {})",
      g.remote_heads(os.path.join(TMP, "none.wiki.git"), None) == (None, {}))
check("master goes to Gitea's main, the other branches by their names",
      g.wiki_refspecs("master", "main", {"master": 1, "draft": 1}) ==
      ["+refs/heads/master:refs/heads/main", "+refs/heads/draft:refs/heads/draft"])

# 1. Gitea's wiki is on this machine, beside the bare repository.
repos = os.path.join(TMP, "gitea-repositories", "xet7")
os.makedirs(repos)
bare = os.path.join(repos, "neonnoir.git")
git("init", "-q", "--bare", bare)
wiki = os.path.join(repos, "neonnoir.wiki.git")
gitea_wiki(wiki)
g.fetch_wiki(bare)
check("the wiki on this machine gets GitHub's wiki on its default branch",
      rev(wiki, "refs/heads/main") == master and rev(wiki, "refs/heads/draft") == master)
check("negative: no second copy as master beside main", rev(wiki, "refs/heads/master") is None)
check("negative: nothing is created through the API when the wiki exists", calls == [])

# 1b. The wiki is on the disk but turned off in Gitea: turned on, so the Wiki tab shows it.
has_wiki["on"] = False
g.fetch_wiki(bare)
check("a wiki turned off in Gitea is turned on", ("PATCH", "", {"has_wiki": True}) in calls)
has_wiki["on"] = True
calls.clear()

# 2. GitHub has no wiki: nothing is done at all.
g.github_wiki_url = lambda: os.path.join(TMP, "none.wiki.git")
g.fetch_wiki(os.path.join(repos, "other.git"))
check("negative: no wiki at GitHub, no call to Gitea", calls == [])
g.github_wiki_url = lambda: github_wiki

# 3. Gitea has no wiki yet: Gitea initializes it (with its hooks), then it is fetched.
bare2 = os.path.join(repos, "pet.git")
git("init", "-q", "--bare", bare2)
wiki2 = os.path.join(repos, "pet.wiki.git")
g.gitea_git_url = lambda: os.path.join(TMP, "nowhere", "pet.git")


def init():
    calls.append(("init", "", None))
    gitea_wiki(wiki2, "Copied from GitHub.")


g.init_gitea_wiki = init
g.fetch_wiki(bare2)
check("a wiki Gitea does not have is initialized by Gitea", ("init", "", None) in calls)
check("and then gets GitHub's wiki", rev(wiki2, "refs/heads/main") == master)
calls.clear()
g.fetch_wiki(bare2)
check("negative: the next run does not initialize it again", calls == [])

# 4. Gitea is on another machine: pushed over git to <name>.wiki.git.
remote = os.path.join(TMP, "remote", "neonnoir.wiki.git")
os.makedirs(os.path.dirname(remote))
gitea_wiki(remote)
g.gitea_git_url = lambda: os.path.join(TMP, "remote", "neonnoir.git")
g.fetch_wiki(None)
check("without the bare repository here, the wiki is pushed to Gitea's main",
      rev(remote, "refs/heads/main") == master and rev(remote, "refs/heads/draft") == master)
check("negative: the copy is under MIRROR_TMP_DIR, not /tmp",
      os.path.exists(os.path.join(TMP, "tmp", "wiki", "xet7", "neonnoir.wiki.git", "HEAD")))
check("negative: no token is written into a URL or shown", not any("secret" in m for m in shown))

# 5. --dry-run changes nothing.
g.DRY = True
wiki3 = os.path.join(repos, "dry.wiki.git")
gitea_wiki(wiki3)
before = rev(wiki3, "refs/heads/main")
bare3 = os.path.join(repos, "dry.git")
git("init", "-q", "--bare", bare3)
g.fetch_wiki(bare3)
check("negative: a dry run fetches nothing", rev(wiki3, "refs/heads/main") == before)

print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)

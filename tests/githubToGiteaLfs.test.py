#!/usr/bin/env python3
"""A migration that downloads Git LFS files says so, instead of "issues 0".

Gitea downloads every Git LFS file of a repository before it reads a single
issue, so microsoft/AEC-Challenge - its .wav datasets are in LFS - showed
"issues 0, PRs 0" every five minutes for hours, as if the migration were stuck.
github-to-gitea reads the repository's .gitattributes first: when it has LFS
patterns and the setting lfs is on, it says what is in LFS and how to leave it
out, and while nothing has arrived yet each progress line says it is still
cloning. A repository without LFS, or a run with lfs = no, says nothing of it.

  python3 -I tests/githubToGiteaLfs.test.py
"""

import base64
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
g.GITHUB_REPO, g.OWNER, g.GITEA_URL = "microsoft/AEC-Challenge", "microsoft", "http://gitea"
os.environ["GITEA_TOKEN"] = "t"
shown = []
g.log = lambda msg="": shown.append(str(msg))
g.github_token = lambda owner: ""
g.gt_total = lambda repo, kind: 0
INFO = {"size": 173024, "private": False, "has_issues": True, "has_wiki": False, "default_branch": "main"}
attributes = {"text": "*.wav filter=lfs diff=lfs merge=lfs -text\n*.md text\n"}


def gh(p, params=None):
    if p.endswith("/contents/.gitattributes"):
        if attributes["text"] is None:
            raise LookupError("404")
        return {"content": base64.b64encode(attributes["text"].encode()).decode()}
    return INFO


g.gh = gh


class SlowThread:
    """A migration that is still running at the first five-minute check."""

    def __init__(self, target, daemon=True):
        self.target, self.joins = target, 0

    def start(self):
        pass

    def join(self, timeout=None):
        self.joins += 1
        if self.joins == 2:
            self.target()

    def is_alive(self):
        return self.joins < 2


g.threading.Thread = SlowThread
g.http = lambda method, url, token, **kw: (201, {})


def migrate(lfs="yes", text="*.wav filter=lfs diff=lfs merge=lfs -text\n"):
    shown.clear()
    attributes["text"] = text
    if not g.CFG.has_section("options"):
        g.CFG.add_section("options")
    g.CFG.set("options", "lfs", lfs)
    g.migrate_into("AEC-Challenge")
    return "\n".join(shown)


out = migrate()
check("the LFS patterns of .gitattributes are named", "Uses Git LFS (*.wav)" in out)
check("... with how to leave the LFS files out", "set lfs = no" in out)
check("... and a progress line with nothing yet says it is still cloning the LFS files",
      "issues 0, PRs 0 - still cloning the git history and the LFS files" in out)
check("uses_lfs lists only the patterns that are LFS", g.uses_lfs(INFO) == "*.wav")

out = migrate(text="*.md text\n")
check("negative: a repository without LFS patterns says nothing of LFS", "LFS" not in out)
check("... and still says it is cloning while nothing has arrived",
      "issues 0, PRs 0 - still cloning the git history, 0 min so far" in out)

out = migrate(lfs="no")
check("negative: with lfs = no nothing is said of LFS", "LFS" not in out)

out = migrate(text=None)
check("negative: no .gitattributes is no LFS, not an error", "LFS" not in out and "Migration done" in out)

g.gt_total = lambda repo, kind: 5
out = migrate()
check("negative: once issues arrive, the line is the plain count", "issues 5, PRs 5\n" in out + "\n"
      and "still cloning" not in out)

print(f"githubToGiteaLfs: {failures} failure(s)")
sys.exit(1 if failures else 0)

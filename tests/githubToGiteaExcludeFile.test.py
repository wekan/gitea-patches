#!/usr/bin/env python3
"""github-to-gitea's exclude file: github-to-gitea.exclude.txt beside the .ini, one
owner/name a line with '#' comments, read with gitea_attachments.parse_exclude_file
(the rule Omi's "Do not sync this repository any more" uses for the same file) and
added to [sources] exclude by excludes() - so a repository Omi excludes is left out of
what an organization or user expands to, in the sync and in backups. The .ini is
never read or written here.

  python3 -I tests/githubToGiteaExcludeFile.test.py
"""

import configparser
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mirror", "common"))
import gitea_attachments as ga  # noqa: E402

path = os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py")
spec = importlib.util.spec_from_file_location("github_to_gitea", path)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))

# --- the parse rule -------------------------------------------------------------------
text = ("# Not synced any more\n"
        "microsoft/AEC-Challenge\n"
        "\n"
        "   microsoft/AIFluency   # 199 GB of LFS\n"
        "wekan/a, wekan/b\n"
        "#wekan/commented-out\n")
check("comments, blank lines, spaces and commas", ga.parse_exclude_file(text) ==
      ["microsoft/AEC-Challenge", "microsoft/AIFluency", "wekan/a", "wekan/b"])
check("an empty or missing text is nothing", ga.parse_exclude_file("") == [] and ga.parse_exclude_file(None) == [])
check("without comments it reads as [sources] exclude does",
      ga.parse_exclude_file("a/b, c/d\ne/f") == ga.parse_excludes("a/b, c/d\ne/f"))

# --- excludes(): [sources] exclude and the file ----------------------------------------
g.EXCLUDE_FILE = os.path.join(TMP, "github-to-gitea.exclude.txt")
check("the file is beside the .ini", os.path.dirname(spec.origin) == os.path.dirname(g.INI_FILE)
      and g.file_excludes(os.path.join(os.path.dirname(g.INI_FILE), "github-to-gitea.exclude.txt")) is not None)
g.CFG = configparser.ConfigParser(interpolation=None)
g.CFG.read_dict({"sources": {"exclude": "\nmicrosoft/verybig\nMicrosoft/AIFluency"}})
check("no file: only [sources] exclude", g.excludes() == ["microsoft/verybig", "Microsoft/AIFluency"])
with open(g.EXCLUDE_FILE, "w") as f:
    f.write(text)
ex = g.excludes()
check("the file's entries are added, once in any case",
      ex == ["microsoft/verybig", "Microsoft/AIFluency", "microsoft/AEC-Challenge", "wekan/a", "wekan/b"])
kept, gone = ga.apply_excludes("microsoft", ["microsoft/AEC-Challenge", "microsoft/vscode", "microsoft/aifluency"], ex)
check("an organization's repositories in the file are left out", kept == ["microsoft/vscode"]
      and gone == ["microsoft/AEC-Challenge", "microsoft/aifluency"])
kept, gone = ga.apply_excludes("microsoft/AEC-Challenge", ["microsoft/AEC-Challenge"], ex)
check("a source naming the repository itself still gets it", kept == ["microsoft/AEC-Challenge"])
check("excludes() does not write the file into [sources] exclude",
      g.CFG.get("sources", "exclude") == "\nmicrosoft/verybig\nMicrosoft/AIFluency")

# --- negative: what is not a line of the file ------------------------------------------
check("a comment-only file excludes nothing", ga.parse_exclude_file("# a/b\n   # c/d\n") == [])
src = open(path, encoding="utf-8").read()
check("excludes() reads the file through parse_exclude_file", "parse_exclude_file" in src and "EXCLUDE_FILE" in src)
ignore = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
check("the exclude file is this machine's, never committed", "github-to-gitea.exclude.txt" in ignore)

print(f"{'FAIL' if failures else 'ok'}: {failures} failures")
sys.exit(1 if failures else 0)

#!/usr/bin/env python3
"""Every mirror script writes what a run shows on the screen - its own lines and the
output of programs it runs - to <WeKan>/.tools/log/mirror/<script>/YYYY-MM-DD_HH-MM-SS.txt,
WeKan being at its fixed place on each operating system.

  python3 -I tests/screenLog.test.py
"""

import glob
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


SCRIPTS = {
    "github-to-gitea": os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py"),
    "github-to-gitea-gh": os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea-gh.py"),
    "gitea-to-gitlab (common)": os.path.join(ROOT, "mirror", "common", "gitea_mirror.py"),
}
RUN = '''
import importlib.util, subprocess, sys
spec = importlib.util.spec_from_file_location("m", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, sys.argv[1].rsplit("/", 2)[0] + "/common")
spec.loader.exec_module(m)
m.start_screen_log("unused", "probe")
print("first from python")
subprocess.run(["sh", "-c", "echo child-out; echo child-err >&2"])
print("last, no newline", end="")
'''
NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?\.txt$")

for label, script in SCRIPTS.items():
    d = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
    env = dict(os.environ, MIRROR_LOG_DIR=d)
    for _ in range(2):
        # stderr where stdout goes, as on a terminal
        subprocess.run([sys.executable, "-I", "-c", RUN, script, d], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, check=True, env=env)
    logs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "probe", "*.txt")))
    check(f"{label}: the log is <log>/mirror/<script>/YYYY-MM-DD_HH-MM-SS.txt",
          len(logs) >= 1 and all(NAME.match(n) for n in logs))
    check(f"{label}: negative: a second run never overwrites the first", len(logs) == 2)
    text = open(os.path.join(d, "probe", logs[0]), encoding="utf-8").read()
    lines = text.splitlines()
    check(f"{label}: Python's lines, a child's stdout and stderr, in the order shown",
          lines[1:] == ["first from python", "child-out", "child-err", "last, no newline"])
    # stdout and stderr to different places: each still reaches its own place, and the
    # log has every line (their order across the two is not defined then).
    d2 = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
    r = subprocess.run([sys.executable, "-I", "-c", RUN, script, d2], capture_output=True, check=True,
                       env=dict(os.environ, MIRROR_LOG_DIR=d2), text=True)
    text2 = open(glob.glob(os.path.join(d2, "probe", "*.txt"))[0], encoding="utf-8").read()
    check(f"{label}: stdout and stderr apart: each to its own place, all in the log",
          "child-out" in r.stdout and "child-err" in r.stderr and "child-err" not in r.stdout
          and all(x in text2 for x in ("first from python", "child-out", "child-err", "last, no newline")))

# WeKan's place on each operating system.
import importlib.util
spec = importlib.util.spec_from_file_location("g", SCRIPTS["github-to-gitea"])
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
home = os.path.expanduser("~")
saved_platform, saved_env = sys.platform, os.environ.pop("MIRROR_LOG_DIR", None)
os.environ["USERPROFILE"] = os.path.join("C:", "Users", "lauri")
for plat, want in [("linux", os.path.join(home, "repos", "wekan", ".tools", "log", "mirror")),
                   ("darwin", os.path.join(home, "Documents", "repos", "wekan", ".tools", "log", "mirror")),
                   ("win32", os.path.join("C:", "Users", "lauri", "Downloads", "repos", "wekan", ".tools", "log", "mirror"))]:
    g.sys.platform = plat
    check(f"{plat}: WeKan's .tools/log/mirror", g.mirror_log_dir() == want)
g.sys.platform = saved_platform
if saved_env is not None:
    os.environ["MIRROR_LOG_DIR"] = saved_env

ignored = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "mirror/github-to-gitea/github-to-gitea_2026-11-08_02-15-30-log.txt"])
check("git ignores the log files", ignored.returncode == 0)
tracked = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "mirror/github-to-gitea/github-to-gitea.py"])
check("negative: the scripts themselves are not ignored", tracked.returncode != 0)

print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)

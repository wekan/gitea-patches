#!/usr/bin/env python3
"""Every mirror script writes what a run shows on the screen - its own lines and the
output of programs it runs - to <script>_YYYY-MM-DD_HH-MM-SS-log.txt beside itself,
and git ignores those files.

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
m.start_screen_log(sys.argv[2], "probe")
print("first from python")
subprocess.run(["sh", "-c", "echo child-out; echo child-err >&2"])
print("last, no newline", end="")
'''
NAME = re.compile(r"^probe_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?-log\.txt$")

for label, script in SCRIPTS.items():
    d = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
    for _ in range(2):
        subprocess.run([sys.executable, "-I", "-c", RUN, script, d], capture_output=True, check=True)
    logs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*-log.txt")))
    check(f"{label}: the log is named <script>_YYYY-MM-DD_HH-MM-SS-log.txt beside it",
          len(logs) >= 1 and all(NAME.match(n) for n in logs))
    check(f"{label}: negative: a second run never overwrites the first", len(logs) == 2)
    text = open(os.path.join(d, logs[0]), encoding="utf-8").read()
    lines = text.splitlines()
    check(f"{label}: Python's lines, a child's stdout and stderr, in the order shown",
          lines[1:] == ["first from python", "child-out", "child-err", "last, no newline"])

ignored = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "mirror/github-to-gitea/github-to-gitea_2026-11-08_02-15-30-log.txt"])
check("git ignores the log files", ignored.returncode == 0)
tracked = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "mirror/github-to-gitea/github-to-gitea.py"])
check("negative: the scripts themselves are not ignored", tracked.returncode != 0)

print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)

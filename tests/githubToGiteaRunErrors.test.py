#!/usr/bin/env python3
"""github-to-gitea keeps going: a workflow file that is not UTF-8 is read (an old
editor's Windows-1252 en dash, 0x96, stopped a run of 900 repositories at
QB64pe), and an unexpected fault in one repository is reported with its traceback
and counted as failed, while the run goes on to the next. Ctrl-C still stops it.

  python3 -I tests/githubToGiteaRunErrors.test.py
"""

import configparser
import importlib.util
import os
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


check("UTF-8 is read as UTF-8", g.decode_text("a–b ä".encode()) == "a–b ä")
check("Windows-1252's en dash 0x96 is read, not an exception", g.decode_text(b"name: Build \x96 test") == "name: Build – test")
check("negative: a byte neither has is U+FFFD, never an exception", g.decode_text(b"a\x81b") == "a�b")

TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
g.CFG = configparser.ConfigParser(interpolation=None)
shown = []
g.log = lambda msg="": shown.append(str(msg))
g.apply_config = lambda: None
g.load_state = lambda: None
g.save_state = lambda: None
g.STORE.clear()
g.expand_source = lambda src: ["o/one", "o/broken", "o/three"]
ran = []


def run_repo(full, steps):
    ran.append(full)
    if full == "o/broken":
        b"\x96".decode()  # the fault that stopped the whole run


g.run_repo = run_repo
ok = g.run(["actions"], "o")
check("the run goes on to the repository after the one that failed", ran == ["o/one", "o/broken", "o/three"])
check("... and reports that it failed", ok is False and any("o/broken: UnicodeDecodeError" in s for s in shown))
check("... with its traceback in the log", any("Traceback" in s for s in shown))
check("... and the failed one is tried again next run", "o/broken" not in g.STORE["_run"]["done"])

ran.clear()
g.STORE.clear()


def interrupted(full, steps):
    ran.append(full)
    raise KeyboardInterrupt


g.run_repo = interrupted
g.run(["actions"], "o")
check("negative: Ctrl-C still stops the run", ran == ["o/one"] and any("Interrupted" in s for s in shown))

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)

#!/usr/bin/env python3
"""CHANGELOG.md is exactly what releases/changelog-short.py makes of CHANGELOG-big.md,
and CHANGELOG-big.md has the shape the generator reads.

  python3 -I tests/changelogShort.test.py
"""

import importlib.util
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "releases", "changelog-short.py")
spec = importlib.util.spec_from_file_location("changelog_short", SCRIPT)
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)
failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


big = open(os.path.join(ROOT, "CHANGELOG-big.md"), encoding="utf-8").read()
short = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()

# The committed files
check("CHANGELOG.md is what the generator writes from CHANGELOG-big.md", short == cs.short_changelog(big))
check("--check passes", subprocess.run([sys.executable, "-I", SCRIPT, "--check"]).returncode == 0)
check("negative: CHANGELOG.md has no <details>, <summary> or HTML links",
      not re.search(r"<(?:/?details|/?summary|a\s)", short))
headings = [l for l in big.split("\n") if l.startswith("# ")]
check("CHANGELOG-big.md: # Platforms, then Upcoming (when there is one), then releases - nothing else",
      headings[:1] == ["# Platforms"] and all(h == f"# {cs.UPCOMING_HEADING}" or cs.RELEASE_HEADING.match(h) for h in headings[1:]))
up = cs.upcoming_of(big)
if up is not None:
    words = len(cs.in_short_of(up).split())
    check(f"Upcoming has an In short paragraph, short ({words} words, at most 120)", 0 < words <= 120)
for heading, version, body in cs.releases_of(big):
    check(f"{version} has an In short paragraph", bool(cs.in_short_of(body)))
summaries = re.findall(r"^<summary>.*$", big, re.M)
check("every entry's <summary> is on one line and links its commit",
      all(s.endswith("</summary>") and "/commit/" in s for s in summaries))

# The rules, on a fixture
fixture = "\n".join(["# Platforms", "", "x", "", f"# {cs.UPCOMING_HEADING}", "",
                     "**In short:** two lines", "of **summary**.", "", "rest"]
                    + sum([[f"# v{n}.0.0-wekan-2026110{n}-abc 2026-11-0{n} gitea-patches release", "",
                            f"**In short:** release {n} with <a href=\"https://x/{n}\">a link</a>.", "",
                            "<details>", "<summary>hidden</summary>", "</details>"] for n in range(7, 0, -1)], []))
out = cs.short_changelog(fixture)
check("the whole In short paragraph, not only its first line", "two lines\nof **summary**." in out)
check("the upcoming section links all changes so far",
      "[All changes so far](CHANGELOG-big.md#upcoming-gitea-patches-release)" in out)
check("only the five newest releases", out.count("gitea-patches release\n") == 6 and "v2.0.0" not in out and "v7.0.0" in out)
check("an HTML link becomes a Markdown link", "[a link](https://x/7)" in out)
check("negative: entries below In short are not copied", "hidden" not in out and "rest" not in out)
check("anchor as GitHub makes it", cs.anchor_of("v29.0.0-wekan-20261107-4f69599981 2026-11-07 gitea-patches release")
      == "v2900-wekan-20261107-4f69599981-2026-11-07-gitea-patches-release")
check("no Upcoming section -> none in the short changelog",
      cs.UPCOMING_HEADING not in cs.short_changelog(fixture.replace(f"# {cs.UPCOMING_HEADING}", "# v9.0.0 2026-11-09 gitea-patches release")))

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)

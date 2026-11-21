#!/usr/bin/env python3
"""github-to-gitea treats a URL as GitHub's API - and so waits out its rate limits - only
when its host, parsed, is exactly api.github.com (code scanning #1,
py/incomplete-url-substring-sanitization). No network.

  python3 -I tests/githubToGiteaApiHost.test.py
"""

import importlib.util
import os
import re
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


# GitHub's API, as the script calls it.
for url in ("https://api.github.com/repos/wekan/wekan/issues?page=2",
            "https://api.github.com/graphql",
            "https://API.GitHub.com/user",
            "https://api.github.com:443/rate_limit"):
    check(f"GitHub's API: {url}", g.is_github_api(url))

# Not GitHub's API: the host only contains, starts or ends with the name, or the name is
# in the path, query, fragment or user info; or it is not https, or not a URL at all.
for url in ("https://api.github.com.example.org/repos",
            "https://evilapi.github.com/repos",
            "https://example.org/api.github.com/repos",
            "https://example.org/?next=https://api.github.com/",
            "https://example.org/#api.github.com",
            "https://api.github.com@example.org/repos",
            "https://gitea.example.org/api/v1/repos/migrate",
            "http://api.github.com/repos",
            "api.github.com",
            "https://[::1/",
            ""):
    check(f"not GitHub's API: {url!r}", not g.is_github_api(url))

# Negative test: no script under mirror/ decides what a URL is by a substring of it.
shape = re.compile(r'''["'][A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(/[^"']*)?["']\s+(not\s+)?in\s+\w*url\b''', re.I)
for d, _, files in os.walk(os.path.join(ROOT, "mirror")):
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(d, f)
            for n, line in enumerate(open(p, encoding="utf-8"), 1):
                check(f"no host substring test at {os.path.relpath(p, ROOT)}:{n}", not shape.search(line))

print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)

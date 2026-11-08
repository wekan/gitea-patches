#!/usr/bin/env python3
"""github-to-gitea's release files: by default only the newest release keeps its files
and older ones lose theirs in Gitea; release_assets_keep takes newest, all or a number;
a file from GitHub is downloaded alone to the temporary directory, uploaded, and
deleted. Stand-ins for GitHub and Gitea; no network.

  python3 -I tests/githubToGiteaReleases.test.py
"""

import configparser
import importlib.util
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("g2g", os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
os.environ["MIRROR_TMP_DIR"] = os.path.join(TMP, "github-to-gitea")
os.environ["GITHUB_TOKEN"], os.environ["GITEA_TOKEN"] = "gh", "gt"
g.OWNER, g.NAME, g.GITHUB_REPO, g.DRY = "wekan", "wekan", "wekan/wekan", False
g.log = lambda msg="": None

# GitHub, newest first: a draft, then v3, v2, v1 - each with one file.
GH = [{"tag_name": t, "name": t, "body": "", "draft": t == "v4", "prerelease": False,
       "assets": [{"name": f"weddy-{t}.zip", "url": f"https://api.github.com/assets/{t}", "size": 10}]}
      for t in ("v4", "v3", "v2", "v1")]


def run(keep, gitea_assets, dry=False, fail_upload=False):
    """One sync_releases; returns (uploads, deletions, files seen in the tmp dir during uploads)."""
    calls, during = [], []
    g.DRY = dry
    g.CFG = configparser.ConfigParser(interpolation=None)
    g.CFG.read_dict({"options": {"release_assets": "github", "release_assets_keep": keep, "assets_max_mb": "100"}})
    rel = {t: {"id": i, "tag_name": t, "name": t, "body": "", "draft": t == "v4", "prerelease": False,
               "assets": [{"id": 100 + i, "name": f"weddy-{t}.zip"}] if t in gitea_assets else []}
           for i, t in enumerate(("v4", "v3", "v2", "v1"), 1)}
    g.gt_all = lambda path, repo=None: iter(rel.values())
    g.gh_all = lambda path, params=None: iter(GH)

    def gitea(method, path, body=None, repo=None, stream=None, **kw):
        calls.append((method, path))
        if method == "POST" and "/assets" in path:
            during.append(sorted(os.listdir(os.environ["MIRROR_TMP_DIR"])))
            return (500, "full") if fail_upload else (201, {})
        return 204, None

    g.gitea = gitea
    g.gt = lambda m, p, b=None, **kw: gitea(m, p, b)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    g.NO_AUTH_REDIRECT.open = lambda req, timeout=None: Resp(b"0123456789")
    g.sync_releases()
    uploads = [p for m, p in calls if m == "POST" and "/assets" in p]
    deletes = [p for m, p in calls if m == "DELETE"]
    return uploads, deletes, during


# Default: the newest published release (v3; v4 is a draft) gets its file, the older
# ones that have files in Gitea lose them.
up, dl, during = run("newest", gitea_assets={"v2", "v1"})
check("newest: only v3 gets its file", up == ["/releases/2/assets?name=weddy-v3.zip"])
check("newest: v2's and v1's files are deleted from Gitea", sorted(dl) == ["/releases/3/assets/103", "/releases/4/assets/104"])
check("negative: a draft is not the newest release", not any("/releases/1/" in p for p in up + dl))
check("one file at a time in the temporary directory while it is uploaded", during == [[f"{os.getpid()}-weddy-v3.zip"]])
check("the downloaded file is deleted after the upload", os.listdir(os.environ["MIRROR_TMP_DIR"]) == [])

up, dl, _ = run("all", gitea_assets=set())
check("all: every published release gets its file, nothing is deleted", len(up) == 3 and dl == [])
up, dl, _ = run("2", gitea_assets={"v1"})
check("2: v3 and v2 get their files, v1's is deleted",
      sorted(up) == ["/releases/2/assets?name=weddy-v3.zip", "/releases/3/assets?name=weddy-v2.zip"] and dl == ["/releases/4/assets/104"])
up, dl, _ = run("newest", gitea_assets={"v3"})
check("negative: a file Gitea already has is not uploaded again", up == [])

run("newest", gitea_assets=set(), fail_upload=True)
check("negative: a failed upload still deletes the downloaded file", os.listdir(os.environ["MIRROR_TMP_DIR"]) == [])
up, dl, _ = run("newest", gitea_assets={"v1", "v2"}, dry=True)
check("negative: a dry run deletes and uploads nothing", up == [] and dl == [])

# The old default none becomes github once, in an ini file from before
# release_assets_keep; none chosen after that stays none.
ini = os.path.join(TMP, "github-to-gitea.ini")
g.INI_FILE = ini
for content, want in (("[options]\nrelease_assets = none\n", "github"),
                      ("[options]\nrelease_assets = none\nrelease_assets_keep = newest\n", "none")):
    with open(ini, "w") as f:
        f.write(content)
    g.CFG = configparser.ConfigParser(interpolation=None)
    g.load_config()
    check(f"an ini file with {content.count(chr(10))} option line(s): release_assets is {want}",
          g.CFG.get("options", "release_assets") == want)
check("release_assets_keep's default is newest", g.CFG.get("options", "release_assets_keep") in ("newest",))

print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)

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
real_gt_all = g.gt_all
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


def run(keep, gitea_assets, dry=False, fail_upload=False, releases_all="", in_gitea=("v4", "v3", "v2", "v1"),
        repo="wekan/wekan", private=False, max_mb="100", size=10):
    """One sync_releases; returns (uploads, deletions, files seen in the tmp dir during uploads)."""
    calls, during = [], []
    g.DRY = dry
    g.CFG = configparser.ConfigParser(interpolation=None)
    g.CFG.read_dict({"options": {"release_assets": "github", "release_assets_keep": keep, "assets_max_mb": max_mb},
                     "releases_all": {"list": releases_all}})
    g.GITHUB_REPO = repo
    g._info.clear()
    g._info[repo] = {"fork": False, "private": private}
    g.ensure_tag = lambda tag, src=None: True
    rel = {t: {"id": i, "tag_name": t, "name": t, "body": "", "draft": t == "v4", "prerelease": False,
               "assets": [{"id": 100 + i, "name": f"weddy-{t}.zip"}] if t in gitea_assets else []}
           for i, t in enumerate(("v4", "v3", "v2", "v1"), 1) if t in in_gitea}
    g.gt_all = lambda path, repo=None: iter(rel.values())
    gh = [dict(r, assets=[dict(a, size=size) for a in r["assets"]]) for r in GH]
    g.gh_all = lambda path, params=None: iter(gh)

    def gitea(method, path, body=None, repo=None, stream=None, **kw):
        calls.append((method, path))
        if method == "POST" and "/assets" in path:
            during.append(sorted(os.listdir(os.environ["MIRROR_TMP_DIR"])))
            return (500, "full") if fail_upload else (201, {})
        if method == "POST" and path == "/releases":
            created.append(body["tag_name"])
            return 201, dict(body, id=50 + len(created), assets=[])
        return 204, None

    created = []
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
    run.created = created
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

# "Releases kept in full": every release, text and files, for the listed repositories;
# every other repository gets only its newest release.
up, dl, _ = run("newest", gitea_assets=set(), in_gitea=())
check("no list: only the newest release's text is synced to Gitea", run.created == ["v3"] and len(up) == 1)
for item in ("wekan/wekan", "WEKAN", "(all)", "(allpublic)"):
    up, dl, _ = run("newest", gitea_assets=set(), in_gitea=(), releases_all=f"\nxet7\n{item}")
    check(f"listed as {item}: every release is synced, oldest first, and all keep their files",
          run.created == ["v1", "v2", "v3", "v4"] and len(up) == 3 and dl == [])
up, dl, _ = run("newest", gitea_assets={"v2", "v1"}, releases_all="(all)")
check("listed: files already in Gitea are kept", dl == [])
up, dl, _ = run("newest", gitea_assets={"v2", "v1"}, releases_all="wekan/wekan", repo="wekan/other")
check("negative: owner/name keeps all for that repository only", run.created == [] and len(dl) == 2)
up, dl, _ = run("newest", gitea_assets=set(), in_gitea=(), releases_all="(allprivate)")
check("negative: (allprivate) does not cover a public repository", run.created == ["v3"])
up, dl, _ = run("newest", gitea_assets=set(), in_gitea=(), releases_all="wekan/wekan2")
check("negative: a longer name is no match", run.created == ["v3"])

# assets_max_mb reads as Gitea's [repository.release] FILE_MAX_SIZE: -1 is no limit,
# 0 is a largest size of 0 - no file - and a file over N MB is skipped.
up, dl, _ = run("newest", gitea_assets=set(), max_mb="1", size=5 * 1048576)
check("negative: a file over assets_max_mb is not uploaded", up == [])
up, dl, _ = run("newest", gitea_assets=set(), max_mb="-1", size=50 * 1024 ** 3)
check("assets_max_mb = -1: a 50 GB file is uploaded", up == ["/releases/2/assets?name=weddy-v3.zip"])
up, dl, _ = run("newest", gitea_assets=set(), max_mb="0", size=10)
check("negative: assets_max_mb = 0 uploads no file at all", up == [])
up, dl, _ = run("newest", gitea_assets=set(), max_mb="", size=50 * 1024 ** 3)
check("an empty assets_max_mb is no limit, as -1", len(up) == 1)

# The menu: add (all) and wekan/wekan, refuse a duplicate and a bad name, remove one.
answers = iter(["a", "(ALL)", "a", "https://github.com/wekan/wekan.git", "a", "Wekan/Wekan", "a", "bad name!",
                "r", "1", "0"])
g.ask = lambda prompt, default="": next(answers)
g.save_config = lambda: None
g.CFG = configparser.ConfigParser(interpolation=None)
g.CFG.read_dict({"releases_all": {"list": ""}})
import contextlib
with contextlib.redirect_stdout(io.StringIO()) as out:
    g.menu_releases_all()
check("the menu adds, refuses a duplicate or a bad name, and removes", g.releases_all() == ["wekan/wekan"])
check("negative: what the menu refused is said", out.getvalue().count("Not valid, or already in the list.") == 2)

# Freeing Gitea's disk: the release files of EVERY Gitea repository, "older" keeping
# the newest published release's (and drafts', and every file of a repository in
# Releases kept in full), "all" deleting every one. The releases stay.
def prune(mode, dry=False, releases_all="", answer=None):
    repos = {"wekan/wekan": False, "xet7/neonnoir": False, "xet7/secret": True, "xet7/empty": False}
    rels = {full: [{"id": 10 * i + j, "tag_name": t, "draft": t == "draft",
                    "assets": [{"id": 100 * i + 10 * j + k, "name": f"{t}-{k}.zip", "size": 1 << 30} for k in (1, 2)]}
                   for j, t in enumerate(("draft", "v3", "v2", "v1"))]
            for i, full in enumerate(repos)}
    rels["xet7/empty"] = []
    deleted = []

    def gitea(method, path, body=None, repo=None, **kw):
        if path.startswith("/repos/search"):
            page = int(path.rsplit("page=", 1)[1])
            items = [{"full_name": f, "private": p} for f, p in repos.items()]
            return 200, {"data": items if page == 1 else items}  # Gitea's last page repeats nothing new
        if method == "GET" and path.startswith("/releases"):
            page = int(path.rsplit("page=", 1)[1])
            return 200, rels[repo] if page == 1 else []
        if method == "DELETE":
            deleted.append((repo, path))
            return 204, None
        return 404, {}

    g.gitea = gitea
    g.gt_all = real_gt_all
    g.DRY = dry
    g.CFG = configparser.ConfigParser(interpolation=None)
    g.CFG.read_dict({"releases_all": {"list": releases_all}})
    if answer is None:
        ok = g.prune_release_files(mode)
    else:
        answers = iter(answer)
        g.ask = lambda prompt, default="": next(answers)
        g.apply_config = lambda: None
        with contextlib.redirect_stdout(io.StringIO()):
            g.menu_prune()
        ok = True
    return ok, deleted


ok, dl = prune("older")
kept = {"v3-1.zip", "v3-2.zip", "draft-1.zip", "draft-2.zip"}
names = {p.rsplit("/", 1)[1] for _, p in dl}
check("older: every Gitea repository loses the files of its older releases",
      ok and {r for r, _ in dl} == {"wekan/wekan", "xet7/neonnoir", "xet7/secret"} and len(dl) == 3 * 4)
check("negative: older keeps the newest published release's files and drafts' files",
      not any(f"/releases/{10 * i + j}/" in p for i in range(4) for j in (0, 1) for _, p in dl))
ok, dl = prune("all")
check("all: every release file of every repository is deleted, drafts' too", ok and len(dl) == 3 * 8)
ok, dl = prune("older", releases_all="xet7")
check("negative: older leaves a repository in Releases kept in full alone",
      {r for r, _ in dl} == {"wekan/wekan"})
ok, dl = prune("older", releases_all="(allprivate)")
check("Releases kept in full by visibility uses Gitea's, asking GitHub nothing", "xet7/secret" not in {r for r, _ in dl})
ok, dl = prune("all", releases_all="(all)")
check("all deletes also in Releases kept in full", len(dl) == 3 * 8)
ok, dl = prune("all", dry=True)
check("negative: a dry run deletes nothing", dl == [])
ok, dl = prune("all", answer=["2", "no"])
check("negative: the menu deletes nothing unless yes is typed", dl == [])
ok, dl = prune("older", answer=["1", "yes"])
check("the menu: 1 and yes keeps the newest release's files", len(dl) == 3 * 4)
ok, dl = prune("all", answer=["2", "YES"])
check("the menu: 2 and yes deletes every release file", len(dl) == 3 * 8)
ok, dl = prune("all", answer=["0"])
check("negative: 0 goes back without deleting", dl == [])
try:
    g.prune_release_files("newest")
    check("negative: an unknown mode is refused", False)
except ValueError:
    check("negative: an unknown mode is refused", True)

# A release of many versions - rustdesk's nightly: the same files with different version
# numbers - gives only the newest version's files; any other release keeps them all.
RUSTDESK = [f"rustdesk-{v}-{p}" for v in ("1.2.0", "1.3.9", "1.4.3", "1.4.2") for p in
            ("x86_64.deb", "aarch64.apk", "x86_64-unknown-linux-gnu-ubuntu-18.04.deb")] + ["rustdesk-1.2.0-0.x86_64.rpm", "SHA256SUMS"]
keep, newest = g.newest_version_files(RUSTDESK)
check("many versions: only the newest version's files, and files without a version",
      newest == "1.4.3" and keep == {"rustdesk-1.4.3-x86_64.deb", "rustdesk-1.4.3-aarch64.apk",
                                     "rustdesk-1.4.3-x86_64-unknown-linux-gnu-ubuntu-18.04.deb", "SHA256SUMS"})
check("versions compare as numbers, not text: 1.10.0 is newer than 1.9.0",
      g.newest_version_files(["a-1.9.0.zip", "a-1.10.0.zip"]) == ({"a-1.10.0.zip"}, "1.10.0"))
for names in (["app-1.0.zip", "lib-2.0.zip", "README"], ["weddy-v3.zip", "weddy-v3.tar.gz"],
              ["wekan-8.12-amd64.zip", "wekan-8.12-arm64.zip"], ["ubuntu-18.04.deb", "ubuntu-20.04.iso"], []):
    check(f"negative: {names or 'no files'} is no release of many versions, every file kept",
          g.newest_version_files(names) == (set(names), None))


def nightly(have, releases_all=""):
    calls = []
    g.CFG = configparser.ConfigParser(interpolation=None)
    g.CFG.read_dict({"options": {"release_assets": "github", "release_assets_keep": "newest", "assets_max_mb": "-1"},
                     "releases_all": {"list": releases_all}})
    g.GITHUB_REPO, g.DRY = "xet7/rustdesk", False
    g._info.clear()
    g._info["xet7/rustdesk"] = {"fork": True, "parent": None, "private": False}
    g.gh_all = lambda path, params=None: iter([{"tag_name": "nightly", "name": "nightly", "body": "", "draft": False,
                                                "prerelease": False, "assets": [{"name": n, "url": "u/" + n, "size": 1}
                                                                                for n in RUSTDESK]}])
    g.gt_all = lambda path, repo=None: iter([{"id": 7, "tag_name": "nightly", "name": "nightly", "body": "", "draft": False,
                                              "prerelease": False, "assets": [{"id": 70 + i, "name": n} for i, n in enumerate(have)]}])
    g.gitea = lambda m, p, b=None, repo=None, **kw: (calls.append((m, p)), (204, None))[1]
    g.gt = lambda m, p, b=None, **kw: (calls.append((m, p)), (200, {}))[1]
    uploads = []
    g.upload_asset = lambda cur, tag, name, path, url, size=0: uploads.append(name)
    g.sync_releases()
    return uploads, [p for m, p in calls if m == "DELETE"]


up, dl = nightly(have=["rustdesk-1.2.0-x86_64.deb", "rustdesk-1.4.3-x86_64.deb"])
check("github-to-gitea: a release of many versions uploads only the newest version's missing files",
      sorted(up) == ["SHA256SUMS", "rustdesk-1.4.3-aarch64.apk", "rustdesk-1.4.3-x86_64-unknown-linux-gnu-ubuntu-18.04.deb"])
check("github-to-gitea: an older version's file already in Gitea is deleted, the newest's stays",
      dl == ["/releases/7/assets/70"])
up, dl = nightly(have=["rustdesk-1.2.0-x86_64.deb"], releases_all="xet7/rustdesk")
check("negative: Releases kept in full keeps every version", len(up) == len(RUSTDESK) - 1 and dl == [])

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

# Release files of any size by default: assets_max_mb is 0. A file with the old
# default 2048 gets 0, once; 2048 chosen after that stays.
check("assets_max_mb's default is -1, no limit", g.CFG.get("options", "assets_max_mb") == "-1")
for content, want in (("[options]\nassets_max_mb = 2048\n", "-1"),
                      ("[options]\nassets_max_mb = 0\n[migrations]\nassets_max_mb_unlimited = done\n", "-1"),
                      ("[options]\nassets_max_mb = 500\n", "500"),
                      ("[options]\nassets_max_mb = 0\n[migrations]\nassets_max_mb_no_limit = done\n", "0"),
                      ("[options]\nassets_max_mb = 2048\n[migrations]\nassets_max_mb_no_limit = done\n", "2048")):
    with open(ini, "w") as f:
        f.write(content)
    g.CFG = configparser.ConfigParser(interpolation=None)
    g.load_config()
    check(f"assets_max_mb {content.split()[3]}" + (" after the migration" if "no_limit = done" in content else
                                                   " from 0-is-no-limit" if "done" in content else "") + f" becomes {want}",
          g.CFG.get("options", "assets_max_mb") == want)

print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)

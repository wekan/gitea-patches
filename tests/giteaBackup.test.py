#!/usr/bin/env python3
"""mirror/common/gitea_backup.py: backing up a Gitea organization or user into
<dir>/<name>/<stamp>/<name>.db and <name>.zip, deleting it from Gitea completely, and
restoring it - into Gitea with the same numbers, authors, comments, releases and
files, and as WeKan boards.

The pure parts run always. The whole round trip runs against a real, DISPOSABLE
Gitea when one is given - never the Gitea you use, because it creates and deletes
organizations:

  python3 -I tests/giteaBackup.test.py
  GITEA_TEST_URL=http://127.0.0.1:3399 GITEA_TEST_TOKEN=<admin token> \\
    GITEA_TEST_USER=alice python3 -I tests/giteaBackup.test.py

GITEA_TEST_USER is a second, ordinary user of that Gitea: the issues are written as
them (Sudo), and the restore must keep them as the author.
"""

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mirror", "common"))
import gitea_backup as gb  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


# --- pure ----------------------------------------------------------------------------------

check("a tag or file name is never a path", [gb.safe_part(x) for x in ("v1/beta", "..", ".", "", "a\\b", "x\ny")]
      == ["v1_beta", "_", "_", "_", "a_b", "x_y"])
here = "/opt/mirror/github-to-gitea"
check("the backup directory is beside the script by default",
      gb.backup_root("", here) == f"{here}/github-to-gitea-backup")
check("a relative backup directory is beside the script", gb.backup_root("b", here) == f"{here}/b")
check("an absolute backup directory is another disk", gb.backup_root("/run/media/u/disk/bk", here) == "/run/media/u/disk/bk")
check("~ is the home directory", gb.backup_root("~/bk", here) == os.path.expanduser("~/bk"))
check("WeKan colors: the nearest one", (gb.wekan_color("#eb4646"), gb.wekan_color("0078c0"), gb.wekan_color("bad"))
      == ("red", "blue", "gray"))
check("a WeKan id has no dash (the zip import reads up to one)", "-" not in gb._wid("a/b", "file", "x-y"))
c = gb.Client("http://g:3000/api/v1", "t")
check("the client takes the address with or without /api/v1", (c.root, c.api) == ("http://g:3000", "http://g:3000/api/v1"))
check("a file is downloaded from this Gitea by its uuid, not from ROOT_URL",
      c.file_url({"uuid": "u1", "browser_download_url": "http://elsewhere/attachments/u1"}) == "http://g:3000/attachments/u1")
try:
    c.file_url({"name": "x", "browser_download_url": "http://elsewhere/x"})
    check("negative: a file on another host is refused (the token would go there)", False)
except gb.BackupError:
    check("negative: a file on another host is refused (the token would go there)", True)

with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
    for p in ("wekan/2026-01-01_00-00-00", "wekan/2026-02-01_00-00-00", "wekan/2026-03-01_00-00-00.partial", "x/junk"):
        os.makedirs(os.path.join(d, p))
    for p in ("wekan/2026-01-01_00-00-00", "wekan/2026-02-01_00-00-00", "wekan/2026-03-01_00-00-00.partial"):
        open(os.path.join(d, p, "wekan.db"), "w").close()
    check("backups: newest first, and a .partial one is not a backup",
          [b[1] for b in gb.backups(d)] == ["2026-02-01_00-00-00", "2026-01-01_00-00-00"])

src = open(os.path.join(ROOT, "mirror", "common", "gitea_backup.py"), encoding="utf-8").read()
check("negative: the zip is written STORED, never deflated (Omi reads its entries in place)",
      "ZIP_DEFLATED" not in src.split("# --- restoring as WeKan boards")[0])
check("negative: deleting an owner needs its name typed", "Type {name} to go on\") != name" in src)
gtg = open(os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py"), encoding="utf-8").read()
check("github-to-gitea: the menu has Backups and the backup_dir setting",
      "menu_backups()" in gtg and '"backup_dir", gitea_backup.DEFAULT_DIR' in gtg)

# --- the round trip against a disposable Gitea --------------------------------------------

URL, TOKEN, USER = (os.environ.get(k) for k in ("GITEA_TEST_URL", "GITEA_TEST_TOKEN", "GITEA_TEST_USER"))
if not (URL and TOKEN and USER):
    print("skip the round trip: GITEA_TEST_URL, GITEA_TEST_TOKEN and GITEA_TEST_USER are not set")
    sys.exit(1 if failures else 0)

cl = gb.Client(URL, TOKEN)
ORG = "backuptestorg"
say = lambda m: print("   ", m)  # noqa: E731


def req(method, path, body=None, sudo=None, want=(200, 201, 204)):
    s, d = cl.request(method, path, body, sudo=sudo)
    if s not in want:
        raise SystemExit(f"{method} {path}: {s} {d}")
    return d


def upload(path, name, data, sudo=None):
    import io
    s, d = cl.upload(path, name, io.BytesIO(data), len(data), sudo=sudo)
    if s != 201:
        raise SystemExit(f"upload {path}: {s} {d}")
    return d


def gitrun(*args, cwd=None):
    r = subprocess.run(["git"] + list(args), cwd=cwd, env=dict(cl.git_env(), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {args}: {r.stdout}{r.stderr}")
    return r.stdout.strip()


cl.request("DELETE", f"/repos/{ORG}/proj")
cl.request("DELETE", f"/repos/{ORG}/empty")
cl.request("DELETE", f"/orgs/{ORG}")
req("POST", "/orgs", {"username": ORG, "full_name": "Backup test", "description": "for the test"})
req("POST", f"/orgs/{ORG}/repos", {"name": "proj", "description": "a project", "default_branch": "main"})
req("POST", f"/orgs/{ORG}/repos", {"name": "empty"})
req("POST", f"/orgs/{ORG}/teams", {"name": "devs", "permission": "write", "units": ["repo.code", "repo.issues"]})
work = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
try:
    w = os.path.join(work, "w")
    gitrun("init", "-q", "-b", "main", w)
    open(os.path.join(w, "a.txt"), "w").write("one\n")
    gitrun("add", ".", cwd=w)
    gitrun("commit", "-qm", "one", cwd=w)
    gitrun("checkout", "-qb", "feature", cwd=w)
    open(os.path.join(w, "b.txt"), "w").write("two\n")
    gitrun("add", ".", cwd=w)
    gitrun("commit", "-qm", "two", cwd=w)
    gitrun("checkout", "-qb", "open-feature", cwd=w)
    open(os.path.join(w, "c.txt"), "w").write("three\n")
    gitrun("add", ".", cwd=w)
    gitrun("commit", "-qm", "three", cwd=w)
    gitrun("checkout", "-q", "main", cwd=w)
    gitrun("tag", "v1.0", cwd=w)
    gitrun("push", "-q", cl.git_url(f"{ORG}/proj"), "main", "feature", "open-feature", "v1.0", cwd=w)
    full = f"{ORG}/proj"
    check("the pushed branches are listed once Gitea's push queue has run",
          gb.wait_branches(cl, full, ["main", "feature", "open-feature"]))
    req("PUT", f"/repos/{full}/collaborators/{USER}", {"permission": "write"})
    req("PUT", f"/repos/{full}/topics", {"topics": ["kanban", "backup"]})
    bug = req("POST", f"/repos/{full}/labels", {"name": "bug", "color": "#ee0701"})
    ms = req("POST", f"/repos/{full}/milestones", {"title": "v1", "description": "first"})
    i1 = req("POST", f"/repos/{full}/issues", {"title": "First issue", "body": "Body of one", "labels": [bug["id"]],
                                               "milestone": ms["id"]}, sudo=USER)
    a1 = upload(f"/repos/{full}/issues/1/assets", "shot.png", b"\x89PNG fake image bytes", sudo=USER)
    req("PATCH", f"/repos/{full}/issues/1", {"body": f"Body of one ![shot](/attachments/{a1['uuid']})"})
    c1 = req("POST", f"/repos/{full}/issues/1/comments", {"body": "A comment"}, sudo=USER)
    upload(f"/repos/{full}/issues/comments/{c1['id']}/assets", "log.txt", b"comment file\n" * 100)
    req("POST", f"/repos/{full}/pulls", {"title": "Merged PR", "head": "feature", "base": "main", "body": "merge me"})
    req("POST", f"/repos/{full}/pulls/2/merge", {"Do": "merge"})
    team = [x for x in req("GET", f"/orgs/{ORG}/teams") if x["name"] == "devs"][0]
    req("PUT", f"/teams/{team['id']}/members/{USER}")
    req("PUT", f"/teams/{team['id']}/repos/{ORG}/proj")
    req("POST", f"/repos/{full}/issues", {"title": "Deleted one"})
    req("DELETE", f"/repos/{full}/issues/3")
    req("POST", f"/repos/{full}/pulls", {"title": "Open PR", "head": "open-feature", "base": "main"}, sudo=USER)
    req("POST", f"/repos/{full}/issues", {"title": "Closed issue", "closed": True})
    req("PATCH", f"/repos/{full}/issues/5", {"state": "closed"})
    rel = req("POST", f"/repos/{full}/releases", {"tag_name": "v1.0", "name": "Version 1", "body": "notes"})
    big = bytes(range(256)) * 4096  # 1 MB
    upload(f"/repos/{full}/releases/{rel['id']}/assets", "wekan-1.0.zip", big)
    req("POST", f"/repos/{full}/wiki/new", {"title": "Home", "content_base64": base64.b64encode(b"Wiki home\n").decode()})
    before_refs = gitrun("ls-remote", cl.git_url(full))

    # Backup.
    root = os.path.join(work, "backups")
    d = gb.backup(cl, ORG, root, say)
    check("backup: <dir>/<name>/<stamp>/<name>.db and <name>.zip, not .partial",
          os.path.isfile(os.path.join(d, f"{ORG}.db")) and os.path.isfile(os.path.join(d, f"{ORG}.zip"))
          and not d.endswith(".partial") and os.path.dirname(os.path.dirname(d)) == root)
    db = sqlite3.connect(os.path.join(d, f"{ORG}.db"))
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    check("backup: complete, of an organization", dict(q("SELECT key, value FROM meta"))["complete"] == "yes"
          and q("SELECT kind FROM owner")[0][0] == "organization")
    check("backup: both repositories, the empty one marked empty",
          q("SELECT name, empty FROM repos ORDER BY name") == [("empty", 1), ("proj", 0)])
    check("backup: issues and pull requests by number, the deleted number missing",
          q("SELECT number, is_pull, state FROM issues WHERE repo = ? ORDER BY number", full)
          == [(1, 0, "open"), (2, 1, "closed"), (4, 1, "open"), (5, 0, "closed")])
    check("backup: the merged pull request's head and base", json.loads(q(
        "SELECT json FROM pulls WHERE number = 2")[0][0])["merged"] is True)
    check("backup: comments, labels, milestones, releases, teams",
          len(q("SELECT 1 FROM comments")) >= 1 and q("SELECT name FROM labels WHERE repo = ?", full) == [("bug",)]
          and len(q("SELECT 1 FROM milestones")) == 1 and q("SELECT tag FROM releases") == [("v1.0",)]
          and ("devs",) in q("SELECT name FROM teams"))
    z = zipfile.ZipFile(os.path.join(d, f"{ORG}.zip"))
    names = set(z.namelist())
    check("backup zip: git, wiki and every file in folders",
          {"proj/git.bundle", "proj/wiki.bundle", "proj/releases/v1.0/wekan-1.0.zip"} <= names
          and any(n.startswith("proj/issues/1/") and n.endswith("-shot.png") for n in names)
          and any(n.startswith(f"proj/comments/1/{c1['id']}/") and n.endswith("-log.txt") for n in names))
    check("backup zip: every entry STORED", all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
    rows = q("SELECT path, size, sha256 FROM files WHERE kind = 'releases'")
    check("backup: a file's size and sha256 are in the .db, and match the zip",
          rows[0][1] == len(big) and rows[0][2] == hashlib.sha256(big).hexdigest()
          and hashlib.sha256(z.read(rows[0][0])).hexdigest() == rows[0][2])
    db.close()
    z.close()

    # Deleting needs the name typed (the menu), and then removes everything.
    answers = iter(["2", ORG, "no", "not-the-name", "0"])
    gb.menu(cl, lambda *a: next(answers), say, root)
    check("negative: delete without the name typed deletes nothing", cl.request("GET", f"/orgs/{ORG}")[0] == 200)
    gb.delete_owner(cl, ORG, say)
    check("delete: the organization and its repositories are gone",
          cl.request("GET", f"/orgs/{ORG}")[0] == 404 and cl.request("GET", f"/repos/{full}")[0] == 404)

    # Restore into Gitea.
    gb.restore_gitea(cl, d, ORG, lambda *a: "1", say)
    issues = {i["number"]: i for i in gb.ga.paged(cl.request, f"/repos/{full}/issues?state=all")}
    check("restore: the same numbers, titles and states", {n: (i["title"], i["state"]) for n, i in issues.items()
          if n != 3} == {1: ("First issue", "open"), 2: ("Merged PR", "closed"), 4: ("Open PR", "open"),
                         5: ("Closed issue", "closed")})
    check("restore: a deleted number becomes a closed placeholder", issues[3]["state"] == "closed")
    check("restore: pull requests are pull requests, the merged one merged",
          issues[2].get("pull_request") and req("GET", f"/repos/{full}/pulls/2")["merged"]
          and issues[4].get("pull_request"))
    check("restore: the author is kept (Sudo)", issues[1]["user"]["login"] == USER and issues[4]["user"]["login"] == USER)
    check("restore: labels and milestone", [x["name"] for x in issues[1]["labels"]] == ["bug"]
          and (issues[1].get("milestone") or {}).get("title") == "v1")
    assets = issues[1].get("assets") or []
    check("restore: the issue's file, and its body points at the new one",
          [a["name"] for a in assets] == ["shot.png"] and assets[0]["uuid"] in issues[1]["body"]
          and a1["uuid"] not in issues[1]["body"])
    com = req("GET", f"/repos/{full}/issues/1/comments")
    check("restore: the comment, by its author, with its file", com and com[0]["user"]["login"] == USER
          and "A comment" in com[0]["body"] and [a["name"] for a in com[0].get("assets") or []] == ["log.txt"])
    rels = req("GET", f"/repos/{full}/releases")
    got = None
    if rels and rels[0].get("assets"):
        import io
        out = io.BytesIO()
        cl.download(cl.file_url(rels[0]["assets"][0]), out)
        got = out.getvalue()
    check("restore: the release and its file, byte for byte", rels and rels[0]["tag_name"] == "v1.0" and got == big)
    after = gitrun("ls-remote", cl.git_url(full))
    keep = lambda text: sorted(l for l in text.splitlines() if "refs/heads/" in l or "refs/tags/" in l)  # noqa: E731
    check("restore: every branch and tag at the same commit", set(keep(before_refs)) <= set(keep(after)))
    check("restore: topics and the wiki", req("GET", f"/repos/{full}/topics")["topics"] == ["backup", "kanban"]
          and cl.request("GET", f"/repos/{full}/wiki/page/Home")[0] == 200)
    team = [x for x in req("GET", f"/orgs/{ORG}/teams") if x["name"] == "devs"]
    check("restore: the team, its member and its repository", team and
          [m["login"] for m in req("GET", f"/teams/{team[0]['id']}/members")] == [USER]
          and [r["name"] for r in req("GET", f"/teams/{team[0]['id']}/repos")] == ["proj"])
    check("restore: the collaborator", cl.request("GET", f"/repos/{full}/collaborators/{USER}")[0] == 204)
    check("restore: allow_manual_merge back as it was", req("GET", f"/repos/{full}")["allow_manual_merge"] is False)
    check("restore: the empty repository", cl.request("GET", f"/repos/{ORG}/empty")[0] == 200)
    log = []
    gb.restore_gitea(cl, d, ORG, lambda *a: "1", log.append)
    check("negative: restoring again leaves existing repositories as they are",
          any("already in Gitea" in m for m in log) and len(list(gb.ga.paged(
              cl.request, f"/repos/{full}/issues?state=all"))) == 5)

    # WeKan.
    zips = gb.wekan_zips(d, os.path.join(d, "wekan"), say)
    proj = [p for p in zips if p.endswith("-proj.zip")][0]
    with zipfile.ZipFile(proj) as wz:
        doc = json.loads(wz.read("wekan.json"))
        entries = [n for n in wz.namelist() if n.startswith("attachments/")]
    check("WeKan: a wekan-board-1.0.0 with a card per issue and pull request",
          doc["_format"] == "wekan-board-1.0.0" and sorted(c["title"] for c in doc["cards"])
          == ["#1 First issue", "#2 Merged PR", "#4 Open PR", "#5 Closed issue"])
    lists = {x["_id"]: x["title"] for x in doc["lists"]}
    check("WeKan: Open, Closed and Merged", {c["title"]: lists[c["listId"]] for c in doc["cards"]}
          == {"#1 First issue": "Open", "#2 Merged PR": "Merged", "#4 Open PR": "Open", "#5 Closed issue": "Closed"})
    check("WeKan: the comment comes with an addComment activity, as the import reads it",
          any(a["activityType"] == "addComment" for a in doc["activities"]) and doc["comments"])
    check("WeKan: each file is attachments/<id>-<name>, the id that its row has",
          sorted(e.split("/")[1].split("-")[0] for e in entries) == sorted(r["_id"] for r in doc["attachments"])
          and all(r.get("url") for r in doc["attachments"]))
finally:
    cl.request("DELETE", f"/repos/{ORG}/proj")
    cl.request("DELETE", f"/repos/{ORG}/empty")
    cl.request("DELETE", f"/orgs/{ORG}")
    shutil.rmtree(work, ignore_errors=True)

sys.exit(1 if failures else 0)

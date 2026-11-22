#!/usr/bin/env python3
"""mirror/common/github_backup.py: a backup of a GitHub organization made straight
from GitHub - no Gitea - in gitea_backup.py's format, and a later one that brings it
up to date, downloading only what is new or changed. A fake GitHub (a local HTTP
server for the API and github.com, another for the hosts files are redirected to, a
third that must never be asked anything) and local git repositories; no network.

  python3 -I tests/githubBackup.test.py
"""

import hashlib
import datetime
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mirror", "common"))
import gitea_backup as gb  # noqa: E402
import github_backup as hb  # noqa: E402

for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(k, None)
failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


TOKEN = "secret-token"
# GitHub's changes after the first backup: dated after it began, whatever the clock says.
LATER = [(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1, minutes=n)).strftime(
    "%Y-%m-%dT%H:%M:%SZ") for n in range(3)]
TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
ID = ["-c", "user.name=U", "-c", "user.email=u@example.org"]


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# --- the fake servers ------------------------------------------------------------------------

class Server:
    def __init__(self, handle):
        self.log = []  # (path, Authorization header or None)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.log.append((self.path, self.headers.get("Authorization")))
                status, headers, body = handle(self.path)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def hits(self, prefix):
        return [p for p, _ in self.log if p.split("?")[0] == prefix or p.startswith(prefix + "?")]


ASSETS = {101: b"release file one " * 100, 102: b"release file two " * 80}
UPLOADS = {"img1": b"\x89PNG first image" * 20, "img2": b"\x89PNG second image" * 20, "late": b"late file" * 9}
STATE = {"late_ok": False}
files = None  # the storage server, made below
gh_srv = None


def json_answer(data, status=200):
    return status, {"Content-Type": "application/json"}, json.dumps(data).encode()


def user(login):
    return {"login": login, "id": hash(login) % 1000}


def make_github():
    """The fake GitHub's data: organization acme with proj, other and skipme."""
    web = gh_srv.url
    G = {"issues": {}, "comments": {}, "releases": {}}

    def issue(n, title, body, updated, pull=False, state="open"):
        i = {"number": n, "title": title, "body": body, "state": state, "user": user("alice"),
             "labels": [{"id": 7, "name": "bug", "color": "eb4646"}], "milestone": None,
             "created_at": "2026-01-01T00:00:00Z", "updated_at": updated, "closed_at": None,
             "html_url": f"{web}/acme/proj/issues/{n}", "url": f"{web}/repos/acme/proj/issues/{n}"}
        if pull:
            i["pull_request"] = {"url": f"{web}/repos/acme/proj/pulls/{n}"}
        G["issues"][n] = i

    issue(1, "First issue", f"See ![shot]({web}/user-attachments/assets/img1) and "
                            f"[elsewhere](http://127.0.0.1:{NEVER_PORT}/user-attachments/assets/x) "
                            f"and https://example.invalid/a.png", "2026-01-02T00:00:00Z")
    issue(2, "A pull request", "Merges things", "2026-01-03T00:00:00Z", pull=True, state="closed")
    issue(3, "Third", f"an old image {files.url}/img/old.png and a missing "
                      f"{web}/user-attachments/files/late/late.txt", "2026-01-04T00:00:00Z")

    def comment(cid, n, body, updated):
        G["comments"][cid] = {"id": cid, "body": body, "user": user("bob"), "created_at": "2026-01-05T00:00:00Z",
                              "updated_at": updated, "issue_url": f"{web}/repos/acme/proj/issues/{n}",
                              "html_url": f"{web}/acme/proj/issues/{n}#issuecomment-{cid}"}

    comment(500, 1, f"a comment with {web}/user-attachments/assets/img2", "2026-01-05T00:00:00Z")

    def asset(aid, name):
        return {"id": aid, "name": name, "size": len(ASSETS[aid]), "updated_at": "2026-01-06T00:00:00Z",
                "url": f"{web}/repos/acme/proj/releases/assets/{aid}",
                "browser_download_url": f"{web}/acme/proj/releases/download/v1/{name}"}

    G["releases"][900] = {"id": 900, "tag_name": "v1", "name": "v1", "body": "first", "draft": False,
                          "prerelease": False, "published_at": "2026-01-06T00:00:00Z", "target_commitish": "main",
                          "assets": [asset(101, "one.zip")]}
    G["asset"] = asset
    G["issue"], G["comment"] = issue, comment
    return G


def repo_json(name, wiki=False):
    return {"id": hash(name) % 100000, "name": name, "full_name": f"acme/{name}", "owner": {"login": "acme"},
            "private": False, "default_branch": "main", "has_wiki": wiki, "topics": ["kanban"] if name == "proj" else [],
            "description": f"the {name}", "created_at": "2025-12-01T00:00:00Z"}


def github(path):
    u = urllib.parse.urlsplit(path)
    q = dict(urllib.parse.parse_qsl(u.query))
    p = u.path
    since = q.get("since", "")
    if p == "/users/acme":
        return json_answer({"login": "acme", "type": "Organization", "avatar_url": f"{files.url}/avatar/acme"})
    if p == "/orgs/acme":
        return json_answer({"login": "acme", "name": "Acme", "avatar_url": f"{files.url}/avatar/acme"})
    if p == "/user":
        return json_answer({"login": "someone"})
    if p == "/orgs/acme/repos":
        return json_answer([repo_json("proj", wiki=True), repo_json("other"), repo_json("skipme")])
    if p == "/orgs/acme/members":
        return json_answer([user("alice")])
    if p == "/orgs/acme/teams":
        return json_answer([{"id": 5, "name": "devs", "slug": "devs", "permission": "push"}])
    if p == "/orgs/acme/teams/devs/members":
        return json_answer([user("alice")])
    if p == "/orgs/acme/teams/devs/repos":
        return json_answer([{"name": "proj"}])
    if p.startswith("/repos/acme/skipme"):
        return json_answer({"message": "must not be asked"}, 500)
    if p in ("/repos/acme/proj/collaborators", "/repos/acme/other/collaborators"):
        return json_answer([dict(user("alice"), role_name="write")])
    if p == "/repos/acme/proj/labels":
        return json_answer([{"id": 7, "name": "bug", "color": "eb4646"}])
    if p == "/repos/acme/proj/milestones":
        return json_answer([{"id": 8, "title": "1.0", "state": "open"}])
    if p == "/repos/acme/proj/issues":
        return json_answer([i for i in G["issues"].values() if i["updated_at"] >= since])
    if p == "/repos/acme/proj/issues/comments":
        return json_answer([c for c in G["comments"].values() if c["updated_at"] >= since])
    if p == "/repos/acme/proj/pulls/2":
        return json_answer({"number": 2, "merged": True, "merge_commit_sha": "abc",
                            "head": {"ref": "feature", "sha": "def"}, "base": {"ref": "main", "sha": "abc"}})
    if p == "/repos/acme/proj/releases":
        return json_answer(list(G["releases"].values()))
    if p.startswith("/repos/acme/proj/releases/assets/"):
        return 302, {"Location": f"{files.url}/s3/asset/{p.rsplit('/', 1)[1]}"}, b""
    if p.startswith("/user-attachments/"):
        key = p.split("/")[3]
        if key == "late" and not STATE["late_ok"]:
            return 404, {"Content-Type": "text/plain"}, b"not found"
        return 302, {"Location": f"{files.url}/s3/ua/{key}"}, b""
    if p.startswith("/repos/acme/other/"):
        return json_answer([])
    return json_answer({"message": "Not Found"}, 404)


def storage(path):
    p = urllib.parse.urlsplit(path).path
    if p.startswith("/s3/asset/"):
        return 200, {"Content-Type": "application/octet-stream"}, ASSETS[int(p.rsplit("/", 1)[1])]
    if p.startswith("/s3/ua/"):
        key = p.rsplit("/", 1)[1]
        return 200, {"Content-Type": "image/png", "Content-Disposition": f'attachment; filename="{key}.png"'}, UPLOADS[key]
    if p == "/img/old.png":
        return 200, {"Content-Type": "image/png"}, b"old image bytes"
    if p == "/avatar/acme":
        return 200, {"Content-Type": "image/png"}, b"avatar"
    return 404, {}, b""


never = Server(lambda path: (200, {"Content-Type": "image/png"}, b"must not be fetched"))
NEVER_PORT = never.httpd.server_address[1]
files = Server(storage)
gh_srv = Server(github)
G = make_github()

# git: proj (with a pull request head and a tag) and its wiki, and other.
GIT = os.path.join(TMP, "git")


def bare(name, commits=1):
    work = os.path.join(TMP, "work", name)
    git("init", "-q", "-b", "main", work)
    for n in range(commits):
        open(os.path.join(work, "f.txt"), "a").write(f"{name} {n}\n")
        git("add", "f.txt", cwd=work)
        git(*ID, "commit", "-q", "-m", f"c{n}", cwd=work)
    where = os.path.join(GIT, "acme", f"{name}.git")
    git("clone", "-q", "--bare", work, where)
    return work, where


proj_work, proj_git = bare("proj", 2)
git("-C", proj_git, "update-ref", "refs/pull/2/head", "main~1")
git("-C", proj_git, "tag", "v1", "main")
bare("proj.wiki")
bare("other")
bare("skipme")


def client():
    return hb.GitHub(TOKEN, api=gh_srv.url, web=gh_srv.url, file_hosts=(files.url,), git_root=GIT,
                     say=shown.append, sleep=lambda s: None)


def tree_hash(d):
    h = hashlib.sha256()
    for base, _, names in sorted(os.walk(d)):
        for n in sorted(names):
            p = os.path.join(base, n)
            h.update(p.encode() + open(p, "rb").read())
    return h.hexdigest()


BACKUPS = os.path.join(TMP, "backups")
shown = []

# --- pure parts ------------------------------------------------------------------------------
c = client()
links = c.file_links(f"x {gh_srv.url}/user-attachments/assets/a1. and ({gh_srv.url}/acme/proj/files/12/b.zip) "
                     f"{files.url}/u/1/2.png http://127.0.0.1:{NEVER_PORT}/user-attachments/assets/z "
                     f"https://example.invalid/user-attachments/assets/q")
check("file links: GitHub's attachments and file hosts, trailing punctuation dropped; other hosts are no file",
      links == [f"{gh_srv.url}/user-attachments/assets/a1", f"{gh_srv.url}/acme/proj/files/12/b.zip",
                f"{files.url}/u/1/2.png"])
real = hb.GitHub(TOKEN)
check("the token goes to api.github.com and github.com over https",
      "Authorization" in real.headers("https://api.github.com/repos/a/b", "*/*")
      and "Authorization" in real.headers("https://github.com/user-attachments/assets/x", "*/*"))
check("negative: no token to look-alike or other hosts, or over http",
      not any("Authorization" in real.headers(u, "*/*") for u in (
          "https://api.github.com.example.org/x", "https://example.org/?api.github.com",
          "https://user-images.githubusercontent.com/1/x.png", "http://github.com/x",
          "https://objects.githubusercontent.com/x")))
check("git: the token only for github.com over https",
      "GIT_CONFIG_VALUE_0" in real.git_env("https://github.com/a/b.git")
      and "GIT_CONFIG_VALUE_0" not in real.git_env("https://gitlab.com/a/b.git")
      and "GIT_CONFIG_VALUE_0" not in real.git_env("/local/a/b.git"))
check("a file's id is stable and fits a JavaScript number", hb.file_id("x") == hb.file_id("x") < 2 ** 53)

# --- the first run: a whole backup from GitHub -------------------------------------------------
first = hb.update(client(), "acme", BACKUPS, shown.append, excludes=["acme/skipme"])
check("a whole backup is made: <dir>/acme/acme.db and acme.zip, no dated directory, nothing left over",
      first == os.path.join(BACKUPS, "acme") and sorted(os.listdir(first)) == ["acme.db", "acme.zip"])
b = gb.Backup(first)
check("gitea_backup.Backup reads it; meta says it came from GitHub",
      b.meta.get("source") == "github" and b.meta.get("complete") == "yes" and b.meta.get("format") == "1")
check("excludes are left out", [r["full_name"] for r in b.repos()] == ["acme/other", "acme/proj"])
check("negative: an excluded repository is never asked of GitHub",
      not any(p.startswith("/repos/acme/skipme") for p, _ in gh_srv.log))
proj = [r for r in b.repos() if r["name"] == "proj"][0]
check("the repository row: GitHub's JSON, topics, collaborators, git and wiki bundles",
      proj["topics"] == ["kanban"] and proj["collaborators"] == [{"login": "alice", "permission": "write"}]
      and proj["git_bundle"] == "proj/git.bundle" and proj["wiki_bundle"] == "proj/wiki.bundle")
check("issues and the pull request, with its details",
      [r["number"] for r in b.rows("SELECT number FROM issues ORDER BY number")] == [1, 2, 3]
      and json.loads(b.rows("SELECT json FROM pulls")[0]["json"])["merged"] is True)
check("teams with their members and repositories", b.rows("SELECT name, members, repos FROM teams")
      == [{"name": "devs", "members": '["alice"]', "repos": '["proj"]'}])
fr = {f["path"]: f for f in b.rows("SELECT * FROM files")}
check("the release file is in releases/<tag>/", fr.get("proj/releases/v1/one.zip", {}).get("size") == len(ASSETS[101]))
img1 = [f for f in fr.values() if json.loads(f["json"]).get("browser_download_url", "").endswith("/img1")]
check("an issue's attachment is in issues/<n>/<id>-<name>, with its SHA-256",
      len(img1) == 1 and img1[0]["path"].startswith("proj/issues/1/") and
      img1[0]["sha256"] == hashlib.sha256(UPLOADS["img1"]).hexdigest() and img1[0]["uuid"] == "")
check("a comment's attachment is in comments/<n>/<comment>/",
      any(p.startswith("proj/comments/1/500/") for p in fr))
check("a user-images file host link is saved too", any(p.startswith("proj/issues/3/") for p in fr))
check("the zip is STORED", all(i.compress_type == zipfile.ZIP_STORED for i in b.zip.infolist()))
missing = json.loads(b.meta["missing_files"])
check("a link that cannot be fetched is reported and listed in missing_files",
      [m["url"].rsplit("/", 2)[-2] for m in missing] == ["late"] and any("late" in s and "not saved" in s for s in shown))
out = gb.wekan_zips(first, os.path.join(TMP, "wekan1"), lambda m: None)
with zipfile.ZipFile([z for z in out if "proj" in z][0]) as z:
    doc = json.loads(z.read("wekan.json"))
    check("wekan_zips reads it: cards, comments and files", len(doc["cards"]) == 3 and len(doc["comments"]) == 1
          and sum(1 for n in z.namelist() if n.startswith("attachments/")) >= 2)
b.close()

# The token: to the fake GitHub only. Its redirects to storage, and storage, get none.
check("the token goes to GitHub's API and web address", all(a == f"token {TOKEN}" for _, a in gh_srv.log))
check("negative: a redirect to another host drops the Authorization header", files.log and
      all(a is None for _, a in files.log))
check("negative: a link to a host that is not GitHub's is never fetched", never.log == [])

# --- GitHub changes, and the second run -------------------------------------------------------
with zipfile.ZipFile(os.path.join(first, "acme.zip")) as z1:
    before_entries = {i.filename: (i.CRC, i.file_size) for i in z1.infolist()}
    first_bytes = {p: z1.read(p) for p in ("proj/releases/v1/one.zip", img1[0]["path"])}
zip_before = os.path.getsize(os.path.join(first, "acme.zip"))
G["releases"][901] = {"id": 901, "tag_name": "v0", "name": "v0", "body": "gone soon", "draft": False,
                      "prerelease": False, "published_at": "2026-01-01T00:00:00Z", "target_commitish": "main",
                      "assets": []}
G["issue"](4, "New issue", f"with {gh_srv.url}/user-attachments/assets/img2 attached", LATER[0])
G["issues"][1]["body"] += "\n\nEdited."
G["issues"][1]["updated_at"] = LATER[1]
G["comment"](501, 1, "a new comment", LATER[2])
G["releases"][900]["assets"].append(G["asset"](102, "two.zip"))
STATE["late_ok"] = True
open(os.path.join(proj_work, "f.txt"), "a").write("new commit\n")
git("add", "f.txt", cwd=proj_work)
git(*ID, "commit", "-q", "-m", "new", cwd=proj_work)
git("push", "-q", proj_git, "main", cwd=proj_work)
new_head = git("-C", proj_git, "rev-parse", "main")
for p in (gh_srv, files):
    p.log.clear()
shown.clear()
second = hb.update(client(), "acme", BACKUPS, shown.append, excludes=["acme/skipme"])
st = hb.update.stats
check("the same one backup is updated in place: no new directory, nothing left over",
      second == first and sorted(os.listdir(first)) == ["acme.db", "acme.zip"])
b2 = gb.Backup(second)
check("meta: runs 2, created kept, updated and github_updated newer, sources github",
      b2.meta.get("runs") == "2" and b2.meta.get("sources") == "github" and
      b2.meta["created"] < b2.meta["updated"] == b2.meta["github_updated"])
zip_after = os.path.getsize(os.path.join(first, "acme.zip"))
new_bytes = sum(i.file_size for i in b2.zip.infolist() if before_entries.get(i.filename) != (i.CRC, i.file_size))
grown = zip_after - zip_before
print(f"    zip {zip_before} -> {zip_after} bytes (+{grown}); changed or new content {new_bytes} bytes")
check("the zip grew only by the new and changed content (no second copy of anything unchanged)",
      grown <= new_bytes + 512 * (len(b2.zip.infolist()) - len(before_entries) + 2))
check("every unchanged entry is the same bytes as before",
      all(b2.zip.getinfo(n).CRC == c for n, (c, _) in before_entries.items() if n != "proj/git.bundle"))
issues = {r["number"]: json.loads(r["json"]) for r in b2.rows("SELECT * FROM issues")}
check("the new issue and the edited one are in it", sorted(issues) == [1, 2, 3, 4]
      and issues[1]["body"].endswith("Edited."))
check("the new comment, and the old one", sorted(r["id"] for r in b2.rows("SELECT id FROM comments")) == [500, 501])
check("counts: 1 new and 1 changed issue, 1 new comment, 1 changed release",
      (st.new["issues"], st.changed["issues"], st.new["comments"], st.changed["releases"]) == (1, 1, 1, 1))
issues_q = [urllib.parse.parse_qs(urllib.parse.urlsplit(p).query) for p in gh_srv.hits("/repos/acme/proj/issues")]
check("issues and comments are asked only since the old backup", issues_q and all("since" in q for q in issues_q)
      and all("since" in urllib.parse.parse_qs(urllib.parse.urlsplit(p).query)
              for p in gh_srv.hits("/repos/acme/proj/issues/comments")))
check("negative: an unchanged pull request's details are not asked again", gh_srv.hits("/repos/acme/proj/pulls/2") == [])
f2 = {f["path"]: f for f in b2.rows("SELECT * FROM files")}
check("the new release file is downloaded; the old one is kept",
      "proj/releases/v1/two.zip" in f2 and "proj/releases/v1/one.zip" in f2
      and files.hits("/s3/asset/102") and not files.hits("/s3/asset/101")
      and not gh_srv.hits("/repos/acme/proj/releases/assets/101"))
check("negative: attachments the old backup has are not downloaded again (img1 of the edited issue, the old image)",
      not files.hits("/s3/ua/img1") and not files.hits("/img/old.png") and not files.hits("/avatar/acme"))
check("the new issue's attachment is downloaded", any(p.startswith("proj/issues/4/") for p in f2))
check("a file that failed before is tried again, and saved",
      any(p.startswith("proj/issues/3/") and "late" in p for p in f2) and json.loads(b2.meta["missing_files"]) == [])
same = all(b2.zip.read(p) == first_bytes[p] for p in ("proj/releases/v1/one.zip", img1[0]["path"]))
check("kept files are the same bytes", same)
bundle = os.path.join(TMP, "check.bundle")
b2.extract("proj/git.bundle", bundle)
heads = git("bundle", "list-heads", bundle)
check("the git bundle has the new commit, the tag and the pull request head",
      new_head in heads and "refs/tags/v1" in heads and "refs/pull/2/head" in heads)
check("an unchanged repository's bundle is kept, not fetched again", st.git_same >= 2 and st.git_fetched == 1)
check("a changed repository is fetched into a mirror seeded from the old bundle",
      any("git:" in s and "only new objects fetched" in s for s in shown))
print("\n".join("    " + s for s in shown))
check("the summary says what was new and the bytes", any(s.startswith("Summary: ") and "downloaded" in s for s in shown))
out = gb.wekan_zips(second, os.path.join(TMP, "wekan2"), lambda m: None)
check("wekan_zips reads the updated backup", len(out) == 2)
b2.close()

# --- a third run: GitHub deleted a release; nothing else changed ------------------------------
del G["releases"][901]
for p in (gh_srv, files):
    p.log.clear()
shown.clear()
zip2 = open(os.path.join(first, "acme.zip"), "rb").read()
hb.update(client(), "acme", BACKUPS, shown.append, excludes=["acme/skipme"])
b3 = gb.Backup(first)
check("a release deleted at GitHub stays in the backup", [r["tag"] for r in b3.rows(
      "SELECT tag FROM releases ORDER BY tag")] == ["v0", "v1"])
check("... as old data: seen is the run before, the rest the newest run (3)",
      b3.rows("SELECT seen FROM releases WHERE tag = 'v0'")[0]["seen"] == 2 and b3.meta["runs"] == "3"
      and b3.rows("SELECT seen FROM releases WHERE tag = 'v1'")[0]["seen"] == 3)
check("negative: unchanged issues, comments and their files are not old data",
      not b3.rows("SELECT 1 FROM issues WHERE seen < 3") and not b3.rows("SELECT 1 FROM comments WHERE seen < 3")
      and not b3.rows("SELECT 1 FROM files WHERE seen < 3"))
b3.close()
check("nothing changed in the zip: it is not written at all", open(os.path.join(first, "acme.zip"), "rb").read() == zip2)
check("negative: no file is downloaded again", files.log == [])
runs, data, paths = gb.old_only_report(first, shown.append)
check("old data: exactly the deleted release", {t: [r.get("tag") for r in v] for t, v in data.items() if v}
      == {"releases": ["v0"]} and paths == {})
check("the old-data list names it under its repository", any("releases 1: v0" in s for s in shown))

# --- two runs at once: the second refuses ------------------------------------------------------
with gb.Lock(first):
    try:
        hb.update(client(), "acme", BACKUPS, shown.append, excludes=["acme/skipme"])
        check("negative: a second run while one holds the lock refuses", False)
    except gb.BackupError as e:
        check("negative: a second run while one holds the lock refuses", "being updated by another run" in str(e))
check("the lock is gone after its run", not os.path.exists(os.path.join(first, gb.LOCK)))
check("negative: nothing was ever fetched from a host that is not GitHub's", never.log == [])
check("negative: storage hosts never got the token", all(a is None for _, a in files.log))

# --- the menus -----------------------------------------------------------------------------
gbs = open(os.path.join(ROOT, "mirror", "common", "gitea_backup.py"), encoding="utf-8").read()
gtg = open(os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py"), encoding="utf-8").read()
check("the Backups menu has the update straight from GitHub", "Update a backup straight from GitHub (no Gitea)" in gbs
      and "github_backup.update(github, name, root, say" in gbs)
check("github-to-gitea: --backup-from-github, with the Sources excludes", "--backup-from-github" in gtg
      and "github_backup.update(github_client(), a.backup_from_github, backup_dir(), log, excludes() + a.exclude)" in gtg)
src = open(os.path.join(ROOT, "mirror", "common", "github_backup.py"), encoding="utf-8").read()
check("negative: github_backup never talks to Gitea", "gitea_backup.Client" not in src and "gb.Client" not in src
      and "/api/v1" not in src)

for s in (never, files, gh_srv):
    s.httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failures else 0)

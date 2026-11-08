#!/usr/bin/env python3
"""mirror/: gitea-to-gitlab and gitea-to-sourceforge against a fake Gitea, a fake GitLab
and local git repositories - no network, nothing written outside a temporary directory.

  python3 -I tests/mirror.test.py
"""

import email.message
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mirror", "common"))
import gitea_mirror as gm  # noqa: E402


def load(name):
    path = os.path.join(ROOT, "mirror", name, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


gl_mod = load("gitea-to-gitlab")
sf_mod = load("gitea-to-sourceforge")
failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


gm.log = lambda msg="": None  # quiet
TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
os.environ["MIRROR_TMP_DIR"] = os.path.join(TMP, "mirror-tmp")
GITEA = "http://gitea.test"

# --- identities ----------------------------------------------------------------------------
g = gm.Gitea(GITEA, "t", "https://gitea.example.org")
gh_repo = {"full_name": "wekan/wekan", "original_url": "https://github.com/wekan/wekan.git"}
local_repo = {"full_name": "xet7/notes", "original_url": ""}
issue = {"number": 7, "html_url": f"{GITEA}/wekan/wekan/issues/7", "user": {"login": "alice"},
         "created_at": "2020-01-01T00:00:00Z", "body": "Broken.", "pull_request": None}
pr = dict(issue, number=8, html_url=f"{GITEA}/wekan/wekan/pulls/8", pull_request={"merged": False})
check("a migrated issue is identified by its GitHub URL", gm.Item(g, gh_repo, issue).url == "https://github.com/wekan/wekan/issues/7")
check("a migrated pull request by its GitHub /pull/ URL", gm.Item(g, gh_repo, pr).url == "https://github.com/wekan/wekan/pull/8")
check("an issue of a repository not from GitHub: Gitea's public URL",
      gm.Item(g, local_repo, dict(issue, html_url=f"{GITEA}/xet7/notes/issues/7")).url == "https://gitea.example.org/xet7/notes/issues/7")
attributed = dict(issue, body="> *From GitHub: @bob, 2021-02-03, https://github.com/wekan/wekan/issues/7#issuecomment-99*\n\nHello there.")
c = gm.Item(g, gh_repo, attributed, issue=issue)
check("a github-to-gitea comment: its GitHub URL, author and date from the attribution line",
      (c.url, c.author, c.created, c.text) == ("https://github.com/wekan/wekan/issues/7#issuecomment-99", "bob", "2021-02-03", "Hello there."))
plain = gm.Item(g, gh_repo, dict(issue, html_url=f"{GITEA}/wekan/wekan/issues/7#issuecomment-5"), issue=issue)
check("a migrated comment without a GitHub id: Gitea's public URL", plain.url == "https://gitea.example.org/wekan/wekan/issues/7#issuecomment-5")
body = gm.Item(g, gh_repo, issue).body()
check("the body starts with WeKan's marker and ends with provenance",
      body.startswith("<!-- wekan-mirror:https://github.com/wekan/wekan/issues/7 -->") and "Original author: alice" in body)
check("marker_of reads it back", gm.marker_of(body) == "https://github.com/wekan/wekan/issues/7")
h = email.message.Message(); h["Retry-After"] = "7"
check("rate limit: Retry-After", gm.rate_wait(h, 0) == 8)
check("rate limit: no header -> growing pause", gm.rate_wait(email.message.Message(), 1) == 120)
check("safe_segment", gm.safe_segment('../a b"c') == "_a_b_c")

# --- SourceForge mount names are WeKan's ----------------------------------------------------------
wekan = {"wekan-snap": ("repo-wekan-snap-5fde119d", "repo-wekan-snap-issues-b015731f"),
         "Wekan-Python-Burndown": ("repo-Wekan-Python-Burndown-4b696266", "repo-Wekan-Python-Burndown-issues-81354215"),
         "a.b_c": ("repo-a-b-c-5b8f934a", "repo-a-b-c-issues-2ae8a15a")}
check("SourceForge mount names equal tools/mirror-repository.mjs sourceForgeMount()",
      all((sf_mod.mount(n), sf_mod.mount(n, "-issues")) == w for n, w in wekan.items()))
check("the repository named like the project is the tool 'code'", sf_mod.SourceForge.git_mount("wekan", "wekan") == "code")
try:
    sf_mod.quoted('/a"b')
    check("negative: sftp refuses a path with a quote", False)
except RuntimeError:
    check("negative: sftp refuses a path with a quote", True)


# --- a fake Gitea and a fake GitLab ---------------------------------------------------------------
class Fake:
    def __init__(self):
        self.gitea_issues = {
            1: {"number": 1, "title": "First", "body": "One. See http://gitea.test/attachments/11111111-2222-3333-4444-555555555555",
                "state": "closed", "user": {"login": "alice"}, "created_at": "2020", "updated_at": "2020",
                "pull_request": None, "comments": 2, "labels": [{"name": "bug", "color": "ff0000"}],
                "milestone": {"title": "v1"}, "html_url": f"{GITEA}/wekan/wekan/issues/1"},
            2: {"number": 2, "title": "A PR", "body": "Two.", "state": "open", "user": {"login": "bob"},
                "created_at": "2020", "updated_at": "2020", "pull_request": {}, "comments": 0, "labels": [],
                "milestone": None, "html_url": f"{GITEA}/wekan/wekan/pulls/2"},
        }
        self.gitea_comments = {1: [
            {"id": 10, "body": "A long enough comment that WeKan's mirror already copied from GitHub.", "user": {"login": "carol"},
             "created_at": "2020", "html_url": f"{GITEA}/wekan/wekan/issues/1#issuecomment-10"},
            {"id": 11, "body": "A new comment.", "user": {"login": "dave"}, "created_at": "2021",
             "html_url": f"{GITEA}/wekan/wekan/issues/1#issuecomment-11"}]}
        self.releases = [{"id": 1, "tag_name": "v1.0", "name": "One", "body": "Notes", "draft": False, "published_at": "2020-01-01T00:00:00Z",
                          "html_url": f"{GITEA}/wekan/wekan/releases/tag/v1.0", "assets": [{"name": "app.zip", "size": 3}]},
                         {"id": 2, "tag_name": "v2.0-draft", "draft": True, "assets": []}]
        self.repos = {"wekan/wekan": {"full_name": "wekan/wekan", "name": "wekan", "owner": {"login": "wekan"}, "private": False,
                                      "original_url": "https://github.com/wekan/wekan.git", "has_wiki": False,
                                      "default_branch": "main", "empty": False, "description": "d"},
                      "wekan/secret": {"full_name": "wekan/secret", "name": "secret", "owner": {"login": "wekan"}, "private": True,
                                       "original_url": "", "has_wiki": False, "default_branch": "main", "empty": False},
                      "other/x": {"full_name": "other/x", "name": "x", "owner": {"login": "other"}, "private": False}}
        # GitLab: issue 1 was already copied by WeKan's mirror, with one of its comments.
        self.gl_issues = {1: {"iid": 1, "state": "opened", "description": "<!-- wekan-mirror:https://github.com/wekan/wekan/issues/1 -->\n\nOne.",
                              "notes": [{"body": "<!-- wekan-mirror:https://github.com/wekan/wekan/issues/1#issuecomment-555 -->\n\n"
                                                 "A long enough comment that WeKan's mirror already copied from GitHub.\n\n---\nMirrored"}]}}
        self.gl_labels, self.gl_milestones, self.gl_releases, self.gl_links, self.uploads = [], [], {}, {}, []
        self.writes = []
        self.fail_issue = None

    def __call__(self, method, url, headers=None, json_body=None, form=None, body=None, timeout=600, retry=None):
        u = urllib.parse.urlsplit(url)
        path, q = urllib.parse.unquote(u.path), dict(urllib.parse.parse_qsl(u.query))
        if method != "GET":
            self.writes.append((method, path))
        if u.netloc == "gitea.test":
            return self.gitea(path.removeprefix("/api/v1"), q)
        return self.gitlab(method, path.removeprefix("/api/v4"), q, json_body, body)

    def page(self, items, q, size=50):
        p = int(q.get("page", 1))
        return 200, items[(p - 1) * size:p * size], {}

    def gitea(self, path, q):
        if path == "/orgs/wekan":
            return 200, {}, {}
        if path.startswith("/orgs/"):
            return (404, {"message": "no"}, {}) if not path.endswith("/repos") else self.page([r for r in self.repos.values()], q)
        if path == "/repos/search":
            p = int(q.get("page", 1))
            return 200, {"data": list(self.repos.values())[(p - 1) * 50:p * 50]}, {}
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        full, rest = m.group(1), m.group(2) or ""
        if rest == "":
            return 200, self.repos[full], {}
        if rest == "/issues":
            items = list(self.gitea_issues.values())
            return self.page(items, q)
        if rest in ("/labels",):
            return self.page([{"name": "bug", "color": "ff0000"}], q)
        if rest == "/milestones":
            return self.page([{"title": "v1", "state": "closed"}], q)
        if rest == "/releases":
            return self.page(self.releases, q)
        m = re.fullmatch(r"/issues/(\d+)/comments", rest)
        if m:
            return self.page(self.gitea_comments.get(int(m.group(1)), []), q)
        return 404, {"message": f"fake gitea: {path}"}, {}

    def gitlab(self, method, path, q, j, body):
        if path == "/projects/wekan/wekan" and method == "GET":
            return 200, {"id": 9, "visibility": "public", "default_branch": "main", "ssh_url_to_repo": "x", "http_url_to_repo": "x"}, {}
        if path.startswith("/projects/") and method == "GET" and path.count("/") == 2 and not path[10:].isdigit():
            return 404, {"message": "404 Project Not Found"}, {}
        if path == "/projects/9/labels":
            if method == "POST":
                self.gl_labels.append(j["name"])
                return 201, j, {}
            return self.page([{"name": n} for n in self.gl_labels], q, 100)
        if path == "/projects/9/milestones":
            if method == "POST":
                m = dict(j, id=len(self.gl_milestones) + 1, state="active")
                self.gl_milestones.append(m)
                return 201, m, {}
            return self.page(self.gl_milestones, q, 100)
        m = re.fullmatch(r"/projects/9/milestones/(\d+)", path)
        if m:
            self.gl_milestones[int(m.group(1)) - 1]["state"] = "closed"
            return 200, {}, {}
        if path == "/projects/9/issues":
            if method == "POST":
                if self.fail_issue and self.fail_issue in j["title"]:
                    return 500, {"message": "boom"}, {}
                iid = max(self.gl_issues, default=0) + 1
                self.gl_issues[iid] = {"iid": iid, "state": "opened", "title": j["title"], "description": j["description"],
                                       "labels": j.get("labels"), "milestone_id": j.get("milestone_id"), "notes": []}
                return 201, self.gl_issues[iid], {}
            return self.page(list(self.gl_issues.values()), q, 100)
        m = re.fullmatch(r"/projects/9/issues/(\d+)(/notes)?", path)
        if m:
            it = self.gl_issues[int(m.group(1))]
            if m.group(2):
                if method == "POST":
                    it["notes"].append({"body": j["body"]})
                    return 201, {}, {}
                return self.page(it["notes"], q, 100)
            if method == "PUT":
                it["state"] = "closed"
            return 200, it, {}
        if path == "/projects/9/uploads":
            self.uploads.append(body.length)
            return 201, {"full_path": "/-/project/9/uploads/abc/file.png"}, {}
        m = re.fullmatch(r"/projects/9/releases/(.+?)(/assets/links)?", path)
        if m:
            tag = m.group(1)
            if m.group(2):
                if method == "POST":
                    self.gl_links.setdefault(tag, []).append(j)
                    return 201, {}, {}
                return self.page(self.gl_links.get(tag, []), q, 100)
            return (200, self.gl_releases[tag], {}) if tag in self.gl_releases else (404, {"message": "404"}, {})
        if path == "/projects/9/releases":
            self.gl_releases[j["tag_name"]] = j
            return 201, j, {}
        if path.startswith("/projects/9/packages/generic/"):
            return 201, {}, {}
        return 404, {"message": f"fake gitlab: {method} {path}"}, {}


def engine(fake, here):
    os.makedirs(here, exist_ok=True)
    gm.http = fake
    e = gm.Engine(gl_mod.GitLab(), here)
    e.cfg.load()
    e.cfg.set("gitea", "url", GITEA)
    e.cfg.set("gitea", "token", "t")
    e.cfg.set("gitlab", "token", "gl")
    e.configure()
    e.load_state()
    e.download = lambda a: (lambda d: (os.makedirs(d, exist_ok=True), open(os.path.join(d, "f"), "w").write("abc"), os.path.join(d, "f"))[2])(
        os.path.join(TMP, "dl", a["name"]))
    gl_mod.GitLab.upload_attachment = lambda self, tracker, key: "https://gitlab.com/-/project/9/uploads/abc/file.png"
    return e


here = os.path.join(TMP, "gl")
os.makedirs(here)
fake = Fake()
e = engine(fake, here)

# sources
check("org source: only repositories the org owns", e.expand("wekan") == ["wekan/secret", "wekan/wekan"])
check("(allprivate): only private ones", e.expand("(allprivate)") == ["wekan/secret"])
check("(allpublic): only public ones", e.expand("(allpublic)") == ["other/x", "wekan/wekan"])

# dry run writes nothing
e.cfg.set("options", "dry_run", "yes")
fake.writes.clear()
e.run(["issues", "releases"], "wekan/wekan")
check("negative: a dry run writes nothing to GitLab", not [w for w in fake.writes if not w[1].startswith("/api/v1")])
check("a dry run plans the missing issue, comment and release", e.counts.get("planned", 0) >= 3)
e.cfg.set("options", "dry_run", "no")

# the real run
ok = e.run(["issues", "releases"], "wekan/wekan")
iss = fake.gl_issues
check("the run succeeds", ok)
check("issue #1 (copied earlier by WeKan's mirror) is not copied again", len([i for i in iss.values() if "issues/1 -->" in i["description"]]) == 1)
check("the PR became an issue titled [PR]", any(i.get("title") == "[PR] A PR" for i in iss.values()))
check("the PR's marker is its GitHub /pull/ URL", any("<!-- wekan-mirror:https://github.com/wekan/wekan/pull/2 -->" in i["description"] for i in iss.values()))
notes = iss[1]["notes"]
check("the comment WeKan already copied (no matching marker) is recognised by its text", len(notes) == 2)
check("the new comment is added with its marker", "issuecomment-11 -->" in notes[1]["body"])
check("the closed issue is closed at GitLab", iss[1]["state"] == "closed")
check("labels and milestones are created, a closed milestone closed", fake.gl_labels == ["bug"] and fake.gl_milestones[0]["state"] == "closed")
check("the release is created, with its file linked from the package registry",
      "v1.0" in fake.gl_releases and fake.gl_links["v1.0"][0]["name"] == "app.zip"
      and "/packages/generic/wekan/v1.0/app.zip" in fake.gl_links["v1.0"][0]["url"])
check("release notes carry the GitHub release URL as marker", fake.gl_releases["v1.0"]["description"].startswith(
      "<!-- wekan-mirror:https://github.com/wekan/wekan/releases/tag/v1.0 -->"))
check("negative: a draft release is not published", "v2.0-draft" not in fake.gl_releases)

# a second run copies nothing
fake.writes.clear()
e.restart = True
e.run(["issues", "releases"], "wekan/wekan")
e.restart = False
check("idempotent: a second run writes nothing", not fake.writes)

# a failed issue is tried again
fake.gitea_issues[3] = {"number": 3, "title": "Will fail", "body": "x", "state": "open", "user": {"login": "x"},
                        "created_at": "2022", "updated_at": "2022", "pull_request": None, "comments": 0, "labels": [],
                        "milestone": None, "html_url": f"{GITEA}/wekan/wekan/issues/3"}
fake.fail_issue = "Will fail"
since = e.state["wekan/wekan"]["issues"]["since"]
check("a run with a failed issue reports failure", not e.run(["issues"], "wekan/wekan"))
check("... and does not move 'since', so the next run reads it again", e.state["wekan/wekan"]["issues"]["since"] == since)
fake.fail_issue = None
e.run(["issues"], "wekan/wekan")
check("... which then copies it", any(i.get("title") == "Will fail" for i in fake.gl_issues.values()))

# resume after an interruption
calls = []
orig = gm.Engine.run_repo


def interrupting(self, full, steps):
    calls.append(full)
    if full == "wekan/wekan" and len(calls) == 2:
        raise KeyboardInterrupt


gm.Engine.run_repo = lambda self, full, steps: interrupting(self, full, steps)
e.run(["git"], "wekan")
calls.clear()
gm.Engine.run_repo = lambda self, full, steps: calls.append(full)
e.run(["git"], "wekan")
check("resume: the interrupted run continues with the repository it stopped at", calls == ["wekan/wekan"])
gm.Engine.run_repo = orig

# --- avatars ------------------------------------------------------------------------------------
fake.repos["wekan/wekan"]["owner"]["avatar_url"] = f"{GITEA}/avatars/org-wekan"
fake.repos["wekan/wekan"]["avatar_url"] = f"{GITEA}/repo-avatars/wekan"
images = {f"{GITEA}/avatars/org-wekan": b"\x89PNG-org", f"{GITEA}/repo-avatars/wekan": b"GIF8-repo"}
e.fetch_avatar = lambda url: images[url]
puts = []
orig_gitlab = fake.gitlab


def gitlab_with_avatars(method, path, q, j, body):
    if path == "/groups/wekan" and method == "GET":
        return 200, {"id": 5}, {}
    if method == "PUT" and path in ("/groups/5", "/projects/9") and body is not None:
        puts.append((path, os.path.basename(body.f.name)))
        return 200, {}, {}
    return orig_gitlab(method, path, q, j, body)


fake.gitlab = gitlab_with_avatars
e.counts = {}
e.run(["avatars"], "wekan/wekan")
check("avatars: the organization's onto its GitLab group, the repository's onto its project",
      sorted(puts) == [("/groups/5", "avatar.png"), ("/projects/9", "avatar.gif")])
puts.clear()
e.__dict__.pop("_avatar_owners", None)
e.run(["avatars"], "wekan/wekan")
check("avatars: the same images are not uploaded again", puts == [])
images[f"{GITEA}/avatars/org-wekan"] = b"\xff\xd8\xffnew"
e.__dict__.pop("_avatar_owners", None)
e.cfg.set("options", "dry_run", "yes")
e.run(["avatars"], "wekan/wekan")
check("negative: a dry run uploads no avatar", puts == [])
e.cfg.set("options", "dry_run", "no")
e.__dict__.pop("_avatar_owners", None)
e.run(["avatars"], "wekan/wekan")
check("avatars: a changed image is uploaded, as .jpg", puts == [("/groups/5", "avatar.jpg")])
check("image_extension", [gm.image_extension(x) for x in (b"\x89PNGx", b"GIF89a", b"RIFF1234WEBPx", b"<svg xmlns")] == [".png", ".gif", ".webp", ".svg"])
fake.gitlab = orig_gitlab

# --- git: pushed, never forced --------------------------------------------------------------
def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


src_parent = os.path.join(TMP, "giteadir", "wekan")
os.makedirs(src_parent)
work = os.path.join(TMP, "worktree")
git("init", "-q", "-b", "main", work)
git("-C", work, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "one")
git("-C", work, "tag", "v1")
git("clone", "-q", "--bare", work, os.path.join(src_parent, "proj.git"))
dest = os.path.join(TMP, "dest.git")
git("init", "-q", "--bare", "-b", "main", dest)
e.gitea = gm.Gitea(os.path.join(TMP, "giteadir"), "")
e.t.git_url = lambda handle, repo: dest
e.t.after_git = lambda handle, repo: None
e.counts = {}
repo = {"full_name": "wekan/proj", "name": "proj", "owner": {"login": "wekan"}, "has_wiki": False}
e.sync_git(repo, {})
check("git: branches and tags reach the destination", "refs/heads/main" in git("-C", dest, "show-ref") and "refs/tags/v1" in git("-C", dest, "show-ref"))
# the destination moves its own way; the source gets a new commit and tag
other = os.path.join(TMP, "other")
git("clone", "-q", dest, other)
git("-C", other, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "destination only")
git("-C", other, "push", "-q", "origin", "main")
dest_main = git("-C", dest, "rev-parse", "main")
git("-C", work, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "two")
git("-C", work, "tag", "v2")
git("-C", work, "push", "-q", os.path.join(src_parent, "proj.git"), "main", "v2")
e.counts = {}
e.sync_git(repo, {})
check("negative: a destination branch that moved its own way is not overwritten", git("-C", dest, "rev-parse", "main") == dest_main)
check("... it is reported as failed", e.counts.get("failed") == 1)
check("... and the new tag still goes", "refs/tags/v2" in git("-C", dest, "show-ref"))

# --- SourceForge: tools, tickets, comments, files ------------------------------------------------
class FakeSF(Fake):
    def __init__(self):
        super().__init__()
        self.tools = [{"mount_point": "code", "name": "git"}]
        self.tickets, self.saved, self.files = {}, [], {}
        self.encodings, self.auth, self.admin, self.paths = {}, set(), True, []

    def __call__(self, method, url, headers=None, json_body=None, form=None, body=None, timeout=600, retry=None, multipart=False):
        u = urllib.parse.urlsplit(url)
        if u.netloc == "gitea.test":
            return super().__call__(method, url, headers, json_body, form, body, timeout, retry)
        path, q = urllib.parse.unquote(u.path).removeprefix("/rest/p/"), dict(urllib.parse.parse_qsl(u.query))
        self.auth.add((headers or {}).get("Authorization"))
        self.paths.append(path)
        if method != "GET":
            self.writes.append((method, path))
            # https://sourceforge.net/api-docs/: tickets' new and save are multipart/form-data.
            self.encodings.setdefault(path.rsplit("/", 1)[-1] if re.search(r"/(new|save)$", path) and "_discuss" not in path
                                      else "other", set()).add("multipart" if multipart else "urlencoded")
        if path == "wekan":
            return 200, {"tools": self.tools}, {}
        if path == "wekan/has_access":
            return (200, {"result": True}, {}) if self.admin else (403, {"error": "forbidden"}, {})
        if re.fullmatch(r"wekan/[\w-]+", path):
            p = int(q.get("page", 0))
            return 200, {"tickets": [{"ticket_num": n} for n in self.tickets][p * 100:(p + 1) * 100]}, {}
        if path == "wekan/admin/install_tool":
            self.tools.append({"mount_point": form["mount_point"], "name": form["tool"]})
            return 200, {"success": True}, {}
        m = re.fullmatch(r"wekan/([\w-]+)/(.*)", path)
        tracker, rest = m.group(1), m.group(2)
        if rest == "new":
            n = len(self.tickets) + 1
            self.tickets[n] = {"ticket_num": n, "summary": form["ticket_form.summary"], "description": form["ticket_form.description"],
                               "status": form["ticket_form.status"], "labels": form["ticket_form.labels"].split(",") if form["ticket_form.labels"] else [],
                               "discussion_thread": {"_id": f"t{n}"}, "posts": []}
            return 200, {"ticket": {"ticket_num": n}}, {}
        m = re.fullmatch(r"(\d+)(/save)?", rest)
        if m:
            t = self.tickets[int(m.group(1))]
            if m.group(2):
                self.saved.append(form)
                t["status"] = form["ticket_form.status"]
            return 200, {"ticket": t}, {}
        m = re.fullmatch(r"_discuss/thread/t(\d+)(/new)?", rest)
        t = self.tickets[int(m.group(1))]
        if m.group(2):
            t["posts"].append({"text": form["text"]})
            return 200, {"post": {}}, {}
        return 200, {"thread": {"posts": t["posts"]}}, {}


sf_here = os.path.join(TMP, "sf")
os.makedirs(os.path.join(sf_here, "work"))
fsf = FakeSF()
gm.http = fsf
s = gm.Engine(sf_mod.SourceForge(), sf_here)
s.cfg.load()
for sec, k, v in (("gitea", "url", GITEA), ("gitea", "token", "t"), ("sourceforge", "token", "0123abcd"), ("sourceforge", "user", "wekan")):
    s.cfg.set(sec, k, v)
s.configure()
s.load_state()
s.download = e.download
scripts = []


def fake_sftp(script):
    scripts.append(script)
    if script.startswith("-ls"):
        d = re.search(r'"([^"]+)"', script).group(1)
        return "\n".join(fsf.files.get(d, []))
    for dest in re.findall(r'rename "[^"]+" "([^"]+)"', script):
        fsf.files.setdefault(os.path.dirname(dest), []).append(os.path.basename(dest))
    return ""


s.t.sftp = fake_sftp
ok = s.run(["issues", "releases"], "wekan/wekan")
check("SourceForge: the run succeeds", ok)
check("SourceForge: the repository named like the project uses the tool 'code'", s.t.describe("wekan", {"name": "wekan"}).endswith("/p/wekan/code"))
check("SourceForge: a tracker is created when the project has none", any(t["name"] == "tickets" for t in fsf.tools))
check("SourceForge: both items become tickets, the PR titled [PR]",
      sorted(t["summary"] for t in fsf.tickets.values()) == ["First", "[PR] A PR"])
first = next(t for t in fsf.tickets.values() if t["summary"] == "First")
check("SourceForge: comments are added with their markers", len(first["posts"]) == 2 and "issuecomment-11 -->" in first["posts"][1]["text"])
check("SourceForge: tickets are created and saved as multipart/form-data (api-docs)",
      fsf.encodings.get("new") == {"multipart"} and fsf.encodings.get("save") == {"multipart"})
check("SourceForge: comments and tools are posted urlencoded (api-docs)", fsf.encodings.get("other") == {"urlencoded"})
check("SourceForge: every request carries the bearer token", fsf.auth == {"Bearer 0123abcd"})
check("SourceForge: the ticket list is read at /p/<project>/<tracker>, without a trailing slash",
      bool(fsf.tickets) and not [x for x in fsf.paths if x.endswith("/")] and any(re.fullmatch(r"wekan/[\w-]+", x) for x in fsf.paths))
check("SourceForge: booleans are saved as \"true\"/\"false\"",
      fsf.saved and fsf.saved[0]["ticket_form.private"] == "false" and fsf.saved[0]["ticket_form.discussion_disabled"] == "false")
check("SourceForge: closing saves the whole form, not only the status",
      fsf.saved and fsf.saved[0]["ticket_form.status"] == "closed" and fsf.saved[0]["ticket_form.summary"] == "First")
put = [x for x in scripts if "put " in x]
check("SourceForge: release files go to <files_dir>/<tag>/ as .part, then renamed",
      any('"/home/frs/project/wekan/GitHub-releases/v1.0/app.zip.part"' in x and 'rename' in x for x in put))
check("SourceForge: the release notes are README.md", fsf.files.get("/home/frs/project/wekan/GitHub-releases/v1.0", [])[:1] == ["README.md"])
fsf.writes.clear()
scripts.clear()
s.restart = True
s.run(["issues", "releases"], "wekan/wekan")
check("SourceForge: idempotent, a second run writes nothing", not fsf.writes and not [x for x in scripts if "put " in x])
s.counts = {}
s.counts = {}
s.t.owner_avatar("wekan", "/x.png"); s.t.repo_avatar({"project": "wekan"}, {}, "/x.png")
check("SourceForge: avatars are reported once as not possible, not failed", s.counts == {"skipped": 1})
s.counts = {}
check("negative: SourceForge refuses a private repository", s.t.ensure_repo("wekan", fsf.repos["wekan/secret"]) is None
      and s.counts.get("skipped") == 1)

# Custom fields as the API names them: ticket_form.custom_fields._<name>, booleans "true"/"false".
fsf.tickets[1]["custom_fields"] = {"_milestone": "v1", "priority": "5", "_done": True}
fsf.saved.clear()
s.t.close_issue({"project": "wekan", "mount": "x"}, 1)
cf = {k: v for k, v in fsf.saved[0].items() if k.startswith("ticket_form.custom_fields.")}
check("SourceForge: custom fields are saved as ticket_form.custom_fields._<name>",
      cf == {"ticket_form.custom_fields._milestone": "v1", "ticket_form.custom_fields._priority": "5",
             "ticket_form.custom_fields._done": "true"})

# multipart/form-data as sent: every field a part, the value unchanged.
mp_body, mp_type = gm.multipart_form({"ticket_form.summary": "Ä title", "ticket_form.private": "false"})
import email.parser
msg = email.parser.BytesParser().parsebytes(b"Content-Type: " + mp_type.encode() + b"\r\n\r\n" + mp_body)
parts = {pt.get_param("name", header="content-disposition"): pt.get_payload(decode=True).decode() for pt in msg.get_payload()}
check("gm.multipart_form encodes each field as a form-data part", mp_type.startswith("multipart/form-data; boundary=")
      and parts == {"ticket_form.summary": "Ä title", "ticket_form.private": "false"})

# check_online asks has_access whether the token may administer the project (install_tool needs it).
s.cfg.set("sources", "list", "wekan")
s.t.sftp = lambda script: ""
res = s.t.check_online(s)
check("SourceForge: Check access says the token may administer the project", any("may administer" in m and good for m, good in res))
fsf.admin = False
res = s.t.check_online(s)
check("negative: Check access reports a token that may not administer the project",
      any("may not administer" in m and not good for m, good in res))
check("... asked with has_access?user=<user>&perm=admin", "wekan/has_access" in fsf.paths)
s.t.sftp = fake_sftp

# negative: a token that is not a SourceForge bearer token is refused before any request.
s.cfg.set("sourceforge", "token", "Bearer not-hex!")
try:
    s.t.configure(s)
    refused = False
except SystemExit as err:
    refused = "hexadecimal" in str(err)
check("negative: a token that is not hexadecimal is refused", refused)
s.cfg.set("sourceforge", "token", "0123abcd")
s.t.configure(s)

# --- releases: only the newest, unless "Releases kept in full" lists the repository ----------
def three_releases(f):
    f.releases = [{"id": i, "tag_name": t, "name": t, "body": "", "draft": False, "published_at": "2020",
                   "html_url": f"{GITEA}/wekan/wekan/releases/tag/{t}", "assets": [{"name": f"{t}.zip", "size": 3}]}
                  for i, t in ((3, "v3"), (2, "v2"), (1, "v1"))]  # Gitea: newest first


class FakeOld(Fake):
    """GitLab already has v1 with its file, from an earlier run."""

    def __init__(self):
        super().__init__()
        three_releases(self)
        self.gl_releases["v1"] = {"tag_name": "v1"}
        self.gl_links["v1"] = [{"id": 71, "name": "v1.zip"}]
        self.packages = [{"id": 81, "name": "wekan", "version": "v1"}, {"id": 82, "name": "wekan", "version": "v3x"}]
        self.package_files = {81: [{"id": 91, "file_name": "v1.zip"}]}
        self.deleted = []

    def gitlab(self, method, path, q, j, body):
        if method == "DELETE":
            self.deleted.append(path)
            return 204, None, {}
        if path == "/projects/9/packages":
            return self.page([p for p in self.packages if p["name"] == q.get("package_name")], q, 100)
        m = re.fullmatch(r"/projects/9/packages/(\d+)/package_files", path)
        if m:
            return self.page(self.package_files.get(int(m.group(1)), []), q, 100)
        if path == "/projects/9" and method == "PUT":
            self.put_project = j
            return 200, {}, {}
        return super().gitlab(method, path, q, j, body)


f3 = FakeOld()
e3 = engine(f3, os.path.join(TMP, "gl3"))
e3.run(["releases"], "wekan/wekan")
check("GitLab: by default only the newest release is copied", sorted(f3.gl_releases) == ["v1", "v3"]
      and [l["name"] for l in f3.gl_links["v3"]] == ["v3.zip"])
check("GitLab: an older release's file link and package are deleted, the release stays",
      sorted(f3.deleted) == ["/projects/9/packages/81", "/projects/9/releases/v1/assets/links/71"] and "v1" in f3.gl_releases)
check("negative: another version's package is not deleted", "/projects/9/packages/82" not in f3.deleted)

for item in ("wekan/wekan", "WEKAN", "(all)", "(allpublic)"):
    f4 = FakeOld()
    e4 = engine(f4, os.path.join(TMP, "gl4-" + re.sub(r"\W", "", item)))
    e4.cfg.set_releases_all(["other", item])
    e4.run(["releases"], "wekan/wekan")
    check(f"GitLab: listed as {item}, every release is copied and nothing is deleted",
          sorted(f4.gl_releases) == ["v1", "v2", "v3"] and f4.deleted == [])
f5 = FakeOld()
e5 = engine(f5, os.path.join(TMP, "gl5"))
e5.cfg.set_releases_all(["(allprivate)", "wekan/wekan2", "wek"])
e5.run(["releases"], "wekan/wekan")
check("negative: (allprivate), a longer name and a shorter owner do not match", sorted(f5.gl_releases) == ["v1", "v3"])
f6 = FakeOld()
e6 = engine(f6, os.path.join(TMP, "gl6"))
e6.cfg.set("options", "release_assets_keep", "2")
e6.run(["releases"], "wekan/wekan")
check("GitLab: release_assets_keep = 2 copies the two newest", sorted(f6.gl_releases) == ["v1", "v2", "v3"]
      and "/projects/9/releases/v1/assets/links/71" in f6.deleted)
f7 = FakeOld()
e7 = engine(f7, os.path.join(TMP, "gl7"))
e7.cfg.set("options", "dry_run", "yes")
e7.configure()
e7.run(["releases"], "wekan/wekan")
check("negative: a dry run deletes nothing", f7.deleted == [])

# A release of many versions: only the newest version's files are copied, and an older
# version's file already at GitLab loses its link and its package file - not the package.
check("gitea-to-*: the same version detection as github-to-gitea",
      gm.newest_version_files(["r-1.2.0-a.deb", "r-1.4.3-a.deb", "r-1.4.3-b.apk", "SUMS"]) ==
      ({"r-1.4.3-a.deb", "r-1.4.3-b.apk", "SUMS"}, "1.4.3"))
fv = FakeOld()
fv.releases = [{"id": 5, "tag_name": "nightly", "name": "nightly", "body": "", "draft": False, "published_at": "2020",
                "html_url": f"{GITEA}/wekan/wekan/releases/tag/nightly",
                "assets": [{"name": n, "size": 3} for n in ("r-1.2.0-a.deb", "r-1.4.3-a.deb", "r-1.4.3-b.apk")]}]
fv.gl_releases["nightly"] = {"tag_name": "nightly"}
fv.gl_links["nightly"] = [{"id": 72, "name": "r-1.2.0-a.deb"}]
fv.packages = [{"id": 83, "name": "wekan", "version": "nightly"}]
fv.package_files = {83: [{"id": 93, "file_name": "r-1.2.0-a.deb"}, {"id": 94, "file_name": "r-1.4.3-a.deb"}]}
ev = engine(fv, os.path.join(TMP, "glv"))
ev.run(["releases"], "wekan/wekan")
check("gitea-to-*: only the newest version's files are copied",
      sorted(l["name"] for l in fv.gl_links["nightly"]) == ["r-1.2.0-a.deb", "r-1.4.3-a.deb", "r-1.4.3-b.apk"]
      and "/projects/9/releases/nightly/assets/links/72" in fv.deleted)
check("gitea-to-*: an older version's package file is deleted, not the whole package",
      "/projects/9/packages/83/package_files/93" in fv.deleted and "/projects/9/packages/83" not in fv.deleted)

# The menu: add, refuse a duplicate and a bad name, remove.
answers = iter(["a", "(ALL)", "a", "http://gitea.test/wekan/wekan.git", "a", "Wekan/Wekan", "a", "bad name!", "r", "1", "0"])
real_ask = gm.ask
gm.ask = lambda prompt, default="": next(answers)
import contextlib
import io
with contextlib.redirect_stdout(io.StringIO()) as out:
    gm.menu_releases_all(e3)
gm.ask = real_ask
check("the menu adds, refuses a duplicate or a bad name, and removes", e3.cfg.releases_all() == ["wekan/wekan"])
check("the list is saved in the settings file", "[releases_all]" in open(e3.ini, encoding="utf-8").read())

# The GitLab wiki is turned on when it is off, so the copied wiki shows in the Wiki tab.
gm.http = f3
url = e3.t.wiki_url({"id": 9, "wiki_enabled": False, "path_with_namespace": "wekan/wekan", "ssh_url_to_repo": "g:wekan/wekan.git",
                     "http_url_to_repo": "https://gitlab.test/wekan/wekan.git"}, f3.repos["wekan/wekan"])
check("GitLab: a project's wiki that is off is turned on, and pushed to",
      getattr(f3, "put_project", None) == {"wiki_access_level": "enabled"} and url.endswith(".wiki.git"))
src = open(os.path.join(ROOT, "mirror", "common", "gitea_mirror.py"), encoding="utf-8").read()
body = src[src.index("    def sync_git("):src.index("    # issues")]
check("negative: the destination's wiki is turned on only after Gitea's wiki is found to have pages",
      body.index("ls-remote") < body.index("self.t.wiki_url("))

# SourceForge: an older release keeps README.md (its notes) and loses its files.
fsf2 = FakeSF()
three_releases(fsf2)
fsf2.files["/home/frs/project/wekan/GitHub-releases/v1"] = ["README.md", "v1.zip"]
gm.http = fsf2
os.makedirs(os.path.join(TMP, "sf2"))
s2 = gm.Engine(sf_mod.SourceForge(), os.path.join(TMP, "sf2"))
s2.cfg.load()
for sec, k, v in (("gitea", "url", GITEA), ("gitea", "token", "t"), ("sourceforge", "token", "0123abcd"), ("sourceforge", "user", "wekan")):
    s2.cfg.set(sec, k, v)
s2.configure()
s2.load_state()
s2.download = e.download
scripts.clear()
fsf = fsf2
s2.t.sftp = fake_sftp
s2.run(["releases"], "wekan/wekan")
rms = [x for x in scripts if x.startswith("-rm") and ".part" not in x]
check("SourceForge: an older release's files are removed, its README.md stays",
      rms == ['-rm "/home/frs/project/wekan/GitHub-releases/v1/v1.zip"\n'])
check("SourceForge: only the newest release is uploaded",
      not any("/v2/" in x for x in scripts if "put " in x) and any("/v3/v3.zip" in x for x in scripts if "put " in x))

# A release file is downloaded to MIRROR_TMP_DIR (WeKan's .tools/tmp), never /tmp, and deleted.
src_file = os.path.join(TMP, "asset.bin")
open(src_file, "wb").write(b"abc")
e8 = gm.Engine(gl_mod.GitLab(), os.path.join(TMP, "gl8"))
e8.gitea = type("G", (), {"headers": lambda self: {}})()
path = gm.Engine.download(e8, {"name": "../x.zip", "size": 3, "browser_download_url": "file://" + src_file})
check("a release file is downloaded under MIRROR_TMP_DIR/<script>",
      path.startswith(os.path.join(TMP, "mirror-tmp") + os.sep) and open(path, "rb").read() == b"abc")
check("negative: a file name cannot climb out of the directory", os.path.basename(path) != ".." and "/../" not in path)
os.environ.pop("MIRROR_TMP_DIR")
check("without MIRROR_TMP_DIR, files go to WeKan's .tools/tmp/<script>",
      gm.mirror_tmp_dir("gitea-to-gitlab").endswith(os.path.join(".tools", "tmp", "gitea-to-gitlab")))
os.environ["MIRROR_TMP_DIR"] = os.path.join(TMP, "mirror-tmp")

# Release files of any size by default; the old default 5000 becomes 0, once.
check("gitea-to-*: assets_max_mb's default is -1, no limit", e3.cfg.get("assets_max_mb") == "-1")
for content, want in (("[options]\nassets_max_mb = 5000\n", "-1"), ("[options]\nassets_max_mb = 300\n", "300"),
                      ("[options]\nassets_max_mb = 0\n[migrations]\nassets_max_mb_unlimited = done\n", "-1"),
                      ("[options]\nassets_max_mb = 0\n[migrations]\nassets_max_mb_no_limit = done\n", "0"),
                      ("[options]\nassets_max_mb = 5000\n[migrations]\nassets_max_mb_no_limit = done\n", "5000")):
    d = os.path.join(TMP, "ini-" + str(abs(hash(content))))
    os.makedirs(d)
    m = gm.Engine(gl_mod.GitLab(), d)
    with open(m.ini, "w") as f:
        f.write(content)
    m.cfg.load()
    check(f"gitea-to-*: {content.split(chr(10))[1]}" + (" after the migration" if "no_limit = done" in content else
                                                      " from 0-is-no-limit" if "done" in content else "") + f" -> {want}",
          m.cfg.get("assets_max_mb") == want)
m.cfg.load()
check("gitea-to-*: the migration is saved, so it runs once", "assets_max_mb_no_limit = done" in open(m.ini).read())

# -1 copies any size, 0 copies no file, N skips a larger one.
for value, want in (("-1", ["v3.zip"]), ("0", []), ("1", [])):
    fz = FakeOld()
    fz.releases[0]["assets"][0]["size"] = 5 * 1048576
    ez = engine(fz, os.path.join(TMP, "glz" + value))
    ez.cfg.set("options", "assets_max_mb", value)
    ez.run(["releases"], "wekan/wekan")
    check(f"gitea-to-*: assets_max_mb = {value} copies {want or 'no file'} of 5 MB",
          [l["name"] for l in fz.gl_links.get("v3", [])] == want)

print(f"\n{failures} failure(s)")
subprocess.run(["rm", "-rf", TMP])
sys.exit(1 if failures else 0)

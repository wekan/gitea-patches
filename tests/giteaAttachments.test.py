#!/usr/bin/env python3
"""mirror/common/gitea_attachments.py: how much space a Gitea organization's, user's or
repository's attachments take, the largest repositories, and deleting the attachments -
against a fake Gitea, so nothing is deleted anywhere. Also that github-to-gitea and the
mirror scripts both offer it, and that Sources can leave a repository out.

  python3 -I tests/giteaAttachments.test.py
"""

import os
import re
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mirror", "common"))
import gitea_attachments as ga  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


MB = 1048576


def asset(i, size):
    return {"id": i, "name": f"file{i}.bin", "size": size}


class FakeGitea:
    """Organization microsoft (repositories verybig, small, other-owner's fork listed
    under it), user xet7 (notes), and every attachment kind."""

    def __init__(self):
        self.repos = {
            "microsoft/verybig": {"full_name": "microsoft/verybig", "owner": {"login": "microsoft"}, "size": 3 * 1024 * 1024},
            "microsoft/small": {"full_name": "microsoft/small", "owner": {"login": "microsoft"}, "size": 10},
            "xet7/notes": {"full_name": "xet7/notes", "owner": {"login": "xet7"}, "size": 2048},
        }
        self.releases = {
            "microsoft/verybig": [{"id": 1, "tag_name": "v2", "assets": [asset(11, 500 * MB), asset(12, 100 * MB)]},
                                  {"id": 2, "tag_name": "v1", "assets": [asset(13, 50 * MB)]}],
            "microsoft/small": [{"id": 3, "tag_name": "v1", "assets": [asset(31, 1 * MB)]}],
            "xet7/notes": [],
        }
        # 120 issues: more than two pages of 50.
        self.issues = {"microsoft/small": [{"id": 100 + n, "number": n, "assets": [asset(1000 + n, 1000)] if n % 40 == 0 else []}
                                           for n in range(1, 121)],
                       "microsoft/verybig": [], "xet7/notes": [{"id": 500, "number": 3, "assets": [asset(501, 2 * MB)]}]}
        self.comments = {"microsoft/small": [{"id": 900, "issue_url": "http://g/api/v1/repos/microsoft/small/issues/40",
                                              "assets": [asset(901, 3000)]}],
                         "microsoft/verybig": [], "xet7/notes": []}
        self.calls = []
        self.deny = set()

    def page(self, items, q):
        limit, page = int(q.get("limit", ["50"])[0]), int(q.get("page", ["1"])[0])
        return 200, items[(page - 1) * limit:page * limit]

    def __call__(self, method, path):
        self.calls.append((method, path))
        u = urllib.parse.urlsplit(path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        if method == "DELETE":
            if p in self.deny:
                return 403, {"message": "forbidden"}
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/releases/(\d+)/assets/(\d+)", p)
            if m:
                for r in self.releases[m.group(1)]:
                    if r["id"] == int(m.group(2)):
                        r["assets"] = [a for a in r["assets"] if a["id"] != int(m.group(3))]
                return 204, None
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/assets/(\d+)", p)
            if m:
                for i in self.issues[m.group(1)]:
                    if i["number"] == int(m.group(2)):
                        i["assets"] = [a for a in i["assets"] if a["id"] != int(m.group(3))]
                return 204, None
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/comments/(\d+)/assets/(\d+)", p)
            if m:
                for c in self.comments[m.group(1)]:
                    if c["id"] == int(m.group(2)):
                        c["assets"] = [a for a in c["assets"] if a["id"] != int(m.group(3))]
                return 204, None
            return 404, None
        if p == "/orgs/microsoft":
            return 200, {"username": "microsoft"}
        if p in ("/users/xet7", "/users/microsoft"):
            return 200, {}
        if p == "/orgs/microsoft/repos":
            # Gitea can list a repository of another owner here (a transfer); it is not microsoft's.
            return self.page([self.repos["microsoft/verybig"], self.repos["microsoft/small"],
                              {"full_name": "someone/else", "owner": {"login": "someone"}, "size": 1}], q)
        if p == "/users/xet7/repos":
            return self.page([self.repos["xet7/notes"]], q)
        if p.startswith("/orgs/"):
            return 404, None
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", p)
        if m and m.group(1) in self.repos:
            rest = m.group(2) or ""
            if rest == "":
                return 200, self.repos[m.group(1)]
            if rest == "/releases":
                return self.page(self.releases[m.group(1)], q)
            if rest == "/issues":
                return self.page(self.issues[m.group(1)], q)
            if rest == "/issues/comments":
                return self.page(self.comments[m.group(1)], q)
        return 404, None


def answers(*values):
    it = iter(values)
    return lambda prompt, default="": next(it, default) or default


said = []
say = said.append

# --- sizes and the largest ------------------------------------------------------------------
check("sizes read as people say them", [ga.human(0), ga.human(1536), ga.human(5 * MB), ga.human(3 * 1024 ** 3)]
      == ["0 B", "2 KB", "5.0 MB", "3.0 GB"])
f = FakeGitea()
repos = ga.scope_repos(f, "microsoft")
check("an organization: only the repositories it owns", [r["full_name"] for r in repos] == ["microsoft/small", "microsoft/verybig"])
check("a user's repositories", [r["full_name"] for r in ga.scope_repos(f, "xet7")] == ["xet7/notes"])
check("one repository", [r["full_name"] for r in ga.scope_repos(f, "microsoft/verybig")] == ["microsoft/verybig"])
for bad in ("nobody", "microsoft/none"):
    try:
        ga.scope_repos(f, bad)
        check(f"negative: {bad} is refused", False)
    except ga.AttachmentError:
        check(f"negative: {bad} is refused", True)

items = ga.repo_attachments(f, "microsoft/small")
check("every kind is found: releases, issues past the first page, comments",
      sorted(a["kind"] for a in items) == ["comments", "issues", "issues", "issues", "releases"])
check("... each with its place and its delete path",
      any(a["where"] == "#120" and a["path"] == "/repos/microsoft/small/issues/120/assets/1120" for a in items)
      and any(a["where"] == "#40 comment 900" and a["path"] == "/repos/microsoft/small/issues/comments/900/assets/901" for a in items))
f.calls.clear()
check("only the kinds asked for are read", [a["kind"] for a in ga.repo_attachments(f, "microsoft/small", ("releases",))] == ["releases"]
      and not any("/issues" in p for _, p in f.calls))

rows = ga.usage(f, repos + ga.scope_repos(f, "xet7"))
by = {r["name"]: r for r in rows}
check("git size is Gitea's size in KiB, as bytes", by["microsoft/verybig"]["git"] == 3 * 1024 ** 3)
check("attachments are summed per repository and per kind",
      by["microsoft/verybig"]["attachments"] == 650 * MB and by["microsoft/small"]["per_kind"]["issues"] == 3000
      and by["microsoft/small"]["per_kind"]["comments"] == 3000 and by["xet7/notes"]["files"] == 1)
check("the largest first, by git and attachments together",
      [r["name"] for r in ga.largest(rows, 2)] == ["microsoft/verybig", "xet7/notes"])
check("... or by attachments", [r["name"] for r in ga.largest(rows, 3, by="attachments")][0] == "microsoft/verybig")
text = ga.report(rows, 2, excluded=["microsoft/verybig"])
check("the report: totals, then only as many as asked for",
      text.startswith("3 repositories: git 3.0 GB, attachments 653.0 MB in 9 files")
      and "The 2 largest" in text and "microsoft/small " not in text.split("The 2 largest")[1])
check("... and an excluded one is marked, not hidden", "microsoft/verybig" in text and "(excluded in Sources)" in text)

said.clear()
ga.space_used(f, answers("microsoft/verybig", "1 2 3", "2"), say)
out = "\n".join(said)
check("space used of one repository lists its largest files", "The 2 largest files of microsoft/verybig:" in out
      and out.index("file11.bin") < out.index("file12.bin") and "file13.bin" not in out)

# --- excluding ------------------------------------------------------------------------------
check("an excluded repository is left out of its owner",
      ga.apply_excludes("microsoft", ["microsoft/verybig", "microsoft/small"], ["Microsoft/VeryBig"])
      == (["microsoft/small"], ["microsoft/verybig"]))
check("negative: a repository asked for by name is not left out",
      ga.apply_excludes("microsoft/verybig", ["microsoft/verybig"], ["microsoft/verybig"]) == (["microsoft/verybig"], []))
check("exclusions are read from the settings' list", ga.parse_excludes("\na/b, c/d\n\n") == ["a/b", "c/d"])
check("a scope is typed as a name or a Gitea address", ga.normalize_scope("http://localhost:2000/wekan/wekan.git/") == "wekan/wekan"
      and ga.normalize_scope("../x") == "" and ga.normalize_scope("wekan/..") == "" and ga.normalize_scope("a b") == "")

# --- deleting -------------------------------------------------------------------------------
f = FakeGitea()
said.clear()
ga.delete_attachments(f, answers("microsoft", "1", "yes"), say, excludes=["microsoft/verybig"])
check("negative: 'yes' is not enough - the scope's name must be typed", not any(m == "DELETE" for m, _ in f.calls)
      and "Nothing deleted." in said)
check("... and what would go is shown first, the excluded repository left out",
      any("microsoft/small: 1 files" in s for s in said) and any("microsoft/verybig: excluded" in s for s in said)
      and not any(s.startswith("  microsoft/verybig: ") and "files" in s for s in said))

f = FakeGitea()
said.clear()
ga.delete_attachments(f, answers("microsoft", "1 2 3", "microsoft"), say, dry=True)
check("a dry run deletes nothing and says what it would", not any(m == "DELETE" for m, _ in f.calls)
      and "Dry run: nothing is deleted." in said and any(s.startswith("Would delete 8 files, 651.0 MB") for s in said)
      and any(s.startswith("    [dry-run] delete microsoft/verybig release v2: file11.bin") for s in said))

f = FakeGitea()
said.clear()
ga.delete_attachments(f, answers("microsoft", "1", "microsoft"), say, excludes=["microsoft/verybig"],
                      note="NOTE: the next sync brings back the newest release's files.")
deleted = [p for m, p in f.calls if m == "DELETE"]
check("the release files of the organization go, the excluded repository's stay",
      deleted == ["/repos/microsoft/small/releases/3/assets/31"] and len(f.releases["microsoft/verybig"][0]["assets"]) == 2)
check("... the issue and comment files were not asked for and stay",
      sum(len(i["assets"]) for i in f.issues["microsoft/small"]) == 3 and f.comments["microsoft/small"][0]["assets"])
check("... and the script's note follows", said[-1].startswith("NOTE:") and any(s.startswith("Deleted 1 files, 1.0 MB") for s in said))

f = FakeGitea()
f.deny.add("/repos/microsoft/verybig/releases/1/assets/12")
said.clear()
ga.delete_attachments(f, answers("microsoft/verybig", "1", "microsoft/verybig"), say)
check("one repository by name; a file Gitea refuses is reported and the others still go",
      [a["id"] for r in f.releases["microsoft/verybig"] for a in r["assets"]] == [12]
      and any("could not be deleted" in s for s in said) and any("not deleted:" in s and "403" in s for s in said))

f = FakeGitea()
said.clear()
ga.delete_attachments(f, answers("xet7", "2", "xet7"), say)
check("a user's issue files", f.issues["xet7/notes"][0]["assets"] == [] and any(s.startswith("Deleted 1 files, 2.0 MB") for s in said))

said.clear()
ga.delete_attachments(FakeGitea(), answers("xet7", "3"), say)
check("nothing of that kind: nothing asked, nothing deleted", said[-1] == "xet7: no comment files.")

# A list endpoint that ignores paging cannot loop for ever.
loops = {"n": 0}


def same_page(method, path):
    loops["n"] += 1
    return 200, [{"id": i} for i in range(50)]


check("an endpoint that ignores page ends after a page with nothing new",
      len(list(ga.paged(same_page, "/x"))) == 50 and loops["n"] == 2)

# --- both scripts offer it ------------------------------------------------------------------
gtg = open(os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py"), encoding="utf-8").read()
common = open(os.path.join(ROOT, "mirror", "common", "gitea_mirror.py"), encoding="utf-8").read()
check("github-to-gitea's menu: Gitea's attachments, with the old every-repository release cleanup in it",
      "10) Gitea's attachments: space used, largest repositories, and deleting them" in gtg
      and re.search(r"gitea_attachments\.menu\([\s\S]*?menu_prune\)", gtg))
check("... and its Sources exclude through the same filter", "gitea_attachments.apply_excludes(src," in gtg
      and 'x) Exclude a repository' in gtg)
check("the mirror scripts' menu offers it after their own items, with only Gitea set up",
      "Gitea's attachments: space used, largest repositories, and deleting them" in common
      and re.search(r"elif c == str\(attachments\):[\s\S]*?gitea_attachments\.menu\(e\.gitea\.request", common))
check("... and their Sources exclude in expand", "gitea_attachments.apply_excludes(src, out, self.cfg.excludes())" in common)
check("negative: nothing here deletes without the typed name or outside a dry run's say-so",
      len(re.findall(r'request\("DELETE"', open(os.path.join(ROOT, "mirror", "common", "gitea_attachments.py")).read())) == 1)

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)

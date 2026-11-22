"""Backing up a GitHub organization or user straight from GitHub - no Gitea at all -
into the backup format of gitea_backup.py (FORMAT 1), and bringing such a backup up
to date.

  update(gh, name, root, say, excludes)

updates NAME's one backup, <root>/<NAME>/<NAME>.db and .zip, IN PLACE with what is
new or changed on GitHub (gitea_backup.updating: the lock, the dated backups of
older versions combined first, the zip written again with the kept entries as they
are, renamed into place when whole). When there is none, it makes a whole one; a
backup made from Gitea is brought up to date the same way, its git reused.

What is read again, and what is kept:

  repositories   every one GitHub lists (new ones too); topics are in GitHub's JSON
  labels, milestones, collaborators, teams, members
                 listed again - they are small - and kept as GitHub has them now
  issues, pull requests, comments
                 only those updated since the last run from GitHub began (GitHub's
                 `since`, meta github_updated); the rest are kept as they are, rows
                 and files - and count as still at the source
  pull request details
                 only for new and changed pull requests
  releases       listed again; a release file the backup has with the same id,
                 size and updated_at is kept as it is, and only the others are
                 downloaded. A release GitHub no longer has is kept, as old data.
  files in text  github.com/user-attachments/..., github.com/<owner>/<repo>/files
                 and /assets, and user-images.githubusercontent.com links in issue and
                 comment bodies: downloaded into the issues/ and comments/ folders and
                 recorded in `files` with their SHA-256. A link that cannot be fetched
                 is reported, listed in meta missing_files, and tried again next time.
  git, wiki      when GitHub's refs are the old bundle's, the old bundle is kept;
                 otherwise a mirror is seeded from the old bundle and fetched from
                 GitHub, so only the new objects are downloaded, and bundled again.
  repositories   one GitHub no longer lists is kept, rows and files, as old data

Rows keep GitHub's own JSON in `json` (close to Gitea's: issue.user.login, labels,
milestone, state, created_at, pull_request; pulls head, base, merged,
merge_commit_sha), and meta has source = github, github = the GitHub address and
github_updated = when the last run from GitHub began. A file's
uuid is empty (GitHub's links are kept as they are in the text) and its json has
browser_download_url, the link it was downloaded from. Nothing else differs from a
backup made from Gitea, so gitea_backup.Backup, restore_gitea and wekan_zips read
both.

The token goes only to GitHub's own API and web addresses (API and WEB, https): a
redirect to any other host - GitHub sends files to signed storage addresses - loses
the Authorization header, and links to other hosts are fetched without it, or not at
all when they are not GitHub's file hosts. Not backed up: code review comments on
pull request lines, Git LFS objects, Actions runs and secrets.
"""

import base64
import datetime
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import gitea_attachments as ga
import gitea_backup as gb

API = "https://api.github.com"
WEB = "https://github.com"
FILE_HOSTS = ("https://user-images.githubusercontent.com",)
RESERVE = 50  # below this many requests left this hour, wait for the reset
CHUNK = gb.CHUNK


class GitHubError(gb.BackupError):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


def origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme.lower()}://{(p.netloc or '').lower()}"


def rate_wait(headers, text, waits):
    """Seconds to wait before asking GitHub again, or 0 when the answer is no rate limit."""
    after = headers.get("Retry-After")
    if after and after.isdigit():
        return int(after) + 1
    if headers.get("X-RateLimit-Remaining") == "0":
        return max(5, int(headers.get("X-RateLimit-Reset", "0") or 0) - int(time.time()) + 5)
    if b"secondary rate limit" in text.lower() or b"abuse" in text.lower():
        return min(60 * 2 ** waits, 900)
    return 0


class _AuthOnlyToSameOrigin(urllib.request.HTTPRedirectHandler):
    """A redirect to another host - a release file or an attachment goes to signed
    storage - is followed without the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and origin(newurl) != origin(req.full_url):
            new.remove_header("Authorization")
        return new


class GitHub:
    """GitHub's API, web and git addresses and a token. The tests give local ones."""

    def __init__(self, token, api=API, web=WEB, file_hosts=FILE_HOSTS, git_root=None, say=print, sleep=time.sleep):
        self.token = (token or "").strip()
        self.api = api.rstrip("/")
        self.web = web.rstrip("/")
        self.file_hosts = tuple(h.rstrip("/") for h in file_hosts)
        self.git_root = (git_root or self.web).rstrip("/")
        self.token_origins = {origin(self.api), origin(self.web)}
        self.say, self.sleep = say, sleep
        self.anonymous = False
        self.opener = urllib.request.build_opener(_AuthOnlyToSameOrigin)
        self.requests = 0
        self._login = None

    def headers(self, url, accept):
        h = {"Accept": accept, "User-Agent": "github-backup"}
        if self.token and not self.anonymous and origin(url) in self.token_origins:
            h["Authorization"] = f"token {self.token}"
        return h

    def get(self, path, params=None):
        """(status, JSON) of an API GET; rate limits are waited out."""
        url = self.api + "/" + path.lstrip("/") + ("?" + urllib.parse.urlencode(params) if params else "")
        attempt = waits = 0
        while True:
            self.requests += 1
            h = self.headers(url, "application/vnd.github+json")
            try:
                with self.opener.open(urllib.request.Request(url, headers=h), timeout=600) as r:
                    text = r.read()
                    left = r.headers.get("X-RateLimit-Remaining")
                    if left and left.isdigit() and int(left) < RESERVE:
                        wait = max(5, int(r.headers.get("X-RateLimit-Reset", "0") or 0) - int(time.time()) + 5)
                        self.say(f"  GitHub: {left} requests left this hour, waiting {wait} s for the reset...")
                        self.sleep(wait)
                    return r.status, (json.loads(text) if text.strip() else None)
            except urllib.error.HTTPError as e:
                text = e.read()
                wait = rate_wait(e.headers, text, waits) if e.code in (403, 429) else 0
                if wait and waits < 20:
                    waits += 1
                    self.say(f"  GitHub rate limit reached, waiting {wait} s...")
                    self.sleep(wait)
                    continue
                if e.code >= 500 and attempt < 4:
                    attempt += 1
                    self.sleep(2 * attempt)
                    continue
                if e.code == 401 and "Authorization" in h:
                    # A token that is wrong or expired: public repositories are read without it.
                    self.anonymous = True
                    self.say("  GitHub: the token is not accepted; reading without it (public "
                             "repositories only, 60 requests an hour).")
                    continue
                try:
                    return e.code, json.loads(text)
                except ValueError:
                    return e.code, text[:300].decode(errors="replace")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 4:
                    attempt += 1
                    self.sleep(2 * attempt)
                    continue
                return 0, str(e)

    def one(self, path, params=None):
        s, d = self.get(path, params)
        if s != 200:
            raise GitHubError(f"GitHub {path}: {s} {d if isinstance(d, str) else (d or {}).get('message')}", s)
        return d

    def all(self, path, params=None):
        """Every item of a list, page by page."""
        out, page = [], 1
        while True:
            data = self.one(path, dict(params or {}, per_page=100, page=page))
            data = data if isinstance(data, list) else []
            out.extend(data)
            if len(data) < 100:
                return out
            page += 1

    def login(self):
        if self._login is None:
            s, d = self.get("user") if self.token else (0, None)
            self._login = (d or {}).get("login", "") if s == 200 and isinstance(d, dict) else ""
        return self._login

    def open_file(self, url, accept="*/*"):
        """An open response for a file: an API release file, or a link from a body. The
        token goes with it only to GitHub's own addresses."""
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise GitHubError(f"{url}: not a web address")
        req = urllib.request.Request(url, headers=self.headers(url, accept))
        self.requests += 1
        try:
            r = self.opener.open(req, timeout=600)
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{e.code} {e.reason}", e.code)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise GitHubError(str(getattr(e, "reason", e)))
        if (r.headers.get("Content-Type") or "").startswith("text/html"):
            r.close()
            raise GitHubError("a web page, not the file (no access?)")
        return r

    def file_links(self, text):
        """The links in a body to files GitHub keeps: github.com/user-attachments/...,
        github.com/<owner>/<repo>/files|assets/..., and the file hosts."""
        web = re.escape(self.web)
        hosts = "|".join(re.escape(h) for h in self.file_hosts)
        stop = r"[^\s)\"'<>\]]+"
        pattern = rf"(?:{web}/(?:user-attachments/(?:assets|files)/{stop}|[\w.-]+/[\w.-]+/(?:files|assets)/\d+/{stop})"
        if hosts:
            pattern += rf"|(?:{hosts})/{stop}"
        pattern += ")"
        out = []
        for m in re.finditer(pattern, text or ""):
            link = m.group(0).rstrip(".,;:!?")
            if link not in out:
                out.append(link)
        return out

    def git_url(self, full, wiki=False):
        return f"{self.git_root}/{full}{'.wiki' if wiki else ''}.git"

    def git_env(self, url):
        """git's environment: the token as HTTP basic authentication - from the
        environment, never the command line - and only for GitHub's own address."""
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if self.token and not self.anonymous and urllib.parse.urlsplit(url).scheme == "https" and \
                origin(url) == origin(self.web):
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                       GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
        return env


# --- helpers ----------------------------------------------------------------------------

def file_id(url):
    """A file's id from its link: the same link, the same id, below 2**53."""
    return int(hashlib.sha256(url.encode()).hexdigest()[:13], 16)


def _name_of(resp, url):
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return urllib.parse.unquote(m.group(1)).strip()
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]) or "file"


def _copy_out(src, out):
    digest, size = hashlib.sha256(), 0
    while chunk := src.read(CHUNK):
        out.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


bundle_heads = gb.bundle_heads
wanted_ref = gb.wanted_ref
remote_heads = gb.remote_heads
Stats = gb.Stats


# --- one backup being written -------------------------------------------------------------

class _Update:
    """One run from GitHub into w (a gitea_backup._Writer). old: the backup as it was
    before this run (gitea_backup.Backup) or None; its rows are in w already, so a
    row or file that is still at GitHub is only written again to mark it seen."""

    def __init__(self, gh, w, old, since, say):
        self.gh, self.w, self.old, self.since, self.say = gh, w, old, since, say
        self.retry = {}
        if old:
            for m in json.loads(old.meta.get("missing_files") or "[]"):
                self.retry.setdefault((m["repo"], m["kind"], str(m["parent"])), []).append(m)

    def old_rows(self, table, full):
        return self.old.rows(f"SELECT * FROM {table} WHERE repo = ?", full) if self.old else []

    def copy_entry(self, zf, path, st):
        """An entry the backup has, kept as it is. Returns its path."""
        info = zf.getinfo(path)
        st.copied[0] += 1
        st.copied[1] += info.file_size
        return path

    def copy_file_row(self, row, st):
        """A file the backup has, still at GitHub: kept as it is, seen now."""
        if row["path"] not in self.w.old_names:
            self.say(f"    not in the .zip: {row['path']}")
            return
        self.w.keep_file(row)
        st.copied[0] += 1
        st.copied[1] += row.get("size") or 0

    def download(self, url, folder, repo, kind, parent, st, accept="*/*", name=None, asset=None, prev=None):
        """A file from GitHub into the zip and the files table; False when it could not be."""
        try:
            resp = self.gh.open_file(url, accept)
        except GitHubError as e:
            self.say(f"    not saved: {url}: {e}")
            if asset is None:
                st.missing.append({"repo": repo, "kind": kind, "parent": str(parent), "url": url})
            elif prev:
                self.w.keep_file(prev)  # still at GitHub: the old copy stays current
            return False
        try:
            with resp:
                name = name or _name_of(resp, url)
                fid = asset["id"] if asset else file_id(url)
                target = folder + (safe(name) if asset else f"{fid}-{safe(name)}")
                path, size, digest, written = self.w.put_stream(target, lambda out: _copy_out(resp, out),
                                                                (repo, kind, str(parent), fid), prev)
        except (OSError, urllib.error.URLError) as e:
            self.say(f"    not saved: {url}: {e}")
            if asset is None:
                st.missing.append({"repo": repo, "kind": kind, "parent": str(parent), "url": url})
            elif prev:
                self.w.keep_file(prev)
            return False
        info = asset if asset else {"id": fid, "name": name, "browser_download_url": url,
                                    "content_type": resp.headers.get("Content-Type") or ""}
        self.w.row("files", path=path, repo=repo, kind=kind, parent=str(parent), id=info["id"], name=name,
                   size=size, sha256=digest, uuid="", json=gb.dumps(info))
        st.downloaded[0] += 1
        st.downloaded[1] += size
        return True

    def links(self, full, kind, parent, folder, text, have, st):
        """Downloads the files a body links to that are not saved yet (have: their links)."""
        for link in self.gh.file_links(text):
            if link not in have:
                if self.download(link, folder, full, kind, parent, st):
                    have.add(link)

    # git -------------------------------------------------------------------------------
    def bundle(self, full, short, wiki, old_path, seed_path, default, st):
        url = self.gh.git_url(full, wiki)
        seed = (self.w.old_zip, seed_path) if seed_path and seed_path in self.w.old_names else None
        try:
            return gb.refresh_bundle(self.w, url, self.gh.git_env(url), old_path, f"{short}/{'wiki' if wiki else 'git'}.bundle",
                                     self.say, st, wiki=wiki, default=default, seed=seed)
        except gb.BackupError as e:
            raise GitHubError(f"{full}{' wiki' if wiki else ''}: {e}")

    # one repository ----------------------------------------------------------------------
    def repo(self, repo, st):
        gh, w = self.gh, self.w
        full = repo["full_name"]
        short = gb.safe_part(repo["name"])
        prev = self.old.rows("SELECT * FROM repos WHERE full_name = ?", full) if self.old else []
        prev = prev[0] if prev else None
        since = self.since if prev else None
        seed_row = None
        if not prev and self.old:
            # The same repository under another case of its name (a backup made from Gitea).
            seed_row = (self.old.rows("SELECT * FROM repos WHERE lower(full_name) = lower(?)", full) or [None])[0]
        git_bundle = self.bundle(full, short, False, prev and prev["git_bundle"],
                                 seed_row and seed_row["git_bundle"], repo.get("default_branch"), st)
        if repo.get("has_wiki") or (prev and prev["wiki_bundle"]):
            wiki_bundle = self.bundle(full, short, True, prev and prev["wiki_bundle"],
                                      seed_row and seed_row["wiki_bundle"], None, st)
        else:
            wiki_bundle = None
        try:
            collaborators = [{"login": u["login"], "permission": PERMISSION.get(u.get("role_name") or "", (
                "admin" if (u.get("permissions") or {}).get("admin") else
                "write" if (u.get("permissions") or {}).get("push") else "read"))}
                for u in gh.all(f"repos/{full}/collaborators")]
        except GitHubError:
            collaborators = json.loads(prev["collaborators"] or "[]") if prev else []
        w.row("repos", full_name=full, name=repo["name"], json=gb.dumps(repo), topics=gb.dumps(repo.get("topics") or []),
              git_bundle=git_bundle, wiki_bundle=wiki_bundle, empty=0 if git_bundle else 1,
              collaborators=gb.dumps(collaborators))
        for table, path, key, col in (("labels", f"repos/{full}/labels", "name", "name"),
                                      ("milestones", f"repos/{full}/milestones", "title", "title")):
            try:
                items = gh.all(path, {"state": "all"} if table == "milestones" else None)
            except GitHubError as e:
                self.say(f"    {table}: {e}; kept from the old backup")
                for r in self.old_rows(table, full):
                    w.row(table, **r)
                continue
            for x in items:
                w.row(table, repo=full, id=x["id"], **{col: x.get(key)}, json=gb.dumps(x))

        old_files = {}
        for f in self.old_rows("files", full):
            old_files.setdefault((f["kind"], f["parent"]), []).append(f)

        # issues and pull requests
        old_issues = {r["number"]: r for r in self.old_rows("issues", full)}
        old_pulls = {r["number"]: r for r in self.old_rows("pulls", full)}
        fresh = self.fresh_issues(full, repo, since)
        changed_issues = {}
        for i in fresh:
            n = i["number"]
            was = old_issues.get(n)
            same = was is not None and json.loads(was["json"]).get("updated_at") == i.get("updated_at")
            if same and (not i.get("pull_request") or n in old_pulls):
                continue
            kind = "pulls" if i.get("pull_request") else "issues"
            if was is None:
                st.new[kind] += 1
            elif not same:
                st.changed[kind] += 1
            changed_issues[n] = i
        listed = {i["number"] for i in fresh}
        for n, r in old_issues.items():
            # Unchanged since the last run: still at GitHub. Without `since` every one
            # was listed, so one that was not is gone: it stays as old data.
            if n not in changed_issues and (since or n in listed):
                w.row("issues", **r)
                if n in old_pulls:
                    w.row("pulls", **old_pulls[n])
        for n, i in sorted(changed_issues.items()):
            is_pull = 1 if i.get("pull_request") else 0
            w.row("issues", repo=full, number=n, is_pull=is_pull, title=i.get("title"), state=i.get("state"),
                  json=gb.dumps(i))
            if is_pull:
                s, pr = gh.get(f"repos/{full}/pulls/{n}")
                if s == 200:
                    w.row("pulls", repo=full, number=n, json=gb.dumps(pr))
                elif n in old_pulls:
                    w.row("pulls", **old_pulls[n])

        # comments
        old_comments = {r["id"]: r for r in self.old_rows("comments", full)}
        try:
            fresh_c = gh.all(f"repos/{full}/issues/comments", dict(
                {"sort": "updated", "direction": "asc"}, **({"since": since} if since else {})))
        except GitHubError as e:
            if e.status not in (404, 410):
                raise
            fresh_c = []
        changed_comments = {}
        for c in fresh_c:
            was = old_comments.get(c["id"])
            if was is not None and json.loads(was["json"]).get("updated_at") == c.get("updated_at"):
                continue
            if was is None:
                st.new["comments"] += 1
            else:
                st.changed["comments"] += 1
            changed_comments[c["id"]] = c
        listed_c = {c["id"] for c in fresh_c}
        for cid, r in old_comments.items():
            if cid not in changed_comments and (since or cid in listed_c):
                w.row("comments", **r)
        for cid, c in changed_comments.items():
            w.row("comments", repo=full, id=cid, issue_number=gb.number_of(c), json=gb.dumps(c))

        # files of issues and comments: every old one is copied, the new links downloaded
        for (kind, parent), rows in old_files.items():
            still = since or (int(parent) in (listed if kind == "issues" else listed_c) if parent.isdigit() else False)
            if kind in ("issues", "comments") and still:
                for f in rows:
                    self.copy_file_row(f, st)

        def have(kind, parent):
            return {json.loads(f["json"] or "{}").get("browser_download_url") for f in old_files.get((kind, str(parent)), [])}
        for n, i in sorted(changed_issues.items()):
            self.links(full, "issues", n, f"{short}/issues/{n}/", i.get("body"), have("issues", n), st)
        for cid, c in changed_comments.items():
            n = gb.number_of(c)
            self.links(full, "comments", cid, f"{short}/comments/{n}/{cid}/", c.get("body"), have("comments", cid), st)
        for (repo_, kind, parent), items in self.retry.items():
            if repo_ != full or (kind == "issues" and int(parent) in changed_issues) or \
                    (kind == "comments" and int(parent) in changed_comments):
                continue  # a changed one was read again just now
            have_now = have(kind, parent)
            for m in items:
                if m["url"] in have_now:
                    continue
                if kind == "issues":
                    folder = f"{short}/issues/{parent}/"
                else:
                    n = gb.number_of(json.loads((old_comments.get(int(parent)) or {}).get("json") or "{}"))
                    folder = f"{short}/comments/{n}/{parent}/"
                self.download(m["url"], folder, full, kind, parent, st)

        # releases
        self.releases(full, short, old_files, st)
        w.db.commit()

    def fresh_issues(self, full, repo, since):
        """The issues and pull requests updated since `since` (all, without it)."""
        params = {"state": "all", "sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        try:
            return self.gh.all(f"repos/{full}/issues", params)
        except GitHubError as e:
            if e.status != 410:  # issues turned off: the pull requests are still there
                if e.status == 404:
                    return []
                raise
        out = []
        for p in self.gh.all(f"repos/{full}/pulls", {"state": "all", "sort": "updated", "direction": "desc"}):
            if since and (p.get("updated_at") or "") < since:
                break
            i = {k: p.get(k) for k in ("url", "html_url", "number", "title", "user", "labels", "state", "locked",
                                        "assignee", "assignees", "milestone", "created_at", "updated_at",
                                        "closed_at", "body", "author_association")}
            i["pull_request"] = {"url": p.get("url"), "html_url": p.get("html_url"), "merged_at": p.get("merged_at")}
            out.append(i)
        return out

    def releases(self, full, short, old_files, st):
        w = self.w
        old_rel = {r["id"]: r for r in self.old_rows("releases", full)}
        try:
            fresh = self.gh.all(f"repos/{full}/releases")
        except GitHubError as e:
            self.say(f"    releases: {e}; kept from the old backup")
            fresh = []
        # A release GitHub no longer has stays in the backup, with its files: old data
        # (seen is not renewed).
        todo = []
        for r in fresh:
            w.row("releases", repo=full, id=r["id"], tag=r.get("tag_name"), json=gb.dumps(r))
            was = old_rel.get(r["id"])
            if was is None:
                st.new["releases"] += 1
            elif _release_key(json.loads(was["json"])) != _release_key(r):
                st.changed["releases"] += 1
            had = {f["id"]: f for f in old_files.get(("releases", str(r["id"])), [])}
            tag = gb.safe_part(r.get("tag_name") or r["id"])
            for a in r.get("assets") or []:
                f = had.get(a["id"])
                if f is not None and f["size"] == a.get("size") and \
                        json.loads(f["json"] or "{}").get("updated_at") == a.get("updated_at"):
                    self.copy_file_row(dict(f, json=gb.dumps(a)), st)
                else:
                    todo.append((r, tag, a, f))
        for r, tag, a, f in todo:
            self.download(a.get("url") or a.get("browser_download_url"), f"{short}/releases/{tag}/", full,
                          "releases", r["id"], st, accept="application/octet-stream",
                          name=a.get("name") or str(a["id"]), asset=a, prev=f)


PERMISSION = {"admin": "admin", "maintain": "admin", "write": "write", "triage": "read", "read": "read"}


def _release_key(r):
    return [r.get(k) for k in ("tag_name", "name", "body", "draft", "prerelease", "published_at",
                               "target_commitish")] + sorted((a.get("id"), a.get("updated_at"), a.get("size"))
                                                             for a in r.get("assets") or [])


def safe(name):
    return gb.safe_part(name)


def owner_repos(gh, login, kind):
    if kind == "organization":
        path, params = f"orgs/{login}/repos", {"type": "all", "sort": "full_name"}
    elif gh.login().lower() == login.lower():
        # The token's own user: its private repositories too, which users/<name>/repos leaves out.
        path, params = "user/repos", {"affiliation": "owner", "visibility": "all", "sort": "full_name"}
    else:
        path, params = f"users/{login}/repos", {"type": "owner", "sort": "full_name"}
    repos = [r for r in gh.all(path, params) if (r.get("owner") or {}).get("login", "").lower() == login.lower()]
    return sorted(repos, key=lambda r: r["full_name"].lower())


def _since(meta):
    """GitHub's `since` for a backup: ten minutes before the last run from GitHub
    began (clocks differ, and an item changed while it was being written is read
    again); None when it was never updated from GitHub."""
    when = meta.get("github_updated") or (meta.get("updated") or meta.get("created") if meta.get("source") == "github"
                                         else "")
    if not when:
        return None
    try:
        t = datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t - datetime.timedelta(minutes=10)).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update(gh, name, root, say, excludes=()):
    """The one backup of the GitHub organization or user NAME under root, updated in
    place from GitHub - or made, when there is none. Returns its directory
    (root/NAME); update.stats is what was done."""
    name = ga.normalize_scope(name)
    if not name or "/" in name:
        raise gb.BackupError("A backup is of an organization or a user: give its name.")
    s, user = gh.get(f"users/{name}")
    if s != 200 or not isinstance(user, dict):
        raise GitHubError(f"{name}: no such organization or user on GitHub ({s})", s)
    login = user.get("login") or name
    kind = "organization" if user.get("type") == "Organization" else "user"
    info = user
    if kind == "organization":
        s, org = gh.get(f"orgs/{login}")
        if s == 200 and isinstance(org, dict):
            info = org
    repos = owner_repos(gh, login, kind)
    names, gone = ga.apply_excludes(login, [r["full_name"] for r in repos], excludes)
    for n in gone:
        say(f"  {n}: excluded; left out")
    keep = set(names)
    repos = [r for r in repos if r["full_name"] in keep]
    total = Stats()
    with gb.updating(root, login, "github", say) as w:
        old = w.previous()
        since = _since(old.meta) if old else None
        if old and since:
            say(f"Updating the backup of the GitHub {kind} {login} from GitHub (run {w.run}): {len(repos)} "
                f"repositories, changes since {since} -> {w.dir}")
        elif old:
            say(f"Updating the backup of {login} (made from Gitea: its git is reused) from GitHub (run {w.run}): "
                f"{len(repos)} repositories -> {w.dir}")
        else:
            say(f"Backing up the GitHub {kind} {login} from GitHub: {len(repos)} repositories -> {w.dir}")
        if not gh.token or gh.anonymous:
            say("  No GitHub token: public repositories only, 60 requests an hour.")
        u = _Update(gh, w, old, since, say)
        w.meta_set(source="github", github=gh.web, owner=login, kind=kind, github_updated=w.started)
        w.row("owner", name=login, kind=kind, json=gb.dumps(info))
        _avatar(u, info, total)
        if kind == "organization":
            _org(u, login, total)
        for n, repo in enumerate(repos, 1):
            say(f"  [{n}/{len(repos)}] {repo['full_name']}")
            st = Stats()
            u.repo(repo, st)
            say(f"    {st.line()}")
            total.add(st)
        if old:
            listed = {r["full_name"] for r in repos} | set(gone)
            for r in old.rows("SELECT * FROM repos ORDER BY full_name"):
                if r["full_name"] not in listed and r["full_name"].lower() not in {x.lower() for x in excludes}:
                    say(f"  {r['full_name']}: no longer on GitHub; kept in the backup, as old data")
        w.meta_set(missing_files=json.dumps(total.missing))
        d, written = w.dir, list(w.added)
    db, zp = os.path.join(d, f"{login}.db"), os.path.join(d, f"{login}.zip")
    say(f"Summary: {total.line()}; git {total.git_fetched} fetched, {total.git_same} unchanged; "
        f"{gh.requests} requests to GitHub; zip: {written[0]} new entries ({ga.human(written[1])})")
    for m in total.missing:
        say(f"  not saved (tried again next time): {m['repo']} {m['kind']} {m['parent']}: {m['url']}")
    say(f"Backup ready: {db} ({ga.human(os.path.getsize(db))}), {zp} ({ga.human(os.path.getsize(zp))})")
    update.stats = total
    return d


def _avatar(u, info, st):
    url = info.get("avatar_url")
    old = u.old.rows("SELECT * FROM files WHERE kind = 'avatar'") if u.old else []
    owner = u.old.rows("SELECT json FROM owner") if u.old else []
    if old and owner and json.loads(owner[0]["json"] or "{}").get("avatar_url") == url:
        u.copy_file_row(old[0], st)
        return
    if not url:
        return
    try:
        resp = u.gh.open_file(url)
    except GitHubError as e:
        u.say(f"  avatar not saved: {e}")
        return
    with resp:
        path, size, digest, _ = u.w.put_stream("owner/avatar.png", lambda out: _copy_out(resp, out),
                                               ("", "avatar", "", 0), old[0] if old else None)
    u.w.row("files", path=path, repo="", kind="avatar", parent="", id=0, name="avatar.png", size=size,
            sha256=digest, uuid="", json=gb.dumps({"browser_download_url": url}))
    st.downloaded[0] += 1
    st.downloaded[1] += size


def _org(u, login, st):
    gh, w = u.gh, u.w
    try:
        for m in gh.all(f"orgs/{login}/members"):
            w.row("members", login=m.get("login"), json=gb.dumps(m))
    except GitHubError:
        for r in (u.old.rows("SELECT * FROM members") if u.old else []):
            w.row("members", **r)
    try:
        teams = gh.all(f"orgs/{login}/teams")
    except GitHubError:  # only a member of the organization sees its teams
        for r in (u.old.rows("SELECT * FROM teams") if u.old else []):
            w.row("teams", **r)
        return
    for t in teams:
        slug = t.get("slug") or t.get("name")
        try:
            people = [m.get("login") for m in gh.all(f"orgs/{login}/teams/{slug}/members")]
            held = [r.get("name") for r in gh.all(f"orgs/{login}/teams/{slug}/repos")]
        except GitHubError:
            people, held = [], []
        w.row("teams", id=t["id"], name=t.get("name"), json=gb.dumps(t), members=gb.dumps(people), repos=gb.dumps(held))

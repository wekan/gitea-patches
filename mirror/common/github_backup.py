"""Backing up a GitHub organization or user straight from GitHub - no Gitea at all -
into the backup format of gitea_backup.py (FORMAT 1), and bringing such a backup up
to date.

  update(gh, name, root, say, excludes)

takes the newest whole backup of NAME under root that was made from GitHub and
writes a NEW backup directory beside it (a new stamp, written as .partial and
renamed when whole) with everything the old one had and what is new or changed on
GitHub since. When there is none, it makes a whole one. The old backup is only read.

What is read again, and what is copied:

  repositories   every one GitHub lists (new ones too); topics are in GitHub's JSON
  labels, milestones, collaborators, teams, members
                 listed again - they are small - and kept as GitHub has them now
  issues, pull requests, comments
                 only those updated since the old backup was started (GitHub's
                 `since`); the rest are copied from the old backup, rows and files
  pull request details
                 only for new and changed pull requests
  releases       listed again; a release file the old backup has with the same id,
                 size and updated_at is copied from the old .zip - stored entries,
                 copied as they are - and only the others are downloaded. A release
                 GitHub no longer has is kept from the old backup.
  files in text  github.com/user-attachments/..., github.com/<owner>/<repo>/files
                 and /assets, and user-images.githubusercontent.com links in issue and
                 comment bodies: downloaded into the issues/ and comments/ folders and
                 recorded in `files` with their SHA-256. A link that cannot be fetched
                 is reported, listed in meta missing_files, and tried again next time.
  git, wiki      when GitHub's refs are the old bundle's, the old bundle is copied;
                 otherwise a mirror is seeded from the old bundle and fetched from
                 GitHub, so only the new objects are downloaded, and bundled again.

Rows keep GitHub's own JSON in `json` (close to Gitea's: issue.user.login, labels,
milestone, state, created_at, pull_request; pulls head, base, merged,
merge_commit_sha), and meta has source = github, github = the GitHub address and
based_on = the stamp of the backup this one was brought up to date from. A file's
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
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

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


def bundle_heads(zf, path):
    """{ref: sha} from a git bundle's header in the zip - read without the pack."""
    heads = {}
    try:
        with zf.open(path) as f:
            if not f.readline().startswith(b"# v"):
                return None
            while True:
                line = f.readline()
                if not line or line == b"\n":
                    return heads
                if line[:1] in (b"@", b"-"):
                    continue
                sha, _, ref = line.decode(errors="replace").strip().partition(" ")
                if ref.startswith("refs/"):
                    heads[ref] = sha
    except (KeyError, OSError, zipfile.BadZipFile):
        return None


def wanted_ref(ref):
    return ref.startswith(("refs/heads/", "refs/tags/")) or re.fullmatch(r"refs/pull/\d+/head", ref) is not None


def remote_heads(url, env):
    """(ok, {ref: sha}) of the branches, tags and pull request heads at url."""
    code, out = gb.git(["ls-remote", url], env=env)
    if code != 0:
        return False, out
    heads = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if wanted_ref(ref) and not ref.endswith("^{}"):
            heads[ref] = sha
    return True, heads


def stamp_dir(root, name):
    stamp = datetime.datetime.now().strftime(gb.STAMP)
    final, n = os.path.join(root, name, stamp), 1
    while os.path.exists(final) or os.path.exists(final + ".partial"):
        final = os.path.join(root, name, f"{stamp}-{n}")
        n += 1
    return final


def github_base(root, name):
    """(newest whole backup of name made from GitHub, newest of any kind) - directories."""
    found = gb.backups(root, name)
    base = None
    for _, _, d in found:
        try:
            b = gb.Backup(d)
        except (gb.BackupError, OSError, ValueError):
            continue
        try:
            if b.meta.get("source") == "github" and b.meta.get("complete") == "yes":
                base = d
                break
        finally:
            b.close()
    return base, (found[0][2] if found else None)


class Stats:
    KEYS = ("issues", "pulls", "comments", "releases")

    def __init__(self):
        self.new = dict.fromkeys(self.KEYS, 0)
        self.changed = dict.fromkeys(self.KEYS, 0)
        self.downloaded = [0, 0]  # files, bytes
        self.copied = [0, 0]
        self.git_fetched = 0
        self.git_same = 0
        self.missing = []

    def add(self, other):
        for k in self.KEYS:
            self.new[k] += other.new[k]
            self.changed[k] += other.changed[k]
        for a, b in ((self.downloaded, other.downloaded), (self.copied, other.copied)):
            a[0] += b[0]
            a[1] += b[1]
        self.git_fetched += other.git_fetched
        self.git_same += other.git_same
        self.missing += other.missing

    def line(self):
        parts = [f"{k} {self.new[k]} new, {self.changed[k]} changed" for k in self.KEYS]
        return "; ".join(parts) + (f"; files {self.downloaded[0]} downloaded ({ga.human(self.downloaded[1])}), "
                                   f"{self.copied[0]} copied ({ga.human(self.copied[1])})")


# --- one backup being written -------------------------------------------------------------

class _Update:
    def __init__(self, gh, w, old, seed, since, say):
        self.gh, self.w, self.old, self.seed, self.since, self.say = gh, w, old, seed, since, say
        self.retry = {}
        if old:
            for m in json.loads(old.meta.get("missing_files") or "[]"):
                self.retry.setdefault((m["repo"], m["kind"], str(m["parent"])), []).append(m)

    def old_rows(self, table, full):
        return self.old.rows(f"SELECT * FROM {table} WHERE repo = ?", full) if self.old else []

    def copy_entry(self, zf, path, st):
        """A zip entry of an old backup into the new one, as it is. Returns its path."""
        info = zf.getinfo(path)

        def fill(out):
            with zf.open(info) as src:
                shutil.copyfileobj(src, out, CHUNK)
            return info.file_size, None
        new, size, _ = self.w.add_stream(path, fill)
        st.copied[0] += 1
        st.copied[1] += size
        return new

    def copy_file_row(self, row, st):
        try:
            path = self.copy_entry(self.old.zip, row["path"], st)
        except KeyError:
            self.say(f"    not in the old .zip: {row['path']}")
            return
        self.w.row("files", **dict(row, path=path))

    def download(self, url, folder, repo, kind, parent, st, accept="*/*", name=None, asset=None):
        """A file from GitHub into the zip and the files table; False when it could not be."""
        try:
            resp = self.gh.open_file(url, accept)
        except GitHubError as e:
            self.say(f"    not saved: {url}: {e}")
            if asset is None:
                st.missing.append({"repo": repo, "kind": kind, "parent": str(parent), "url": url})
            return False
        try:
            with resp:
                name = name or _name_of(resp, url)
                fid = asset["id"] if asset else file_id(url)
                target = folder + (safe(name) if asset else f"{fid}-{safe(name)}")
                path, size, digest = self.w.add_stream(target, lambda out: _copy_out(resp, out))
        except (OSError, urllib.error.URLError) as e:
            self.say(f"    not saved: {url}: {e}")
            if asset is None:
                st.missing.append({"repo": repo, "kind": kind, "parent": str(parent), "url": url})
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
        kind = "wiki" if wiki else "git"
        url = self.gh.git_url(full, wiki)
        env = self.gh.git_env(url)
        ok, heads = remote_heads(url, env)
        if not ok or not heads:
            if old_path:
                if not ok:
                    self.say(f"    {kind}: cannot read {url}; the old bundle is kept: {str(heads)[-200:]}")
                return self.copy_entry(self.old.zip, old_path, st)
            if not ok and not wiki:
                self.say(f"    git: cannot read {url}: {str(heads)[-300:]}")
            return None
        if old_path:
            had = bundle_heads(self.old.zip, old_path) or {}
            if all(had.get(r) == s for r, s in heads.items()):
                st.git_same += 1
                self.say(f"    {kind}: unchanged")
                return self.copy_entry(self.old.zip, old_path, st)
        mirror = os.path.join(self.w.tmp, "mirror.git")
        shutil.rmtree(mirror, ignore_errors=True)
        gb.git(["init", "--quiet", "--bare", mirror])
        src = (self.old.zip, old_path) if old_path else (self.seed.zip, seed_path) if seed_path else None
        if src:
            seed_file = os.path.join(self.w.tmp, "seed.bundle")
            with src[0].open(src[1]) as f, open(seed_file, "wb") as out:
                shutil.copyfileobj(f, out, CHUNK)
            code, out = gb.git(["-C", mirror, "fetch", "--quiet", seed_file, "+refs/*:refs/*"])
            os.remove(seed_file)
            if code != 0:
                self.say(f"    {kind}: the old bundle could not be read; fetching everything: {out[-200:]}")
        code, out = gb.git(["-C", mirror, "fetch", "--quiet", "--no-tags", url, "+refs/heads/*:refs/heads/*",
                            "+refs/tags/*:refs/tags/*", "+refs/pull/*/head:refs/pull/*/head"], env=env)
        if code != 0:
            shutil.rmtree(mirror, ignore_errors=True)
            if old_path:
                self.say(f"    {kind}: git fetch failed; the old bundle is kept: {out[-300:]}")
                return self.copy_entry(self.old.zip, old_path, st)
            raise GitHubError(f"{full}{' wiki' if wiki else ''}: git fetch failed: {out[-500:]}")
        if default:
            gb.git(["-C", mirror, "symbolic-ref", "HEAD", f"refs/heads/{default}"])
        bundle = os.path.join(self.w.tmp, "repo.bundle")
        code, out = gb.git(["-C", mirror, "bundle", "create", "--quiet", bundle, "--all"])
        shutil.rmtree(mirror, ignore_errors=True)
        if code != 0:
            if "empty bundle" in out.lower() or "refusing to create empty bundle" in out.lower():
                return None
            raise GitHubError(f"{full}: git bundle failed: {out[-500:]}")
        path, size, _ = self.w.add_file(f"{short}/{kind}.bundle", bundle)
        os.remove(bundle)
        st.git_fetched += 1
        self.say(f"    {kind}: {ga.human(size)}{' (only new objects fetched)' if src else ''}")
        return path

    # one repository ----------------------------------------------------------------------
    def repo(self, repo, st):
        gh, w = self.gh, self.w
        full = repo["full_name"]
        short = gb.safe_part(repo["name"])
        prev = self.old.rows("SELECT * FROM repos WHERE full_name = ?", full) if self.old else []
        prev = prev[0] if prev else None
        since = self.since if prev else None
        seed_row = None
        if not prev and self.seed:
            seed_row = (self.seed.rows("SELECT * FROM repos WHERE lower(full_name) = lower(?)", full) or [None])[0]
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
        for n, r in old_issues.items():
            if n not in changed_issues:
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
        for cid, r in old_comments.items():
            if cid not in changed_comments:
                w.row("comments", **r)
        for cid, c in changed_comments.items():
            w.row("comments", repo=full, id=cid, issue_number=gb.number_of(c), json=gb.dumps(c))

        # files of issues and comments: every old one is copied, the new links downloaded
        for (kind, parent), rows in old_files.items():
            if kind in ("issues", "comments"):
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
        ids = {r["id"] for r in fresh}
        for rid, r in old_rel.items():  # gone from GitHub: kept, with its files
            if rid not in ids:
                w.row("releases", **r)
                for f in old_files.get(("releases", str(rid)), []):
                    self.copy_file_row(f, st)
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
                    todo.append((r, tag, a))
        for r, tag, a in todo:
            self.download(a.get("url") or a.get("browser_download_url"), f"{short}/releases/{tag}/", full,
                          "releases", r["id"], st, accept="application/octet-stream",
                          name=a.get("name") or str(a["id"]), asset=a)


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


def update(gh, name, root, say, excludes=()):
    """A new backup of the GitHub organization or user NAME under root: the newest one
    made from GitHub, brought up to date - or a whole one, when there is none. Returns
    the new backup's directory."""
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
    base, newest = github_base(root, login)
    old = gb.Backup(base) if base else None
    seed = gb.Backup(newest) if newest and newest != base else None
    started = datetime.datetime.now(datetime.timezone.utc)
    since = None
    if old:
        try:
            t = datetime.datetime.fromisoformat(old.meta["created"].replace("Z", "+00:00"))
            # Ten minutes before the old one began: clocks differ, and an item changed
            # while it was being written is read again.
            since = (t - datetime.timedelta(minutes=10)).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (KeyError, ValueError):
            since = None
    try:
        repos = owner_repos(gh, login, kind)
        names, gone = ga.apply_excludes(login, [r["full_name"] for r in repos], excludes)
        for n in gone:
            say(f"  {n}: excluded; left out")
        keep = set(names)
        repos = [r for r in repos if r["full_name"] in keep]
        final = stamp_dir(root, login)
        partial = final + ".partial"
        os.makedirs(partial)
        if old:
            say(f"Updating the backup of the GitHub {kind} {login} ({os.path.basename(base)}) from GitHub: "
                f"{len(repos)} repositories, changes since {since} -> {final}")
        else:
            say(f"Backing up the GitHub {kind} {login} from GitHub" +
                (f" (the newest backup, {os.path.basename(newest)}, was made from Gitea: its git is reused)"
                 if seed else "") + f": {len(repos)} repositories -> {final}")
        if not gh.token or gh.anonymous:
            say("  No GitHub token: public repositories only, 60 requests an hour.")
        w = gb._Writer(partial, login)
        u = _Update(gh, w, old, seed, since, say)
        total = Stats()
        try:
            meta = {"format": str(gb.FORMAT), "created": started.isoformat(), "gitea": "", "source": "github",
                    "github": gh.web, "owner": login, "kind": kind, "complete": "no",
                    "based_on": os.path.basename(base) if base else ""}
            for k, v in meta.items():
                w.row("meta", key=k, value=v)
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
                gone_now = [r for r in old.rows("SELECT * FROM repos ORDER BY full_name")
                            if r["full_name"] not in listed and r["full_name"].lower() not in {x.lower() for x in excludes}]
                for r in gone_now:
                    say(f"  {r['full_name']}: no longer on GitHub; kept from the old backup")
                    _copy_repo(u, r, total)
            w.row("meta", key="missing_files", value=json.dumps(total.missing))
            w.row("meta", key="complete", value="yes")
        finally:
            w.close()
        os.rename(partial, final)
    finally:
        if old:
            old.close()
        if seed:
            seed.close()
    db, zp = os.path.join(final, f"{login}.db"), os.path.join(final, f"{login}.zip")
    say(f"Summary: {total.line()}; git {total.git_fetched} fetched, {total.git_same} unchanged; "
        f"{gh.requests} requests to GitHub")
    for m in total.missing:
        say(f"  not saved (tried again next time): {m['repo']} {m['kind']} {m['parent']}: {m['url']}")
    say(f"Backup ready: {db} ({ga.human(os.path.getsize(db))}), {zp} ({ga.human(os.path.getsize(zp))})")
    update.stats = total
    return final


def _avatar(u, info, st):
    url = info.get("avatar_url")
    old = u.old.rows("SELECT * FROM files WHERE kind = 'avatar'") if u.old else []
    if old and json.loads(u.old.rows("SELECT json FROM owner")[0]["json"] or "{}").get("avatar_url") == url:
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
        path, size, digest = u.w.add_stream("owner/avatar.png", lambda out: _copy_out(resp, out))
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


def _copy_repo(u, row, st):
    """A repository of the old backup that GitHub no longer has: every row and file."""
    full = row["full_name"]
    paths = {}
    for col in ("git_bundle", "wiki_bundle"):
        if row[col]:
            paths[col] = u.copy_entry(u.old.zip, row[col], st)
    u.w.row("repos", **dict(row, **paths))
    for table in ("labels", "milestones", "issues", "pulls", "comments", "releases"):
        for r in u.old_rows(table, full):
            u.w.row(table, **r)
    for f in u.old_rows("files", full):
        u.copy_file_row(f, st)

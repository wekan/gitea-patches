#!/usr/bin/env python3
"""Mirror repositories from the local Gitea to GitLab: git branches and tags, the wiki,
issues and pull requests with comments, labels and milestones, and releases with their
files.

  ./gitea-to-gitlab.py                      menu: sources, settings, destinations, checks
  ./gitea-to-gitlab.py --all                every configured step for every source (cron)
  ./gitea-to-gitlab.py issues releases      only these steps
  ./gitea-to-gitlab.py --source wekan       only this source
  ./gitea-to-gitlab.py --check-online       read-only: can Gitea and GitLab be reached
  ./gitea-to-gitlab.py --check-missing      read-only: what is not mirrored yet

Settings are in gitea-to-gitlab.ini next to this file (mode 600, holds tokens), the
progress in gitea-to-gitlab-state.json, and every run's log in log/<date-time>/.

GITLAB
  - A Gitea repository owner/name goes to <namespace>/name at GitLab: the namespace is
    the GitLab group or user from the menu's Destinations, or the setting namespace
    (default: the same name as the Gitea owner). The group must exist; the project is
    created in it when missing, private when the Gitea repository is private.
  - git is pushed over SSH with your SSH key (push = ssh), or over HTTPS with the token
    (push = https); never forced, never deleting anything.
  - Pull requests become issues titled "[PR] ...": a merge request needs a source
    branch in the same project, and the conversation is what is being kept.
  - Files attached to issues and comments in Gitea are uploaded to the GitLab project,
    so the copy does not depend on this Gitea being reachable.
  - Release files go to the project's generic package registry (up to 5 GB each on
    gitlab.com, where project uploads stop at 100 MB) and are linked from the release.
  - The marker <!-- wekan-mirror:URL --> is the same as in WeKan's releases/mirror.sh,
    so what that mirror already copied from GitHub is recognised, not copied again.
"""

import base64
import os
import re
import subprocess
import sys
import urllib.parse
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import gitea_mirror as gm  # noqa: E402


def enc(s):
    return urllib.parse.quote(str(s), safe="")


class GitLab:
    NAME = "gitea-to-gitlab"
    TITLE = "GitLab"
    DESTINATION_WORD = "GitLab group or user"
    DEFAULT_KEY = "[gitlab] namespace"
    DESTINATION_HELP = ("the GitLab group or user named in Destinations (default: the same\n"
                        "  name as its Gitea owner); a missing project is created there.")
    INI_HEADER = """\
# gitea-to-gitlab.ini - settings of gitea-to-gitlab.py. Edit them from the menu
# (./gitea-to-gitlab.py) or by hand. This file holds tokens: mode 600.
#
# [sources] list: one source per line.
#   wekan/wekan   mirrors only this Gitea repository
#   wekan         mirrors EVERY repository of the Gitea organization or user wekan
#   (all)         every repository the Gitea token can read; (allpublic), (allprivate)
# [destinations]: <gitea owner> = <GitLab group or user>, when it is not the same name.
"""
    SETTINGS = [
        ("gitlab", "url", "https://gitlab.com", "address of GitLab"),
        ("gitlab", "token", "", "GitLab personal access token (scope api)"),
        ("gitlab", "namespace", "{owner}", "GitLab group or user of a Gitea owner without a Destinations line"),
        ("gitlab", "push", "ssh", "git push over ssh (your SSH key) or https (the token)"),
        ("gitlab", "private", "yes", "mirror private Gitea repositories as private GitLab projects (no = skip them)"),
    ]

    # settings
    def missing_settings(self, cfg):
        return [] if cfg.get("token", "gitlab") else ["[gitlab] token"]

    def configure(self, e):
        self.e = e
        self.web = e.cfg.get("url", "gitlab").rstrip("/")
        self.api = self.web + "/api/v4"
        self.token = e.cfg.get("token", "gitlab")
        self.mode = e.cfg.get("push", "gitlab").strip().lower()
        self.host = urllib.parse.urlsplit(self.web).netloc

    def default_destination(self, cfg):
        return cfg.get("namespace", "gitlab") or "{owner}"

    def where(self, cfg):
        return cfg.get("url", "gitlab")

    def describe(self, dest, repo):
        return f"{getattr(self, 'web', 'https://gitlab.com')}/{dest}/{repo['name']}"

    # API
    def call(self, method, path, json_body=None, **kw):
        status, data, headers = gm.http(method, self.api + path, {"PRIVATE-TOKEN": self.token}, json_body=json_body, **kw)
        return status, data, headers

    def ok(self, method, path, json_body=None, want=(200, 201), **kw):
        status, data, _ = self.call(method, path, json_body, **kw)
        if status not in want:
            raise RuntimeError(f"GitLab {method} {path}: {status} {gm.message(data)}")
        return data

    def pages(self, path):
        sep = "&" if "?" in path else "?"
        page = 1
        while True:
            data = self.ok("GET", f"{path}{sep}per_page=100&page={page}")
            yield from data or []
            if len(data or []) < 100:
                return
            page += 1

    # the project
    def ensure_repo(self, dest, repo):
        path = f"{dest}/{repo['name']}"
        status, data, _ = self.call("GET", f"/projects/{enc(path)}")
        private = bool(repo.get("private"))
        if status == 200:
            if private and data.get("visibility") != "private":
                self.e.record("failed", f"{path}: the GitLab project is {data.get('visibility')} but the Gitea repository "
                                        f"is private; not mirrored")
                return None
            return data
        if status != 404:
            raise gm.RepoError(f"GitLab project {path}: {status} {gm.message(data)}")
        if private and self.e.cfg.get("private", "gitlab").strip().lower() not in ("yes", "true", "1", "on", "y"):
            self.e.record("skipped", f"{repo['full_name']}: private, and [gitlab] private = no")
            return None
        if self.e.dry:
            self.e.record("planned", f"GitLab project {path} ({'private' if private else 'public'}); "
                                     f"the rest of this repository is checked after it exists")
            return None
        status, ns, _ = self.call("GET", f"/namespaces/{enc(dest)}")
        if status != 200:
            raise gm.RepoError(f"GitLab group or user {dest} not found ({status}): create the group at "
                               f"{self.web}/groups/new, or set the destination in the menu")
        data = self.ok("POST", "/projects", {
            "name": repo["name"], "path": repo["name"], "namespace_id": ns["id"],
            "description": (repo.get("description") or "")[:2000],
            "visibility": "private" if private else "public",
            "issues_enabled": True, "wiki_enabled": bool(repo.get("has_wiki")),
            "initialize_with_readme": False})
        self.e.record("copied", f"GitLab project {path}")
        return data

    # git
    def git_url(self, handle, repo):
        return handle["ssh_url_to_repo"] if self.mode == "ssh" else handle["http_url_to_repo"]

    def wiki_url(self, handle, repo):
        if not handle.get("wiki_enabled", True):
            return ""
        return re.sub(r"\.git$", ".wiki.git", self.git_url(handle, repo))

    def git_env(self):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if self.mode != "ssh":
            basic = base64.b64encode(f"oauth2:{self.token}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{self.web}/.extraHeader",
                       GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
        else:
            env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        return env

    def after_git(self, handle, repo):
        want = repo.get("default_branch")
        if self.e.dry or not want or handle.get("default_branch") == want:
            return
        status, _, _ = self.call("GET", f"/projects/{handle['id']}/repository/branches/{enc(want)}")
        if status == 200:
            self.ok("PUT", f"/projects/{handle['id']}", {"default_branch": want})
            handle["default_branch"] = want
            self.e.record("copied", f"default branch {want}")

    # issues
    def issue_tracker(self, handle, repo, labels, milestones):
        pid = handle["id"]
        have = {l["name"] for l in self.pages(f"/projects/{pid}/labels")}
        for name, l in labels.items():
            if name in have:
                continue
            if self.e.dry:
                self.e.record("planned", f"label {name}")
                continue
            self.ok("POST", f"/projects/{pid}/labels", {"name": name, "color": "#" + (l.get("color") or "cccccc").lstrip("#"),
                                                        "description": l.get("description") or ""})
            self.e.record("copied", f"label {name}")
        ms = {m["title"]: m for m in self.pages(f"/projects/{pid}/milestones")}
        for title, m in milestones.items():
            cur = ms.get(title)
            if cur is None:
                if self.e.dry:
                    self.e.record("planned", f"milestone {title}")
                    continue
                body = {"title": title, "description": m.get("description") or ""}
                if m.get("due_on"):
                    body["due_date"] = m["due_on"][:10]
                cur = ms[title] = self.ok("POST", f"/projects/{pid}/milestones", body)
                self.e.record("copied", f"milestone {title}")
            if m.get("state") == "closed" and cur.get("state") != "closed" and not self.e.dry:
                self.ok("PUT", f"/projects/{pid}/milestones/{cur['id']}", {"state_event": "close"})
        return {"pid": pid, "milestones": {t: m["id"] for t, m in ms.items() if "id" in m}, "repo": repo}

    def issue_inventory(self, tracker):
        out = {}
        for i in self.pages(f"/projects/{tracker['pid']}/issues?scope=all"):
            url = gm.marker_of(i.get("description"))
            if url:
                out.setdefault(url, i["iid"])
        return out

    def create_issue(self, tracker, item, body, data):
        title = ("[PR] " if data.get("pull_request") is not None else "") + (data.get("title") or "")
        req = {"title": title[:255], "description": body,
               "labels": ",".join(l["name"] for l in data.get("labels") or [])}
        ms = (data.get("milestone") or {}).get("title")
        if ms and ms in tracker["milestones"]:
            req["milestone_id"] = tracker["milestones"][ms]
        return self.ok("POST", f"/projects/{tracker['pid']}/issues", req)["iid"]

    def comments(self, tracker, iid):
        return [n.get("body") or "" for n in self.pages(f"/projects/{tracker['pid']}/issues/{iid}/notes?sort=asc")
                if not n.get("system")]

    def add_comment(self, tracker, iid, body):
        self.ok("POST", f"/projects/{tracker['pid']}/issues/{iid}/notes", {"body": body})

    def is_closed(self, tracker, iid):
        return self.ok("GET", f"/projects/{tracker['pid']}/issues/{iid}").get("state") == "closed"

    def close_issue(self, tracker, iid):
        self.ok("PUT", f"/projects/{tracker['pid']}/issues/{iid}", {"state_event": "close"})

    def rewrite(self, tracker, repo, text):
        """Gitea attachments -> uploads of the GitLab project, remembered in the state so
        a file is uploaded once."""
        base = re.escape(self.e.gitea.base)
        pattern = re.compile(rf"(?:{base})?/(?:[\w.-]+/[\w.-]+/)?attachments/([0-9a-f-]{{36}})")
        uploads = self.e.state.setdefault(repo["full_name"], {}).setdefault("uploads", {})

        def one(m):
            key = m.group(1)
            if key not in uploads:
                try:
                    uploads[key] = self.upload_attachment(tracker, key)
                    self.e.save_state()
                except (RuntimeError, OSError) as err:
                    self.e.record("skipped", f"attachment {key}: {err}; the link points to Gitea")
                    return self.e.gitea.public + f"/attachments/{key}"
            return uploads[key]
        return pattern.sub(one, text or "")

    def upload_attachment(self, tracker, key):
        import urllib.request
        d = os.path.join(self.e.here, "work", "tmp", uuid.uuid4().hex)
        os.makedirs(d)
        try:
            req = urllib.request.Request(f"{self.e.gitea.base}/attachments/{key}", headers=self.e.gitea.headers())
            with urllib.request.urlopen(req, timeout=600) as r:
                cd = r.headers.get("Content-Disposition") or ""
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
                name = gm.safe_segment(urllib.parse.unquote(m.group(1)) if m else key)
                path = os.path.join(d, name)
                with open(path, "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
            up = self.ok("POST", f"/projects/{tracker['pid']}/uploads", body=gm.FileBody(path, "file"), timeout=None)
            return self.web + up["full_path"] if up.get("full_path") else self.web + up["url"]
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    # releases
    def release_target(self, handle, repo):
        return {"pid": handle["id"], "repo": repo, "ref": handle.get("default_branch") or repo.get("default_branch") or "main"}

    def release_files(self, target, r):
        status, data, _ = self.call("GET", f"/projects/{target['pid']}/releases/{enc(r['tag_name'])}")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"GitLab release {r['tag_name']}: {status} {gm.message(data)}")
        return {l["name"] for l in self.pages(f"/projects/{target['pid']}/releases/{enc(r['tag_name'])}/assets/links")}

    def asset_name(self, name):
        return name

    def create_release(self, target, r, notes):
        body = {"name": r.get("name") or r["tag_name"], "tag_name": r["tag_name"], "description": notes,
                "ref": r.get("target_commitish") or target["ref"]}
        if r.get("published_at"):
            body["released_at"] = r["published_at"]
        self.ok("POST", f"/projects/{target['pid']}/releases", body)

    def upload_file(self, target, r, name, path):
        pkg = re.sub(r"[^A-Za-z0-9._-]", "-", target["repo"]["name"]).strip(".-") or "release"
        ver = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._+-]", "-", r["tag_name"])).strip(".-") or "0"
        fname = re.sub(r"[^A-Za-z0-9._+~-]", "_", name)
        where = f"/projects/{target['pid']}/packages/generic/{enc(pkg)}/{enc(ver)}/{enc(fname)}"
        self.ok("PUT", where, body=gm.FileBody(path), timeout=None)
        self.ok("POST", f"/projects/{target['pid']}/releases/{enc(r['tag_name'])}/assets/links",
                {"name": name, "url": self.api + where, "link_type": "package"})

    # avatars
    def owner_avatar(self, dest, path):
        """A group's avatar; for a user namespace, only the token's own user can be set."""
        status, group, _ = self.call("GET", f"/groups/{enc(dest)}")
        if status == 200:
            self.ok("PUT", f"/groups/{group['id']}", body=gm.FileBody(path, "avatar"))
            return True
        status, me, _ = self.call("GET", "/user")
        if status == 200 and me.get("username", "").lower() == dest.lower():
            self.ok("PUT", "/user/avatar", body=gm.FileBody(path, "avatar"))
            return True
        self.e.record("skipped", f"avatar of {dest}: a GitLab user other than the token's own; GitLab's API cannot set it")
        return False

    def repo_avatar(self, handle, repo, path):
        self.ok("PUT", f"/projects/{handle['id']}", body=gm.FileBody(path, "avatar"))
        return True

    # checks
    def check_online(self, e):
        out = []
        status, data, _ = self.call("GET", "/user")
        out.append((f"{self.web}: " + (f"token of {data.get('username')}" if status == 200 else f"API {status} {gm.message(data)}"),
                    status == 200))
        if self.mode == "ssh":
            r = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", f"git@{self.host}"],
                               capture_output=True, text=True)
            good = "Welcome" in (r.stdout + r.stderr)
            out.append((f"ssh git@{self.host}: " + ("SSH key accepted" if good else (r.stderr or r.stdout).strip()[-200:]), good))
        for owner in sorted({s.split("/")[0] for s in e.cfg.sources() if s not in gm.SPECIAL}):
            dest = e.destination(owner)
            status, data, _ = self.call("GET", f"/namespaces/{enc(dest)}")
            out.append((f"{owner} -> {dest}: " + (f"{data.get('kind')} found" if status == 200 else f"not found ({status})"),
                        status == 200))
        return out


if __name__ == "__main__":
    gm.main(GitLab(), HERE, __doc__)

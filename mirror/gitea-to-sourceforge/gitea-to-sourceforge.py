#!/usr/bin/env python3
"""Mirror repositories from the local Gitea to SourceForge: git branches and tags, issues
and pull requests with their comments, and releases with their files.

  ./gitea-to-sourceforge.py                  menu: sources, settings, destinations, checks
  ./gitea-to-sourceforge.py --all            every configured step for every source (cron)
  ./gitea-to-sourceforge.py issues releases  only these steps
  ./gitea-to-sourceforge.py --source wekan   only this source
  ./gitea-to-sourceforge.py --check-online   read-only: can Gitea and SourceForge be reached
  ./gitea-to-sourceforge.py --check-missing  read-only: what is not mirrored yet

Settings are in gitea-to-sourceforge.ini next to this file (mode 600, holds the token),
the progress in gitea-to-sourceforge-state.json, and every run's log in WeKan's
.tools/log/mirror/gitea-to-sourceforge/YYYY-MM-DD_HH-MM-SS.txt.

SOURCEFORGE - the same layout as WeKan's releases/mirror.sh, so both find each other's work
  - A Gitea owner's repositories go to ONE SourceForge project: the project from the
    menu's Destinations, or the setting project (default: the Gitea owner's name). A
    SourceForge project cannot be created through its API: make it at sourceforge.net.
  - Each repository is a Git tool of that project. The repository named like the project
    is the tool "code" (sourceforge.net/p/<project>/code); every other one is the tool
    repo-<name>-<hash>, created when missing. git goes over SSH with your SSH key
    registered at SourceForge (setting user); never forced, never deleting anything.
  - Issues and pull requests (as "[PR] ...") become tickets of a Tracker tool: for the
    repository named like the project the project's existing tracker, otherwise
    repo-<name>-issues-<hash>, created when missing. Needs the OAuth token.
  - Release files go to the project's Files over sftp:
    <files_dir>[/<repository>]/<tag>/, with README.md holding the release notes. Each
    file is uploaded as .part and renamed when complete, so an interrupted upload is
    never mistaken for a finished file.
  - SourceForge projects are public: a private Gitea repository is not mirrored.
    SourceForge's wiki is not a git repository, so the wiki is not mirrored.
"""

import hashlib
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import gitea_mirror as gm  # noqa: E402

REST = "https://sourceforge.net/rest/p"


def mount(name, suffix=""):
    """The mount point WeKan's tools/mirror-repository.mjs sourceForgeMount() gives."""
    value = f"{name}{suffix}"
    return f"repo-{re.sub(r'[^A-Za-z0-9-]', '-', value)[:60]}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def quoted(path):
    if re.search(r'["\r\n]', path):
        raise RuntimeError(f"sftp cannot safely use the path {path!r}")
    return f'"{path}"'


class SourceForge:
    NAME = "gitea-to-sourceforge"
    TITLE = "SourceForge"
    DESTINATION_WORD = "SourceForge project"
    DEFAULT_KEY = "[sourceforge] project"
    DESTINATION_HELP = ("a Git tool of the SourceForge project named in Destinations\n"
                        "  (default: the same name as its Gitea owner). The project must exist.")
    INI_HEADER = """\
# gitea-to-sourceforge.ini - settings of gitea-to-sourceforge.py. Edit them from the menu
# (./gitea-to-sourceforge.py) or by hand. This file holds a token: mode 600.
#
# [sources] list: one source per line.
#   wekan/wekan   mirrors only this Gitea repository
#   wekan         mirrors EVERY repository of the Gitea organization or user wekan
#   (all)         every repository the Gitea token can read; (allpublic), (allprivate)
# [destinations]: <gitea owner> = <SourceForge project>, when it is not the same name.
"""
    SETTINGS = [
        ("sourceforge", "token", "", "SourceForge OAuth bearer token (tickets, creating tools)"),
        ("sourceforge", "user", "", "SourceForge user name for ssh: git push and release files (your SSH key)"),
        ("sourceforge", "project", "{owner}", "SourceForge project of a Gitea owner without a Destinations line"),
        ("sourceforge", "files_dir", "GitHub-releases", "directory of release files in the project's Files (WeKan's mirror uses GitHub-releases)"),
        ("sourceforge", "open_status", "open", "ticket status of an open issue"),
        ("sourceforge", "closed_status", "closed", "ticket status of a closed issue"),
    ]

    def missing_settings(self, cfg):
        return [f"[sourceforge] {k}" for k in ("token", "user") if not cfg.get(k, "sourceforge")]

    def configure(self, e):
        self.e = e
        self.token = e.cfg.get("token", "sourceforge")
        self.user = e.cfg.get("user", "sourceforge")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.user):
            raise SystemExit("[sourceforge] user is not a valid SourceForge user name")
        self.files_dir = e.cfg.get("files_dir", "sourceforge").strip("/") or "GitHub-releases"
        self.open_status = e.cfg.get("open_status", "sourceforge") or "open"
        self.closed_status = e.cfg.get("closed_status", "sourceforge") or "closed"

    def default_destination(self, cfg):
        return cfg.get("project", "sourceforge") or "{owner}"

    def where(self, cfg):
        return "https://sourceforge.net"

    def describe(self, dest, repo):
        return f"https://sourceforge.net/p/{dest}/{self.git_mount(dest, repo['name'])}"

    @staticmethod
    def git_mount(project, name):
        return "code" if name == project else mount(name)

    # REST
    def call(self, method, path, form=None):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return gm.http(method, f"{REST}/{path}", headers, form=form)

    def ok(self, method, path, form=None):
        status, data, _ = self.call(method, path, form)
        if status not in (200, 201):
            raise RuntimeError(f"SourceForge {method} {path}: {status} {gm.message(data)}")
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"SourceForge {method} {path}: {data.get('info') or data}")
        return data or {}

    def pages(self, path, key):
        page = 0
        while True:
            sep = "&" if "?" in path else "?"
            data = self.ok("GET", f"{path}{sep}limit=100&page={page}")
            items = (data.get(key) if key in data else (data.get("thread") or {}).get(key)) or []
            yield from items
            if len(items) < 100:
                return
            page += 1

    def install(self, project, tool, mount_point, label):
        """A tool of the project, created when missing; refuses a mount point another tool has."""
        info = self.ok("GET", project)
        tools = {t["mount_point"]: t for t in info.get("tools") or []}
        if mount_point in tools:
            if tools[mount_point].get("name") != tool:
                raise gm.RepoError(f"SourceForge {project}/{mount_point} is already a {tools[mount_point].get('name')} tool")
            return False
        if self.e.dry:
            self.e.record("planned", f"SourceForge {tool} tool {project}/{mount_point}")
            return True
        self.ok("POST", f"{project}/admin/install_tool",
                {"tool": tool, "mount_point": mount_point, "mount_label": label[:50], "order": "last"})
        if mount_point not in {t["mount_point"] for t in self.ok("GET", project).get("tools") or []}:
            raise gm.RepoError(f"SourceForge did not confirm the {tool} tool {project}/{mount_point}")
        self.e.record("copied", f"SourceForge {tool} tool {project}/{mount_point}")
        return True

    # the repository
    def ensure_repo(self, project, repo):
        if repo.get("private"):
            self.e.record("skipped", f"{repo['full_name']}: private; SourceForge projects are public")
            return None
        status, info, _ = self.call("GET", project)
        if status == 404:
            raise gm.RepoError(f"SourceForge project {project} does not exist: create it at "
                               f"https://sourceforge.net/create/ (its API cannot), or set the destination in the menu")
        if status != 200:
            raise gm.RepoError(f"SourceForge project {project}: {status} {gm.message(info)}")
        m = self.git_mount(project, repo["name"])
        if self.install(project, "git", m, repo["name"]) and self.e.dry:
            return None
        return {"project": project, "mount": m}

    def git_url(self, handle, repo):
        return f"ssh://{self.user}@git.code.sf.net/p/{handle['project']}/{handle['mount']}"

    def wiki_url(self, handle, repo):
        return ""

    def git_env(self):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        return env

    def after_git(self, handle, repo):
        pass

    # issues
    def issue_tracker(self, handle, repo, labels, milestones):
        project = handle["project"]
        tools = self.ok("GET", project).get("tools") or []
        trackers = [t["mount_point"] for t in tools if t.get("name") == "tickets"]
        if repo["name"] == project:
            # The repository named like the project uses the project's tracker - the one
            # WeKan's mirror made (github-issues), or its only one.
            want = "github-issues" if "github-issues" in trackers else (trackers[0] if len(trackers) == 1 else "issues")
        else:
            want = mount(repo["name"], "-issues")
        if want not in trackers:
            self.install(project, "tickets", want, f"{repo['name']} issues")
            if self.e.dry:
                return {"project": project, "mount": want, "new": True}
        return {"project": project, "mount": want}

    def issue_inventory(self, tracker):
        if tracker.get("new"):
            return {}
        # The index has no descriptions, so every ticket is read once; the state then
        # remembers which ticket each item became and this is not needed again.
        out = {}
        base = f"{tracker['project']}/{tracker['mount']}"
        for t in self.pages(f"{base}/", "tickets"):
            detail = self.ok("GET", f"{base}/{t['ticket_num']}").get("ticket") or {}
            url = gm.marker_of(detail.get("description"))
            if url:
                out.setdefault(url, t["ticket_num"])
        return out

    def create_issue(self, tracker, item, body, data):
        title = ("[PR] " if data.get("pull_request") is not None else "") + (data.get("title") or "")
        res = self.ok("POST", f"{tracker['project']}/{tracker['mount']}/new", {
            "ticket_form.summary": title[:250], "ticket_form.description": body,
            "ticket_form.labels": ",".join(l["name"] for l in data.get("labels") or []),
            "ticket_form.status": self.open_status})
        num = (res.get("ticket") or {}).get("ticket_num") or res.get("ticket_num")
        if not num:
            raise RuntimeError("SourceForge did not return the new ticket's number")
        return num

    def detail(self, tracker, num):
        return self.ok("GET", f"{tracker['project']}/{tracker['mount']}/{num}").get("ticket") or {}

    def thread(self, tracker, num):
        t = (self.detail(tracker, num).get("discussion_thread") or {}).get("_id")
        if not t:
            raise RuntimeError(f"SourceForge ticket {num} has no discussion thread")
        return t

    def comments(self, tracker, num):
        t = self.thread(tracker, num)
        return [p.get("text") or "" for p in self.pages(f"{tracker['project']}/{tracker['mount']}/_discuss/thread/{t}", "posts")]

    def add_comment(self, tracker, num, body):
        t = self.thread(tracker, num)
        self.ok("POST", f"{tracker['project']}/{tracker['mount']}/_discuss/thread/{t}/new", {"text": body})

    def is_closed(self, tracker, num):
        return self.detail(tracker, num).get("status") == self.closed_status

    def close_issue(self, tracker, num):
        full = self.detail(tracker, num)
        # Allura saves a whole validated form: a status-only form can erase other fields.
        form = {"ticket_form.summary": full.get("summary") or "", "ticket_form.description": full.get("description") or "",
                "ticket_form.status": self.closed_status, "ticket_form.labels": ",".join(full.get("labels") or []),
                "ticket_form.assigned_to": full.get("assigned_to") or "",
                "ticket_form.private": str(bool(full.get("private"))),
                "ticket_form.discussion_disabled": str(bool(full.get("discussion_disabled")))}
        for k, v in (full.get("custom_fields") or {}).items():
            form[f"ticket_form.custom_fields.{k}"] = "" if v is None else str(v)
        self.ok("POST", f"{tracker['project']}/{tracker['mount']}/{num}/save", form)

    def rewrite(self, tracker, repo, text):
        # Links into the local Gitea point to its public address instead.
        base = self.e.gitea.base
        return (text or "").replace(base + "/", self.e.gitea.public + "/") if self.e.gitea.public != base else text

    # releases: the project's Files, over sftp
    def sftp(self, script):
        r = subprocess.run(["sftp", "-oBatchMode=yes", "-oConnectTimeout=30", "-b", "-", f"{self.user}@frs.sourceforge.net"],
                           input=script, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"sftp: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    def release_target(self, handle, repo):
        root = f"/home/frs/project/{handle['project']}/{self.files_dir}"
        parent = root if repo["name"] == handle["project"] else f"{root}/{gm.safe_segment(repo['name'])}"
        return {"root": root, "parent": parent}

    def release_files(self, target, r):
        d = f"{target['parent']}/{gm.safe_segment(r['tag_name'])}"
        out = self.sftp(f"-ls -1 {quoted(d)}\n")
        names = {line.strip().split("/")[-1] for line in out.splitlines()
                 if line.strip() and not line.startswith("sftp>")}
        return names or None

    def asset_name(self, name):
        return gm.safe_segment(name)

    def put(self, target, r, name, path):
        d = f"{target['parent']}/{gm.safe_segment(r['tag_name'])}"
        dest = f"{d}/{gm.safe_segment(name)}"
        self.sftp(f"-mkdir {quoted(target['root'])}\n-mkdir {quoted(target['parent'])}\n-mkdir {quoted(d)}\n"
                  f"-rm {quoted(dest + '.part')}\nput {quoted(path)} {quoted(dest + '.part')}\n"
                  f"rename {quoted(dest + '.part')} {quoted(dest)}\n")

    def create_release(self, target, r, notes):
        work = os.path.join(self.e.here, "work")
        os.makedirs(work, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as d:
            p = os.path.join(d, "README.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"# {r.get('name') or r['tag_name']}\n\n{notes}\n")
            self.put(target, r, "README.md", p)

    def upload_file(self, target, r, name, path):
        self.put(target, r, name, path)

    # avatars: SourceForge's REST API has no project icon - it is set in the project's
    # admin pages - so they are reported, once per run, not copied.
    def owner_avatar(self, dest, path):
        if not getattr(self, "_icon_reported", False):
            self._icon_reported = True
            self.e.record("skipped", "avatars: SourceForge has no API for project icons; set the icon in the project's admin")
        return False

    def repo_avatar(self, handle, repo, path):
        return self.owner_avatar(handle["project"], path)

    # checks
    def check_online(self, e):
        out = []
        projects = sorted({e.destination(s.split("/")[0]) for s in e.cfg.sources() if s not in gm.SPECIAL})
        for p in projects:
            status, data, _ = self.call("GET", p)
            out.append((f"project {p}: " + ("found" if status == 200 else f"{status} {gm.message(data)}"), status == 200))
        try:
            self.sftp("pwd\n")
            out.append((f"sftp {self.user}@frs.sourceforge.net: SSH key accepted", True))
        except RuntimeError as err:
            out.append((f"sftp {self.user}@frs.sourceforge.net: {err}", False))
        if not projects:
            out.append(("no organization or user sources, so no project to check", True))
        return out


if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "work"), exist_ok=True)
    gm.main(SourceForge(), HERE, __doc__)

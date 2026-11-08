#!/usr/bin/env python3
"""Mirror repositories from the local Gitea to SourceForge: git branches and tags, issues
and pull requests with their comments, and releases with their files - by default the
newest release only, every release for the repositories in the menu's "Releases kept in
full".

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
    repo-<name>-issues-<hash>, created when missing. Needs the OAuth2 sign-in below.
  - Release files go to the project's Files over sftp:
    <files_dir>[/<repository>]/<tag>/, with README.md holding the release notes. Each
    file is uploaded as .part and renamed when complete, so an interrupted upload is
    never mistaken for a finished file.
  - SourceForge projects are public: a private Gitea repository is not mirrored.
    SourceForge's wiki is not a git repository, so the wiki is not mirrored.

SIGNING IN - an OAuth2 application of your own
  1. At https://sourceforge.net/auth/oauth/ register an OAuth2 application, with a
     redirect URL such as https://localhost/ (SourceForge takes only https://). The
     script sends none, so SourceForge uses the application's first redirect URL; the
     setting redirect_uri names another of them, exactly as registered.
  2. Put its Client ID and Client Secret in the settings (client_id, client_secret).
  3. Menu 10) Sign in to SourceForge: open the address it shows, authorize, and paste
     the address the browser then goes to - a page that does not load is fine, the
     code is in its address. The access token and its refresh token are saved in the
     settings file, and the access token is renewed with the refresh token when it
     expires, so cron runs need no browser after that.
"""

import base64
import hashlib
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import gitea_mirror as gm  # noqa: E402

# SourceForge's REST API, as https://sourceforge.net/api-docs/ documents it: OAuth2 with
# an application's Client ID and Client Secret, tickets created and saved as
# multipart/form-data, comments posted as a urlencoded form. The API has no file
# releases: those go over sftp to frs.sourceforge.net, as SourceForge documents.
REST = "https://sourceforge.net/rest/p"
AUTHORIZE = "https://sourceforge.net/auth/oauth2/authorize"
TOKEN = "https://sourceforge.net/rest/oauth2/token"
OAUTH_SECTION = "sourceforge_oauth2"  # tokens; written by the sign-in, not by the menu
# What a bearer token may hold (RFC 6750 b64token): nothing that could end the header.
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def pkce_pair():
    """(code_verifier, S256 code_challenge) - api-docs: 43-128 characters, base64url of SHA-256."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def code_from(answer, state):
    """The authorization code from the pasted redirect address, or a code pasted alone."""
    answer = answer.strip()
    if "?" not in answer and "=" not in answer:
        return answer
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(answer).query or answer.split("?", 1)[-1]))
    if q.get("error"):
        raise SystemExit(f"SourceForge did not authorize: {q.get('error_description') or q['error']}")
    if q.get("state") != state:
        raise SystemExit("The address is not the answer to this sign-in (its state differs): sign in again")
    if not q.get("code"):
        raise SystemExit("The address has no code: paste the whole address the browser went to")
    return q["code"]


def mount(name, suffix=""):
    """The mount point WeKan's tools/mirror-repository.mjs sourceForgeMount() gives."""
    value = f"{name}{suffix}"
    return f"repo-{re.sub(r'[^A-Za-z0-9-]', '-', value)[:60]}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def tf(value):
    """A boolean as the API takes it: "true" or "false"."""
    return "true" if value else "false"


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
        ("sourceforge", "client_id", "", "Client ID of your OAuth2 application at https://sourceforge.net/auth/oauth/"),
        ("sourceforge", "client_secret", "", "Client Secret of the same OAuth2 application"),
        ("sourceforge", "redirect_uri", "",
         "empty = the application's own (first) redirect URL; else one registered there, exactly the same"),
        ("sourceforge", "user", "", "SourceForge user name for ssh: git push and release files (your SSH key)"),
        ("sourceforge", "project", "{owner}", "SourceForge project of a Gitea owner without a Destinations line"),
        ("sourceforge", "files_dir", "GitHub-releases", "directory of release files in the project's Files (WeKan's mirror uses GitHub-releases)"),
        ("sourceforge", "open_status", "open", "ticket status of an open issue"),
        ("sourceforge", "closed_status", "closed", "ticket status of a closed issue"),
    ]

    MENU_EXTRA = [("Sign in to SourceForge (OAuth2: Client ID and Client Secret in Settings)", "sign_in")]

    def missing_settings(self, cfg):
        return [f"[sourceforge] {k}" for k in ("client_id", "client_secret", "user") if not cfg.get(k, "sourceforge")]

    def configure(self, e):
        self.e = e
        self.client_id = e.cfg.get("client_id", "sourceforge").strip()
        self.client_secret = e.cfg.get("client_secret", "sourceforge").strip()
        cfg = e.cfg.cfg
        # redirect_uri was https://localhost/ by default, and SourceForge refuses it
        # ("Mismatching redirect URI") unless the application registered exactly that.
        # Now empty is the default - the application's own - and the old default goes, once.
        if not cfg.has_option("migrations", "sourceforge_redirect_uri_registered"):
            if cfg.get("sourceforge", "redirect_uri", fallback="").strip() == "https://localhost/":
                cfg.set("sourceforge", "redirect_uri", "")
            if not cfg.has_section("migrations"):
                cfg.add_section("migrations")
            cfg.set("migrations", "sourceforge_redirect_uri_registered", "done")
            e.cfg.save()
        self.redirect_uri = e.cfg.get("redirect_uri", "sourceforge").strip()
        self.files_dir = e.cfg.get("files_dir", "sourceforge").strip("/") or "GitHub-releases"
        self.open_status = e.cfg.get("open_status", "sourceforge") or "open"
        self.closed_status = e.cfg.get("closed_status", "sourceforge") or "closed"
        self.user = e.cfg.get("user", "sourceforge")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.user):
            raise SystemExit("[sourceforge] user is not a valid SourceForge user name")
        if self.redirect_uri and not self.redirect_uri.startswith("https://"):
            raise SystemExit("[sourceforge] redirect_uri must begin with https://, as SourceForge requires")
        old = cfg.get("sourceforge", "token", fallback="").strip()
        if cfg.has_option("sourceforge", "token"):
            # A bearer token made by hand (the earlier setting token) still works until it
            # is replaced by signing in; it has no refresh token and does not expire.
            if old and not self.oauth("access_token"):
                if not cfg.has_section(OAUTH_SECTION):
                    cfg.add_section(OAUTH_SECTION)
                cfg.set(OAUTH_SECTION, "access_token", old)
            cfg.remove_option("sourceforge", "token")
            e.cfg.save()
        self.token = self.oauth("access_token")
        if self.token and not TOKEN_FORMAT.fullmatch(self.token):
            raise SystemExit(f"[{OAUTH_SECTION}] access_token is not a bearer token: sign in again (menu 10)")

    # OAuth2: https://sourceforge.net/api-docs/ - authorization code, then refresh token
    def oauth(self, key):
        return self.e.cfg.cfg.get(OAUTH_SECTION, key, fallback="").strip()

    def save_tokens(self, data):
        if not isinstance(data, dict) or not data.get("access_token"):
            raise SystemExit(f"SourceForge gave no access token: {gm.message(data)}")
        if not TOKEN_FORMAT.fullmatch(data["access_token"]):
            raise SystemExit("SourceForge's access token has characters a bearer token cannot have")
        cfg = self.e.cfg.cfg
        if not cfg.has_section(OAUTH_SECTION):
            cfg.add_section(OAUTH_SECTION)
        cfg.set(OAUTH_SECTION, "access_token", data["access_token"])
        # A refresh answer may leave the refresh token out: the old one is still good.
        if data.get("refresh_token"):
            cfg.set(OAUTH_SECTION, "refresh_token", data["refresh_token"])
        expires = data.get("expires_in")
        cfg.set(OAUTH_SECTION, "expires_at", str(int(time.time()) + int(expires)) if str(expires or "").isdigit() else "")
        self.e.cfg.save()
        self.token = data["access_token"]

    def token_request(self, form):
        status, data, _ = gm.http("POST", TOKEN, form=dict(form, client_id=self.client_id, client_secret=self.client_secret))
        if status != 200:
            raise SystemExit(f"SourceForge {TOKEN}: {status} {gm.message(data)}")
        self.save_tokens(data)

    def refresh(self):
        """A new access token from the refresh token; False when there is none to use."""
        rt = self.oauth("refresh_token")
        if not rt:
            return False
        self.token_request({"grant_type": "refresh_token", "refresh_token": rt})
        return True

    def ensure_token(self):
        if not self.token:
            if sys.stdin.isatty() and not getattr(self, "_asked", False):
                self._asked = True
                if gm.ask("Not signed in to SourceForge. Sign in now? (y/n)", "y").lower().startswith("y"):
                    self.sign_in(self.e)
                    return
            raise SystemExit("Not signed in to SourceForge: menu 10) Sign in to SourceForge")
        expires = self.oauth("expires_at")
        if expires.isdigit() and int(expires) - 60 <= time.time():
            self.refresh()

    def sign_in(self, e):
        """The authorization code flow, with PKCE: the browser authorizes, the address comes back here."""
        missing = [m for m in self.missing_settings(e.cfg) if "user" not in m]
        if missing:
            raise SystemExit(f"Missing {', '.join(missing)}: register an OAuth2 application at "
                             "https://sourceforge.net/auth/oauth/ and put them in Settings")
        self.configure(e)  # the settings as they are now
        state = secrets.token_urlsafe(16)
        verifier, challenge = pkce_pair()
        # Without redirect_uri, SourceForge (oauthlib) uses the application's first
        # registered one in both requests, so they always match it.
        redirect = {"redirect_uri": self.redirect_uri} if self.redirect_uri else {}
        url = AUTHORIZE + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", **redirect,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        where = self.redirect_uri or "the application's redirect URL"
        print(f"""
Open this address in a browser, sign in to SourceForge and press Authorize:

  {url}

The browser then goes to {where} with ?code=...&state=... at its end - the page may
not load, that is fine. Copy that whole address from the browser's address bar (not
the address above) and paste it here.
If SourceForge says "Mismatching redirect URI": empty the setting redirect_uri, or
set it exactly as the application's redirect URL at https://sourceforge.net/auth/oauth/.""")
        answer = gm.ask("Address (or the code alone)")
        if not answer.strip():
            raise SystemExit("Nothing pasted: sign in again (menu 10) and paste the address the browser went to")
        if answer.strip().startswith(AUTHORIZE):
            raise SystemExit("That is the sign-in address itself: paste the address the browser went to "
                             "after Authorize, the one with ?code=")
        code = code_from(answer, state)
        self.token_request({"grant_type": "authorization_code", "code": code, **redirect, "code_verifier": verifier})
        gm.log("Signed in to SourceForge: the access token and refresh token are saved in "
               f"{os.path.basename(e.ini)} [{OAUTH_SECTION}].")

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
    def call(self, method, path, form=None, multipart=False, retried=False):
        self.ensure_token()
        headers = {"Authorization": f"Bearer {self.token}"}
        if multipart:
            r = gm.http(method, f"{REST}/{path}", headers, form=form, multipart=True)
        else:
            r = gm.http(method, f"{REST}/{path}", headers, form=form)
        # An access token revoked or expired early: renew it once and ask again.
        if r[0] == 401 and not retried and self.refresh():
            return self.call(method, path, form, multipart, retried=True)
        return r

    def ok(self, method, path, form=None, multipart=False):
        status, data, _ = self.call(method, path, form, multipart)
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
        for t in self.pages(base, "tickets"):
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
            "ticket_form.status": self.open_status}, multipart=True)
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
                "ticket_form.private": tf(full.get("private")),
                "ticket_form.discussion_disabled": tf(full.get("discussion_disabled"))}
        for k, v in (full.get("custom_fields") or {}).items():
            # The API names a custom field ticket_form.custom_fields._<name>.
            name = k if k.startswith("_") else f"_{k}"
            form[f"ticket_form.custom_fields.{name}"] = "" if v is None else (tf(v) if isinstance(v, bool) else str(v))
        self.ok("POST", f"{tracker['project']}/{tracker['mount']}/{num}/save", form, multipart=True)

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
        work = gm.mirror_tmp_dir(self.NAME)
        os.makedirs(work, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as d:
            p = os.path.join(d, "README.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"# {r.get('name') or r['tag_name']}\n\n{notes}\n")
            self.put(target, r, "README.md", p)

    def upload_file(self, target, r, name, path):
        self.put(target, r, name, path)

    # The release's notes are its README.md: an older release keeps it, and loses the rest.
    NOTES_FILES = ("README.md",)

    def delete_files(self, target, r, names):
        d = f"{target['parent']}/{gm.safe_segment(r['tag_name'])}"
        self.sftp("".join(f"-rm {quoted(d + '/' + gm.safe_segment(n))}\n" for n in names))

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
        try:
            self.ensure_token()
            out.append(("signed in (OAuth2)" + (", the access token is renewed with its refresh token"
                                               if self.oauth("refresh_token") else ""), True))
        except SystemExit as err:
            out.append((str(err), False))
            projects = []
        for p in projects:
            status, data, _ = self.call("GET", p)
            out.append((f"project {p}: " + ("found" if status == 200 else f"{status} {gm.message(data)}"), status == 200))
            if status != 200:
                continue
            # The token may create tools (admin/install_tool) only for a project admin.
            status, data, _ = self.call("GET", f"{p}/has_access?user={self.user}&perm=admin")
            admin = status == 200 and isinstance(data, dict) and data.get("result") is True
            out.append((f"project {p}: the token " + ("may administer it" if admin else
                        f"may not administer it ({status} {gm.message(data)}): tools cannot be created"), admin))
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

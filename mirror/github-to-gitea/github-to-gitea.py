#!/usr/bin/python3
"""Import repositories, organizations and users from GitHub into a local Gitea so that they look
in Gitea the way they do on GitHub: issues as issues, pull requests as real pull requests,
with the same numbers, comments, labels, milestones, wikis and releases - and GitHub
Actions workflows in Gitea Actions form, so that the release files are built locally.

  ./github-to-gitea.py                     menu: sources, settings, steps, help
  ./github-to-gitea.py --all               every configured step for every source (cron)
  ./github-to-gitea.py issues releases     only these steps
  ./github-to-gitea.py --source wekan      only this source

Settings and sources are in github-to-gitea.ini (next to this file, mode 600).
The source "wekan/wekan" imports one repository. The source "wekan" (an organization)
or "xet7" (a user) imports every repository that organization or user OWNS on GitHub into
the Gitea organization or user of the same name; when Gitea has neither, an organization
is created. The special sources import every repository the GitHub token can write to,
of every owner, each into the Gitea organization or user named like its GitHub owner:
  (all)         private and public repositories with write access
  (allpublic)   public repositories with write access
  (allprivate)  private repositories with write access
Organizations and users the token has no access to can be sources too: their public
repositories are read without the token, which is slower (60 requests an hour).
GitHub's rate limits are waited out, and an interrupted run resumes where it stopped.

STEPS
  migrate   Gitea's own GitHub migration (POST /repos/migrate). Only it keeps the numbers:
            on GitHub issues and pull requests share one number space, and Gitea's API
            cannot set a number. If the repository was already imported wrongly (the
            numbers do not match) and replace_broken is on, a correct import is made as
            <name>-github, checked, and the names are swapped:
            old -> <name>-old-YYYYMMDD (not deleted), new -> <name>.
  git       fetches GitHub's branches, tags and refs/pull/*/head into the bare repository
            (setting bare_repo) - a local git fetch, no push - so that the commits of new
            pull requests are in Gitea. Without the bare repository on this machine, a new
            pull request's head commit is fetched from GitHub into tmp/ beside the script
            and pushed to Gitea as its branch github-pr/<N>, and nothing else is pushed.
  issues    new numbers in order (a pull request as a real one, from branch
            github-pr/<N>), and changes to existing ones: title, body, state, labels, new
            comments. If a pull request cannot be made a real one, the repository stops
            instead of making it an issue (the numbers would get out of step).
  releases  missing releases, with the tag on the right commit; files according to the
            setting release_assets (github / local / none). By default only the
            newest release keeps its files (setting release_assets_keep: newest, all,
            or a number of newest releases); the files of older ones are deleted
            from Gitea. A file from GitHub is downloaded alone to WeKan's
            .tools/tmp/github-to-gitea/, uploaded to Gitea, and deleted.
  actions   .github/workflows -> branch actions_branch (main + .gitea/workflows + .gitea/gh).
            See the menu item "Help: Gitea Actions".
  avatars   the avatar of each GitHub organization or user onto its Gitea organization
            (or the token's own Gitea user), and a repository's custom social preview
            image - GitHub's only repository picture - as its Gitea avatar. An image is
            uploaded again only when it changes.
"""

import argparse
import base64
import configparser
import datetime
import hashlib
import json
import mimetypes
import shutil
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
INI_FILE = os.path.join(HERE, "github-to-gitea.ini")
STATE_FILE = os.path.join(HERE, "github-to-gitea-state.json")
SHIM_FILE = os.path.join(HERE, "github-to-gitea-gh.py")
STEPS = ["migrate", "git", "issues", "releases", "actions", "avatars"]
# The default steps before "avatars" was added: a settings file still saying this gets it.
OLD_DEFAULT_STEPS = "migrate, git, issues, releases, actions"
STEP_NAMES = {"migrate": "migration (repository, issues, PRs, comments, labels, milestones, wiki)",
              "git": "git fetch into the bare repository (branches, tags, PR refs)",
              "issues": "new issues/PRs and changes to existing ones",
              "releases": "releases (and their files, when release_assets is not none)",
              "actions": "GitHub Actions -> Gitea Actions (branch actions_branch)",
              "avatars": "avatars of organizations, users and repositories"}

# Every setting, its default and its meaning: this is also what the menu shows.
SETTINGS = [
    ("gitea", "url", "http://localhost:2000", "address of Gitea"),
    ("gitea", "token", "", "Gitea token (admin, or access to the target organizations or users)"),
    ("github", "token", "", "GitHub token (migration, and 5000 requests/hour)"),
    ("options", "steps", ", ".join(STEPS), "steps that 'Run everything' runs"),
    ("options", "replace_broken", "no", "replace a wrongly imported repository by a correct migration (the old one stays as -old-<date>)"),
    ("options", "dry_run", "no", "dry run: show what would be done, change nothing"),
    ("options", "include_forks", "yes", "import the forks of an organization or user"),
    ("options", "include_archived", "yes", "import the archived repositories of an organization or user"),
    ("options", "wiki", "yes", "import the wiki in the migration"),
    ("options", "lfs", "yes", "import Git LFS files in the migration"),
    ("options", "release_assets", "github", "release files: github (download from GitHub) | local (assets_dir) | none"),
    ("options", "release_assets_keep", "newest", "releases that keep their files: newest | all | N (the N newest); the files of older releases are deleted from Gitea"),
    ("options", "assets_dir", "", "directory <tag>/<files> (release_assets = local); {owner} {name} allowed"),
    ("options", "assets_max_mb", "2048", "largest file to upload (Gitea's [attachment] MAX_SIZE)"),
    ("options", "bare_repo", "~/gitea/data/gitea-repositories/{owner}/{name}.git",
     "Gitea's bare repositories on this machine ({owner} {name}); git fetch only when found"),
    ("options", "actions_branch", "gitea-actions", "branch the converted workflows are written to"),
]
BARE_CANDIDATES = [
    "/var/lib/gitea/data/gitea-repositories/{owner}/{name}.git",
    "/run/media/user/L930G/gitea/data/gitea-repositories/{owner}/{name}.git",
]
CFG = configparser.ConfigParser(interpolation=None)
GITEA_URL = ""
GITHUB_REPO = ""
OWNER, NAME = "", ""
DRY = False
STORE = {}


class RepoError(Exception):
    """Processing of one repository stops; the next source goes on."""


def fail(msg):
    raise RepoError(msg)


def opt(key, section="options"):
    return CFG.get(section, key, fallback=next(d for s, k, d, _ in SETTINGS if s == section and k == key))


def flag(key):
    return opt(key).strip().lower() in ("yes", "true", "1", "on", "y")
ATTRIB = "> *From GitHub: "
# Bodies written by the Finnish version start like this; they are recognised too.
ATTRIBS = (ATTRIB, "> *GitHubista: ")


def log(msg):
    print(msg, flush=True)


# --- HTTP -------------------------------------------------------------------

class Body:
    """A multipart body read from disk in pieces: release files are gigabytes."""

    def __init__(self, path, field):
        name = os.path.basename(path)
        b = uuid.uuid4().hex
        self.ctype = f"multipart/form-data; boundary={b}"
        self.parts = [
            (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\n"
             f"Content-Type: {mimetypes.guess_type(name)[0] or 'application/octet-stream'}\r\n\r\n").encode(),
            open(path, "rb"),
            f"\r\n--{b}--\r\n".encode(),
        ]
        self.length = len(self.parts[0]) + os.path.getsize(path) + len(self.parts[2])

    def read(self, n=-1):
        while self.parts:
            p = self.parts[0]
            if isinstance(p, bytes):
                if not p:
                    self.parts.pop(0)
                    continue
                n2 = len(p) if n is None or n < 0 else n
                self.parts[0] = p[n2:]
                return p[:n2]
            chunk = p.read(n if n and n > 0 else 1 << 20)
            if chunk:
                return chunk
            p.close()
            self.parts.pop(0)
        return b""


# Below this many requests left, GitHub calls wait for the hourly reset: the token is
# shared with Gitea's own migration, which would otherwise run into the limit mid-import.
GITHUB_RESERVE = 50


def github_wait(headers, text, waits):
    """Seconds to wait before asking GitHub again, or 0 when the answer is not a rate limit.
    Primary limit: until X-RateLimit-Reset. Secondary limit: Retry-After, otherwise a
    growing pause (GitHub: at least a minute)."""
    after = headers.get("Retry-After")
    if after and after.isdigit():
        return int(after) + 1
    if headers.get("X-RateLimit-Remaining") == "0":
        return max(5, int(headers.get("X-RateLimit-Reset", "0")) - int(time.time()) + 5)
    if b"secondary rate limit" in text.lower() or b"abuse" in text.lower():
        return min(60 * 2 ** waits, 900)
    return 0


def http(method, url, token, body=None, stream=None, timeout=600, retry=True):
    headers = {"Accept": "application/json", "User-Agent": "github-to-gitea"}
    if token:
        headers["Authorization"] = f"token {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif stream is not None:
        data = stream
        headers["Content-Type"] = stream.ctype
        headers["Content-Length"] = str(stream.length)
    github = "api.github.com" in url
    attempt = waits = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read()
                left = r.headers.get("X-RateLimit-Remaining")
                if github and left and left.isdigit() and int(left) < GITHUB_RESERVE:
                    wait = max(5, int(r.headers.get("X-RateLimit-Reset", "0")) - int(time.time()) + 5)
                    log(f"  GitHub: {left} requests left this hour, waiting {wait} s for the reset...")
                    time.sleep(wait)
                return r.status, (json.loads(text) if text.strip() else None)
        except urllib.error.HTTPError as e:
            text = e.read()
            # A rate limit is not a failure: wait it out, it does not use up the attempts.
            wait = github_wait(e.headers, text, waits) if github and e.code in (403, 429) else 0
            if wait and waits < 20 and stream is None:
                waits += 1
                log(f"  GitHub rate limit reached{'' if token else ' (no token: 60 requests an hour)'}, "
                    f"waiting {wait} s...")
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < 5 and stream is None and retry:
                attempt += 1
                time.sleep(3 * attempt)
                continue
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, text[:500].decode(errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < 5 and stream is None and retry:
                attempt += 1
                time.sleep(3 * attempt)
                continue
            return 0, str(e)


# GitHub owners whose repositories the token cannot read (a fine-grained token limited to
# other owners answers 401/403 even for public repositories): they are read without it.
# That works for public repositories, but at 60 requests an hour instead of 5000.
NO_TOKEN = set()


def gh_owner(path):
    parts = path.split("/")
    return parts[1].lower() if len(parts) > 1 and parts[0] in ("repos", "users", "orgs") else ""


def github_token(owner):
    return None if owner.lower() in NO_TOKEN else os.environ["GITHUB_TOKEN"]


def gh(path, params=None):
    url = "https://api.github.com/" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    owner = gh_owner(path)
    token = github_token(owner)
    status, data = http("GET", url, token)
    if status in (401, 403) and token and owner:
        s2, d2 = http("GET", url, None)
        if s2 == 200:
            NO_TOKEN.add(owner)
            log(f"  GitHub: the token cannot read {owner}; reading its public repositories without "
                f"the token (60 requests an hour - slower).")
            status, data = s2, d2
    if status != 200:
        raise LookupError(f"GitHub {path}: {status} {data if isinstance(data, str) else (data or {}).get('message')}")
    return data


def gh_all(path, params=None):
    page = 1
    while True:
        data = gh(path, dict(params or {}, per_page=100, page=page))
        yield from data
        if len(data) < 100:
            return
        page += 1


def gitea(method, path, body=None, repo=None, **kw):
    """path starts with /; repo=None -> a path from the API root, otherwise /repos/<repo>."""
    url = GITEA_URL + (f"/repos/{repo}" if repo else "") + path
    if DRY and method != "GET":
        log(f"    [dry-run] {method} {path} {json.dumps(body, ensure_ascii=False)[:120] if body else ''}")
        return 200, {}
    return http(method, url, os.environ["GITEA_TOKEN"], body=body, **kw)


def gt(method, path, body=None, **kw):
    return gitea(method, path, body, repo=f"{OWNER}/{NAME}", **kw)


def gt_all(path, repo=None):
    """Every page. Stops on an empty page - and on a page that brings nothing new:
    Gitea ignores page for some endpoints (an issue's comments with ?since=) and
    answers every page with the same items, which was a loop without end."""
    sep = "&" if "?" in path else "?"
    seen, page = set(), 1
    while True:
        status, data = gitea("GET", f"{path}{sep}limit=50&page={page}", repo=repo or f"{OWNER}/{NAME}")
        if status != 200 or not data:
            return
        new = []
        for item in data:
            key = item.get("id", json.dumps(item, sort_keys=True)) if isinstance(item, dict) else json.dumps(item)
            if key not in seen:
                seen.add(key)
                new.append(item)
        if not new:
            return
        yield from new
        page += 1


def gt_total(repo, kind):
    url = f"{GITEA_URL}/repos/{repo}/issues?state=all&type={kind}&limit=1"
    req = urllib.request.Request(url, headers={"Authorization": f"token {os.environ['GITEA_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return int(r.headers.get("X-Total-Count", "0"))
    except Exception:
        return -1


# --- state and secrets -------------------------------------------------------

INI_HEADER = """\
# github-to-gitea.ini - settings of github-to-gitea.py. Edit them from the menu
# (./github-to-gitea.py) or by hand. This file holds tokens: mode 600.
#
# [sources] list: one source per line.
#   wekan/wekan   imports only this repository from GitHub into the Gitea repository wekan/wekan
#   wekan         imports EVERY repository of the organization or user into the Gitea organization or user wekan
#   xet7          the same for a user: github.com/xet7 -> Gitea xet7 (an organization is created if needed)
#   (all)         every private and public repository the GitHub token can write to
#   (allpublic)   every public repository the GitHub token can write to
#   (allprivate)  every private repository the GitHub token can write to
"""


def load_config():
    CFG.read_dict({"gitea": {}, "github": {}, "sources": {"list": ""}, "options": {}})
    if os.path.exists(INI_FILE):
        CFG.read(INI_FILE, encoding="utf-8")
    added = False
    # release_assets was "none" by default before release_assets_keep existed: a file
    # from then has none only as that old default, and gets the new one, github.
    if not CFG.has_option("options", "release_assets_keep") and \
            CFG.get("options", "release_assets", fallback="none").strip() == "none":
        CFG.set("options", "release_assets", "github")
        added = True
    for s, k, d, _ in SETTINGS:
        if not CFG.has_option(s, k):
            CFG.set(s, k, d)
            added = True
    if CFG.get("options", "steps", fallback="").strip() == OLD_DEFAULT_STEPS:
        CFG.set("options", "steps", ", ".join(STEPS))
        added = True
    if added:
        save_config()  # every setting visible in the file, with its default


def save_config():
    tmp = INI_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(INI_HEADER + "\n")
        CFG.write(f)
    os.replace(tmp, INI_FILE)


def sources():
    return [s.strip() for s in CFG.get("sources", "list", fallback="").replace(",", "\n").split("\n") if s.strip()]


def set_sources(items):
    CFG.set("sources", "list", "\n" + "\n".join(items) if items else "")


def apply_config():
    global GITEA_URL, DRY
    GITEA_URL = opt("url", "gitea").rstrip("/") + "/api/v1"
    os.environ["GITEA_TOKEN"] = opt("token", "gitea")
    os.environ["GITHUB_TOKEN"] = opt("token", "github")
    DRY = flag("dry_run")
    missing = [n for n, v in (("[gitea] token", os.environ["GITEA_TOKEN"]),
                              ("[github] token", os.environ["GITHUB_TOKEN"])) if not v]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}: set it in the menu's Settings or in {INI_FILE}")


def select_repo(full):
    """The following steps work on this repository: GitHub owner/name = Gitea owner/name."""
    global GITHUB_REPO, OWNER, NAME
    GITHUB_REPO = full
    OWNER, NAME = full.split("/", 1)
    return STORE.setdefault(full, {})


def load_state():
    global STORE
    STORE = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}


def save_state(_state=None):
    if DRY:
        return
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STORE, f, indent=1)
    os.replace(tmp, STATE_FILE)


def utc(stamp):
    """GitHub's '...Z' and Gitea's '...+02:00' in one form, for comparison."""
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- 1. migration ------------------------------------------------------------

def repo_exists(name):
    status, _ = gitea("GET", f"/repos/{OWNER}/{name}")
    return status == 200


WIP_PREFIX = re.compile(r"^\s*(?:WIP:|\[WIP\])\s*", re.I)


def same_title(gitea_title, github_title):
    """Gitea's title and GitHub's as the same: Gitea prefixes a draft pull request's
    title with its work-in-progress marker (WORK_IN_PROGRESS_PREFIXES, by default "WIP:"
    and "[WIP]") when it imports it - riscv/riscv-attached-matrix-extension #2 is
    "WIP: Switch Zvame to Ztt" in Gitea. Removing the prefix would undraft it."""
    return gitea_title == github_title or WIP_PREFIX.sub("", gitea_title) == github_title


def numbering_matches(name):
    """Do Gitea's numbers match GitHub's? A few numbers from across the range are checked."""
    status, data = gitea("GET", "/issues?state=all&limit=1", repo=f"{OWNER}/{name}")
    if status != 200:
        return False
    if not data:
        # No issue in Gitea: right only if GitHub has none either.
        try:
            return not gh(f"repos/{GITHUB_REPO}/issues", {"state": "all", "per_page": 1})
        except LookupError:
            return True  # issues are turned off on GitHub
    top = data[0]["number"]
    for num in sorted({1, 2, max(1, top // 3), max(1, top // 2), top}):
        try:
            g = gh(f"repos/{GITHUB_REPO}/issues/{num}")
        except LookupError:
            continue
        if g.get("number") != num:
            # GitHub moved this number (wekan/ldap: #2 answers as #69): it no longer exists
            # on GitHub, and Gitea rightly does not have it. It says nothing about numbering.
            continue
        s, t = gitea("GET", f"/issues/{num}", repo=f"{OWNER}/{name}")
        if s != 200 or not same_title(t["title"], g["title"]) or ("pull_request" in g) != bool(t.get("pull_request")):
            log(f"  #{num}: Gitea '{(t or {}).get('title') if s == 200 else s}' ≠ GitHub '{g['title']}'")
            return False
    return True


def migrate_into(name):
    log(f"Gitea's GitHub migration: github.com/{GITHUB_REPO} -> {OWNER}/{name}")
    log("  Imports the git history, labels, milestones, issues, PRs, comments and reviews")
    log("  with the right numbers. Takes hours (GitHub rate limit); progress below.")
    if DRY:
        log("    [dry-run] POST /repos/migrate")
        return True
    try:
        info = gh(f"repos/{GITHUB_REPO}")
    except LookupError as e:
        fail(str(e))
    if info.get("size", 1) == 0:
        log("  The GitHub repository is empty; creating an empty repository.")
        s, d = gitea("POST", f"/orgs/{OWNER}/repos", {"name": name, "private": info["private"],
                                                       "description": info.get("description") or ""})
        if s not in (200, 201):
            # OWNER is a Gitea user, not an organization: create it there (needs an admin
            # token), never under the token's own user.
            s, d = gitea("POST", f"/admin/users/{OWNER}/repos", {"name": name, "private": info["private"],
                                                                 "description": info.get("description") or ""})
        if s not in (200, 201) and not DRY:
            fail(f"creating the empty repository {OWNER}/{name} failed: {s} {d}")
        return True
    body = {"clone_addr": f"https://github.com/{GITHUB_REPO}.git", "service": "github",
            "auth_token": github_token(OWNER) or "", "repo_owner": OWNER, "repo_name": name,
            "mirror": False, "private": bool(info.get("private")),
            "issues": bool(info.get("has_issues", True)), "pull_requests": True,
            "labels": True, "milestones": True,
            "wiki": flag("wiki") and bool(info.get("has_wiki")), "lfs": flag("lfs"),
            # Releases are imported separately (step releases): the migration would download
            # every file of every release with no size limit (hundreds of gigabytes for WeKan).
            "releases": False,
            "description": info.get("description") or ""}
    repo = f"{OWNER}/{name}"
    for attempt in (1, 2):
        result = {}

        def run():
            # No automatic retry: a migration that broke off has already created the
            # repository, and a repeated POST would only answer "already exists".
            result["r"] = http("POST", GITEA_URL + "/repos/migrate", os.environ["GITEA_TOKEN"],
                               body=body, timeout=None, retry=False)

        th = threading.Thread(target=run, daemon=True)
        th.start()
        while th.is_alive():
            th.join(300)
            if th.is_alive():
                log("  {t}: issues {i}, PRs {p}".format(t=time.strftime('%H:%M'), i=gt_total(repo, 'issues'), p=gt_total(repo, 'pulls')))
        status, data = result["r"]
        if status == 201:
            log("Migration done: issues {i}, PRs {p}".format(i=gt_total(repo, 'issues'), p=gt_total(repo, 'pulls')))
            return True
        reason = f"{status} {data.get('message') if isinstance(data, dict) else data}"
        log(f"Migration failed: {reason}")
        if repo_exists(name):
            remove_leftover(name)
        fsck = re.search(r"object [0-9a-f]{40}: (\w+): ", reason)
        if fsck:
            # Gitea's git checks every object it fetches (transfer.fsckObjects), and this
            # history has one it refuses: another attempt ends the same way.
            fail(f"git fsck refused this repository's history ({fsck.group(1)}). GitHub accepts it; "
                 f"to accept it in Gitea too, add to Gitea's app.ini and restart Gitea:\n"
                 f"    [git.config]\n    fetch.fsck.{fsck.group(1)} = ignore\n"
                 f"  and run this again. The original error: {reason}")
        if attempt == 1:
            log("  Trying the migration once more in 60 s ({n}/2)...".format(n=2))
            time.sleep(60)
    fail("migration failed: {m}".format(m=reason))


def leftover(name):
    """A repository a failed migration left behind: empty in Gitea while GitHub has content."""
    status, data = gitea("GET", f"/repos/{OWNER}/{name}")
    if status != 200 or not data.get("empty"):
        return False
    try:
        return gh(f"repos/{GITHUB_REPO}").get("size", 0) > 0
    except LookupError:
        return False


def remove_leftover(name):
    log("  {r} is empty while GitHub has content: a failed migration left it. Removing it (nothing is lost).".format(r=f"{OWNER}/{name}"))
    status, data = gitea("DELETE", f"/repos/{OWNER}/{name}")
    if status not in (200, 204) and not DRY:
        fail("Removing {r} failed: {s} {d}".format(r=f"{OWNER}/{name}", s=status, d=data))


def rename(old, new):
    status, data = gitea("PATCH", f"/repos/{OWNER}/{old}", {"name": new})
    if status != 200 and not DRY:
        fail(f"Renaming {old} -> {new} failed: {status} {data}")
    log(f"  {OWNER}/{old} -> {OWNER}/{new}")


def ensure_migrated(state, replace_broken):
    if repo_exists(NAME) and leftover(NAME):
        remove_leftover(NAME)
        state.pop("migrated", None)
    if not repo_exists(NAME):
        state["since"] = now_iso()
        migrate_into(NAME)
        save_state(state)
        return
    if state.get("migrated") or numbering_matches(NAME):
        state["migrated"] = True
        save_state(state)
        return
    log(f"{OWNER}/{NAME}: the numbers do not match GitHub - the repository was imported wrongly "
        f"(for example PRs as issues).")
    if not replace_broken:
        fail("Turn on replace_broken (menu, or the setting) and run again: a correct import is made "
                 f"as {NAME}-github, and the old one is kept as {NAME}-old-<date>.")
    tmp = f"{NAME}-github"
    if repo_exists(tmp) and leftover(tmp):
        remove_leftover(tmp)
    if repo_exists(tmp):
        log(f"{OWNER}/{tmp} already exists (an earlier migration); using it.")
    else:
        state["since"] = now_iso()
        migrate_into(tmp)
    if not DRY and not numbering_matches(tmp):
        fail(f"{OWNER}/{tmp}: the numbers do not match yet - is the migration still running? Names not swapped.")
    old = f"{NAME}-old-{time.strftime('%Y%m%d')}"
    log("Swapping the names (the old one is not deleted):")
    rename(NAME, old)
    rename(tmp, NAME)
    state["migrated"] = True
    state.setdefault("since", now_iso())
    save_state(state)


# --- 2. git refs ----------------------------------------------------------------

def bare_paths(path=None):
    """Where Gitea's bare repository can be. Gitea keeps it in lower case -
    gitea-repositories/xet7/neonnoir.git for xet7/NEONnoir - so that name comes first,
    then the name as written."""
    out = []
    for p in [path or opt("bare_repo")] + BARE_CANDIDATES:
        for owner, name in ((OWNER.lower(), NAME.lower()), (OWNER, NAME)):
            q = os.path.expanduser(p.format(owner=owner, name=name))
            if q not in out:
                out.append(q)
    return out


def find_bare(path=None):
    for p in bare_paths(path):
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "HEAD")):
            return p
    return None


def fetch_git(bare):
    if not bare:
        log(f"Gitea's bare repository is not on this machine (setting bare_repo: {opt('bare_repo')}; "
            f"looked at {bare_paths()[0]}); skipping git fetch.")
        return
    log(f"git fetch from GitHub -> {bare}")
    cmd = ["git", "-C", bare, "fetch", "--quiet", f"https://github.com/{GITHUB_REPO}.git",
           "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
           # Into a namespace of their own, so that Gitea's own refs/pull/* stay as they are.
           "+refs/pull/*/head:refs/github-pull/*/head"]
    if DRY:
        log("    [dry-run] " + " ".join(cmd))
        return
    subprocess.run(cmd, check=True)


# --- 3. issues and pull requests -------------------------------------------------

_labels = None
_milestones = None


def label_ids(gh_labels):
    global _labels
    if _labels is None:
        _labels = {l["name"]: l["id"] for l in gt_all("/labels")}
    ids = []
    for l in gh_labels or []:
        if l["name"] not in _labels:
            s, d = gt("POST", "/labels", {"name": l["name"], "color": "#" + (l.get("color") or "cccccc"),
                                          "description": l.get("description") or ""})
            if s == 201 and d:
                _labels[l["name"]] = d["id"]
        if l["name"] in _labels:
            ids.append(_labels[l["name"]])
    return ids


def milestone_id(gh_ms):
    global _milestones
    if not gh_ms:
        return 0
    if _milestones is None:
        _milestones = {m["title"]: m["id"] for m in gt_all("/milestones?state=all")}
    if gh_ms["title"] not in _milestones:
        s, d = gt("POST", "/milestones", {"title": gh_ms["title"], "description": gh_ms.get("description") or "",
                                          "state": gh_ms.get("state", "open")})
        if s == 201 and d:
            _milestones[gh_ms["title"]] = d["id"]
    return _milestones.get(gh_ms["title"], 0)


def attribution(item):
    """The API records the token's owner as the author; the migration keeps the author, the API
    does not. So whatever the API imports gets a line naming the original author and a link."""
    user = (item.get("user") or {}).get("login", "ghost")
    return f"{ATTRIB}@{user}, {item['created_at'][:10]}, {item['html_url']}*\n\n"


def gitea_top():
    s, d = gt("GET", "/issues?state=all&limit=1")
    return d[0]["number"] if s == 200 and d else 0


def git_auth_env(token):
    """git's environment with the token as HTTP basic authentication - what git over
    HTTPS takes on GitHub and on Gitea - from the environment, never on the command
    line, where `ps` would show it."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")


def github_git_url():
    return f"https://github.com/{GITHUB_REPO}.git"


def gitea_git_url():
    root = GITEA_URL[: -len("/api/v1")] if GITEA_URL.endswith("/api/v1") else GITEA_URL
    return f"{root}/{OWNER}/{NAME}.git"


def push_pr_head(num, sha, branch):
    """Without Gitea's bare repository on this machine, the PR's head commit goes to
    Gitea the other way: fetched from GitHub into a cache under .tools/tmp, and pushed
    to Gitea as the branch github-pr/<N> - the branch the PR is made from anyway. Only
    that one ref is pushed; branches, tags and Gitea's own refs/pull/* are not touched.
    True when the branch is there afterwards."""
    cache = os.path.join(HERE, "tmp", "github-to-gitea-git", OWNER, NAME + ".git")
    gh_token = github_token(GITHUB_REPO.split("/")[0])
    try:
        if not os.path.exists(os.path.join(cache, "HEAD")):
            os.makedirs(cache, exist_ok=True)
            subprocess.run(["git", "init", "--quiet", "--bare", cache], check=True)
        log(f"  PR #{num}: fetching {sha[:10]} from GitHub, to push it to Gitea as {branch}")
        subprocess.run(["git", "-C", cache, "fetch", "--quiet", "--no-tags", github_git_url(),
                        f"+refs/pull/{num}/head:refs/github-pull/{num}/head"], check=True,
                       env=git_auth_env(gh_token) if gh_token else dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        got = subprocess.run(["git", "-C", cache, "rev-parse", f"refs/github-pull/{num}/head"],
                             capture_output=True, text=True, check=True).stdout.strip()
        if got != sha:
            log(f"  PR #{num}: GitHub's refs/pull/{num}/head is {got[:10]}, not {sha[:10]}; not pushed")
            return False
        if DRY:
            log(f"    [dry-run] git push {gitea_git_url()} {sha}:refs/heads/{branch}")
            return True
        # Gitea answers a push with "Create a new pull request for ..."; the PR is made
        # here anyway, so git's output is shown only when the push fails.
        r = subprocess.run(["git", "-C", cache, "push", "--quiet", gitea_git_url(), f"{sha}:refs/heads/{branch}"],
                           env=git_auth_env(os.environ["GITEA_TOKEN"]), capture_output=True, text=True)
        if r.returncode != 0:
            log((r.stdout + r.stderr).rstrip())
            raise subprocess.CalledProcessError(r.returncode, r.args)
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"  PR #{num}: pushing the head commit to Gitea failed: {e}")
        return False
    s, _ = gt("GET", "/branches/" + urllib.parse.quote(branch, safe=""))
    return s == 200


def create_pull(num):
    pr = gh(f"repos/{GITHUB_REPO}/pulls/{num}")
    branch = f"github-pr/{num}"
    s, _ = gt("GET", "/branches/" + urllib.parse.quote(branch, safe=""))
    if s != 200:
        sha = pr["head"]["sha"]
        # Without the bare repository here, a commit Gitea does not have is pushed at
        # once: asking Gitea to make a branch of it first only writes a 500 to its log.
        has_commit = gt("GET", f"/git/commits/{sha}?stat=false&verification=false&files=false")[0] == 200
        if not has_commit and find_bare() is None and not DRY:
            s, d = (201, None) if push_pr_head(num, sha, branch) else (404, "the push did not work")
        else:
            s, d = gt("POST", "/branches", {"new_branch_name": branch, "old_ref_name": sha})
        if s != 201 and not DRY:
            fail(f"PR #{num}: head commit {pr['head']['sha'][:10]} is missing from the Gitea repository ({s} {d}),\n"
                     f"and pushing it from GitHub did not work either (see above). Run the script on the machine "
                     f"that has Gitea's bare repository (setting bare_repo), or with a Gitea token that may push. "
                     f"The PR is not made an issue: the numbers would get out of step.")
    s, d = gt("POST", "/pulls", {"title": pr["title"], "body": attribution(pr) + (pr.get("body") or ""),
                                 "head": branch, "base": pr["base"]["ref"],
                                 "labels": label_ids(pr.get("labels")),
                                 "milestone": milestone_id(pr.get("milestone"))})
    if s != 201 and not DRY:
        fail(f"PR #{num}: Gitea did not create the PR: {s} {d}")
    return d


def create_issue(item):
    s, d = gt("POST", "/issues", {"title": item["title"], "body": attribution(item) + (item.get("body") or ""),
                                  "labels": label_ids(item.get("labels")),
                                  "milestone": milestone_id(item.get("milestone"))})
    if s != 201 and not DRY:
        fail(f"Issue #{item['number']}: Gitea did not create the issue: {s} {d}")
    return d


def placeholder(num, why):
    log(f"  #{num}: {why}; creating a closed placeholder so that the following numbers match")
    s, d = gt("POST", "/issues", {"title": f"(GitHub #{num} was deleted or transferred)",
                                  "body": f"https://github.com/{GITHUB_REPO}/issues/{num}", "closed": True})
    return d


def sync_item(num, item, state, since):
    """Title, body, state, labels and new comments as GitHub has them."""
    is_pr = "pull_request" in item
    s, cur = gt("GET", f"/issues/{num}")
    if s != 200:
        log(f"  #{num}: not in Gitea ({s}), skipping")
        return
    if is_pr and item["state"] == "closed" and cur["state"] == "open":
        pr = gh(f"repos/{GITHUB_REPO}/pulls/{num}")
        if pr.get("merged"):
            s, d = gt("POST", f"/pulls/{num}/merge", {"Do": "manually-merged",
                                                     "merge_commit_id": pr.get("merge_commit_sha") or ""})
            if s in (200, 204):
                log(f"  #{num}: marked as merged")
                cur["state"] = "closed"
    patch = {}
    if not same_title(cur["title"], item["title"]):
        patch["title"] = item["title"]
    want_state = "closed" if item["state"] == "closed" else "open"
    if cur["state"] != want_state:
        patch["state"] = want_state
    body = item.get("body") or ""
    have = cur.get("body") or ""
    if have.startswith(ATTRIBS):
        if have != attribution(item) + body:
            patch["body"] = attribution(item) + body
    elif have != body:
        patch["body"] = body
    if patch:
        gt("PATCH", f"/issues/{num}", patch)
        log(f"  #{num}: updated {', '.join(patch)}")
    want = sorted(l["name"] for l in item.get("labels") or [])
    if want != sorted(l["name"] for l in cur.get("labels") or []):
        gt("PUT", f"/issues/{num}/labels", {"labels": label_ids(item.get("labels"))})
        log(f"  #{num}: labels {want}")
    if not item.get("comments"):
        return
    done = state.setdefault("comments", {}).setdefault(str(num), [])
    # The migration keeps a comment's time and an API import carries its link, so comments
    # already in Gitea are recognised by those - including ones written during the migration.
    there = set()
    for gc in gt_all(f"/issues/{num}/comments" + (f"?since={since}" if since else "")):
        there.add(utc(gc["created_at"]))
        there.update(w for w in (gc.get("body") or "").split() if "#issuecomment-" in w)
    for c in gh_all(f"repos/{GITHUB_REPO}/issues/{num}/comments", {"since": since} if since else None):
        # Comments written before the previous run (or the migration) are already in Gitea;
        # 'since' also returns the ones of those that were edited.
        if c["id"] in done or (since and c["created_at"] < since) or \
                utc(c["created_at"]) in there or any(c["html_url"] in w for w in there):
            continue
        s, _ = gt("POST", f"/issues/{num}/comments", {"body": attribution(c) + (c.get("body") or "")})
        if s == 201 or DRY:
            done.append(c["id"])
            log(f"  #{num}: new comment (@{c['user']['login']})")


def sync_issues(state):
    have = gitea_top()
    newest = gh(f"repos/{GITHUB_REPO}/issues", {"state": "all", "sort": "created", "direction": "desc", "per_page": 1})
    top = newest[0]["number"] if newest else 0
    log(f"Issues and PRs: Gitea up to #{have}, GitHub up to #{top}.")
    started = now_iso()
    for num in range(have + 1, top + 1):
        try:
            item = gh(f"repos/{GITHUB_REPO}/issues/{num}")
        except LookupError as e:
            d = placeholder(num, str(e))
        else:
            if item.get("number") != num:  # transferred to another repository
                d = placeholder(num, "transferred")
            else:
                d = create_pull(num) if "pull_request" in item else create_issue(item)
                log(f"  #{num}: {'PR' if 'pull_request' in item else 'issue'} {item['title']}")
                if not DRY and d and d.get("number") == num:
                    sync_item(num, item, state, None)
        if not DRY and (not d or d.get("number") != num):
            fail(f"#{num}: Gitea gave number {d and d.get('number')}; the numbers no longer match. "
                     f"Fix it by hand before going on.")
        save_state(state)

    since = state.get("since")
    if since:
        log(f"Changes on GitHub since {since}...")
        for item in gh_all(f"repos/{GITHUB_REPO}/issues", {"state": "all", "since": since,
                                                          "sort": "updated", "direction": "asc"}):
            if item["number"] <= have:
                sync_item(item["number"], item, state, since)
    state["since"] = started
    save_state(state)


# --- 4. releases ---------------------------------------------------------------

def tag_sha(tag):
    ref = gh(f"repos/{GITHUB_REPO}/git/ref/tags/{urllib.parse.quote(tag)}")["object"]
    while ref["type"] == "tag":
        ref = gh(f"repos/{GITHUB_REPO}/git/tags/{ref['sha']}")["object"]
    return ref["sha"]


def ensure_tag(tag):
    s, _ = gt("GET", "/tags/" + urllib.parse.quote(tag, safe=""))
    if s == 200:
        return True
    try:
        sha = tag_sha(tag)
    except LookupError as e:
        log(f"  {tag}: the tag is not on GitHub either ({e})")
        return False
    s, d = gt("POST", "/tags", {"tag_name": tag, "target": sha})
    if s == 201 or DRY:
        log(f"  {tag}: tag created at commit {sha[:10]}")
        return True
    log(f"  {tag}: could not create the tag ({s} {d}); commit {sha[:10]} is missing - skipping the release")
    return False


def asset_files(tag, r):
    """(name, path or None, url) of the files that belong to the release, by the setting."""
    mode = opt("release_assets").strip().lower()
    if mode == "local" and opt("assets_dir"):
        folder = os.path.join(HERE, os.path.expanduser(opt("assets_dir").format(owner=OWNER, name=NAME)), tag)
        if os.path.isdir(folder):
            return [(n, os.path.join(folder, n), None) for n in sorted(os.listdir(folder))
                    if os.path.isfile(os.path.join(folder, n))]
    if mode == "github":
        return [(a["name"], None, a["url"], a.get("size", 0)) for a in r.get("assets") or []]
    return []


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub redirects a file to a signed address, which rejects an Authorization header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


NO_AUTH_REDIRECT = urllib.request.build_opener(_DropAuthOnRedirect)


def upload_asset(cur, tag, name, path, url, size=0):
    """One file to the Gitea release. A file from GitHub is downloaded alone to
    mirror_tmp_dir(), uploaded, and deleted - also when the upload fails - so at most
    one release file is on the disk at a time."""
    limit = int(opt("assets_max_mb") or 0) * 1024 * 1024
    if DRY:
        log(f"    [dry-run] upload {tag}/{name}")
        return
    downloaded = None
    if path is None:
        if limit and size > limit:
            log(f"  {tag}: {name} skipped ({size // 1048576} MB > assets_max_mb)")
            return
        tmpdir = mirror_tmp_dir()
        os.makedirs(tmpdir, exist_ok=True)
        # The name is GitHub's: its last path element only, never a path.
        downloaded = path = os.path.join(tmpdir, f"{os.getpid()}-{os.path.basename(name) or 'file'}")
        req = urllib.request.Request(url, headers={"Accept": "application/octet-stream",
                                                   "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
                                                   "User-Agent": "github-to-gitea"})
        try:
            with NO_AUTH_REDIRECT.open(req, timeout=None) as resp, open(path, "wb") as f:
                while chunk := resp.read(1 << 20):
                    f.write(chunk)
        except BaseException:
            _remove(downloaded)
            raise
    elif limit and os.path.getsize(path) > limit:
        log(f"  {tag}: {name} skipped (over assets_max_mb)")
        return
    try:
        s, d = gitea("POST", f"/releases/{cur['id']}/assets?name={urllib.parse.quote(name)}",
                     repo=f"{OWNER}/{NAME}", stream=Body(path, "attachment"), timeout=None)
        log(f"  {tag}: {name} " + ("uploaded" if s == 201 else f"ERROR {s} {d}"))
    finally:
        if downloaded:
            _remove(downloaded)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def keep_count():
    """How many of the newest releases keep their files: None for all."""
    v = opt("release_assets_keep").strip().lower()
    if v == "all":
        return None
    if v in ("", "newest"):
        return 1
    return max(0, int(v))


def delete_assets(cur, tag):
    """The files of a release outside release_assets_keep go from Gitea."""
    for a in cur.get("assets") or []:
        if DRY:
            log(f"    [dry-run] delete {tag}/{a['name']}")
            continue
        s, d = gitea("DELETE", f"/releases/{cur['id']}/assets/{a['id']}", repo=f"{OWNER}/{NAME}")
        log(f"  {tag}: {a['name']} " + ("deleted (an older release)" if s in (200, 204) else f"not deleted: {s} {d}"))


def sync_releases():
    existing = {r["tag_name"]: r for r in gt_all("/releases")}
    gh_rel = list(gh_all(f"repos/{GITHUB_REPO}/releases"))
    log(f"Releases: {len(existing)} in Gitea, {len(gh_rel)} on GitHub.")
    # GitHub lists the newest first; a draft is no release yet.
    published = [r["tag_name"] for r in gh_rel if not r.get("draft")]
    n = keep_count()
    keep = set(published if n is None else published[:n])
    for r in reversed(gh_rel):  # vanhin ensin
        tag = r["tag_name"]
        want = {"tag_name": tag, "name": r.get("name") or tag, "body": r.get("body") or "",
                "draft": r["draft"], "prerelease": r["prerelease"]}
        cur = existing.get(tag)
        if cur is None:
            # The tag has to be on the right commit BEFORE the release, or Gitea would make
            # it at the tip of target_commitish (main).
            if not ensure_tag(tag):
                continue
            s, cur = gt("POST", "/releases", want)
            if s != 201 and not DRY:
                log(f"  {tag}: release not created: {s} {cur}")
                continue
            log(f"  {tag}: release created")
        elif any((cur.get(k) or "") != want[k] for k in ("name", "body")) or \
                cur.get("prerelease") != want["prerelease"] or cur.get("draft") != want["draft"]:
            gt("PATCH", f"/releases/{cur['id']}", want)
            log(f"  {tag}: release updated")
        if cur and cur.get("id") and opt("release_assets").strip().lower() != "none":
            if tag in keep:
                have = {a["name"] for a in cur.get("assets") or []}
                for item in asset_files(tag, r):
                    if item[0] not in have:
                        upload_asset(cur, tag, *item)
            else:
                delete_assets(cur, tag)


# --- 5. GitHub Actions -> Gitea Actions ------------------------------------------
#
# Gitea reads .gitea/workflows when it exists and .github/workflows only otherwise, so
# the converted copies go to .gitea/workflows on a branch of their own, ACTIONS_BRANCH:
# main itself stays byte for byte what GitHub has (the next sync would overwrite it
# anyway). Run a release from Gitea's Actions tab: pick the workflow, and as the branch
# pick ACTIONS_BRANCH. The files keep their names, so Release All Platforms is still
# release-all.yml and Release all missing is release-all-missing.yml.
#
# What changes, and why:
#   triggers    only workflow_dispatch and workflow_call stay. push / pull_request /
#               schedule would run a build for every synced commit and every migrated PR
#               (they did: the "Docker" and "Dependency Review" runs of the old repo).
#   uses: ./.github/workflows/x.yml  ->  ./.gitea/workflows/x.yml (Gitea resolves local
#               reusable workflows itself, from the same commit).
#   gh          every job first puts .gitea/gh on PATH: gh release view/list/download/
#               upload/create/edit and gh workflow run, answered by THIS Gitea's API, so the
#               release files land on the local release (github-to-gitea-gh.py).
#   tokens      github.token is a Gitea token here. It goes to the shim as
#               LOCAL_GITEA_TOKEN; GH_TOKEN / GITHUB_TOKEN, which releases/*.sh send to
#               api.github.com (FerretDB, Node.js, Bun lookups), become the repository
#               secret GH_READ_TOKEN - GitHub answers 401 to a Gitea token, and empty is
#               the unauthenticated 60 requests/hour.
#   publishing  jobs that publish OUTSIDE this Gitea - Docker Hub/Quay/GHCR, Snap Store,
#               Launchpad, Sandstorm App Market, wekan.fi, charts, Nextcloud, UCS, the
#               ondra/gantt repos - run only when the repository variable
#               PUBLISH_EXTERNAL is 'true'. A local build never publishes by accident.
#   recover     release-all-missing's recover reruns failed jobs through the GitHub
#               Actions API, which Gitea does not have; it is skipped (plan runs after it
#               regardless, as on GitHub when recover fails).
#   macOS, Windows  their jobs wait for a runner with that label forever, so they run only
#               when the variable GITEA_MACOS_RUNNER / GITEA_WINDOWS_RUNNER is 'true'.
#               The jobs after them already tolerate skipped dependencies (!cancelled()).

CONVERTER_VERSION = 1
KEEP_TRIGGERS = {"workflow_dispatch", "workflow_call"}
EXTERNAL = "vars.PUBLISH_EXTERNAL == 'true'"
GATES = {"wekan/wekan": {
    "release-all.yml": {j: EXTERNAL for j in ("website", "charts", "docker", "snap-auth-check", "snap-native",
                                             "snap-launchpad", "snap-variants", "update-ondra-gantt-repos")},
    "release-all-missing.yml": {"charts": EXTERNAL, "recover": "github.server_url == 'https://github.com'"},
    "docker-variant.yml": {"retag": EXTERNAL},
    "nextcloud.yml": {"nextcloud": EXTERNAL},
    "ucs.yml": {"publish": EXTERNAL},
    "sandstorm-store.yml": {"sandstorm-store": EXTERNAL},
    "update-ondra-gantt-repos.yml": {"update": EXTERNAL},
}}
SHIM_STEP = """\
      - name: Use gh of this Gitea (github-to-gitea)
        shell: bash
        env:
          GH_SHIM_TOKEN: ${{{{ github.token }}}}
        run: |
          d="${{RUNNER_TEMP:-/tmp}}/gitea-gh"; mkdir -p "$d"
          curl -fsSL -H "Authorization: token $GH_SHIM_TOKEN" -o "$d/gh" \\
            "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/raw/branch/{branch}/.gitea/gh"
          chmod +x "$d/gh"; echo "$d" >> "$GITHUB_PATH"
"""


def _block_end(lines, i, indent):
    """Index after the block that starts at line i (its children are indented > indent)."""
    j = i + 1
    while j < len(lines):
        s = lines[j]
        if s.strip() and not s.lstrip().startswith("#") and len(s) - len(s.lstrip()) <= indent:
            break
        j += 1
    return j


def convert_workflow(name, text):
    """A GitHub workflow as Gitea Actions on this server runs it, or None when nothing
    in it can be started by hand (only push/PR/schedule triggers)."""
    lines = text.split("\n")
    # triggers
    on = next((i for i, l in enumerate(lines) if l.rstrip() in ("on:", '"on":', "'on':")), None)
    if on is None:
        return None
    end = _block_end(lines, on, 0)
    kept, i = [], on + 1
    while i < end:
        m = re.match(r"^  ([A-Za-z_]+):", lines[i])
        if m:
            j = _block_end(lines, i, 2)
            if m.group(1) in KEEP_TRIGGERS:
                kept += lines[i:j]
            i = j
        else:
            if not lines[i].strip() or lines[i].lstrip().startswith("#"):
                kept.append(lines[i])
            i += 1
    if not any(re.match(r"^  [A-Za-z_]+:", l) for l in kept):
        return None
    lines[on + 1:end] = kept
    # token for the shim, at workflow level
    envi = next((i for i, l in enumerate(lines) if l.rstrip() == "env:"), None)
    add = "  LOCAL_GITEA_TOKEN: ${{ github.token }}  # gh of this Gitea (.gitea/gh)"
    if envi is not None:
        lines.insert(envi + 1, add)
    else:
        jobs_i = next(i for i, l in enumerate(lines) if l.rstrip() == "jobs:")
        lines[jobs_i:jobs_i] = ["env:", add, ""]
    text = "\n".join(lines)
    text = text.replace("uses: ./.github/workflows/", "uses: ./.gitea/workflows/")
    text = re.sub(r"(\b(?:GH_TOKEN|GITHUB_TOKEN): *)\$\{\{ *(?:github\.token|secrets\.GITHUB_TOKEN) *\}\}",
                  r"\1${{ secrets.GH_READ_TOKEN }}", text)
    lines = text.split("\n")
    # job gates
    jobs_i = next(i for i, l in enumerate(lines) if l.rstrip() == "jobs:")
    gates = dict(GATES.get(GITHUB_REPO, {}).get(name, {}))
    out, i = lines[:jobs_i + 1], jobs_i + 1
    while i < len(lines):
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j = _block_end(lines, i, 2)
        job = lines[i:j]
        body = "\n".join(job)
        cond = gates.get(m.group(1))
        if not cond:
            if re.search(r"^    runs-on: *macos-|^ +runner: *macos-", body, re.M):
                cond = "vars.GITEA_MACOS_RUNNER == 'true'"
            elif re.search(r"^    runs-on: *windows-", body, re.M):
                cond = "vars.GITEA_WINDOWS_RUNNER == 'true'"
        if cond:
            k = next((n for n, l in enumerate(job) if re.match(r"^    if:", l)), None)
            if k is None:
                job.insert(1, f"    if: ${{{{ {cond} }}}}")
            else:
                old = job[k].split(":", 1)[1].strip()
                inner = old[3:-2].strip() if old.startswith("${{") and old.endswith("}}") else old
                job[k] = f"    if: ${{{{ {cond} && ({inner}) }}}}"
        # gh shim as the first step
        for n, l in enumerate(job):
            if l.rstrip() == "    steps:":
                job[n + 1:n + 1] = SHIM_STEP.format(branch=opt("actions_branch")).rstrip("\n").split("\n")
                break
        out += job
        i = j
    header = (f"# Converted for Gitea Actions by .tools/github-to-gitea.py from\n"
              f"# .github/workflows/{name}. Do not edit here: the next sync regenerates it.\n")
    return header + "\n".join(out)


def raw_file(path, ref="main"):
    url = f"{GITEA_URL.rsplit('/api', 1)[0]}/{OWNER}/{NAME}/raw/branch/{ref}/{urllib.parse.quote(path)}"
    req = urllib.request.Request(url, headers={"Authorization": f"token {os.environ['GITEA_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode()


def sync_actions(state):
    ACTIONS_BRANCH = opt("actions_branch")
    s, info = gt("GET", "")
    base = (info or {}).get("default_branch") or "main"
    s, br = gt("GET", f"/branches/{urllib.parse.quote(base, safe='')}")
    if s != 200:
        log(f"Actions: branch {base} not found, skipping.")
        return
    main_sha = br["commit"]["id"]
    shim = open(SHIM_FILE, encoding="utf-8").read()
    key = f"{main_sha}:{CONVERTER_VERSION}:{__import__('hashlib').sha256(shim.encode()).hexdigest()[:16]}"
    s, cur = gt("GET", "/branches/" + urllib.parse.quote(ACTIONS_BRANCH, safe=""))
    if s == 200 and state.get("actions") == key:
        log(f"Actions: {ACTIONS_BRANCH} is already up to date ({base} {main_sha[:10]}).")
        return
    s, entries = gt("GET", f"/contents/.github/workflows?ref={urllib.parse.quote(base)}")
    if s != 200:
        log(f"Actions: no .github/workflows ({base}), skipping.")
        return
    files, skipped = [], []
    for e in sorted(entries, key=lambda e: e["name"]):
        if e["type"] != "file" or not e["name"].endswith((".yml", ".yaml")):
            continue
        conv = convert_workflow(e["name"], raw_file(e["path"], base))
        if conv is None:
            skipped.append(e["name"])
            continue
        files.append({"operation": "create", "path": f".gitea/workflows/{e['name']}",
                      "content": __import__("base64").b64encode(conv.encode()).decode()})
    files.append({"operation": "create", "path": ".gitea/gh",
                  "content": __import__("base64").b64encode(shim.encode()).decode()})
    log(f"Actions: converted {len(files) - 1} workflows; left out, as nothing in them can be started by hand: "
        f"{', '.join(skipped) or '-'}")
    if gt("GET", "/branches/" + urllib.parse.quote(ACTIONS_BRANCH, safe=""))[0] == 200:
        gt("DELETE", "/branches/" + urllib.parse.quote(ACTIONS_BRANCH, safe=""))
    ident = {"name": "Lauri Ojansivu", "email": "x@xet7.org"}
    s, d = gt("POST", "/contents", {
        "branch": base, "new_branch": ACTIONS_BRANCH, "files": files,
        "message": "Gitea Actions: .github/workflows converted by github-to-gitea.py\n\n"
                   "Only workflow_dispatch/workflow_call triggers, gh answered by this Gitea,\n"
                   "external publishing behind vars.PUBLISH_EXTERNAL.",
        "author": ident, "committer": ident})
    if s != 201 and not DRY:
        fail(f"Actions: creating branch {ACTIONS_BRANCH} failed: {s} {d}")
    log(f"Actions: branch {ACTIONS_BRANCH} = {base} {main_sha[:10]} + .gitea/workflows + .gitea/gh")
    state["actions"] = key
    save_state(state)


def convert_local(src, dst):
    """--convert-only: convert a local .github/workflows into dst, to check it."""
    os.makedirs(os.path.join(dst, ".gitea", "workflows"), exist_ok=True)
    for name in sorted(os.listdir(src)):
        if name.endswith((".yml", ".yaml")):
            conv = convert_workflow(name, open(os.path.join(src, name), encoding="utf-8").read())
            log(f"  {name}: {'converted' if conv else 'left out (cannot be started by hand)'}")
            if conv:
                open(os.path.join(dst, ".gitea", "workflows", name), "w", encoding="utf-8").write(conv)


# --- 6. avatars ------------------------------------------------------------------

AVATAR_MAX = 10 * 1048576
_owner_avatars = set()


def fetch_image(url):
    """An avatar or preview image from GitHub (no token: they are public)."""
    req = urllib.request.Request(url, headers={"User-Agent": "github-to-gitea"})
    with NO_AUTH_REDIRECT.open(req, timeout=120) as r:
        data = r.read(AVATAR_MAX + 1)
    if len(data) > AVATAR_MAX:
        raise ValueError("larger than 10 MB")
    return data


def put_avatar(path, data, what):
    """The image to a Gitea avatar endpoint - unless this same image already went there,
    so a run uploads only avatars that changed."""
    digest = hashlib.sha256(data).hexdigest()
    done = STORE.setdefault("_avatars", {})
    if done.get(path) == digest:
        return
    if DRY:
        log("    [dry-run] avatar {what}".format(what=what))
        return
    s, d = gitea("POST", path, {"image": base64.b64encode(data).decode()})
    if s in (200, 204):
        done[path] = digest
        save_state()
        log("  {what}: avatar copied".format(what=what))
    else:
        log("  {what}: avatar not copied: {s} {d}".format(what=what, s=s, d=d))


def sync_owner_avatar(owner):
    """The GitHub organization's or user's avatar onto the Gitea organization of the
    same name - or onto the token's own Gitea user: Gitea's API sets no other user's."""
    if owner in _owner_avatars:
        return
    _owner_avatars.add(owner)
    try:
        url = gh(f"users/{owner}").get("avatar_url")
    except LookupError as e:
        log("  {what}: avatar not downloaded: {e}".format(what=owner, e=e))
        return
    if not url:
        return
    if gitea("GET", f"/orgs/{owner}")[0] == 200:
        path = f"/orgs/{owner}/avatar"
    else:
        s, me = gitea("GET", "/user")
        if s != 200 or (me or {}).get("login", "").lower() != owner.lower():
            log("  {owner}: a Gitea user other than the token's own - Gitea's API sets only the token user's avatar; skipped".format(owner=owner))
            return
        path = "/user/avatar"
    try:
        data = fetch_image(url)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("  {what}: avatar not downloaded: {e}".format(what=owner, e=e))
        return
    put_avatar(path, data, owner)


def sync_repo_avatar():
    """A repository's custom social preview image - GitHub's only repository picture,
    in its GraphQL API only - as the Gitea repository's avatar."""
    owner, name = GITHUB_REPO.split("/", 1)
    q = "query($o:String!,$n:String!){repository(owner:$o,name:$n){usesCustomOpenGraphImage openGraphImageUrl}}"
    s, d = http("POST", "https://api.github.com/graphql", os.environ["GITHUB_TOKEN"],
                body={"query": q, "variables": {"o": owner, "n": name}})
    repo = ((d or {}).get("data") or {}).get("repository") if s == 200 and isinstance(d, dict) else None
    if not repo or not repo.get("usesCustomOpenGraphImage") or not repo.get("openGraphImageUrl"):
        return
    try:
        data = fetch_image(repo["openGraphImageUrl"])
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("  {what}: avatar not downloaded: {e}".format(what=GITHUB_REPO, e=e))
        return
    put_avatar(f"/repos/{OWNER}/{NAME}/avatar", data, GITHUB_REPO)


# --- main --------------------------------------------------------------------

# --- sources ------------------------------------------------------------------

# Special sources: every repository the token's user can WRITE to, of every owner (the
# user, its organizations and other people's repositories it collaborates on).
SPECIAL = {"(all)": "all", "(allpublic)": "public", "(allprivate)": "private"}
SPECIAL_NAMES = {"(all)": "private and public repositories with write access",
                 "(allpublic)": "public repositories with write access",
                 "(allprivate)": "private repositories with write access"}
_login = None


def token_login():
    """The GitHub login of the token, '' when it cannot be told."""
    global _login
    if _login is None:
        try:
            _login = gh("user")["login"]
        except LookupError:
            _login = ""
    return _login


def wanted(r):
    if r.get("fork") and not flag("include_forks"):
        return False
    return not (r.get("archived") and not flag("include_archived"))


def writable(r):
    p = r.get("permissions") or {}
    return bool(p.get("push") or p.get("maintain") or p.get("admin"))


def expand_source(src):
    """'owner/name' -> [it]; 'owner' -> every repository OWNED by that organization or user;
    (all) / (allpublic) / (allprivate) -> the repositories the token can write to."""
    try:
        if src.lower() in SPECIAL:
            repos = [r for r in gh_all("user/repos", {"visibility": SPECIAL[src.lower()], "sort": "full_name",
                                                      "affiliation": "owner,collaborator,organization_member"})
                     if writable(r)]
        elif "/" in src:
            return [src]
        else:
            kind = gh(f"users/{src}")["type"]
            if kind == "Organization":
                repos = gh_all(f"orgs/{src}/repos", {"type": "all", "sort": "full_name"})
            elif src.lower() == token_login().lower():
                # The token's own user: also its private repositories, which users/<name>/repos
                # does not list. affiliation=owner leaves out the organizations' repositories
                # and others' repositories it may write to - those are not github.com/<user>.
                repos = gh_all("user/repos", {"affiliation": "owner", "visibility": "all", "sort": "full_name"})
            else:
                repos = gh_all(f"users/{src}/repos", {"type": "owner", "sort": "full_name"})
            repos = [r for r in repos if r["owner"]["login"].lower() == src.lower()]
        return [r["full_name"] for r in repos if wanted(r)]
    except LookupError as e:
        raise RepoError(f"{src}: {e}")


def website(blog):
    """GitHub's blog field as a Gitea website. Gitea accepts only a full URL, and GitHub
    often has it without https:// (EthKan: "ethkan.io"); anything else is left out."""
    blog = (blog or "").strip()
    if blog and not re.match(r"^https?://", blog, re.I):
        blog = "https://" + blog
    host = urllib.parse.urlsplit(blog).hostname or ""
    return blog if "." in host and " " not in blog else ""


def ensure_owner(owner):
    """The Gitea owner: an existing organization or user of that name, otherwise a new
    organization (never a new user) - also when the GitHub owner is a user, such as xet7."""
    if owner in _owners or gitea("GET", f"/users/{owner}")[0] == 200:
        _owners.add(owner)
        return
    try:
        info = gh(f"users/{owner}")
    except LookupError:
        info = {}
    log(f"Creating the Gitea organization {owner}")
    org = {"username": owner, "full_name": (info.get("name") or "")[:100],
           "description": (info.get("bio") or info.get("description") or "")[:255],
           "website": website(info.get("blog")), "visibility": "public"}
    s, d = gitea("POST", "/orgs", org)
    if s == 422 and org["website"]:
        # Gitea did not accept the address: the organization is made without it.
        org["website"] = ""
        s, d = gitea("POST", "/orgs", org)
    if s not in (200, 201) and not DRY:
        raise RepoError(f"could not create the organization {owner}: {s} {d}")
    _owners.add(owner)


_owners = set()


def run_repo(full, steps):
    state = select_repo(full)
    log(f"\n=== GitHub {GITHUB_REPO} -> Gitea {GITEA_URL.rsplit('/api', 1)[0]}/{OWNER}/{NAME}")
    ensure_owner(OWNER)
    if "migrate" in steps:
        ensure_migrated(state, flag("replace_broken"))
    elif not repo_exists(NAME):
        raise RepoError("the repository is not in Gitea; run the migration first")
    if "git" in steps:
        fetch_git(find_bare())
    if "issues" in steps:
        sync_issues(state)
    if "releases" in steps:
        sync_releases()
    if "actions" in steps:
        sync_actions(state)
    if "avatars" in steps:
        sync_owner_avatar(OWNER)
        sync_repo_avatar()


RESTART = False


def run(steps, only=None):
    """Every repository of the sources, one after another. The run is recorded in the state
    file as it goes: one that was interrupted (Ctrl-C, crash, reboot) is resumed by the next
    run of the same steps and sources, which skips the repositories it already finished.
    Repositories that FAILED are not skipped - the next run tries them again."""
    apply_config()
    load_state()
    todo, failed = [], []
    srcs = [only] if only else sources()
    for src in srcs:
        try:
            todo += expand_source(src)
        except RepoError as e:
            failed.append((src, str(e)))
    if not todo and not failed:
        log("No sources. Add one from the menu, for example 'wekan/wekan', 'wekan', 'xet7' or '(all)'.")
    todo = list(dict.fromkeys(todo))
    key = {"steps": list(steps), "sources": srcs}
    prev = STORE.get("_run") or {}
    done = set()
    if not RESTART and prev.get("key") == key and not prev.get("finished"):
        done = set(prev.get("done") or []) & set(todo)
        if done:
            log(f"Resuming the interrupted run of {prev.get('started')}: {len(done)} of {len(todo)} "
                f"repositories already done, skipping them (--restart runs them all).")
    STORE["_run"] = {"key": key, "started": prev.get("started") if done else now_iso(), "done": sorted(done)}
    save_state()
    interrupted = False
    for full in todo:
        if full in done:
            continue
        try:
            run_repo(full, steps)
            done.add(full)
            STORE["_run"]["done"] = sorted(done)
            save_state()
        except RepoError as e:
            log(f"  ERROR: {e}")
            failed.append((full, str(e)))
        except KeyboardInterrupt:
            log("\nInterrupted. The next run with the same steps and sources continues from here.")
            interrupted = True
            break
    if not interrupted:
        STORE["_run"]["finished"] = now_iso()
        save_state()
    log(f"\nDone: {len(todo)} {'repository' if len(todo) == 1 else 'repositories'}" + (f", {len(failed)} errors:" if failed else "."))
    for full, msg in failed:
        log(f"  {full}: {msg}")
    return not failed


# --- menu --------------------------------------------------------------------

def ask(prompt, default=""):
    try:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        return default
    return v or default


def masked(section, key):
    v = CFG.get(section, key, fallback="")
    if key == "token":
        return f"{v[:4]}…{v[-4:]}" if len(v) > 8 else ("(set)" if v else "(missing)")
    return v


def menu_settings():
    while True:
        print("\nSettings (saved in github-to-gitea.ini)")
        for i, (s, k, _, desc) in enumerate(SETTINGS, 1):
            print(f" {i:2}) [{s}] {k} = {masked(s, k)}\n       {desc}")
        print("  0) Back")
        c = ask("Choice")
        if c in ("", "0"):
            return
        if c.isdigit() and 1 <= int(c) <= len(SETTINGS):
            s, k, d, desc = SETTINGS[int(c) - 1]
            cur = CFG.get(s, k, fallback=d)
            v = ask(f"{s}.{k} ({desc})", "" if k == "token" else cur)
            if k == "token" and not v:
                continue
            if k == "steps":
                bad = [x for x in re.split(r"[,\s]+", v) if x and x not in STEPS]
                if bad:
                    print(f"Unknown step: {', '.join(bad)}. Steps: {', '.join(STEPS)}")
                    continue
            if k == "release_assets" and v not in ("none", "local", "github"):
                print("The value is github, local or none.")
                continue
            if k == "release_assets_keep" and not (v in ("newest", "all") or (v.isdigit() and int(v) > 0)):
                print("The value is newest, all, or how many of the newest releases (1, 2, 3...).")
                continue
            CFG.set(s, k, v)
            save_config()


SOURCE_HELP = """\
  A source is one of:
    owner/name    one repository, e.g. wekan/wekan
    org           every repository of a GitHub organization, e.g. wekan
    user          every repository a GitHub user OWNS, e.g. xet7 (not the repositories of
                  organizations it belongs to); private ones too when it is the token's user
    (all)         every private and public repository the GitHub token can write to
    (allpublic)   every public repository the GitHub token can write to
    (allprivate)  every private repository the GitHub token can write to
  Each repository goes to the Gitea organization or user named like its GitHub owner;
  when Gitea has neither, an organization is created. Organizations and users the token
  has no access to work too, for public repositories, but slower (60 requests an hour)."""


def menu_sources():
    while True:
        items = sources()
        print("\nSources:")
        print(SOURCE_HELP + "\n\n  Configured sources:")
        for i, s in enumerate(items, 1):
            what = SPECIAL_NAMES.get(s.lower()) or ("one repository" if "/" in s else
                                                    "organization or user: every repository it owns")
            print(f" {i:2}) {s}  ({what})")
        if not items:
            print("    (none)")
        print("  a) Add   r) Remove   s) Show what would be imported   0) Back")
        c = ask("Choice").lower()
        if c in ("", "0"):
            return
        if c == "a":
            v = ask("GitHub repository, organization or user, or (all) / (allpublic) / (allprivate)").strip().strip("/")
            v = v.replace("https://github.com/", "").removesuffix(".git")
            if v.lower() in SPECIAL:
                v = v.lower()
            if v and v not in items and (v in SPECIAL or re.fullmatch(r"[\w.-]+(/[\w.-]+)?", v)):
                set_sources(items + [v])
                save_config()
            elif v:
                print("Not valid, or already in the list.")
        elif c == "r":
            n = ask("Number to remove")
            if n.isdigit() and 1 <= int(n) <= len(items):
                del items[int(n) - 1]
                set_sources(items)
                save_config()
        elif c == "s":
            apply_config()
            for s in items:
                try:
                    repos = expand_source(s)
                    print(f"  {s}: {len(repos)} repositories")
                    for r in repos:
                        print(f"     {r}")
                except RepoError as e:
                    print(f"  {s}: {e}")


def pick_source():
    items = sources()
    for i, s in enumerate(items, 1):
        print(f" {i:2}) {s}")
    print("  a) all sources   or type a repository, organization or user, or (all) / (allpublic) / (allprivate)")
    c = ask("Source", "a")
    if c == "a":
        return None
    if c.isdigit() and 1 <= int(c) <= len(items):
        return items[int(c) - 1]
    c = c.strip().strip("/")
    return (c.lower() if c.lower() in SPECIAL else c) or None


def menu():
    while True:
        items = sources()
        print(f"""
GitHub -> Gitea     Gitea: {opt('url', 'gitea')}     settings: {os.path.basename(INI_FILE)}
Sources: {', '.join(items) or '(none - add one with 3)'}
Dry run: {'ON' if flag('dry_run') else 'off'}   Replace wrongly imported: {'yes' if flag('replace_broken') else 'no'}   Release files: {opt('release_assets')} ({opt('release_assets_keep')})

 1) Run every step ({opt('steps')}) for every source
 2) Run chosen steps for a chosen source
 3) Sources: add / remove / show repositories
 4) Settings (Gitea, tokens, steps, release files, bare repository, Actions branch...)
 5) Dry run on / off
 6) Replace a wrongly imported repository: on / off
 7) Convert a local .github/workflows to Gitea Actions (a check, no Gitea)
 8) Help: Gitea Actions runners and variables
 0) Quit""")
        c = ask("Choice")
        try:
            if c in ("0", "q", ""):
                return
            if c == "1":
                run([x for x in re.split(r"[,\s]+", opt("steps")) if x])
            elif c == "2":
                for i, s in enumerate(STEPS, 1):
                    print(f" {i}) {s}: {STEP_NAMES[s]}")
                chosen = ask("Steps as numbers or names (e.g. 3 4, or issues releases)", "1 2 3 4 5")
                steps = [STEPS[int(x) - 1] if x.isdigit() and 1 <= int(x) <= len(STEPS) else x
                         for x in re.split(r"[,\s]+", chosen) if x]
                steps = [s for s in STEPS if s in steps]
                if steps:
                    run(steps, pick_source())
            elif c == "3":
                menu_sources()
            elif c == "4":
                menu_settings()
            elif c == "5":
                CFG.set("options", "dry_run", "no" if flag("dry_run") else "yes")
                save_config()
            elif c == "6":
                CFG.set("options", "replace_broken", "no" if flag("replace_broken") else "yes")
                save_config()
            elif c == "7":
                src = ask("Source directory (.github/workflows)", os.path.join(os.path.dirname(HERE), ".github", "workflows"))
                dst = ask("Target directory", os.path.join(HERE, "tmp", "gitea-actions-convert"))
                select_repo(ask("Repository whose rules apply", "wekan/wekan"))
                convert_local(src, dst)
                print(f"Converted: {dst}/.gitea/workflows")
            elif c == "8":
                print(ACTIONS_HELP)
        except SystemExit as e:
            print(e)
        except KeyboardInterrupt:
            print("\nInterrupted.")


ACTIONS_HELP = """
Gitea Actions (step actions):
  - .github/workflows is converted into the branch actions_branch (default gitea-actions);
    the default branch stays what GitHub has. Start a release from Gitea's Actions tab:
    pick the workflow, and gitea-actions as the branch. gh commands (gh release ...) go to
    this Gitea's API, so the release files land on the local Gitea release.
  - An act_runner is needed with the runs-on labels of the workflows; for WeKan
    ubuntu-latest, ubuntu-24.04 (amd64) and ubuntu-24.04-arm (arm64). The runner's
    containers must reach Gitea at its ROOT_URL (plain localhost does not work).
  - Repository variables (Settings -> Actions -> Variables):
      GITEA_MACOS_RUNNER=true / GITEA_WINDOWS_RUNNER=true  when such a runner exists
      PUBLISH_EXTERNAL=true   allow publishing outside (Docker, Snap, Sandstorm, wekan.fi, chart)
  - Repository secret GH_READ_TOKEN: a GitHub read token for the scripts' GitHub lookups
    (without it, 60 requests an hour).
"""



# --- the screen's log ---------------------------------------------------------------------

def wekan_tools_dir():
    """<WeKan>/.tools: the WeKan checkout is at a fixed place on each operating system -
    ~/repos/wekan on Linux, ~/Documents/repos/wekan on macOS and Downloads/repos/wekan
    under the user's profile on Windows."""
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        wekan = os.path.join(os.environ.get("USERPROFILE", home), "Downloads", "repos", "wekan")
    elif sys.platform == "darwin":
        wekan = os.path.join(home, "Documents", "repos", "wekan")
    else:
        wekan = os.path.join(home, "repos", "wekan")
    return os.path.join(wekan, ".tools")


def mirror_log_dir():
    """<WeKan>/.tools/log/mirror (wekan_tools_dir). MIRROR_LOG_DIR overrides it."""
    if os.environ.get("MIRROR_LOG_DIR"):
        return os.environ["MIRROR_LOG_DIR"]
    return os.path.join(wekan_tools_dir(), "log", "mirror")


def mirror_tmp_dir():
    """<WeKan>/.tools/tmp/github-to-gitea: where a release file from GitHub is while it
    is uploaded. MIRROR_TMP_DIR overrides it."""
    return os.environ.get("MIRROR_TMP_DIR") or os.path.join(wekan_tools_dir(), "tmp", "github-to-gitea")


def start_screen_log(here, name):
    """Everything this run shows on the screen - its own lines, its prompts and the
    output of the git and other programs it runs - also goes to
    <WeKan>/.tools/log/mirror/<name>/YYYY-MM-DD_HH-MM-SS.txt (mirror_log_dir()). Copied
    at the file descriptors, so a child process's messages are in it too, and flushed
    to the file before the program ends."""
    import atexit
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    d = os.path.join(mirror_log_dir(), name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{stamp}.txt")
    n = 0
    while os.path.exists(path):
        n += 1
        path = os.path.join(d, f"{stamp}-{n}.txt")
    out = open(path, "ab", buffering=0)
    lock = threading.Lock()
    saved, threads = [], []
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        original = os.dup(fd)
        r, w = os.pipe()
        os.dup2(w, fd)
        os.close(w)

        def copy(r=r, original=original):
            while True:
                try:
                    data = os.read(r, 65536)
                except OSError:
                    break
                if not data:
                    break
                with lock:
                    try:
                        os.write(original, data)
                    except OSError:
                        pass
                    out.write(data)
            os.close(r)

        try:  # a pipe is block-buffered: keep Python's lines in step with its children's
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
        t = threading.Thread(target=copy, daemon=True)
        t.start()
        saved.append((fd, original))
        threads.append(t)

    def finish():
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        for fd, original in saved:
            os.dup2(original, fd)  # closes the pipe's last writer: the copier sees the end
        for t in threads:
            t.join(timeout=5)
        out.close()

    atexit.register(finish)
    print(f"Log: {path}", flush=True)
    return path


def main():
    start_screen_log(HERE, "github-to-gitea")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("steps", nargs="*", metavar="step",
                   help=f"{', '.join(STEPS)}; with no steps, in a terminal, the menu opens")
    p.add_argument("--all", action="store_true", help="run the configured 'steps' for every source, without the menu")
    p.add_argument("--source", help="only this source (owner/name, an organization or user, or (all) / "
                                    "(allpublic) / (allprivate)) instead of the configured sources")
    p.add_argument("--restart", action="store_true",
                   help="do not resume an interrupted run: process every repository again")
    p.add_argument("--convert-only", nargs=2, metavar=("SRC", "DST"),
                   help="only convert a local .github/workflows (SRC) into DST, no Gitea")
    a = p.parse_args()
    global RESTART
    RESTART = a.restart
    load_config()
    if a.convert_only:
        select_repo(a.source if a.source and "/" in a.source else "wekan/wekan")
        convert_local(*a.convert_only)
        return
    bad = set(a.steps) - set(STEPS)
    if bad:
        p.error(f"unknown step: {', '.join(sorted(bad))}")
    if a.steps or a.all or a.source:
        steps = a.steps or [x for x in re.split(r"[,\s]+", opt("steps")) if x]
        sys.exit(0 if run(steps, a.source) else 1)
    if not sys.stdin.isatty():
        p.error("no terminal for the menu: give steps or --all")
    menu()


if __name__ == "__main__":
    main()

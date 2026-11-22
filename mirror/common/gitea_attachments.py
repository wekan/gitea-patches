"""How much space the attachments of a local Gitea take, and deleting them - for
github-to-gitea and the mirror scripts (gitea-to-gitlab, gitea-to-sourceforge),
which share this module.

An attachment is a file in Gitea's attachment storage: a release's file, or a file
attached to an issue, a pull request or a comment. A repository's git size (Gitea's
"size", git and LFS) is not an attachment and is never deleted here.

  scope        "owner" - every repository an organization or user owns - or
               "owner/name", one repository
  kinds        which attachments: "releases", "issues" (issues and pull requests),
               "comments"

Every call to Gitea goes through request(method, path) -> (status, data), with path
from the API root (/repos/...), so each script passes its own client and a test a
fake one. Nothing here reads a setting or prints by itself: say(text) is given.

Excluded repositories (Sources -> Exclude, e.g. microsoft with microsoft/verybig
left out) are left out of what an organization or user expands to - never of a
repository that is asked for by name.
"""

import re

KINDS = ("releases", "issues", "comments")
KIND_NAMES = {"releases": "release files", "issues": "issue and pull request files",
              "comments": "comment files"}
PAGE = 50


class AttachmentError(Exception):
    pass


def human(size):
    """Bytes as B, KB, MB, GB or TB (powers of 1024), one decimal from MB up."""
    size = float(size or 0)
    for unit in ("B", "KB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    for unit in ("MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def paged(request, path):
    """Every item of a list endpoint. Stops on a short page, and on a page that
    brings nothing new (an endpoint that ignores page answers every page alike)."""
    sep = "&" if "?" in path else "?"
    seen, page = set(), 1
    while True:
        status, data = request("GET", f"{path}{sep}limit={PAGE}&page={page}")
        if status == 404:
            return
        if status != 200:
            raise AttachmentError(f"Gitea {path}: {status} {data}")
        items = (data.get("data") if isinstance(data, dict) else data) or []
        new = [i for i in items if i.get("id") not in seen]
        for i in new:
            seen.add(i.get("id"))
            yield i
        if len(items) < PAGE or not new:
            return
        page += 1


# --- which repositories -------------------------------------------------------------------

def normalize_scope(text):
    """'owner' or 'owner/name' from what was typed, a Gitea address too; '' if not one."""
    v = re.sub(r"^https?://[^/]+/", "", (text or "").strip()).strip("/").removesuffix(".git")
    if not re.fullmatch(r"[\w.-]+(/[\w.-]+)?", v) or any(part in (".", "..") for part in v.split("/")):
        return ""  # "." and ".." are no names: they would walk out of /repos/<owner>/<name>
    return v


def parse_excludes(text):
    """The [sources] exclude list: owner/name entries, one a line or comma separated."""
    return [s.strip() for s in (text or "").replace(",", "\n").split("\n") if s.strip()]


def apply_excludes(source, names, excludes):
    """(kept, left out). A source that names one repository is never filtered: asking
    for it by name is asking for it."""
    if "/" in source:
        return list(names), []
    gone = {e.lower() for e in excludes}
    kept = [n for n in names if n.lower() not in gone]
    return kept, [n for n in names if n.lower() in gone]


def scope_repos(request, scope):
    """The repositories of a scope, as Gitea's repository objects, by name."""
    if "/" in scope:
        status, repo = request("GET", f"/repos/{scope}")
        if status != 200:
            raise AttachmentError(f"{scope}: no such repository in Gitea ({status})")
        return [repo]
    status, _ = request("GET", f"/orgs/{scope}")
    path = f"/orgs/{scope}/repos" if status == 200 else f"/users/{scope}/repos"
    if status != 200 and request("GET", f"/users/{scope}")[0] != 200:
        raise AttachmentError(f"{scope}: no such organization or user in Gitea")
    repos = [r for r in paged(request, path) if (r.get("owner") or {}).get("login", "").lower() == scope.lower()]
    return sorted(repos, key=lambda r: r["full_name"].lower())


# --- what a repository holds ----------------------------------------------------------------

def repo_attachments(request, full, kinds=KINDS):
    """Every attachment of one repository: dicts with kind, id, name, size, where (a
    readable place: a release tag, #issue, or a comment) and path (its DELETE)."""
    out = []
    if "releases" in kinds:
        for r in paged(request, f"/repos/{full}/releases"):
            for a in r.get("assets") or []:
                out.append({"kind": "releases", "id": a["id"], "name": a.get("name") or "", "size": a.get("size") or 0,
                            "where": f"release {r.get('tag_name') or r.get('id')}",
                            "path": f"/repos/{full}/releases/{r['id']}/assets/{a['id']}"})
    if "issues" in kinds:
        for i in paged(request, f"/repos/{full}/issues?state=all"):
            for a in i.get("assets") or []:
                out.append({"kind": "issues", "id": a["id"], "name": a.get("name") or "", "size": a.get("size") or 0,
                            "where": f"#{i['number']}",
                            "path": f"/repos/{full}/issues/{i['number']}/assets/{a['id']}"})
    if "comments" in kinds:
        for c in paged(request, f"/repos/{full}/issues/comments"):
            for a in c.get("assets") or []:
                issue = re.search(r"/(?:issues|pulls)/(\d+)", c.get("issue_url") or c.get("html_url") or "")
                out.append({"kind": "comments", "id": a["id"], "name": a.get("name") or "", "size": a.get("size") or 0,
                            "where": f"#{issue.group(1)} comment {c['id']}" if issue else f"comment {c['id']}",
                            "path": f"/repos/{full}/issues/comments/{c['id']}/assets/{a['id']}"})
    return out


def usage(request, repos, kinds=KINDS, say=None):
    """One row a repository: name, git (bytes), attachments (bytes), per kind
    (bytes), files (count), total. A repository Gitea cannot list is reported, with
    its git size, and the others are still counted."""
    rows = []
    for n, repo in enumerate(repos, 1):
        full = repo["full_name"]
        if say and len(repos) > 1:
            say(f"  {n}/{len(repos)} {full}")
        row = {"name": full, "git": (repo.get("size") or 0) * 1024, "attachments": 0, "files": 0,
               "per_kind": {k: 0 for k in kinds}, "error": ""}
        try:
            for a in repo_attachments(request, full, kinds):
                row["attachments"] += a["size"]
                row["per_kind"][a["kind"]] += a["size"]
                row["files"] += 1
        except AttachmentError as e:
            row["error"] = str(e)
        row["total"] = row["git"] + row["attachments"]
        rows.append(row)
    return rows


def largest(rows, n, by="total"):
    """The n largest rows, by total (git and attachments) or by attachments."""
    return sorted(rows, key=lambda r: (-r[by], r["name"].lower()))[:max(0, n)]


def report(rows, n, kinds=KINDS, excluded=()):
    """The text of the space report: totals, then the n largest repositories by
    their whole size and their attachments."""
    gone = {e.lower() for e in excluded}
    git = sum(r["git"] for r in rows)
    att = sum(r["attachments"] for r in rows)
    lines = [f"{len(rows)} {'repository' if len(rows) == 1 else 'repositories'}: git {human(git)}, "
             f"attachments {human(att)} in {sum(r['files'] for r in rows)} files, together {human(git + att)}."]
    for k in kinds:
        lines.append(f"  {KIND_NAMES[k]}: {human(sum(r['per_kind'].get(k, 0) for r in rows))}")
    top = largest(rows, n)
    if top:
        width = max(len(r["name"]) for r in top)
        lines.append(f"\nThe {len(top)} largest, by git and attachments together:")
        lines.append(f"  {'repository'.ljust(width)}  {'git':>10}  {'attachments':>11}  {'files':>6}  {'together':>10}")
        for r in top:
            mark = "  (excluded in Sources)" if r["name"].lower() in gone else ""
            error = f"  (attachments not read: {r['error']})" if r["error"] else ""
            lines.append(f"  {r['name'].ljust(width)}  {human(r['git']):>10}  {human(r['attachments']):>11}  "
                         f"{r['files']:>6}  {human(r['total']):>10}{mark}{error}")
    return "\n".join(lines)


# --- deleting -------------------------------------------------------------------------------

def delete(request, items, dry=False, say=None):
    """Delete the attachments (from repo_attachments) from Gitea, which removes their
    files from its storage. Returns (deleted count, bytes, failed count). A dry run
    deletes nothing and counts what it would."""
    done = size = failed = 0
    for a in items:
        what = f"{a['path'].split('/')[2]}/{a['path'].split('/')[3]} {a['where']}: {a['name']} ({human(a['size'])})"
        if dry:
            if say:
                say(f"    [dry-run] delete {what}")
        else:
            status, data = request("DELETE", a["path"])
            if status not in (200, 204):
                failed += 1
                if say:
                    say(f"    not deleted: {what}: {status} {data}")
                continue
            if say:
                say(f"    deleted {what}")
        done += 1
        size += a["size"]
    return done, size, failed


# --- the menu, shared by github-to-gitea and the mirror scripts ---------------------------

SCOPE_PROMPT = "Gitea organization or user (every repository it owns), or owner/name (one repository)"


def ask_kinds(ask):
    for i, k in enumerate(KINDS, 1):
        print(f"  {i}) {KIND_NAMES[k]}")
    chosen = ask("Which, as numbers (e.g. 1, or 1 2 3)", "1 2 3")
    picked = {KINDS[int(x) - 1] for x in re.split(r"[,\s]+", chosen) if x.isdigit() and 1 <= int(x) <= len(KINDS)}
    return tuple(k for k in KINDS if k in picked)


def ask_count(ask, prompt, default=20):
    v = ask(prompt, str(default))
    return int(v) if v.isdigit() and int(v) > 0 else default


def ask_scope(ask):
    scope = normalize_scope(ask(SCOPE_PROMPT))
    if not scope:
        print("Not an organization, user or owner/name.")
    return scope


def scoped(request, scope, excludes, say):
    """The repositories of a scope, without the excluded ones of an owner."""
    repos = scope_repos(request, scope)
    names, gone = apply_excludes(scope, [r["full_name"] for r in repos], excludes)
    for name in gone:
        say(f"  {name}: excluded in Sources; left out")
    keep = set(names)
    return [r for r in repos if r["full_name"] in keep]


def space_used(request, ask, say, excludes=()):
    """Read-only: totals, the N largest repositories by git and attachments together,
    and for one repository its N largest files."""
    scope = ask_scope(ask)
    if not scope:
        return
    kinds = ask_kinds(ask)
    n = ask_count(ask, "How many of the largest to show", 20)
    say(f"Space used in Gitea by {scope}" + ("" if kinds == KINDS else f" ({', '.join(KIND_NAMES[k] for k in kinds)})"))
    repos = scope_repos(request, scope)
    rows = usage(request, repos, kinds, say)
    say(report(rows, n, kinds, excluded=[] if "/" in scope else excludes))
    if "/" in scope:
        files = sorted(repo_attachments(request, scope, kinds), key=lambda a: -a["size"])[:n]
        if files:
            say(f"\nThe {len(files)} largest files of {scope}:")
            for a in files:
                say(f"  {human(a['size']):>10}  {a['where']}: {a['name']}")


def delete_attachments(request, ask, say, dry=False, excludes=(), note=""):
    """Delete the chosen attachments of an organization, a user or one repository.
    Shows what would go first, and goes on only when the scope's name is typed."""
    scope = ask_scope(ask)
    if not scope:
        return
    kinds = ask_kinds(ask)
    if not kinds:
        return
    say(f"Reading the attachments of {scope} ...")
    repos = scoped(request, scope, excludes, say)
    plan = []
    for repo in repos:
        try:
            items = repo_attachments(request, repo["full_name"], kinds)
        except AttachmentError as e:
            say(f"  {repo['full_name']}: not read: {e}")
            continue
        if items:
            plan.append((repo["full_name"], items))
            say(f"  {repo['full_name']}: {len(items)} files, {human(sum(a['size'] for a in items))}")
    files = sum(len(items) for _, items in plan)
    size = sum(a["size"] for _, items in plan for a in items)
    what = ", ".join(KIND_NAMES[k] for k in kinds)
    if not files:
        say(f"{scope}: no {what}.")
        return
    say(f"{scope}: {files} files, {human(size)}, in {len(plan)} "
        f"{'repository' if len(plan) == 1 else 'repositories'} ({what}).")
    if dry:
        say("Dry run: nothing is deleted.")
    elif ask(f"This deletes them from Gitea for good. Type {scope} to go on") != scope:
        say("Nothing deleted.")
        return
    done = freed = failed = 0
    for full, items in plan:
        d, s, f = delete(request, items, dry, say)
        done, freed, failed = done + d, freed + s, failed + f
    say(f"{'Would delete' if dry else 'Deleted'} {done} files, {human(freed)}"
        + (f"; {failed} could not be deleted." if failed else "."))
    if note and not dry and done:
        say(note)


def menu(request, ask, say, dry=False, excludes=(), note="", extra=()):
    """Gitea's attachments: space used, and deleting them. extra: (label, function)
    items a script adds after these two."""
    while True:
        print("\nGitea's attachments: release files, and files attached to issues, pull requests"
              "\nand comments" + (" (dry run: nothing is deleted)" if dry else ""))
        print("  1) Space used: an organization, a user or one repository, and the largest (read-only)")
        print("  2) Delete attachments: of an organization or user (every repository), or of one repository")
        for n, (label, _) in enumerate(extra, 3):
            print(f"  {n}) {label}")
        print("  0) Back")
        c = ask("Choice")
        try:
            if c in ("", "0"):
                return
            if c == "1":
                space_used(request, ask, say, excludes)
            elif c == "2":
                delete_attachments(request, ask, say, dry, excludes, note)
            elif c.isdigit() and 3 <= int(c) < 3 + len(extra):
                extra[int(c) - 3][1]()
        except AttachmentError as e:
            say(str(e))

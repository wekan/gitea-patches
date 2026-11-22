#!/usr/bin/env python3
"""update-translations.sh: the newest upstream Gitea translations, with this
repository's own merged in, into dist/translations/gitea-locale/ - the copy the
WeKan version control prototypes take their Gitea translations from.

A local git repository plays upstream go-gitea/gitea; nothing is fetched from
the network and the real dist/ is not touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failed = 0


def check(name, cond):
    global failed
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


def flat(d, p=""):
    out = {}
    for k, v in d.items():
        kk = f"{p}.{k}" if p else k
        out.update(flat(v, kk) if isinstance(v, dict) else {kk: v})
    return out


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


tmp = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
try:
    # A copy of this repository's scripts and translations.
    root = os.path.join(tmp, "patches")
    shutil.copytree(os.path.join(ROOT, "releases"), os.path.join(root, "releases"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(root, "dist", "translations"))
    for name in os.listdir(os.path.join(ROOT, "dist", "translations")):
        src = os.path.join(ROOT, "dist", "translations", name)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(root, "dist", "translations", name))

    # Our Finnish for one key, and the English it was translated from.
    ours = flat(json.load(open(os.path.join(ROOT, "dist/translations/fi-FI.json"))))
    ours_en = flat(json.load(open(os.path.join(ROOT, "dist/translations/fi-FI.en-US.json"))))
    key = next(k for k in sorted(ours) if "%" not in ours_en[k] and "<" not in ours_en[k])

    # Upstream: English, a partial Finnish without that key, and German.
    up = os.path.join(tmp, "upstream")
    loc = os.path.join(up, "options", "locale")
    os.makedirs(loc)
    # Gitea's locale files are flat: "repo.issues.x" is one key.
    json.dump({key: ours_en[key], "home_title": "Home"}, open(os.path.join(loc, "locale_en-US.json"), "w"))
    json.dump({"home_title": "Koti"}, open(os.path.join(loc, "locale_fi-FI.json"), "w"))
    json.dump({"home_title": "Startseite"}, open(os.path.join(loc, "locale_de-DE.json"), "w"))
    for c in (["init", "-q", "-b", "main"], ["add", "."],
              ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "locale"],
              ["config", "uploadpack.allowfilter", "true"]):
        run(["git", "-C", up, *c], check=True)

    env = dict(os.environ, GITEA_UPSTREAM="file://" + up, TMPDIR=tmp)
    r = run(["bash", os.path.join(root, "releases", "update-translations.sh")], env=env)
    check("the update runs: " + (r.stderr.strip()[-300:] or "ok"), r.returncode == 0)
    out = os.path.join(root, "dist", "translations", "gitea-locale")
    files = sorted(os.listdir(out)) if os.path.isdir(out) else []
    check("every upstream locale is kept", {"locale_en-US.json", "locale_fi-FI.json", "locale_de-DE.json"} <= set(files))
    fi = flat(json.load(open(os.path.join(out, "locale_fi-FI.json")))) if "locale_fi-FI.json" in files else {}
    check("this repository's Finnish is merged in, where upstream has none", fi.get(key) == ours[key])
    check("upstream's own Finnish stays for a key this repository does not have", fi.get("home_title") == "Koti")
    de = flat(json.load(open(os.path.join(out, "locale_de-DE.json")))) if "locale_de-DE.json" in files else {}
    check("other languages are upstream's", de.get("home_title") == "Startseite")
    source = open(os.path.join(out, "SOURCE")).read() if "SOURCE" in files else ""
    head = run(["git", "-C", up, "rev-parse", "HEAD"]).stdout.strip()
    check("SOURCE names the upstream commit", head and head in source)

    # Negative: a ref that is an option, and a ref that does not exist, change nothing.
    before = {f: open(os.path.join(out, f), "rb").read() for f in files}
    r = run(["bash", os.path.join(root, "releases", "update-translations.sh"), "--upload-pack=x"], env=env)
    check("negative: a ref that is an option is refused", r.returncode == 2)
    r = run(["bash", os.path.join(root, "releases", "update-translations.sh"), "no-such-branch"], env=env)
    check("negative: a missing ref fails", r.returncode != 0)
    after = {f: open(os.path.join(out, f), "rb").read() for f in sorted(os.listdir(out))}
    check("negative: a failed update leaves the previous files as they were", before == after)

    menu = open(os.path.join(ROOT, "build.sh")).read()
    check("the menu offers it", "6) Download the newest Gitea translations from upstream" in menu
          and "6) command=translations;;" in menu)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"updateTranslations: {failed} failed")
sys.exit(1 if failed else 0)

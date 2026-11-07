#!/usr/bin/env python3
"""apply-translations.py: the merge rules, positive and negative, with no network.

  python3 -I tests/translations.test.py
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "releases", "apply-translations.py")
spec = importlib.util.spec_from_file_location("apply_translations", SCRIPT)
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)

failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


# --- merge rules ------------------------------------------------------------------
en = {"a": "Apple", "b": "Banana %s", "c": "Cherry", "d": "<b>Date</b>", "e": "Elder", "f": "Fig", "g": "100% zoom"}
upstream = {"a": "Omena-upstream", "c": "Kirsikka-upstream", "f": "Viikuna-upstream", "x": "removed key"}
ours = {"a": "Omena", "b": "Banaani %s", "c": "Kirsikka", "d": "Taateli", "e": "Seljapensas", "g": "Zoomaus 100 %",
        "gone": "poistettu"}
source = {"a": "Apple", "b": "Banana %s", "c": "Cherry (old English)", "d": "<b>Date</b>",
          "e": "Elder (old English)", "g": "100% zoom", "gone": "Gone"}
out, st = at.merge(en, upstream, ours, source)

check("ours wins when the English is unchanged", out["a"] == "Omena")
check("printf verbs kept", out["b"] == "Banaani %s")
check("English changed and upstream has a translation -> upstream", out["c"] == "Kirsikka-upstream")
check("English changed and upstream has none -> ours", out["e"] == "Seljapensas")
check("negative: a string missing the English HTML tags is not used", "d" not in out)
check("a key this repository does not have keeps upstream's value", out["f"] == "Viikuna-upstream")
check("'100% zoom' has no format verb", out["g"] == "Zoomaus 100 %")
check("negative: keys upstream removed are dropped", "x" not in out and "gone" not in out)
check("output is in en-US order", list(out) == [k for k in en if k in out])
check("statistics", st == dict(ours=3, ours_stale=1, upstream_newer=1, bad_tokens=1, upstream=2, removed=1))

check("verbs: %[1]s and %d", at.tokens("x %[1]s y %d") == (["%[1]s", "%d"], []))
check("negative: a lost %s is a mismatch", at.tokens("Banaani") != at.tokens("Banana %s"))
check("negative: a lost <a> is a mismatch", at.tokens("katso") != at.tokens('see <a href="x">docs</a>'))

# --- the script on a tree: writes, checksums, format ----------------------------
with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
    patches = os.path.join(tmp, "patches")
    tree = os.path.join(tmp, "gitea")
    os.makedirs(os.path.join(patches, "dist", "translations"))
    os.makedirs(os.path.join(tree, "options", "locale"))

    def write(path, data):
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    write(os.path.join(tree, "options", "locale", "locale_en-US.json"), en)
    write(os.path.join(tree, "options", "locale", "locale_xx-XX.json"), upstream)
    d = os.path.join(patches, "dist", "translations")
    write(os.path.join(d, "xx-XX.json"), ours)
    write(os.path.join(d, "xx-XX.en-US.json"), source)

    def sums():
        with open(os.path.join(d, "xx-XX.sha256sum"), "w") as f:
            for n in ("xx-XX.json", "xx-XX.en-US.json"):
                f.write(hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest() + "  " + n + "\n")

    sums()
    run = lambda *a: subprocess.run([sys.executable, "-I", SCRIPT, patches, tree, *a], capture_output=True, text=True)
    target = os.path.join(tree, "options", "locale", "locale_xx-XX.json")

    r = run("--check")
    check("--check writes nothing", r.returncode == 0 and json.load(open(target)) == upstream)
    r = run()
    check("writes the merged file", r.returncode == 0 and json.load(open(target, encoding="utf-8")) == out)
    check("format: 2-space JSON, UTF-8, newline at the end",
          open(target, encoding="utf-8").read() == json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    check("prints the languages", "LANGS=xx-XX" in r.stdout)

    with open(os.path.join(d, "xx-XX.json"), "a") as f:
        f.write(" ")
    r = run()
    check("negative: an edited translation without a new checksum is refused",
          r.returncode != 0 and "checksum mismatch" in (r.stdout + r.stderr))

# --- the real data ---------------------------------------------------------------
d = os.path.join(ROOT, "dist", "translations")
for n in os.listdir(d):
    if n.endswith(".json") and not n.endswith(".en-US.json"):
        lang = n[:-5]
        at.verify(d, lang)
        fi = json.load(open(os.path.join(d, n), encoding="utf-8"))
        src = json.load(open(os.path.join(d, f"{lang}.en-US.json"), encoding="utf-8"))
        check(f"{lang}: every string has its English source", set(fi) == set(src))
        bad = [k for k in fi if at.tokens(fi[k]) != at.tokens(src[k])]
        check(f"{lang}: every string keeps the %-verbs and HTML tags of its English ({bad[:3]})", not bad)
        check(f"{lang}: has a .md (the commit message)", os.path.exists(os.path.join(d, f"{lang}.md")))

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)

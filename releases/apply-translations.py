#!/usr/bin/env python3
"""Merge this repository's translations into an upstream Gitea source tree.

  apply-translations.py <patches-root> <gitea-tree> [--check]

For every dist/translations/<lang>.json, writes <gitea-tree>/options/locale/locale_<lang>.json.
--check only reports; it writes nothing.

WHY THIS IS NOT A .patch: upstream's locale files change through Crowdin almost
every day, and a line-based patch of a 4000-line JSON file stops applying the
first time Crowdin touches a nearby line - the fork's Finnish commits already
conflicted with upstream main 16 commits later. A translation is DATA, keyed by
message key, so it is merged by key, and that merge cannot conflict:

  dist/translations/<lang>.json         the strings this repository translated or
                                        corrected, and ONLY those: a key it did not
                                        touch keeps upstream's value, including later
                                        Crowdin updates of it.
  dist/translations/<lang>.en-US.json   the English text each of those strings was
                                        translated from.
  dist/translations/<lang>.sha256sum    checksums of both, verified first.

For each key of upstream's locale_en-US.json, in that order (the order upstream's
own locale files use):

  - this repository has a string, and upstream's English is still the English it was
    translated from -> this repository's string;
  - upstream's English has CHANGED since -> upstream's own translation when it has
    one (a translator saw the new English), otherwise this repository's string -
    an older translation of the same key beats falling back to English;
  - this repository's string does not carry the same printf verbs and HTML tags as
    the English -> it is NOT used: a missing %s breaks the page, a wrong word does not;
  - otherwise upstream's translation, when it has one.

Keys upstream has removed are dropped. Nothing here is written to options/locale/
locale_en-US.json, and no other file is touched.
"""

import hashlib
import json
import os
import re
import sys

# Go fmt verbs as Gitea's i18n formats them, and HTML tag names. "100% zoom" has no
# verb in it: 'z' is not one.
VERB = re.compile(r"%(?:\[\d+\])?[-+# 0]*(?:\d+|\*)?(?:\.\d+)?[vTtbcdoOqxXUeEfFgGsp%]")
TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")


def tokens(s):
    return sorted(VERB.findall(s)), sorted(TAG.findall(s))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def verify(directory, lang):
    """The .sha256sum lists both data files by bare name, as `sha256sum` writes it."""
    path = os.path.join(directory, f"{lang}.sha256sum")
    want = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                digest, name = line.split(None, 1)
                want[name.strip().lstrip("*")] = digest
    for name in (f"{lang}.json", f"{lang}.en-US.json"):
        if name not in want:
            raise SystemExit(f"{path}: no checksum for {name}")
        with open(os.path.join(directory, name), "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
        if got != want[name]:
            raise SystemExit(f"{name}: checksum mismatch ({got} != {want[name]}); "
                             f"regenerate {lang}.sha256sum after editing the translation")


def merge(en, upstream, ours, source):
    """(merged dict, statistics). See the module docstring for the rules."""
    out, st = {}, dict(ours=0, ours_stale=0, upstream_newer=0, bad_tokens=0, upstream=0, removed=0)
    for key, english in en.items():
        value = None
        if key in ours:
            if tokens(ours[key]) != tokens(english):
                st["bad_tokens"] += 1
            elif source.get(key) == english:
                value = ours[key]
                st["ours"] += 1
            elif key in upstream:
                st["upstream_newer"] += 1
            else:
                value = ours[key]
                st["ours_stale"] += 1
        if value is None and key in upstream:
            value = upstream[key]
            st["upstream"] += 1
        if value is not None:
            out[key] = value
    st["removed"] = sum(1 for k in ours if k not in en)
    return out, st


def dump(d):
    # Exactly the format of upstream's locale files: 2-space indent, UTF-8 as is,
    # a newline at the end. A file nothing changed in stays byte for byte the same.
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    root, tree, check = argv[1], argv[2], "--check" in argv[3:]
    directory = os.path.join(root, "dist", "translations")
    locale = os.path.join(tree, "options", "locale")
    en = load(os.path.join(locale, "locale_en-US.json"))
    langs = sorted(n[:-5] for n in os.listdir(directory)
                   if n.endswith(".json") and not n.endswith(".en-US.json")) if os.path.isdir(directory) else []
    for lang in langs:
        verify(directory, lang)
        target = os.path.join(locale, f"locale_{lang}.json")
        upstream = load(target) if os.path.exists(target) else {}
        merged, st = merge(en, upstream, load(os.path.join(directory, f"{lang}.json")),
                           load(os.path.join(directory, f"{lang}.en-US.json")))
        print(f"{lang}: {len(merged)}/{len(en)} strings translated - {st['ours']} from gitea-patches, "
              f"{st['ours_stale']} from gitea-patches for English that has since changed, "
              f"{st['upstream']} from upstream, {st['upstream_newer']} upstream translations of changed "
              f"English kept, {st['bad_tokens']} skipped for mismatched %-verbs or HTML tags, "
              f"{st['removed']} keys upstream has removed.")
        if not check:
            with open(target, "w", encoding="utf-8") as f:
                f.write(dump(merged))
    if not langs:
        print("No translations in dist/translations.")
    print("LANGS=" + " ".join(langs))


if __name__ == "__main__":
    main(sys.argv)

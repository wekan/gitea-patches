#!/usr/bin/env python3
"""Write CHANGELOG.md, the short changelog, from CHANGELOG-big.md.

  python3 releases/changelog-short.py           # write CHANGELOG.md
  python3 releases/changelog-short.py --check   # exit 1 if it is out of date

CHANGELOG-big.md is the changelog that is written: # Platforms, # Upcoming and every
release in full, with its <details> entries. CHANGELOG.md is only read: the "In short"
paragraph of the unreleased # Upcoming gitea-patches release, when there is one, and
then of each of the five newest releases, with a link to all of its changes - in plain
Markdown, no <details>, <summary> or HTML links. It is never edited by hand: run this
after every change to CHANGELOG-big.md, and tests/changelogShort.test.py fails when it
is not what this writes.

The same rules as WeKan's releases/changelog-short.mjs, for gitea-patches' headings:
"# Upcoming gitea-patches release" and "# <tag> <YYYY-MM-DD> gitea-patches release".
"""

import os
import re
import sys

BIG = "CHANGELOG-big.md"
SHORT = "CHANGELOG.md"
RELEASES = 5
UPCOMING_HEADING = "Upcoming gitea-patches release"
RELEASE_HEADING = re.compile(r"^# (\S+ \d{4}-\d{2}-\d{2} gitea-patches release)\s*$")


def anchor_of(heading):
    """The anchor GitHub gives a heading: lower case, only letters, digits, _, spaces
    and hyphens kept, spaces as hyphens."""
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading.lower()))


def to_markdown(text):
    """<details>, <summary> and their closing tags go, keeping the text inside them; an
    HTML link becomes a Markdown link."""
    text = re.sub(r"""<a\s+[^>]*?href\s*=\s*(["'])(.*?)\1[^>]*>([\s\S]*?)</a>""",
                  lambda m: f"[{m.group(3).strip()}]({m.group(2)})", text, flags=re.I)
    text = re.sub(r"</?(?:details|summary)\b[^>]*>", "", text, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def releases_of(big):
    """The releases of the big changelog, newest first: (heading, version, body)."""
    out, current = [], None
    for line in big.split("\n"):
        m = RELEASE_HEADING.match(line)
        if m:
            current = [m.group(1), m.group(1).split(" ")[0], []]
            out.append(current)
        elif line.startswith("# "):
            current = None
        elif current is not None:
            current[2].append(line)
    return [(h, v, "\n".join(b)) for h, v, b in out]


def upcoming_of(big):
    """The unreleased section's body, or None when there is none."""
    lines = big.split("\n")
    if f"# {UPCOMING_HEADING}" not in lines:
        return None
    start = lines.index(f"# {UPCOMING_HEADING}") + 1
    end = start
    while end < len(lines) and not lines[end].startswith("# "):
        end += 1
    return "\n".join(lines[start:end])


def in_short_of(body):
    """A section's "In short" paragraph without its label - the whole paragraph, to the
    first blank line - or '' when it has none."""
    m = re.search(r"^\*\*In short:\*\*[ \t]*([\s\S]*?)(?=\n[ \t]*\n|\Z)", body, re.M)
    return to_markdown(m.group(1)) if m else ""


def short_changelog(big):
    parts = [
        f"The upcoming gitea-patches release and the {RELEASES} newest, in short. Everything",
        "each one changed, and the platforms, are in",
        f"[{BIG}]({BIG}).",
        "",
        f"Written by `python3 releases/changelog-short.py` from {BIG}:",
        "make changes there.",
    ]
    upcoming = upcoming_of(big)
    if upcoming is not None:
        summary = in_short_of(upcoming)
        parts += ["", f"# {UPCOMING_HEADING}", ""]
        if summary:
            parts += [summary, ""]
        parts.append(f"[All changes so far]({BIG}#{anchor_of(UPCOMING_HEADING)})")
    for heading, version, body in releases_of(big)[:RELEASES]:
        summary = in_short_of(body)
        parts += ["", f"# {heading}", ""]
        if summary:
            parts += [summary, ""]
        parts.append(f"[All changes in {version}]({BIG}#{anchor_of(heading)})")
    return "\n".join(parts) + "\n"


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, BIG), encoding="utf-8") as f:
        want = short_changelog(f.read())
    path = os.path.join(root, SHORT)
    have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if "--check" in argv:
        if have != want:
            sys.exit(f"{SHORT} is not what {BIG} gives: run python3 releases/changelog-short.py")
        return
    if have != want:
        with open(path, "w", encoding="utf-8") as f:
            f.write(want)
    print(f"{SHORT}: the upcoming release and the {RELEASES} newest from {BIG}, in short.")


if __name__ == "__main__":
    main(sys.argv[1:])

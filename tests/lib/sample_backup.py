#!/usr/bin/env python3
"""A small backup (gitea_backup.py FORMAT 1, the one-backup layout <dir>/sample/
sample.db and sample.zip) written without a Gitea: one organization "sample",
repository sample/proj with an open issue that has a file, a comment, a merged pull
request and a closed issue. Then its WeKan import .zip.

  python3 -I tests/lib/sample_backup.py <directory>   prints the WeKan .zip's path

WeKan's tests/playwright/specs/gitea-backup-wekan-import.e2e.js imports that .zip.
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "mirror", "common"))
import gitea_backup as gb  # noqa: E402

FILE = b"sample attachment bytes\n" * 50


def make(root):
    with gb.updating(root, "sample", "gitea", lambda m: None) as w:
        _fill(w)
    return os.path.join(root, "sample")


def _fill(w):
    w.meta_set(gitea="http://gitea.invalid", owner="sample", kind="organization")
    w.row("owner", name="sample", kind="organization", json=gb.dumps({"username": "sample"}))
    full = "sample/proj"
    w.row("repos", full_name=full, name="proj", json=gb.dumps({"full_name": full, "name": "proj",
          "description": "A sample project", "created_at": "2025-12-01T10:00:00Z"}), topics="[]",
          git_bundle=None, wiki_bundle=None, empty=1, collaborators="[]")
    w.row("labels", repo=full, id=1, name="bug", json=gb.dumps({"id": 1, "name": "bug", "color": "eb4646"}))
    user = {"login": "alice"}
    issues = [(1, 0, "open", "Sample open issue", "Body of the issue"),
              (2, 1, "closed", "Sample merged PR", "Merges a feature"),
              (3, 0, "closed", "Sample closed issue", "")]
    for n, pull, state, title, body in issues:
        w.row("issues", repo=full, number=n, is_pull=pull, title=title, state=state, json=gb.dumps({
            "number": n, "title": title, "body": body, "state": state, "user": user,
            "labels": [{"id": 1, "name": "bug"}] if n == 1 else [], "created_at": f"2025-12-0{n}T10:00:00Z",
            "updated_at": f"2025-12-0{n}T11:00:00Z", **({"pull_request": {"merged": True}} if pull else {})}))
    w.row("pulls", repo=full, number=2, json=gb.dumps({"number": 2, "merged": True}))
    w.row("comments", repo=full, id=10, issue_number=1, json=gb.dumps({
        "id": 10, "body": "Sample comment text", "user": {"login": "bob"}, "created_at": "2025-12-01T12:00:00Z"}))
    path, size, digest = w.add_stream("proj/issues/1/5-notes.txt", lambda out: (out.write(FILE), (len(FILE), ""))[1])
    w.row("files", path=path, repo=full, kind="issues", parent="1", id=5, name="notes.txt", size=size,
          sha256=digest, uuid="", json="{}")


if __name__ == "__main__":
    d = make(sys.argv[1])
    zips = gb.wekan_zips(d, os.path.join(d, "wekan"), lambda m: None)
    print(zips[0])

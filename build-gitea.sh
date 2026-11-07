#!/bin/bash
# Build Gitea from source in .tools/w/gitea (github.com/wekan/gitea), wherever
# this script is started from.
cd "$(dirname "$(readlink -f "$0")")/w/gitea" || { echo "ERROR: .tools/w/gitea not found" >&2; exit 1; }

rm -f gitea

make deps

TAGS="bindata sqlite sqlite_unlock_notify" make build

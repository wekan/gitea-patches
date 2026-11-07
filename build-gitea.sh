#!/bin/bash
# Build Gitea for this computer from the patched source in ./gitea, which
# `./build.sh source` makes (upstream + this repository's patches), wherever this
# script is started from.
cd "$(dirname "$(readlink -f "$0")")/gitea" || { echo "ERROR: ./gitea not found; run ./build.sh source first" >&2; exit 1; }

rm -f gitea

make deps

TAGS="bindata sqlite sqlite_unlock_notify" make build

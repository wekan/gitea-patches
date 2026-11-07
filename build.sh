#!/usr/bin/env bash
# gitea-patches menu.
#
#   ./build.sh                    the menu
#   ./build.sh source [ref]       clone upstream + apply the patches into ./gitea, and zip it into ./out
#   ./build.sh build              build Gitea for this computer from ./gitea (./build-gitea.sh)
#   ./build.sh test               the offline tests
#   ./build.sh release-all [ref]  run Release All on GitHub (gh workflow run)
#   ./build.sh release-all-missing [tag]
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
cd "$root"
command="${1:-}"
if [ -z "$command" ]; then
  printf '1) Prepare the patched source into ./gitea and zip it into ./out
2) Build Gitea for this computer from ./gitea
3) Run the tests
4) Release All (GitHub Actions)
5) Release All Missing (GitHub Actions)
0) Exit
Selection: '
  read -r choice
  case "$choice" in
    1) command=source;; 2) command=build;; 3) command=test;;
    4) command=release-all;; 5) command=release-all-missing;; 0) exit 0;;
    *) echo 'Invalid selection' >&2; exit 2;;
  esac
else
  shift
fi
case "$command" in
  source)
    rm -rf gitea out
    bash releases/apply-patches.sh . gitea "${1:-}"
    python3 -I releases/source-zips.py gitea out ;;
  build)
    exec ./build-gitea.sh ;;
  test)
    python3 -I tests/translations.test.py
    bash tests/apply-patches.sh
    bash tests/source-zips.sh
    python3 -I tests/mirror.test.py ;;
  release-all)
    exec gh workflow run release-all.yml -R wekan/gitea-patches ${1:+-f ref="$1"} ;;
  release-all-missing)
    exec gh workflow run release-all-missing.yml -R wekan/gitea-patches ${1:+-f tag="$1"} ;;
  *) echo "Usage: $0 [source [ref]|build|test|release-all [ref]|release-all-missing [tag]]" >&2; exit 2 ;;
esac

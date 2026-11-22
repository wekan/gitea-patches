# TODO

What is not done yet here, and why. When an item is done, remove it from this
file and write its entry in the `# Upcoming gitea-patches release` section of
`CHANGELOG-big.md`.

## In progress

- Finnish: `dist/translations/gitea-locale/locale_fi-FI.json` has 4017 of
  upstream's 4107 keys (go-gitea/gitea 293b635e22, 2026-11-22); the 90 that
  upstream added since are still English, to be translated into
  `dist/translations/fi-FI.json` (and its `fi-FI.en-US.json` and
  `.sha256sum`), then merged with `./build.sh translations`.

## For the maintainer: needs a person, not code

- Push `main`: the upstream translations download, the backups' single
  directory per owner, combining dated backups, old data, and migrating all of
  Gitea into backups.
- Run "Migrate all Gitea to backups" first with `--dry-run`, and check that
  `backup_dir` is on the disk meant to become the Backups disk.

## Known failures not caused by the code

- `tests/source-zips.sh` fails on this machine because the system `unzip`
  warns about UTF-8 file names; the zips themselves are right.

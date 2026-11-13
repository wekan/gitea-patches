# A migration's clone may take a day by default

`[git.timeout] MIGRATE` is 86400 seconds when it is not set; upstream's default is
600. A migration clones the whole history before it imports anything else, and ten
minutes is not enough for a large history: LibreOffice/translations and
LibreOffice/translations-weblate broke off with `clone timed out, consider
increasing [git.timeout] MIGRATE`, and a clone that times out starts over the
next time - it fails the same way, however often it is tried.

| | before | now |
|---|--------|-----|
| `MIGRATE` not set | 600 s | 86400 s |
| `MIGRATE` set | that | that |

The timeout covers only the migration's clone (`services/repository/migrate.go`);
mirror updates keep their own `[git.timeout] MIRROR`, and a clone that finishes is
not slowed by a longer limit.

Test: `TestGitTimeoutMigrate` (the default, and a configured timeout); it fails
without the change.

Not proposed upstream yet.

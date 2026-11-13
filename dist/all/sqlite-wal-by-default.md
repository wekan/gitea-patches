# SQLite in WAL mode by default

`[database] SQLITE_JOURNAL_MODE` is `WAL` when it is not set; upstream leaves it to
SQLite's own default, `DELETE`. In `DELETE` mode every reader blocks the one
writer, so a long import and Gitea's background tasks wait for each other until
one of them gives up after `SQLITE_TIMEOUT` (20 s) with `database is locked`
(`SQLITE_BUSY`). In `WAL` mode readers and the writer work at once.

What it fixes: a GitHub migration of godotengine/godot, done by github-to-gitea,
broke off at 42,535 issues - after two and a half hours - when the nightly
repository health check (`[cron.repo_health_check]`, `@midnight`, a `git fsck` of
every repository) and the mirror and Actions queries ran beside it; storytold/harfbuzz
ended the same way. A migration cannot be resumed: its repository is removed and
the next attempt starts from the first issue.

| | before | now |
|---|--------|-----|
| `SQLITE_JOURNAL_MODE` not set | SQLite's default, `DELETE` | `WAL` |
| `SQLITE_JOURNAL_MODE = DELETE` (or any mode) | that mode | that mode |

An existing database turns to WAL the next time Gitea opens it, and stays so;
SQLite keeps the mode in the file. WAL needs memory shared between the processes
using the database, so a database on a network filesystem keeps `DELETE`, set in
`app.ini`. Gitea's own unit tests set their journal mode (`MEMORY`) themselves and
are not changed.

Test: `TestLoadDBSettingSQLiteJournalMode` (the default, and a configured mode);
it fails without the change.

Not proposed upstream yet.

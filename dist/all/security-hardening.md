# Security hardening from code scanning

What GitHub code scanning (CodeQL) found in Gitea and was worth changing. The
alerts were raised on [wekan/caddy-gitea-plugin](https://github.com/wekan/caddy-gitea-plugin),
which builds Gitea into Caddy beside WeKan: there Gitea shares its process with
other servers, so a request that exhausts memory stops all of them. The changes
are Gitea's own code, not the plugin's, so they are here, where every Gitea this
repository builds gets them. No upstream pull request has been made yet.

- **Result slice capacities** (`models/db.PageCapacity`, and every
  `make(..., 0, pageSize)` of a page of results): a capacity was taken from a
  page size as it is - the API's `?limit=`, the container registry's `?n=` for
  `/v2/<image>/tags/list` and `/v2/_catalog` - so `n=1000000000` reserved
  gigabytes before one row was read, for a public package without signing in.
  Each capacity is now at most 100, through one function whose bound CodeQL
  sees; the queries keep their limits. `TestNoPageSizeCapacity` fails on a
  `make(..., 0, x.PageSize)` anywhere in Gitea.
- **32-bit conversions** (`services/context/api.go`, `pagination.go`,
  `services/migrations/github.go`): `int64` totals and an `int64` issue number
  became `int`, which wraps on 32-bit platforms (linux/386, arm) -
  `min(total, MaxInt)` is no bound there. `totalToInt` bounds a total to the
  `int32` range, each bound returning from its own branch, and an issue number
  outside 1..MaxInt32 is refused. `TestTotalToInt` pins it.
- **Access log** (`services/context/access_log.go`): the template got the
  request itself, so a token in the query string or a credential header could
  be written to the log. It now gets a copy with `token`, `access_token`,
  `private_token` and `password`, and `Authorization`, `Cookie`, `X-Gitea-OTP`
  and `Proxy-Authorization`, replaced by `[REDACTED]`; the request is unchanged.
  A `REQUEST_ID_HEADERS` entry that names one of those headers counts as
  absent, so a credential is never logged as the request ID.
  `TestRedactedRequest` and `TestRequestIDNeverACredential` pin it.
- **Paths** (`modules/session`, `modules/tempdir`, `modules/git/hook.go`): the
  file session store refuses a session ID that is not local to its directory
  before it builds a path, as the temp directory does for names - a
  repository's name among them - that would leave it (`filepath.IsLocal`); a
  Git hook's path is built from Gitea's own list of hook names, not the name
  the request gave. Tests try `..`, absolute paths and names with `/`.
- **External renderers** (`modules/markup/external`): `GITEA_PREFIX_SRC` and
  `GITEA_PREFIX_RAW` come from the request's Host and `X-Forwarded-Proto`; they
  are substituted into the renderer's arguments only, never into the program
  to run.
- **SQL** (`models/actions/run_job_summary.go`, `models/issues/milestone_list.go`):
  the job summary upsert passes its constant statement and each value as their
  own arguments, as its MSSQL branch did, and the dashboard's milestone counts
  bind the name keyword in a constant `LIKE` statement, matching as before.
  Tests store and search SQL as text.
- **Output and redirects**: cat-file's request pipe is a `gitcmd.PipeWriter`,
  not any `io.Writer`, so writing an object name to git is not taken for a
  response; the mail preview's color scheme is one of two constant styles; the
  fetch redirect delegate's check is named `isValidRedirect`.
- **Fast hashes that are not password storage** (`modules/auth/password/pwn`,
  `modules/secret`, `modules/base`, `models/auth/twofactor.go`): the SHA-1 Have
  I Been Pwned's range API is keyed by (only five hex digits leave the server),
  the SHA-256 that turns `SECRET_KEY` into the AES key every stored secret was
  encrypted with, the MD5 that turns it into the key of the stored TOTP
  secrets, and the general `EncodeSha256` are computed through the crypto
  package's registry, saying what they are. The bytes are the same;
  `TestTwoFactorEncryptionKeyUnchanged` reads a TOTP secret stored the old way.

Upstream removed the repository adopt feature (go-gitea/gitea#40181), and with
it `services/repository/adopt.go`, whose page of unadopted directories had the
same unbounded capacity; it is not patched here. `GetHook` returns upstream's
`util.NewNotExistErrorf` for a name that is not a Git hook, as upstream does
since go-gitea/gitea#40328.

The other alerts were reviewed and are not changed; why is in the
caddy-gitea-plugin wiki's
[Hall of Fame](https://github.com/wekan/caddy-gitea-plugin/wiki/Hall-of-Fame).

# Release files of any size by default

Gitea limits each release file to `[repository.release] FILE_MAX_SIZE`, 2048 MB by
default. This patch makes the default `-1`, no limit: a release is as large as what
it releases - disk images, toolchains, bundles - and a Gitea that mirrors another
forge's releases (github-to-gitea) has no say in their sizes. A limit set in
`app.ini` is kept.

`-1` did not work in upstream Gitea before, in two places, and both are fixed:

| Where | What happened with a negative limit | Now |
|-------|-------------------------------------|-----|
| `services/attachment/attachment.go` | an API upload sent as `application/octet-stream` was wrapped in `http.MaxBytesReader(-1<<20)`, which Go takes as 0: every file refused | the reader is used only for a limit of 0 or more |
| `web_src/js/features/dropzone.ts` | the upload form gave Dropzone a negative `maxFilesize`, which refuses every file (and without one Dropzone applies its own 256 MiB) | `dropzoneMaxFilesize` gives Dropzone 0, its "no limit", for 0 or less |

Multipart uploads - the web form and github-to-gitea - already passed a known size,
which the limit check skips when the limit is negative.

Large files still need room where Gitea writes an upload before storing it (the
`TMPDIR` of the Gitea process), and a reverse proxy's own request size limit lifted.

Tests: `TestLoadRepositoryReleaseFileMaxSize` (the default, and a configured limit),
`TestUploadAttachmentSizeLimit` (known size and streamed, with and without a limit;
it fails without the `attachment.go` change) and `web_src/js/features/dropzone.test.ts`.

Not proposed upstream yet: upstream may prefer to keep a default limit and only make
`-1` work, which is the part a pull request there would start with.

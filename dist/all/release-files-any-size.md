# Release files of any size by default

Gitea limits each release file to `[repository.release] FILE_MAX_SIZE`, 2048 MB by
default. This patch makes the default `-1`, no limit: a release is as large as what
it releases - disk images, toolchains, bundles - and a Gitea that mirrors another
forge's releases (github-to-gitea) has no say in their sizes. A limit set in
`app.ini` is kept, and `0` is a largest size of 0: no file.

`-1` did not work in upstream Gitea before, in two places, and both are fixed:

| Where | What happened with a negative limit | Now |
|-------|-------------------------------------|-----|
| `services/attachment/attachment.go` | an API upload sent as `application/octet-stream` was wrapped in `http.MaxBytesReader(-1<<20)`, which Go takes as 0: every file refused | the reader is used only for a limit of 0 or more |
| `web_src/js/features/dropzone.ts` | the upload form gave Dropzone the limit as `maxFilesize`: a negative one refuses every file, 0 is Dropzone's "no limit" although the server lets no file through, and without one Dropzone applies its own 256 MiB | `dropzoneSizeOptions` reads the limit as the server does: negative is `maxFilesize` 0 (no limit), 0 is `maxFiles` 0 (no file), a number is that many MiB |

Multipart uploads - the web form and github-to-gitea - already passed a known size,
which the limit check skips when the limit is negative.

Large files still need room where Gitea writes an upload before storing it (the
`TMPDIR` of the Gitea process), and a reverse proxy's own request size limit lifted.

Tests: `TestLoadRepositoryReleaseFileMaxSize` (the default, and a configured limit),
`TestUploadAttachmentSizeLimit` (known size and streamed; no limit, a limit and 0;
it fails without the `attachment.go` change) and `web_src/js/features/dropzone.test.ts`.

Not proposed upstream yet: upstream may prefer to keep a default limit and only make
`-1` work, which is the part a pull request there would start with.

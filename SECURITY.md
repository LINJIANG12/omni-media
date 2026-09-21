# Security

## Reporting

Do not open a public issue for credential leaks or exploitable vulnerabilities.
Contact the repository owner through the GitHub profile listed on the repository
page and include a minimal reproduction, affected version, and impact.

## Security Model

- OmniMedia is a single local stdio MCP server (`omni_media.server`). The `--mode`
  argument, chosen by the registration key in the host config (`omni-media` →
  `native`, `omni-media-ext` → `ext`), decides which tools exist at all: native
  mode never registers the external-model tool.
- The host controls which local paths are passed in. `read_audio`, `read_media`,
  and `inspect_media` are declared read-only. One exception is explicit:
  `read_media` writes the transcript to `output_file` when the caller supplies one
  (absolute paths only — it replaces the file on the first slice and appends on
  continuation slices).
- `native` mode performs no network requests and requires no credentials; its
  slices stay local (cache directory `$OMNI_MEDIA_CACHE_DIR`, default
  `~/.cache/omni-media/slices`).
- `ext` mode sends the audio slice of the requested media to the endpoint
  configured by the user. That is the only data leaving the machine.
- Credentials come exclusively from `config.json`. Treat it as a secret-bearing
  file and never commit it; it is listed in `.gitignore`, and
  `omni-media config show` prints only a masked key.
- Generated audio slices and direct-to-disk transcripts may contain sensitive
  meeting or course content. Delete the cache directory (and any `output_file`
  target) when local retention is not desired.

## Supported Versions

Security fixes target the current `main` branch (currently `0.4.x`). Older source
snapshots are not separately maintained.

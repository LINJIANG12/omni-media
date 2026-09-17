# Security

## Reporting

Do not open a public issue for credential leaks or exploitable vulnerabilities.
Contact the repository owner through the GitHub profile listed on the repository
page and include a minimal reproduction, affected version, and impact.

## Security Model

- Both MCP servers are local stdio processes. The host controls which local
  paths are passed to them.
- `read_audio`, `read_media`, and `inspect_media` are declared read-only.
- Native mode performs no network requests and requires no credentials.
- External mode sends media or extracted text to the endpoint configured by the
  user. Treat `config.json` as a secret-bearing file and never commit it.
- Generated audio slices may contain sensitive meeting or course content.
  Delete `~/.omni-media/slices/` when local retention is not desired.

## Supported Versions

Security fixes target the current `main` branch. Older source snapshots are not
separately maintained.

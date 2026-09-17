# OmniMedia

OmniMedia provides two independent MCP servers for local audio and video:

| Server | Who listens | Tool | Credentials |
| :--- | :--- | :--- | :--- |
| `mcp/` | The host model's native audio capability | `read_audio` | None |
| `mcp-ext/` | A Gemini- or OpenAI-compatible endpoint | `read_media` | Local `config.json` |

Both implementations use the same `<!-- OMNI_STATUS: {...} -->` pagination
contract with `contract_version: 1`. They can be installed independently and
registered under separate keys (`omni-media` and `omni-media-ext`).

## Requirements

- Python 3.10 or newer
- MCP Python SDK 2.1.0 or newer, below 3.0
- `ffmpeg` and `ffprobe` available on `PATH`

## Source Install

The supported distribution channel is GitHub. Clone the repository, then
install either server in editable mode:

```bash
cd omni-media/mcp
pip install -e .

# External-model variant, if needed
cd ../mcp-ext
pip install -e .
```

## Any MCP Host

Print a host-neutral stdio configuration and paste it into the client:

```bash
omni-media print-config
omni-media-ext print-config
```

The output contains the current Python executable, the module entry point, and
the absolute `PYTHONPATH`. It does not write files, start the server, or expose
credentials.

Known automatic adapters remain available through:

```bash
omni-media apply --target codex
omni-media-ext apply --target codex
```

## External Endpoint Configuration

```bash
omni-media-ext config --init
omni-media-ext status --probe
```

In a source checkout, `config --init` writes to the repository root. For an
installed package, it writes to `~/.omni-media-ext/config.json`.

## License

MIT. See [LICENSE](LICENSE).

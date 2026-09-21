# 🎛️ OmniMedia — Audio/Video Listening MCP Server for AI Agents

> **One package, two listening channels.** There is a single implementation
> (`omni_media/`); the `--mode` launch argument decides which channel is exposed.
> If the host can hear audio itself, use `--mode native` (the service only cuts
> slices, zero credentials). If the host is text-only, use `--mode ext` and the
> service reads the media through an external model instead.
> Both channels share the **same pagination contract** — same status comment,
> same fields, same continuation loop — so switching only changes the
> registration key and the tool name.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?style=flat-square)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?style=flat-square)](#)
[![MCP: >=2.1.0,<3](https://img.shields.io/badge/MCP-%3E%3D2.1.0%2C%3C3-blue.svg?style=flat-square)](#)

[简体中文](README.md) · [English](README.en.md)

---

## 🎧 Choosing a channel

| | Native listening (`--mode native`) | External-model reading (`--mode ext`) |
| :--- | :--- | :--- |
| Host registration key | `omni-media` | `omni-media-ext` |
| Who listens | The **host model's** native audio capability (the service only slices) | The **service** calls a Gemini- or OpenAI-compatible endpoint |
| Tool surface | `inspect_media` + `read_audio` | `inspect_media` + `read_media` |
| Credentials | **None** | Endpoint and `api_key` in `config.json` |
| Network | Never leaves the machine | Sends each audio slice to the configured endpoint |
| Best for | Hosts with audio modality (lowest latency, zero cost) | Any text-only agent |

Both channels can be mounted at the same time: the registration keys differ, so
they never overwrite each other. `--mode all` registers both tool sets and is
**only meant for manual debugging** — hosts tell the channels apart by
registration key, so `all` makes it impossible to see from the tool list which
channel you mounted.

## 📦 Install

**Python 3.10+**. The only Python dependency is `mcp>=2.1.0,<3`; audio slicing
additionally needs system `ffmpeg` (`ffprobe` is optional — when missing, the
duration falls back to parsing `ffmpeg -i`). The supported distribution channel
is GitHub; no PyPI wheel is published.

```bash
cd omni-media
pip install -e .            # runtime
pip install -e ".[dev]"     # adds pytest, for running the tests
```

```text
omni-media/
├── omni_media/              # the only implementation package
│   └── config.example.json  # the only config template (shipped with the package)
├── tests/
├── selfcheck.py             # 5-part architecture self-check
└── pyproject.toml           # dependencies and the 4 launchers
```

## 🚀 The four launchers

All four bind to the same package `omni_media`; there is no second implementation:

| Launcher | Implementation entry | Mode | Purpose |
| :--- | :--- | :--- | :--- |
| `omni-media-mcp` | `omni_media.server:main_native` | fixed `native` | Mount this when the host has audio modality (only `read_audio`) |
| `omni-media-ext-mcp` | `omni_media.server:main_ext` | fixed `ext` | Mount this on a text-only host (only `read_media`) |
| `omni-media` | `omni_media.cli:main` | management CLI | Reports the key `omni-media`: `status` / `apply` / `print-config` / `config` / `serve` |
| `omni-media-ext` | `omni_media.cli:main_ext` | management CLI | Same, but reports the key `omni-media-ext` |

The first two *are* the MCP stdio servers, with the mode baked into the entry
point. The last two are management CLIs that differ only in which registration
key they report, so the configs produced by `print-config` / `apply` always carry
the correct `--mode`.

### Mounting

**The registration key decides the tool surface**: `omni-media` exposes only
`read_audio` (no credentials); `omni-media-ext` exposes only `read_media` (needs
an `api_key` in `config.json`). `args` must contain `--mode`:

```json
{
  "mcpServers": {
    "omni-media": {
      "command": "python",
      "args": ["-m", "omni_media.server", "--mode", "native"],
      "env": { "PYTHONPATH": "/absolute/path/to/omni-media" }
    },
    "omni-media-ext": {
      "command": "python",
      "args": ["-m", "omni_media.server", "--mode", "ext"],
      "env": { "PYTHONPATH": "/absolute/path/to/omni-media" }
    }
  }
}
```

Prefer an absolute interpreter path for `command` (GUI hosts do not always
inherit `PATH`). To avoid hand-writing it, run `omni-media print-config` (native)
or `omni-media-ext print-config` (external model): the output contains the
absolute interpreter path and the absolute `PYTHONPATH`, ready to paste.

After `pip install -e .` you may instead use the console scripts (mode already
baked in, no `--mode` needed):

```json
{
  "mcpServers": {
    "omni-media": { "command": "omni-media-mcp", "args": [] },
    "omni-media-ext": { "command": "omni-media-ext-mcp", "args": [] }
  }
}
```

Automatic adapters for known hosts (`--target` accepts `antigravity` / `codex` /
`dsh` / `opencode` / `zcode` / `all`; a diff preview is printed before writing):

```bash
omni-media status                      # environment + config + per-host mount status and paths
omni-media apply --target codex        # register the native channel
omni-media-ext apply --target codex    # register the external-model channel
omni-media unapply --target codex      # clean removal, no residue
```

> `omni-media serve [--mode native|ext|all]` also starts a server, but `serve`
> defaults to `all`; for mounting, use the `--mode` form above or the two
> dedicated MCP launchers.

## 🤖 First-Time Setup for AI Agents

> 💡 **For AI Agents taking on audio/video transcription tasks**:
> If this is your first time using this MCP service to transcribe audio or video, ensure the following configuration is in place to prevent failures due to missing credentials or timeouts:
>
> 1. **System Dependencies**:
>    - Ensure `ffmpeg` and `ffprobe` are installed and available in the system `PATH`:
>      ```bash
>      ffmpeg -version
>      ffprobe -version
>      ```
> 2. **Configuration (Required for `--mode ext` external model channel)**:
>    - Initialize the configuration file:
>      ```bash
>      omni-media config init
>      ```
>      (Saved to `config.json` in the repo root for source checkouts, or `~/.omni-media/config.json` for pip installs).
>    - Edit `config.json` and configure a valid multimodal endpoint under `endpoints`:
>      - `protocol`: `openai` (compatible with most OpenAI-format gateways/proxies) or `gemini`;
>      - `base_url`: Endpoint base URL;
>      - `api_key`: Valid API key;
>      - `model`: Multimodal model with native audio understanding (e.g. `gemini-2.5-flash`, `gemini-1.5-flash`, `gpt-4o-audio-preview`).
> 3. **Verify Recommended Performance Defaults**:
>    - `slice_minutes`: Default **30** minutes. Modern LLMs support 64k tokens single-turn output. A 30-minute lecture transcription (~12k-16k words) fits well within token limits, minimizing roundtrip overhead;
>    - `max_payload_mb`: Default **35** MB (accommodates 30-minute audio payloads);
>    - `max_concurrency`: Default **5**. A concurrency of 5 transcribes ~150 minutes of audio per minute without tripping upstream API rate limits (RPM 429).
> 4. **Self-check and Validation**:
>    - Verify configuration:
>      ```bash
>      omni-media status
>      omni-media config show
>      ```
>    - Run test suite:
>      ```bash
>      python selfcheck.py
>      ```

## 🔑 Configuration (only `--mode ext` needs it)

```bash
omni-media config init      # create config.json from the template (refuses if it exists; --force overwrites, backing up to config.json.bak.<timestamp> first)
omni-media config locate    # print the configuration file actually in effect
omni-media config show      # print the masked public view (api_key keeps only head/tail)
python -c "import json;print(json.load(open('omni_media/config.example.json',encoding='utf-8'))['active'])"
```

- **There is exactly one template**: `omni_media/config.example.json`, shipped
  with the package and read by `config init` in both a source checkout and a pip
  installation.
- **Where it is written**: source checkout → `config.json` in the repository
  root; pip installation → `~/.omni-media/config.json` (a legacy
  `~/.omni-media-ext/config.json` is still read as a fallback).
- `config.json` is gitignored and **holds a plaintext api_key — never commit it**.
- Keys: `active` (default endpoint name), `defaults` (`slice_minutes` default 30 /
  `max_payload_mb` default 35 / `timeout_sec` / `max_retries` / `max_concurrency` default 5),
  and `endpoints.<name>` (`protocol` / `base_url` / `api_key` / `model`; the OpenAI
  protocol adds `openai_mode` / `audio_format` / `text_model` / `language`).
- **Slice & concurrency notes**:
  - `slice_minutes`: Default slice length (30 minutes recommended for long lecture videos);
  - `max_payload_mb`: Payload budget per slice (35MB recommended for 30m audio);
  - `max_concurrency`: Suggested caller concurrency (default 5); underlying FFmpeg concurrency is controlled by `MAX_CONCURRENT_FFMPEG` (default 5, overridable via `OMNI_MAX_CONCURRENT_FFMPEG`).

## 🧪 Self-check and tests

```bash
python selfcheck.py                # 5 parts: imports, modes, adapters, temp-dir cleanup, CLI commands
python -m pytest -q                # requires pip install -e ".[dev]"
python -m pytest -q -m network     # run cases needing a real endpoint/network (skipped by default)
```

## 📄 Pagination and continuation contract

Long media comes back in slices; the state lives in the
`<!-- OMNI_STATUS: {...} -->` comment of each tool reply (currently
`contract_version = 1`) and carries `next_start_time` /
`next_duration_minutes`, so the next slice is read by copying those arguments.

- `MAX_ONESHOT_MINUTES = 75` is the hard per-slice ceiling; when a long file is
  read without an explicit duration it is split at
  `DEFAULT_SAFE_SLICE_MINUTES = 30` minutes.
- `read_audio` also applies an 8 MiB inline gate: above it, or for video slices,
  it switches to "write the slice to disk and return the path".
- The slice cache directory comes from `OMNI_MEDIA_CACHE_DIR` and defaults to
  `~/.cache/omni-media/slices`; the `inline` channel uses a system temp directory
  that is removed immediately.

## 🔗 Companion skill: video2book

This repository is the **stage-one listening channel** of the
**[video2book](https://github.com/LINJIANG12/video2book)** skill. That skill
requires the audio to have actually been processed (it must not fabricate a
transcript), and this is done through `read_audio` on hosts with audio modality
or `read_media` on text-only hosts.

**Boundary**: this repository only ships the MCP service — it does not import
skill code and does not write skill artifacts (it reads media and writes only the
slice cache plus an explicitly requested transcript path). The skill repository
never imports this package; the two cooperate purely over MCP.

## 📄 License

**MIT**, see [LICENSE](LICENSE).

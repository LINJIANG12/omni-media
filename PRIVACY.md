# Privacy

OmniMedia does not include telemetry, analytics, advertising identifiers, or a
remote account service. It is a single local package (`omni_media`) that exposes
one of two listening channels depending on the `--mode` launch argument.

## Native listening (`omni-media`, `--mode native`)

- Reads only the media path explicitly supplied by the MCP caller.
- Extracts audio with local `ffmpeg`. The `inline` channel slices into a system
  temporary directory that is removed as soon as the reply is built; the `file`
  channel writes slices to the cache directory described below.
- Makes no network requests and neither reads nor transmits API credentials.

## External-model reading (`omni-media-ext`, `--mode ext`)

- Reads the local media path supplied by the caller.
- Extracts the requested slice into a system temporary directory (removed
  immediately) and sends that audio to the endpoint selected in `config.json`.
- Returns the transcript in the conversation by default. If the caller passes an
  absolute `output_file`, the transcript is written there instead and persists on
  disk until you delete it.
- Reads credentials only from the local configuration file; it never reads
  credential values from environment variables.
- Endpoint operators are selected by the user and are governed by their own
  privacy and retention policies.

## Local files OmniMedia touches

- **Slice cache**: `$OMNI_MEDIA_CACHE_DIR` when set, otherwise
  `~/.cache/omni-media/slices` (see `omni_media/core/limits.py`).
- **Configuration**: `config.json` in the repository root for a source checkout,
  or `~/.omni-media/config.json` for an installed package. A legacy
  `~/.omni-media-ext/config.json` is still read as a fallback when the primary
  file does not exist. An explicit `--config <path>` overrides both.
- **Transcripts**: only the `output_file` paths passed by the caller.

## Local data removal

Delete the cache directory above (or the whole `~/.cache/omni-media/` tree), your
`config.json` in whichever location applies, any legacy
`~/.omni-media-ext/config.json`, and any transcript file you had the service write
directly to disk. Remote copies held by an endpoint operator must be removed
through that operator.

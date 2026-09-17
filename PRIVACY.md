# Privacy

OmniMedia does not include telemetry, analytics, advertising identifiers, or a
remote account service.

## Native listener (`omni-media`)

- Reads only the media path explicitly supplied by the MCP caller.
- Extracts audio with local `ffmpeg` and writes slices to `~/.omni-media/slices/`.
- Does not upload media and does not read or transmit API credentials.

## External-model listener (`omni-media-ext`)

- Reads the local media path supplied by the caller.
- Sends extracted audio or transcript text to the endpoint selected in
  `config.json`.
- Stores API credentials only in that local configuration file. The service
  does not read credential values from environment variables.
- Endpoint operators are selected by the user and are governed by their own
  privacy and retention policies.

## Local data removal

Remove the slice cache at `~/.omni-media/`, the optional configuration at
`~/.omni-media-ext/config.json`, and any configured endpoint-side data to delete
local or remote copies.

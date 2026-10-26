# Changelog

## [0.2.1] - 2026-10-26

- Included the optional self-hosted Muse MCP bridge for scan listing, image retrieval and local acknowledgement.
- Added `setup --agent muse` while preserving the existing pairing and encrypted-claim recovery behavior.
- Added connector tests and a synthetic end-to-end CLI-to-MCP scan test.
- Added public source and security links and corrected installation guidance to retain the full plugin folder.

## [0.2.0] - 2026-10-26

- Renamed the public pairing URI to `mokutori://pair` and the proof prefix to `mokutori-pair/1`.
- Added the bundled QR generator so `pair --png` works without a package install.
- Added the public-bot engine sources, pairing checks, encrypted inbox handling and local end-to-end tests
  needed to install the plugin in Grok Bot, Codex or Claude Code.
- Documented the Grok Bot upgrade path and the boundary between the plugin's local cache and the gateway's
  encrypted retention.

## [0.1.0] - unreleased

- Initial internal Mitori agent plugin prototype.

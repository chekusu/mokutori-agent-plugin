# Changelog

## [0.2.0] - 2026-10-26

- Renamed the public pairing URI to `mokutori://pair` and the proof prefix to `mokutori-pair/1`.
- Added the bundled QR generator so `pair --png` works without a package install.
- Added the public-bot engine sources, pairing checks, encrypted inbox handling and local end-to-end tests
  needed to install the plugin in Grok Bot, Codex or Claude Code.
- Documented the Grok Bot upgrade path and the boundary between the plugin's local cache and the gateway's
  encrypted retention.

## [0.1.0] - unreleased

- Initial internal Mitori agent plugin prototype.

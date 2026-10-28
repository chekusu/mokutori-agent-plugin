# Changelog

## [Unreleased]

- Add the optional agent brand `a` to both pairing links: `grok-bot` for `--agent grok` and `muse` for
  `--agent muse`; other agents omit it. Setup now records its agent, and `setup --agent` on an existing
  installation records a missing value locally without touching the gateway.
- Require `setup --name`, the bot display name Mokutori shows for this agent (at most 80 characters).
  A missing name, or "Mokutori", is refused instead of silently naming every bot "Mokutori". The skill tells
  agents to use the name their owner knows them by, and to ask when unsure.
- Add `rename --name NEW` to update the bot's gateway display name in place; `status` reports `name` and
  `agent`. Bots still named "Mokutori" should be renamed.

- Add an optional Muse bridge audit log (`MOKUTORI_MUSE_AUDIT_PATH`). It is off by default and records
  only allowlisted MCP methods, tool names, batch IDs, success flags and returned image hashes; never
  keys, headers, sessions, document text or response bodies.

## [0.2.4] - 2026-10-27

- Return a clickable `pair_link` alongside the QR image, using `https://mokutori.com/pair#...` with
  all pairing parameters in the fragment and no HTTP query secrets.
- Keep the existing `pair_url` and QR custom-scheme payload for installed scanning builds. Both entry
  points use the same one-time secret, expiry and pairing proof.
- Require the skill to show both image and clickable link, and only report success after a nonempty
  `pair-check.paired` result. Document installation fallback and the absence of trusted online presence.
- Preserve 0.2.3 owner-home migration, historical pairing records and encrypted inbox recovery.

## [0.2.3] - 2026-10-26

- Restore existing `setup.owner_home` installations for pairing, inbox recovery and unpairing. Reuse the
  original owner credential without copying it or creating symlinks; keep private-file validation.
- Restore `setup --owner-home` for new receivers under an existing Yobiru owner, and report the selected
  owner directory in `status`.
- Preserve legacy pairing records that lack modern proof/job fields. Report non-secret warnings instead
  of crashing, replaying permissions or marking an incomplete pairing successful.
- Keep malformed or expired pairing entries from blocking new codes while retaining their history.
- Add migration tests covering external owner reuse, credential-file restrictions, old pairing state,
  encrypted receiving and acknowledgement.

## [0.2.2] - 2026-10-26

- Fix the bundled QR generator importing on Python 3.8. QR image content and the pairing protocol are unchanged.

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

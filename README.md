# Mokutori for agents

Pages scanned with the Mokutori iPad app arrive directly in your own agent: Grok Bot, Codex, Claude Code or
any agent that can run a shell command. Muse connects through the included optional MCP bridge.
No computer or desktop app is needed on the scanning side.

**Current plugin version: 0.2.4**

[Website](https://mokutori.com/) · [Source](https://github.com/chekusu/mokutori-agent-plugin) ·
[Releases](https://github.com/chekusu/mokutori-agent-plugin/releases) ·
[MIT license](https://github.com/chekusu/mokutori-agent-plugin/blob/main/LICENSE) ·
[Security policy](https://github.com/chekusu/mokutori-agent-plugin/blob/main/SECURITY.md)

```
Mokutori iPad ──sealed chat (pages + OCR)──> Friends gateway ──> this agent's bot ──> `mokutori inbox`
     ▲                                        (bots.yobiru.com)
     └── scans the one-time QR code from `mokutori pair`
```

- **Pairing by QR code or a phone link.** `mokutori pair` returns both a QR image and a clickable HTTPS
  `pair_link`. Send both to the owner. They can scan the image with Mokutori, or tap the link on the phone
  where the app is installed. Both carry the same end-to-end key and one-time secret. The app asks the
  owner to confirm the receiver, then performs the handshake. Report success only when `pair-check`
  returns a nonempty `paired` result.
- **End-to-end encrypted.** Each batch is sealed on the iPad for the agent's key and the iPad's own key
  (X25519, ChaCha20-Poly1305). The gateway stores ciphertext plus routing metadata and attachment sizes.
- **Batches.** Up to five pages per message: corrected JPEGs plus OCR markdown, within the gateway's
  limits (8 files, 8 MiB). Pages are queued on the iPad and retried until they are stored.

## Install

Clone the complete package into a persistent location on the agent's host:

```sh
git clone https://github.com/chekusu/mokutori-agent-plugin.git
cd mokutori-agent-plugin
sh scripts/mokutori status
```

Keep `scripts/`, `skills/`, the plugin manifests and the optional `muse_connector.py` together.
The skill resolves the launcher relative to its own path, so copying only `SKILL.md` or the skill directory
breaks its commands. If a host supports loading a skill by path, point it at
`/absolute/path/mokutori-agent-plugin/skills/mokutori-scans/SKILL.md` in this checkout.

| Agent | How |
| --- | --- |
| Codex | Install the complete folder as a plugin; its manifest is `.codex-plugin/plugin.json`. |
| Claude Code | Install this folder as a plugin (`.claude-plugin/plugin.json`); the skill is discovered from `skills/`. |
| Grok Bot | Register the skill with the owner's permission and keep the folder under `/workspace`. If Grok still has the older Mitori plugin, follow the upgrade note below before pairing. |
| Muse | Run the included [MCP bridge](muse/README.md) on your own host and connect Muse to its HTTPS `/mcp` endpoint with that installation's API key. |

Then tell the agent: “连接我的 Mokutori iPad”. The skill walks through setup (one email code), pairing and
reading pages. Requires Python 3.8+ (standard library only).

The Muse bridge gives one API key access to one private library. It is a self-hosted integration;
publishing this source package does not provide a shared hosted library or imply marketplace approval.

## Upgrade an older Grok Bot plugin

Version 0.2.0 is the first public plugin release using the renamed Mokutori pairing protocol. Replace the old
plugin files in Grok's workspace with this release, then reload the skill. Keep the existing
`$MOKUTORI_AGENT_HOME` directory: it contains the bot credentials, paired-device records, inbox and receipts,
and an upgrade must not delete it.

Version 0.2.3 also restores older installations that stored `setup.owner_home` in `state.json` to reuse an
existing Yobiru owner. That exact directory continues to hold the owner credential; the plugin does not copy
it or create a credential symlink. `status` reports both `home` (Mokutori's receiver data) and `owner_home`
(the credential location), so verify both paths and the unchanged `bot_id` after upgrading. Keep using
`--home "EXISTING_MOKUTORI_HOME"` before every command when the original receiver used a custom path.
If `owner_home` is unavailable, restore access to that original directory; do not reset setup or substitute
another owner's credential. Credential files must still be private regular files, not symlinks.

Old pairing records may contain only `used_by`, without the proof/job fields required by the current
recovery path. Version 0.2.3 preserves these records and existing devices/receipts, and reports
`pairing_warnings` instead of replaying permissions or claiming that an old unfinished request succeeded.
Do not manually mark a pairing accepted or replace the whole state from a backup to clear this warning.
If that device still needs pairing, generate a fresh code. Keep any pre-upgrade backups intact.

Generate a fresh QR code after upgrading:

```sh
sh scripts/mokutori status
sh scripts/mokutori pair --png /tmp/mokutori-pair.png --minutes 30
sh scripts/mokutori pair-check --wait 600
```

Version 0.2.4 adds a clickable HTTPS link alongside the image. Show the owner **both** `qr_png` and
`pair_link`, formatted as a clickable “Open Mokutori to pair” link, rather than only showing a QR code
on the phone they need to pair.

| Output | Purpose |
| --- | --- |
| `qr_png` | The QR image; its payload remains the existing custom scheme for older scanning builds. |
| `pair_link` | `https://mokutori.com/pair#v=1&g=...&b=...&k=...&i=...&s=...` for a clickable phone link. |
| `pair_url` | The existing `mokutori://pair?...` URL, retained for compatible clients and the QR payload. |

The HTTPS URL puts all pairing parameters after `#`, with no query string. Browsers do not send this
fragment in the HTTP request for `/pair`. Do not move the secret into a query/path, use a URL shortener,
or publish the full link. With a supporting app and Universal Link association, tapping opens the app's
receiver confirmation. Otherwise `/pair` provides installation and app-opening guidance. Installing the
app does not preserve an expired code: return to the original link or request a fresh one after installation.

New pairings use `mokutori://pair` and the `mokutori-pair/1` proof prefix. A QR generated by an older Mitori
plugin (`mitori://pair`) is not compatible with this protocol; do not reuse it. Pairing codes are one-time secrets and
expire, so show the new image only to the owner and remove the temporary image after scanning.

For a **new** installation whose owner already uses Yobiru, `setup --owner-home /absolute/YOBIRU_HOME --agent grok`
creates a new Mokutori receiver under that existing owner without another email registration.
An existing installation needs only the plugin upgrade; do not rerun setup or use `setup --force` to repair it.

## Commands

```sh
sh scripts/mokutori setup --email you@example.com --agent codex      # step one: a code is emailed
sh scripts/mokutori setup --email-code 123456 --agent codex          # step two
sh scripts/mokutori pair --terminal                                  # one-time QR code
sh scripts/mokutori pair-check --wait 120                            # accept the iPad
sh scripts/mokutori inbox                                            # save and show new pages
sh scripts/mokutori inbox --ack                                      # clear local unread markers
sh scripts/mokutori devices
sh scripts/mokutori unpair ACTOR_ID
sh scripts/mokutori status
```

## Under the hood

The transport is the Yobiru 0.7.14 Friends jobs protocol, pinned to the exact official source revision in
`scripts/engine/SOURCE.json`. This plugin does not use the retired notice inbox. `scripts/engine/` is an unmodified copy of the Yobiru
public-bot client (MIT, see `scripts/engine/LICENSE` and `scripts/engine/SOURCE.json`); `scripts/third_party/qrcodegen.py` is
Project Nayuki's QR Code generator (MIT). The agent owns a bot on the gateway with two local identities:
an owner credential (accepts paired iPads) and a native reader binding (claims and reads jobs). The iPad is a
pseudonymous caller; after pairing it is a trusted contact allowed to send files.

Pairings use the `mokutori-pair/1` proof prefix. `mokutori://pair?...` and the HTTPS fragment link carry
the same parameters and consume the same one-time pairing; the HTTPS link does not create another pairing.

The plugin has no trusted real-time agent presence field. The gateway's `agent_connected` and
`binding_configured` describe configuration, and pairing describes permission. Neither proves that a Grok,
Muse or other personal agent is currently online. Consumers should keep online status unknown unless they
have separate reliable presence evidence.

The iPad app is maintained separately. `tests/app_protocol_reference.py` is a self-contained
Python reference of its pairing and delivery protocol, and `tests/test_mokutori_plugin.py` runs the whole flow (setup,
QR pairing, a forged and a replayed pairing, a sealed three-page batch, inbox, acknowledgement, unpair)
against a real local gateway. The runner archives the pinned commit from an existing local Yobiru Git
repository and uses fresh temporary gateway and identity folders. The same runner includes the Muse
connector tests and a CLI-to-MCP scan retrieval round trip. It does not contact a production gateway:

```sh
python3 tests/run_e2e.py /path/to/yobiru
```

Receiving claims a job, verifies its sender and encryption binding, saves its files and a private local receipt,
then finishes it with `outcome: read` without sending a reply. `inbox` keeps showing the saved batch until
`inbox --ack` clears its local unread marker; acknowledgement never deletes the files. Repeated sends and
repeated inbox reads reuse the same message receipt. An interrupted saved claim resumes its own lease with
`/start`. Yobiru 0.7.14 can mark an accepted sender's expired claim read; the plugin verifies gateway metadata
and recovers its own authenticated local cache, including pairing proofs. Before renewing a claim, it
saves an independent private encrypted snapshot so the official client cannot delete the only copy when
the lease expires between listing and renewal. The snapshot is removed only after the local receipt is saved. Truly uncertain claims include
an owner-only retry command. They are retried only after that explicit resolution, with the same message
and files checked again. Existing local acknowledgement is retained in that same atomic receipt update. A lost finish response is reconciled against gateway metadata. Disconnecting a device blocks future sends and stops new receipts from
that device; previously saved pages remain available.

Limits: an agent learns about new pages only when it runs `inbox` (or a scheduled task does); waking an
idle hosted agent is not automatic. The official gateway stores sealed messages for up to 30 days.

Test image: `tests/synthetic-page.jpg` is a 32×32 downsample of the iPad project's generated
`synthetic-flat.png` fixture. It contains no user scan or personal metadata. `tests/seal_vectors.json`
uses fixed, synthetic keys and identifiers for protocol tests; those keys must never be used with real documents.

## License

Mokutori plugin code is released under the [MIT License](LICENSE). The bundled Yobiru engine retains its
[MIT license](scripts/engine/LICENSE), and Project Nayuki's QR generator retains its MIT notice in
[`qrcodegen.py`](scripts/third_party/qrcodegen.py). Agent names and trademarks belong to their respective owners;
compatibility does not imply endorsement.

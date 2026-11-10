# Changelog

## [Unreleased]

- Claude Code can install the plugin from this repository as a marketplace
  (`/plugin install mokutori --marketplace chekusu/mokutori-agent-plugin`); `.claude-plugin/marketplace.json` lists
  the plugin at the repository root.
- The README and manifests describe the Mokutori app for iPhone and iPad (it is universal since 1.0) and link to
  the App Store.

## [0.3.6] - 2026-11-07

- Batches no longer go missing late in a long scan. A claimed job carries a transcript of its conversation, and
  the gateway reads every earlier message of the conversation to build it, so late in a long conversation of page
  batches a claim took longer than the engine's 15-second request limit. The client gave up while the gateway had
  already started the job; ten minutes later the expired claim was marked read and the batch never reached this
  folder (pages 113–125 in one scan, found by `recover`). Claims now wait up to 170 seconds, and a claim whose
  answer is lost is received at once from the gateway's own copy of that message (read with the owner
  credential, checked as a claimed job is). The receipt notes `lease_lost` until the gateway ends that claim.
- `inbox` no longer stops with a lease error on a job that started at the gateway without a claim here (the
  same lost answer, seen on a later run): it saves that batch from the gateway's copy and goes on with the rest.
- `inbox` claims for about 90 seconds and then reports how many jobs still wait in `waiting_at_gateway`; `next`
  says to run `inbox` again. A host that cuts long commands loses nothing already saved.

## [0.3.5] - 2026-11-07

- `recover` no longer times out on a long history. The gateway reads every kept body of a conversation (about
  100 in a full one) from its store before it answers, which took longer than the client's 15-second request
  limit, so 0.3.4's `recover` failed on a real agent. A history request now has 170 seconds, and a run checks
  conversations for about `--seconds` (default 40; at least one) and keeps its place, so a host that cuts long
  commands loses nothing: run `recover` again until `remaining_conversations` is 0. Conversations that were open
  while a gap in this folder's page numbers opened are checked first, so the missing batches usually come back
  in the first run. `--restart` checks everything again; a finished pass starts over on the next run.
- The report adds `checked_now` (conversation, time, turns, seconds), `remaining_conversations`, and per device
  `conversations_checked`, `turns_at_gateway`, `batches_here` and `gaps_here` (page numbers still missing in
  this folder). `missing_pages` appears once every conversation of the device is checked.

## [0.3.4] - 2026-11-07

- New `recover` command for pages the owner sent that the agent does not have. It compares what each paired
  device sent through the gateway (the owner's view of its conversations, read-only) with the data folder, and
  saves every batch the gateway still keeps (30 days by default) but the folder never got or lost, as an unread
  batch for `inbox`. This covers batches marked read by another run or host whose files did not survive, batches
  that ended at the gateway before they were read, and deleted files. Each batch is opened with the bot's key and
  verified as `inbox` verifies a job; nothing changes at the gateway. Per device it reports `missing_here`,
  `waiting_at_gateway`, `files_missing_here`, `unreadable` and `missing_pages`: page numbers no batch at the
  gateway or here carries, which the app never delivered. `other_senders_waiting` shows who sent the messages
  `inbox` ignores as unpaired, when, and with how many files. `--check` only reports; `recover MESSAGE_ID ...`
  saves named batches again.
- `inbox` adds `ignored_note` when it ignores messages from unpaired senders, pointing to `recover --check`.

## [0.3.3] - 2026-11-06

- `inbox --ack` no longer acknowledges batches the agent has not seen. With pages sent continuously, the next
  batch often arrived between an agent's `inbox` and its `inbox --ack`; the `--ack` run saved it and acknowledged
  it in the same step, so it never showed in a later `inbox` and whole batches went missing from the middle of a
  scan. `--ack` now clears only the batches it names (`--ack MESSAGE_ID ...`), or with none named the batches an
  earlier `inbox` printed. Anything else is printed under `batches` as still unread, and `next` says to read it and
  acknowledge it. The output adds `acknowledged_ids`; `batches` in an `--ack` run now lists the batches still
  unread, not the ones just acknowledged.
- `inbox` prints a long backlog a part at a time: at most `--limit` batches and about 20,000 characters, so an
  agent host that cuts long tool output (Claude Code at 30,000 characters) no longer drops batches from the middle
  of what `--ack` then clears. `remaining` says how many more wait, and batches are shown in arrival order.

## [0.3.2] - 2026-11-02

- A second agent of the same owner sets up with the owner's email. The official gateway lets one email register
  one owner, so a Muse set up after a Grok Bot stopped at `email_in_use`, and the only way round it copied the
  owner credential, which administers every bot, to the new machine. `setup --email-code` now adds the new bot to
  that owner with the same code (Yobiru 0.7.20 `POST /v1/enroll/join`), and the new data home keeps only a
  manager credential for its own bot. `pair-accept`, `pair-decline`, `rename`, `wake`, `unpair` and recovery use
  it unchanged; it cannot reach the owner's other bots. `status` shows `credential: "manager"` for such a setup.
  Existing setups, including those with `setup.owner_home`, behave as before.
- The skill and README no longer suggest copying an owner directory to another machine (`--owner-home` is for an
  owner directory on the same machine).
- Update the bundled engine to Yobiru 0.7.20, which knows manager credentials and the join route. The full suite
  runs against the pinned 0.7.20 gateway, plus a new join test with email verification on.

## [0.3.1] - 2026-11-01

- Add `wake set --url-file F --secret-file F`, `wake status`, `wake test` and `wake clear` for Yobiru 0.7.17
  hosted wakes: the gateway calls a routine's webhook when a pairing request or pages arrive, so a hosted agent
  such as Grok Bot is woken without a machine running a connector. Registering sends a note telling the agent to
  run `pair-requests` and `inbox` (the gateway's own text names no client) and a test wake unless `--no-test`.
  `wake status` shows failures, the next allowed wake and a route the gateway turned off. The URL and key are read
  from private files and never printed; gateways without hosted wakes are reported plainly.
- Update the bundled engine to Yobiru 0.7.17. It opens sealed fields of up to 16 MiB, so a full 8 MiB batch from
  Mokutori can be read once the app sends one.

## [0.3.0] - 2026-11-01

- Pairing now waits for the owner. A verified pairing request is recorded, not admitted:
  `pair-check`, `pair-requests` and `inbox` list it in `pair_requests`, the agent asks the owner, and
  `pair-accept PAIRING_ID` admits the device (grants `chat,files` and accepts the request). `pair-decline
  PAIRING_ID` rejects the request at the gateway, so Mokutori can tell the owner it was declined.
- A pairing request counts when it reached the gateway before its code expired, however late the agent
  reads it. Before, an agent that checked after the 15 minutes silently dropped a request that had arrived
  in time, and Mokutori waited forever.
- The skill tells agents to keep checking with a short scheduled task after `pair`, and to accept only
  after the owner agrees. Pairing records saved by 0.2.x keep their behaviour.

## [0.2.5] - 2026-10-28

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

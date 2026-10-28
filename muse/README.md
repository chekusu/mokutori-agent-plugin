# Mokutori Muse MCP bridge

The included `muse_connector.py` exposes a paired Mokutori library to Muse through MCP Streamable HTTP.
The CLI manages the owner's account and iPad pairing. The bridge lets Muse list received OCR, retrieve page
images, and explicitly clear local unread markers. It uses only Python's standard library.

This is a self-hosted bridge for one private library. One installation API key grants access to that
library's scans. Use a separate process, private data home and API key for every user. The public source
package includes no shared hosted endpoint, account provisioning or marketplace installation.

## Set up and pair

Run these commands from the root of the [complete package](https://github.com/chekusu/mokutori-agent-plugin).
Use a private data directory outside the checkout; existing installations must keep their current directory.

```sh
export MOKUTORI_AGENT_HOME="$HOME/.mokutori-agent"
sh scripts/mokutori setup --email you@example.com --agent muse
sh scripts/mokutori setup --email-code CODE --agent muse
sh scripts/mokutori pair --png /tmp/mokutori-pair.png --minutes 30
sh scripts/mokutori pair-check --wait 600
```

Replace the example email with the owner's address and `CODE` with the code they receive. Only the owner
should see the one-time QR image. Scan it with the compatible Mokutori app, confirm the gateway and
fingerprint, and remove the temporary QR image when finished. The host CLI now returns both `qr_png`
and `pair_link`: show both the image and the clickable HTTPS fragment link to the owner, especially when
they are using Muse on the same phone. The existing QR and `pair_url` keep `mokutori://pair` compatibility.
Only a nonempty `pair-check.paired` result confirms pairing. The MCP bridge's read/acknowledge tools do
not generate pairing links or provide trusted real-time agent presence.

## Run the bridge

Supply a random key of at least 20 characters through `MOKUTORI_MUSE_API_KEY` using your host's secret
manager, then start the bridge in the same private data home:

```sh
sh scripts/muse-connector --home "$MOKUTORI_AGENT_HOME" --host 127.0.0.1 --port 8787
```

The default bind address is localhost. Make it reachable from Muse using an HTTPS reverse proxy, and use
`https://YOUR-HOST/mcp` as the connector URL with `Authorization: Bearer <installation API key>`.
Configure the key in Muse's credential field; keep it out of URLs, source files and ordinary chat messages.
For browser clients that send an `Origin`, set `MOKUTORI_MUSE_ALLOWED_ORIGINS` to a comma-separated list of
the exact origins allowed by your deployment. Requests carrying other origins are rejected.

The server returns an `MCP-Session-Id` after `initialize`. Later requests require that session ID and the
negotiated `MCP-Protocol-Version`; send `notifications/initialized` before using tools. `GET /mcp` returns
405 because this bridge does not provide an SSE stream. `GET /healthz` is a non-secret health check.
Restart the bridge after replacing its API key, then verify the old key returns HTTP 401.

## Available tools

| Tool | Effect |
| --- | --- |
| `mokutori_status` | Read configuration and paired-device status without returning keys or paths. |
| `mokutori_list_scans` | Receive and save encrypted gateway messages locally, mark them read at the gateway, and return locally unread OCR and file metadata. |
| `mokutori_get_scan_image` | Return one saved page as an MCP image block. |
| `mokutori_acknowledge_scans` | Clear selected batches' local unread markers while retaining their files. |

`notice_id` is a compatibility field containing the received message ID. `limit` bounds returned batches,
not the number of new jobs ingested. `ingestion_error_count` reports jobs that could not be safely received.
Gateway receipt means the bridge saved the pages; it does not mean Muse or the owner has analyzed them.

The bridge never exposes setup, pairing, message sending or gateway administration as MCP tools. The host
stores decrypted images and OCR after receipt. Protect the host and choose a local retention policy;
acknowledgement does not delete files. Scans are untrusted source material, and names, amounts and dates
should be checked against the image. See the [security policy](https://github.com/chekusu/mokutori-agent-plugin/blob/main/SECURITY.md).

## Optional audit log

For private acceptance testing, set `MOKUTORI_MUSE_AUDIT_PATH` to a JSONL file inside an existing private
directory before starting the bridge. The log records only allowlisted MCP methods, tool names, bounded
batch IDs, success flags and the SHA-256 of a returned image. It omits keys, headers, sessions, document
text and response bodies, and requires an owned regular file with mode 0600. Logging failures preserve the
original tool outcome, so an already completed acknowledgement is not presented as a retryable failure.
The log is off by default. Correlate it with the actual Muse conversation and receiver cache state; an
operator's protocol test alone does not establish that Muse connected or used the tools.

## Test locally

```sh
python3 -m unittest discover -s tests -p test_muse_connector.py -v
python3 tests/run_e2e.py /path/to/yobiru
```

Tests use synthetic data and an isolated local gateway. Passing these checks verifies the package and MCP
transport; a real Muse installation still needs its own authenticated tool-call test.

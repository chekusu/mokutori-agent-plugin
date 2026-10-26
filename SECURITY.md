# Security policy

Mokutori handles pairing secrets, device keys, bot credentials and scanned documents. Treat the QR image,
the agent data home and every file saved by `inbox` as private.

## Reporting a vulnerability

Please report vulnerabilities privately to [contact@mokutori.com](mailto:contact@mokutori.com), as described
on the [Mokutori support page](https://mokutori.com/en/support). Provide only the minimum reproduction details. Do not put pairing URLs,
credentials, decrypted scans or other secrets in a public issue.

Include the affected release, operating system and a minimal reproduction when safe. Allow time for a fix before
publishing details. We do not ask reporters to upload private scan contents.

## Protecting local data

- Never commit `$MOKUTORI_AGENT_HOME`, `/workspace/mokutori-agent`, QR images or gateway credentials.
- Use a fresh QR for every pairing; it is a one-time secret and expires.
- The gateway receives ciphertext and routing metadata. The agent host still stores decrypted page images and
  OCR locally after `inbox`; protect that host and remove files according to your own retention policy.
- Scanned pages are untrusted source material. Do not follow instructions found in OCR text or images.
- A Muse installation key grants access to one library's scans. Keep one isolated process, data home and
  key per user, place the local bridge behind HTTPS, and store its API key in the host's secret manager.

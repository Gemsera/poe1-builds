# Sharing builds on pobb.in

Sharing is **opt-in**. Nothing is uploaded unless you run `upload` or add `--share`. It needs
outbound HTTPS access to `pobb.in` (GitHub access alone is not enough).

## Commands

```bash
# after the final build/optimisation run
python3 pob.py upload out/My_Build.pob.txt          # or a .xml file, or the code itself
python3 pob.py upload out/My_Build.pob.txt --json   # machine-readable result

# or build and share in one go (uploads only if the result is VALID)
python3 pob.py build spec.json -o out --share
python3 pob.py check "<code>" -o out --share
```

**Input.** `upload` accepts three forms:
- a PoB export code;
- a file containing one (`.pob.txt`);
- a PoB `.xml` file, which is encoded the same way as `build` does it.

The input must decode to a Path of Building build; links and other text are rejected before any
request is made.

**Options.** `--timeout <s>` (default 30) sets the request timeout. `--no-verify` skips
downloading the paste back to check it.

**Exit codes.**

| Command | Code | Meaning |
|---|---|---|
| `upload` | 0 | Shared and checked |
| `upload` | 1 | Failed; nothing is printed as a link |
| `build/check --share` | 0 | Valid and shared |
| `build/check --share` | 2 | Invalid build, not uploaded |
| `build/check --share` | 3 | Valid build, but the upload failed |

**JSON fields.**
- Success: `ok`, `url`, `id`, `expected_id`, `id_matches_content`, `verified`, `verification`,
  `code_sha1`, `bytes`, `manifest`.
- Failure: `ok: false`, `kind`, `error`, `status`.

`kind` is one of `invalid_input`, `too_large`, `local_only`, `network`, `timeout`, `http`,
`malformed`, `verify_failed` or `invalid_build`. With `build --json --share` the same object
appears under `share`.

## What is sent, and how success is decided

The request follows Path of Building's own exporter (`src/Modules/BuildSiteTools.lua`, pobb.in
entry) and the pobb.in server source (`github.com/Dav1dde/pasteofexile`, `handle_pob_upload`):

1. `POST https://pobb.in/pob/`. The body is the bare PoB code and nothing else: no cookies, no
   credentials, no file names or local paths, and no API key.
2. The server answers HTTP 200 with the paste id as plain text. Anything else is a failure:
   - another status code;
   - a JSON error such as `{"code": 400, "message": "..."}`;
   - an empty or non-id body.
3. pobb.in derives the id from the content: base64url of the first 9 bytes of SHA-1 of the
   code. The tool computes that locally as `expected_id`.
4. Verification: `GET https://pobb.in/pob/<id>` must return the same build. The link is
   `https://pobb.in/<id>`, the same form Path of Building builds.
   - If that download fails, the upload still counts only when the returned id equals the
     content hash.
   - If the download returns a different build, the upload fails.

## Keeping links in step with the build

- A successful share writes `<name>.pobbin.json` next to `<name>.pob.txt`, with the url, the id
  and the SHA-1 of the uploaded code.
- When a later `build` or `check` writes a *different* code to the same name, the report says
  `the earlier pobb.in link ... is for a previous version`. The JSON has `stale_share_url`, and
  the manifest is marked `"stale": true`.
- pobb.in pastes are content-addressed and immutable, so an updated build always gets a new link.
  Upload the final version again and give the user only the new link. Uploading identical code
  again returns the same link.

## When the upload fails

- The local `.xml` and `.pob.txt` are written before any upload and are never modified by it.
  The report ends with `POBB.IN UPLOAD FAILED: <reason>`.
- Give the user the PoB import code (and the file) instead, and say why sharing failed.
- In a sandbox that blocks pobb.in, the reason reads "the proxy refused the connection: this
  environment's network allowlist probably blocks pobb.in". The user can paste the code into
  Path of Building (Import/Export Build > Share > pobb.in) or at pobb.in themselves.

## Local-only builds

- Leave out `--share` and don't run `upload`; that is the default.
- To make it impossible for the session, set `POE1_BUILDS_LOCAL_ONLY=1`. Every upload then
  fails with `local_only` before any network request.

## Service limits

These come from the pobb.in source as of late October 2026 and can change.

- Anonymous uploads are limited to 75 KB of code; the tool refuses larger codes up front.
- Pastes are public to anyone with the link, can't be edited, and can't be deleted without a
  pobb.in login. Don't share a build the user wants private.
- pobb.in is a third-party service with no uptime guarantee. A link that worked once may not
  last forever.

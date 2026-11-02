"""Upload a Path of Building export code to pobb.in and return its public link.

Protocol, taken from the reference implementations (not guessed):
- Path of Building Community, src/Modules/BuildSiteTools.lua (pobb.in entry):
  postUrl "https://pobb.in/pob/", postFields "" (the request body is the bare PoB code),
  codeOut "https://pobb.in/" is prefixed to the response body to form the link,
  downloadURL "pobb.in/pob/<id>" returns the stored code. Only HTTP 200 counts as success.
- pobb.in server (github.com/Dav1dde/pasteofexile, worker/src/api.rs handle_pob_upload):
  the body must be a PoB code that decodes and parses as a build; the response body is the paste
  id as plain text; the id is base64url(no padding) of the first 9 bytes of SHA-1(body), so it is
  content-addressed and immutable; anonymous uploads are limited to 75 KB; errors are JSON
  {"code": <status>, "message": "..."} with status 400 (bad / invalid build) or 404.

Only the build code is sent: no cookies, credentials, file names or local paths.
Standard library only (urllib); the sandbox's HTTPS proxy settings are honoured automatically.
"""
import base64
import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.request
import zlib

POST_URL = "https://pobb.in/pob/"
DOWNLOAD_URL = "https://pobb.in/pob/%s"
PUBLIC_URL = "https://pobb.in/%s"
MAX_UPLOAD_BYTES = 75 * 1024
USER_AGENT = "poe1-builds (headless Path of Building Community)"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,90}$")
LOCAL_ONLY_ENV = "POE1_BUILDS_LOCAL_ONLY"


class UploadError(Exception):
    """kind: invalid_input | too_large | local_only | network | timeout | http | malformed | verify_failed"""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def expected_id(code: str) -> str:
    """The id pobb.in assigns to this exact code (content hash, see module docstring)."""
    digest = hashlib.sha1(code.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest[:9]).decode("ascii").rstrip("=")


def code_sha1(code: str) -> str:
    return hashlib.sha1(code.encode("utf-8")).hexdigest()


def _decode(code: str) -> str:
    raw = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
    return zlib.decompress(raw).decode("utf-8")


def validate_code(code: str) -> str:
    """Return the normalised code (whitespace removed) or raise UploadError(invalid_input/too_large)."""
    code = re.sub(r"\s+", "", code or "")
    if not code:
        raise UploadError("invalid_input", "empty build code")
    if not re.match(r"^[A-Za-z0-9_\-+/=]+$", code):
        raise UploadError("invalid_input", "not a Path of Building export code (unexpected characters)")
    try:
        xml = _decode(code)
    except Exception as e:  # binascii.Error, zlib.error, UnicodeDecodeError
        raise UploadError("invalid_input", "not a Path of Building export code (%s)" % e)
    if "<PathOfBuilding" not in xml[:500] or "<Build" not in xml:
        raise UploadError("invalid_input", "the code decodes, but not to a Path of Building build")
    if len(code.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise UploadError("too_large", "build code is %d bytes; pobb.in accepts at most %d bytes without logging in"
                          % (len(code), MAX_UPLOAD_BYTES))
    return code


def _request(opener, req, timeout):
    try:
        with opener(req, timeout=timeout) as r:
            return r.getcode(), r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        msg = body.strip()[:300]
        try:
            j = json.loads(body)
            if isinstance(j, dict) and j.get("message"):
                msg = str(j["message"])[:300]
        except ValueError:
            pass
        raise UploadError("http", "HTTP %d from pobb.in%s" % (e.code, (": " + msg) if msg else ""), status=e.code)
    except (socket.timeout, TimeoutError):
        raise UploadError("timeout", "pobb.in did not answer within %ss" % timeout)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise UploadError("timeout", "pobb.in did not answer within %ss" % timeout)
        hint = ""
        if "403" in str(reason) and "unnel" in str(reason):
            hint = " (the proxy refused the connection: this environment's network allowlist probably blocks pobb.in)"
        raise UploadError("network", "could not reach pobb.in: %s%s" % (reason, hint))
    except OSError as e:
        raise UploadError("network", "could not reach pobb.in: %s" % e)


def upload(code: str, timeout: float = 30, verify: bool = True, opener=None) -> dict:
    """Upload a PoB code. Returns a result dict; raises UploadError on any failure.

    Success requires HTTP 200 with a well-formed id AND either a download that returns the same
    code (verify=True) or, when verify=False, an id equal to the content hash of what was sent.
    """
    if os.environ.get(LOCAL_ONLY_ENV, "").strip() not in ("", "0"):
        raise UploadError("local_only", "%s is set: uploads are disabled for this session" % LOCAL_ONLY_ENV)
    opener = opener or urllib.request.urlopen
    code = validate_code(code)
    exp = expected_id(code)
    req = urllib.request.Request(POST_URL, data=code.encode("utf-8"), method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain"})
    status, body = _request(opener, req, timeout)
    if status != 200:
        raise UploadError("http", "unexpected HTTP %s from pobb.in" % status, status=status)
    pid = body.strip()
    if not ID_RE.match(pid):
        raise UploadError("malformed", "pobb.in answered 200 but not with a paste id: %r" % body[:120])
    result = {"ok": False, "id": pid, "url": PUBLIC_URL % pid, "expected_id": exp, "id_matches_content": pid == exp,
              "code_sha1": code_sha1(code), "bytes": len(code), "verified": False, "verification": None}
    if verify:
        dreq = urllib.request.Request(DOWNLOAD_URL % pid, headers={"User-Agent": USER_AGENT})
        try:
            dstatus, stored = _request(opener, dreq, timeout)
        except UploadError as e:
            if not result["id_matches_content"]:
                raise UploadError("verify_failed", "upload returned id %s, which is not the hash of this build, and the "
                                  "stored copy could not be downloaded to check it: %s" % (pid, e))
            result["verification"] = "download failed (%s); id equals the content hash of this build" % e
        else:
            stored = re.sub(r"\s+", "", stored)
            same = dstatus == 200 and (stored == code or _safe_decode(stored) == _safe_decode(code))
            if not same:
                raise UploadError("verify_failed", "pobb.in returned id %s but the stored build differs from the one uploaded" % pid)
            result["verified"] = True
            result["verification"] = "downloaded %s and it matches the uploaded build" % (DOWNLOAD_URL % pid)
    elif result["id_matches_content"]:
        result["verification"] = "not downloaded; id equals the content hash of this build"
    else:
        raise UploadError("verify_failed", "pobb.in returned id %s, which is not the hash of this build (expected %s); "
                          "rerun with verification enabled" % (pid, exp))
    result["ok"] = True
    return result


def _safe_decode(code):
    try:
        return _decode(code)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Share manifest: <build>.pobbin.json next to the local files, so a link is never
# presented for a build that has changed since it was uploaded.
# ---------------------------------------------------------------------------
def manifest_path(base):
    return base + ".pobbin.json"


def write_manifest(base, result, source):
    data = {k: result[k] for k in ("url", "id", "code_sha1", "verified", "verification")}
    data["source"] = source
    with open(manifest_path(base), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return manifest_path(base)


def stale_link(base, current_code):
    """If <base>.pobbin.json exists for a different code, mark it stale and return its url."""
    p = manifest_path(base)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("code_sha1") == code_sha1(re.sub(r"\s+", "", current_code)):
        return None
    if not data.get("stale"):
        data["stale"] = True
        data["note"] = "the local build changed after this upload; this link shows the older version"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    return data.get("url")

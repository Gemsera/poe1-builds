"""Tests for pobb.in sharing. All HTTP is mocked: nothing is uploaded.

Run: python3 tests/test_pobb_in.py   (the build-integration tests need scripts/setup.sh to have run)
"""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
EXAMPLE = os.path.join(HERE, "..", "examples", "rf_chieftain.json")
sys.path.insert(0, SCRIPTS)
import pob  # noqa: E402
import pobb_in  # noqa: E402

MIN_XML = '<?xml version="1.0"?><PathOfBuilding><Build level="1" className="Witch"/><Tree/></PathOfBuilding>'
CODE = pob.encode_code(MIN_XML)
OTHER_CODE = pob.encode_code(MIN_XML.replace('level="1"', 'level="2"'))


class Resp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def getcode(self):
        return self.status

    def read(self):
        return self.body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePobbIn:
    """Behaves like the pobb.in endpoints Path of Building uses: POST /pob/ stores the body under
    base64url(sha1(body)[:9]) and returns that id; GET /pob/<id> returns the stored body."""

    def __init__(self):
        self.store, self.requests = {}, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.get_method() == "POST" and req.full_url == "https://pobb.in/pob/":
            body = req.data.decode("utf-8")
            pid = base64.urlsafe_b64encode(hashlib.sha1(req.data).digest()[:9]).decode().rstrip("=")
            self.store[pid] = body
            return Resp(200, pid)
        if req.get_method() == "GET" and req.full_url.startswith("https://pobb.in/pob/"):
            pid = req.full_url.rsplit("/", 1)[1]
            if pid in self.store:
                return Resp(200, self.store[pid])
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"code":404,"message":"not found"}'))
        raise AssertionError("unexpected request %s %s" % (req.get_method(), req.full_url))


def http_error(status, body=b""):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))
    return opener


class UploadUnitTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop(pobb_in.LOCAL_ONLY_ENV, None)

    def test_success_and_verified(self):
        srv = FakePobbIn()
        r = pobb_in.upload(CODE, opener=srv)
        self.assertTrue(r["ok"])
        self.assertTrue(r["verified"])
        self.assertTrue(r["id_matches_content"])
        self.assertEqual(r["url"], "https://pobb.in/" + r["id"])
        post = srv.requests[0]
        self.assertEqual(post.data.decode(), CODE)                 # body is the bare code
        self.assertNotIn("Cookie", post.headers)
        self.assertNotIn("Authorization", post.headers)

    def test_whitespace_in_code_is_normalised(self):
        srv = FakePobbIn()
        r = pobb_in.upload(CODE[:20] + "\n" + CODE[20:] + "  \n", opener=srv)
        self.assertEqual(srv.requests[0].data.decode(), CODE)
        self.assertTrue(r["ok"])

    def test_malformed_200_response(self):
        opener = lambda req, timeout=None: Resp(200, "<html>Maintenance</html>")
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=opener)
        self.assertEqual(cm.exception.kind, "malformed")

    def test_empty_200_response(self):
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=lambda req, timeout=None: Resp(200, ""))
        self.assertEqual(cm.exception.kind, "malformed")

    def test_unexpected_status(self):
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=lambda req, timeout=None: Resp(201, "abcdefghijkl"))
        self.assertEqual(cm.exception.kind, "http")

    def test_http_error_with_json_message(self):
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=http_error(400, b'{"code":400,"message":"Paste too large, please login"}'))
        self.assertEqual(cm.exception.kind, "http")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("Paste too large", str(cm.exception))

    def test_http_500_without_body(self):
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=http_error(500))
        self.assertEqual(cm.exception.status, 500)

    def test_timeout(self):
        def opener(req, timeout=None):
            raise socket.timeout("timed out")
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=opener, timeout=2)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_timeout_wrapped_in_urlerror(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError(socket.timeout("timed out"))
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=opener)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_network_failure_proxy_block(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=opener)
        self.assertEqual(cm.exception.kind, "network")
        self.assertIn("allowlist", str(cm.exception))

    def test_invalid_inputs_make_no_request(self):
        srv = FakePobbIn()
        not_pob = base64.urlsafe_b64encode(__import__("zlib").compress(b"<html>hello</html>")).decode()
        for bad in ("", "   ", "this is not a code!", "AAAA", not_pob):
            with self.assertRaises(pobb_in.UploadError) as cm:
                pobb_in.upload(bad, opener=srv)
            self.assertEqual(cm.exception.kind, "invalid_input", bad)
        self.assertEqual(srv.requests, [])

    def test_too_large(self):
        big = pob.encode_code(MIN_XML.replace("<Tree/>", "<Tree>%s</Tree>" % os.urandom(60000).hex()))
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(big, opener=FakePobbIn())
        self.assertEqual(cm.exception.kind, "too_large")

    def test_download_differs(self):
        def opener(req, timeout=None):
            if req.get_method() == "POST":
                return Resp(200, pobb_in.expected_id(CODE))
            return Resp(200, OTHER_CODE)
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=opener)
        self.assertEqual(cm.exception.kind, "verify_failed")

    def test_unknown_id_and_no_download(self):
        def opener(req, timeout=None):
            if req.get_method() == "POST":
                return Resp(200, "zzzzzzzzzzzz")
            raise urllib.error.URLError(OSError("connection reset"))
        with self.assertRaises(pobb_in.UploadError) as cm:
            pobb_in.upload(CODE, opener=opener)
        self.assertEqual(cm.exception.kind, "verify_failed")

    def test_no_verify_requires_hash_match(self):
        r = pobb_in.upload(CODE, opener=lambda req, timeout=None: Resp(200, pobb_in.expected_id(CODE)), verify=False)
        self.assertTrue(r["ok"])
        self.assertFalse(r["verified"])
        with self.assertRaises(pobb_in.UploadError):
            pobb_in.upload(CODE, opener=lambda req, timeout=None: Resp(200, "zzzzzzzzzzzz"), verify=False)

    def test_local_only_env_blocks_upload(self):
        srv = FakePobbIn()
        with mock.patch.dict(os.environ, {pobb_in.LOCAL_ONLY_ENV: "1"}):
            with self.assertRaises(pobb_in.UploadError) as cm:
                pobb_in.upload(CODE, opener=srv)
        self.assertEqual(cm.exception.kind, "local_only")
        self.assertEqual(srv.requests, [])


class CliTests(unittest.TestCase):
    def run_upload(self, arg, *extra):
        ap_args = argparse.Namespace(build=arg, timeout=5, verify=True, json="--json" in extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pob.cmd_upload(ap_args)
        return rc, out.getvalue(), err.getvalue()

    def test_json_success_and_manifest(self):
        td = tempfile.mkdtemp()
        try:
            path = os.path.join(td, "b.pob.txt")
            with open(path, "w") as f:
                f.write(CODE + "\n")
            srv = FakePobbIn()
            with mock.patch("urllib.request.urlopen", srv):
                rc, out, _ = self.run_upload(path, "--json")
            self.assertEqual(rc, 0)
            res = json.loads(out)
            self.assertTrue(res["ok"])
            self.assertEqual(res["url"], "https://pobb.in/" + pobb_in.expected_id(CODE))
            with open(os.path.join(td, "b.pobbin.json")) as f:
                self.assertEqual(json.load(f)["code_sha1"], pobb_in.code_sha1(CODE))
        finally:
            shutil.rmtree(td)

    def test_xml_file_input(self):
        td = tempfile.mkdtemp()
        try:
            path = os.path.join(td, "b.xml")
            with open(path, "w") as f:
                f.write(MIN_XML)
            srv = FakePobbIn()
            with mock.patch("urllib.request.urlopen", srv):
                rc, out, _ = self.run_upload(path)
            self.assertEqual(rc, 0)
            self.assertEqual(pob.decode_code(srv.requests[0].data.decode()), MIN_XML)
        finally:
            shutil.rmtree(td)

    def test_json_failure_exit_code(self):
        with mock.patch("urllib.request.urlopen", http_error(503)):
            rc, out, _ = self.run_upload(CODE, "--json")
        self.assertEqual(rc, 1)
        res = json.loads(out)
        self.assertFalse(res["ok"])
        self.assertNotIn("url", res)                               # no success URL on failure

    def test_text_failure_prints_no_url(self):
        with mock.patch("urllib.request.urlopen", http_error(503)):
            rc, out, err = self.run_upload(CODE)
        self.assertEqual(rc, 1)
        self.assertNotIn("pobb.in/", out)
        self.assertIn("UPLOAD FAILED", err)

    def test_link_and_garbage_input(self):
        for bad in ("https://pobb.in/abcdefghijkl", "not a build"):
            with mock.patch("urllib.request.urlopen", FakePobbIn()):
                rc, out, err = self.run_upload(bad, "--json")
            self.assertEqual(rc, 1, bad)
            self.assertEqual(json.loads(out)["kind"], "invalid_input")


POB_READY = os.path.exists(pob.LUAJIT) and os.path.exists(pob.POB_SRC)


@unittest.skipUnless(POB_READY, "Path of Building not set up (run scripts/setup.sh)")
class BuildIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        with open(EXAMPLE) as f:
            self.spec = pob.load_json_lenient(f.read())
        self.spec["name"] = "share_test"

    def tearDown(self):
        shutil.rmtree(self.td)

    def build(self, spec, share, opener):
        path = os.path.join(self.td, "spec.json")
        with open(path, "w") as f:
            json.dump(spec, f)
        a = argparse.Namespace(spec=path, out=self.td, json=True, show_code=False, audit=False, investment=None,
                               content=None, game_mode=None, share=share, timeout=5, verify=True)
        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", opener), contextlib.redirect_stdout(out):
            rc = pob.cmd_build(a)
        return rc, json.loads(out.getvalue())

    def test_failed_upload_preserves_local_build(self):
        def down(req, timeout=None):
            raise urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))
        rc, resp = self.build(self.spec, True, down)
        self.assertEqual(rc, 3)                                     # valid build, upload failed
        self.assertTrue(resp["valid"])
        self.assertFalse(resp["share"]["ok"])
        self.assertTrue(resp["pob_code"])
        for f in resp["files"]:
            self.assertTrue(os.path.getsize(f) > 0, f)
        with open(resp["files"][1]) as f:
            self.assertEqual(f.read().strip(), resp["pob_code"])
        self.assertFalse(os.path.exists(os.path.join(self.td, "share_test.pobbin.json")))

    def test_link_is_for_final_build(self):
        srv = FakePobbIn()
        rc, first = self.build(self.spec, True, srv)
        self.assertEqual(rc, 0)
        url1 = first["share"]["url"]

        # "optimise": change the build, rebuild locally without sharing -> old link is flagged stale
        self.spec["level"] = self.spec.get("level", 90) + 1
        rc, second = self.build(self.spec, False, srv)
        self.assertEqual(second.get("stale_share_url"), url1)
        with open(os.path.join(self.td, "share_test.pobbin.json")) as f:
            self.assertTrue(json.load(f)["stale"])

        # share the final version: new link, and its stored content is exactly the final local code
        rc, final = self.build(self.spec, True, srv)
        self.assertEqual(rc, 0)
        url2, pid2 = final["share"]["url"], final["share"]["id"]
        self.assertNotEqual(url1, url2)
        with open(final["files"][1]) as f:
            final_code = f.read().strip()
        self.assertEqual(srv.store[pid2], final_code)
        self.assertEqual(pid2, pobb_in.expected_id(final_code))
        self.assertNotEqual(srv.store[first["share"]["id"]], final_code)
        with open(os.path.join(self.td, "share_test.pobbin.json")) as f:
            m = json.load(f)
        self.assertEqual(m["url"], url2)
        self.assertNotIn("stale", m)

    def test_invalid_build_is_not_uploaded(self):
        srv = FakePobbIn()
        bad = dict(self.spec, level=1)                              # far too many points for level 1
        rc, resp = self.build(bad, True, srv)
        self.assertEqual(rc, 2)
        self.assertEqual(resp["share"]["kind"], "invalid_build")
        self.assertEqual(srv.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

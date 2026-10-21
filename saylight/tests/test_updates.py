"""Tests for updates.py: version logic, throttling, URL rules, and the check, download and launch against a local
HTTPS server that plays GitHub (ReleaseServer, also used by test_update_service.py). Nothing leaves the machine
and nothing is installed: the "installer" is a few hundred KB of test bytes and the launcher is a fake."""

import hashlib
import http.server
import json
import os
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse

from .. import updates as U

# Test-only certificate and key for 127.0.0.1 and localhost (EC P-256, valid 2026-2126). They protect nothing.
TEST_CERT = """-----BEGIN CERTIFICATE-----
MIIB1TCCAXugAwIBAgIUBe32rrSWtiM5V+w+9jeRhpSHzyQwCgYIKoZIzj0EAwIw
HzEdMBsGA1UEAwwUU2F5bGlnaHQgdGVzdCBzZXJ2ZXIwIBcNMjYwMTAxMDAwMDAw
WhgPMjEyNjAxMDEwMDAwMDBaMB8xHTAbBgNVBAMMFFNheWxpZ2h0IHRlc3Qgc2Vy
dmVyMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEk3QI8s7Luh8nopKhzPTRqTA6
Z3ZRTmD1UNCKT6rTFDtyj7rTVmxUT75KqCcfIWPO8s3HDVrDUsHklMl3iJ0KraOB
kjCBjzAdBgNVHQ4EFgQU8+pha1MMb7vxA3otrb3lvPgN8DYwHwYDVR0jBBgwFoAU
8+pha1MMb7vxA3otrb3lvPgN8DYwGgYDVR0RBBMwEYIJbG9jYWxob3N0hwR/AAAB
MAwGA1UdEwEB/wQCMAAwDgYDVR0PAQH/BAQDAgeAMBMGA1UdJQQMMAoGCCsGAQUF
BwMBMAoGCCqGSM49BAMCA0gAMEUCIQDDS33Bn7iL4H5oBpUROzguv1jYVp1/jHsq
aM6EPofabwIgfH9nyfaWe/r4YJjzp82NoLYCjzFak66z+nQ4BLpx+t4=
-----END CERTIFICATE-----
"""
TEST_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg8WTUOAjZ7dyNwg95
HSJeLiXii40Vv+DIkLJ5MhJ9vyKhRANCAASTdAjyzsu6HyeikqHM9NGpMDpndlFO
YPVQ0IpPqtMUO3KPutNWbFRPvkqoJx8hY87yzccNWsNSweSUyXeInQqt
-----END PRIVATE KEY-----
"""

REPO_PATH = "/ColeCraven/saylight"
API_PATH = "/api/repos/ColeCraven/saylight/releases/latest"
ETAG = '"abc"'


class _QuietServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that timed out on purpose closes the socket under the handler


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.owner._serve(self, "GET")

    def do_HEAD(self):
        self.server.owner._serve(self, "HEAD")


class ReleaseServer:
    """A local HTTPS stand-in for GitHub on 127.0.0.1: the releases API, the releases/latest redirect, and asset
    downloads that redirect to a separate storage path, the way github.com redirects to its CDN. Every response
    sets a cookie, so tests can show that none is ever sent back."""

    def __init__(self) -> None:
        self.reset()
        self._httpd: _QuietServer | None = None

    def reset(self) -> None:
        self.tag = "v1.3.0"
        self.payload = bytes(range(256)) * 1200        # stands in for Saylight-Setup.exe (307,200 bytes)
        self.served_setup: bytes | None = None         # bytes actually served, when they should differ
        self.checksum_text: str | None = None          # None: a correct "hash  Saylight-Setup.exe" line
        self.api_mode = "ok"                            # "ok" | "403" | "404" | "garbage" | "list" | "big" | "redirect"
        self.api_location = ""                         # where the API redirects to in "redirect" mode
        self.setup_mode = "ok"                          # "ok" | "truncated" | "nolength" | "missing"
        self.release_overrides: dict = {}
        self.asset_overrides: dict = {}
        self.asset_location: str | None = None         # where asset downloads redirect to
        self.latest_location: str | None = None        # Location for HEAD releases/latest
        self.slow_s = 0.0
        self.requests: list[tuple[str, str, dict]] = []

    # --- Lifetime

    def start(self) -> None:
        folder = tempfile.mkdtemp(prefix="sl_update_cert_")
        try:
            cert, key = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
            for path, text in ((cert, TEST_CERT), (key, TEST_KEY)):
                with open(path, "w", encoding="ascii") as f:
                    f.write(text)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self._httpd = _QuietServer(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        threading.Thread(target=self._httpd.serve_forever, args=(0.05,), daemon=True).start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    # --- What clients use

    @property
    def base(self) -> str:
        return f"https://127.0.0.1:{self._httpd.server_port}"

    @property
    def repo_url(self) -> str:
        return self.base + REPO_PATH

    def site(self) -> U.Site:
        return U.Site(api_url=self.base + API_PATH, latest_page=self.repo_url + "/releases/latest",
                      repo_url=self.repo_url, asset_hosts=("127.0.0.1",), proxies={})

    @staticmethod
    def context() -> ssl.SSLContext:
        return ssl.create_default_context(cadata=TEST_CERT)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    def release_json(self) -> dict:
        download = f"{self.repo_url}/releases/download/{self.tag}/"
        setup = {"name": "Saylight-Setup.exe", "size": len(self.payload), "digest": "sha256:" + self.sha256,
                 "browser_download_url": download + "Saylight-Setup.exe", **self.asset_overrides}
        checksum = {"name": "Saylight-Setup.exe.sha256", "size": 85,
                    "browser_download_url": download + "Saylight-Setup.exe.sha256"}
        return {"tag_name": self.tag, "html_url": f"{self.repo_url}/releases/tag/{self.tag}", "draft": False,
                "prerelease": False, "assets": [setup, checksum], **self.release_overrides}

    def release(self) -> U.Release:
        result = U.check("1.2.0", site=self.site(), context=self.context())
        assert result.release is not None, result
        return result.release

    def hits(self, prefix: str, method: str = "") -> int:
        return sum(1 for m, path, _ in self.requests if path.startswith(prefix) and (not method or m == method))

    # --- Serving

    def _serve(self, h: http.server.BaseHTTPRequestHandler, method: str) -> None:
        self.requests.append((method, h.path, dict(h.headers)))
        path = urllib.parse.urlsplit(h.path).path
        if path == API_PATH and method == "GET":
            return self._api(h)
        if path == REPO_PATH + "/releases/latest" and method == "HEAD":
            return self._send(h, 302, location=self.latest_location or f"{self.repo_url}/releases/tag/{self.tag}")
        if path.startswith(REPO_PATH + "/releases/download/"):
            name = path.rsplit("/", 1)[-1]
            return self._send(h, 302, location=self.asset_location or f"{self.base}/storage/{name}")
        if path == "/storage/Saylight-Setup.exe":
            return self._setup(h)
        if path == "/storage/Saylight-Setup.exe.sha256":
            if self.checksum_text == "missing":
                return self._send(h, 404)
            text = f"{self.sha256}  Saylight-Setup.exe\n" if self.checksum_text is None else self.checksum_text
            return self._send(h, 200, text.encode())
        return self._send(h, 404)

    def _api(self, h) -> None:
        mode = self.api_mode
        if self.slow_s:
            time.sleep(self.slow_s)
        if mode in ("403", "404"):
            return self._send(h, int(mode))
        if mode == "redirect" and not h.path.endswith("?moved"):
            return self._send(h, 302, location=self.api_location)
        if h.headers.get("If-None-Match") == ETAG and mode == "ok":
            return self._send(h, 304)
        body = {"ok": json.dumps(self.release_json()), "redirect": json.dumps(self.release_json()),
                "garbage": "<html>", "list": "[1, 2]", "big": " " * (U.MAX_BODY + 10)}[mode]
        return self._send(h, 200, body.encode(), etag=ETAG)

    def _setup(self, h) -> None:
        body = self.payload if self.served_setup is None else self.served_setup
        if self.setup_mode == "missing":
            return self._send(h, 404)
        if self.setup_mode == "truncated":
            h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body[: len(body) // 2])
            return None
        return self._send(h, 200, body, length=self.setup_mode != "nolength")

    @staticmethod
    def _send(h, code: int, body: bytes = b"", *, location: str = "", etag: str = "", length: bool = True) -> None:
        h.send_response(code)
        h.send_header("Set-Cookie", "logged_in=no; Path=/; Secure; HttpOnly")
        if location:
            h.send_header("Location", location)
        if etag:
            h.send_header("ETag", etag)
        if length:
            h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if body and h.command != "HEAD":
            h.wfile.write(body)


class _Server(unittest.TestCase):
    server: ReleaseServer

    @classmethod
    def setUpClass(cls):
        cls.server = ReleaseServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        self.site = self.server.site()
        self.context = self.server.context()

    def check(self, current: str = "1.2.0", etag: str = "", **kw) -> U.CheckResult:
        return U.check(current, etag, site=self.site, context=self.context, **kw)


# --- Pure logic ---------------------------------------------------------------

class Versions(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(U.parse_version("v1.2"), (1, 2, 0, 9, 0))
        self.assertEqual(U.parse_version("1.2.0"), (1, 2, 0, 9, 0))
        self.assertEqual(U.parse_version("V1.3.0-rc.2"), (1, 3, 0, 2, 2))
        self.assertIsNone(U.parse_version("latest"))
        self.assertIsNone(U.parse_version(""))
        # Too many digits for int() is just another tag that isn't a version, not an error.
        self.assertIsNone(U.parse_version("v" + "9" * 5000))
        self.assertIsNone(U.parse_version("1.3.0-rc." + "9" * 5000))
        self.assertFalse(U.is_newer("v" + "9" * 5000, "1.2.0"))

    def test_compare(self):
        cases = [("v1.2.1", "1.2.0", True), ("v1.2.0", "1.2.0", False), ("v1.10.0", "1.9.9", True),
                 ("v1.2", "1.2.0", False), ("v1.3.0-rc.1", "1.2.0", True), ("v1.3.0-rc.1", "1.3.0", False),
                 ("v1.3.0", "1.3.0-rc.1", True), ("v1.1.9", "1.2.0", False), ("nightly", "1.2.0", False),
                 ("v2", "1.99.99", True), ("v1.3.0", "", False)]
        for latest, current, want in cases:
            self.assertEqual(U.is_newer(latest, current), want, (latest, current))

    def test_due_once_a_day(self):
        now = 1_800_000_000
        self.assertTrue(U.due(0, now))
        self.assertFalse(U.due(now - 3600, now))
        self.assertFalse(U.due(now - 86399, now))
        self.assertTrue(U.due(now - 86400, now))
        self.assertTrue(U.due(now + 999, now))  # the clock moved backwards


class UrlRules(unittest.TestCase):
    def test_only_https_on_the_listed_hosts(self):
        hosts = ("github.com", *U.ASSET_HOSTS)
        good = ["https://github.com/ColeCraven/saylight/releases/download/v1.3.0/Saylight-Setup.exe",
                "https://release-assets.githubusercontent.com/github-production-release-asset/1/2?sp=r&sig=x",
                "https://objects.githubusercontent.com/github-production-release-asset-2e65be/1/2",
                "https://GitHub.com/ColeCraven/saylight/"]
        bad = ["http://release-assets.githubusercontent.com/x",
               "https://release-assets.githubusercontent.com.evil.example/x",
               "https://evil.example/release-assets.githubusercontent.com/x",
               "https://github.com@evil.example/x", "https://user@github.com/x",
               "https://github.com:99999/x", "ftp://github.com/x", "https:///x", "", "javascript:alert(1)"]
        for url in good:
            self.assertTrue(U.url_allowed(url, hosts), url)
        for url in bad:
            self.assertFalse(U.url_allowed(url, hosts), url)

    def test_release_urls_stay_inside_the_repo(self):
        prefix = U.GITHUB.download_prefix
        self.assertTrue(U._under(prefix + "v1.3.0/Saylight-Setup.exe", prefix))
        for url in (prefix + "../../../evil/x", prefix + "v1.3.0/%2e%2e/%2E%2E/x", prefix + "a/..\\..\\x",
                    "https://github.com/ColeCraven/saylight-evil/releases/download/v1/Saylight-Setup.exe"):
            self.assertFalse(U._under(url, prefix), url)

    def test_checksum_file_formats(self):
        digest = "ab" * 32
        self.assertEqual(U.parse_checksum(digest), digest)
        self.assertEqual(U.parse_checksum(digest.upper() + "\r\n"), digest)
        self.assertEqual(U.parse_checksum(f"{digest}  Saylight-Setup.exe\n"), digest)
        self.assertEqual(U.parse_checksum(f"{digest} *Saylight-Setup.exe"), digest)  # sha256sum binary mode
        self.assertEqual(U.parse_checksum(f"{digest}  dist/Saylight-Setup.exe"), digest)
        self.assertEqual(U.parse_checksum(f"\ufeff{digest}  saylight-setup.exe"), digest)
        self.assertEqual(U.parse_checksum(f"{'cd' * 32}  Other.zip\n{digest}  Saylight-Setup.exe\n"), digest)
        self.assertEqual(U._decode(f"{digest}  Saylight-Setup.exe".encode("utf-16")), f"{digest}  Saylight-Setup.exe")
        for text in ("", "not a hash", f"{'cd' * 32}  Other.zip", "ab" * 31, "SHA256 " + digest):
            self.assertEqual(U.parse_checksum(text), "", text)


# --- The check -----------------------------------------------------------------

class Check(_Server):
    def test_newer_release_and_what_is_sent(self):
        r = self.check()
        self.assertEqual((r.status, r.release.version, r.etag), ("newer", "1.3.0", ETAG))
        self.assertEqual(r.release.url, f"{self.server.repo_url}/releases/tag/v1.3.0")
        self.assertEqual(r.release.setup_url, f"{self.server.repo_url}/releases/download/v1.3.0/Saylight-Setup.exe")
        self.assertEqual(r.release.checksum_url, r.release.setup_url + ".sha256")
        self.assertEqual((r.release.setup_size, r.release.sha256), (len(self.server.payload), self.server.sha256))
        self.check()  # the first answer set a cookie; it must not come back
        for _, _, sent in self.server.requests:
            self.assertEqual(sorted(k.lower() for k in sent),
                             ["accept", "accept-encoding", "connection", "host", "user-agent",
                              "x-github-api-version"])
            self.assertEqual(sent["User-Agent"], "Saylight/1.2.0 (+https://github.com/ColeCraven/saylight)")

    def test_current_draft_and_prerelease(self):
        self.assertEqual(self.check(current="1.3.0").status, "current")
        self.assertEqual(self.check(current="1.4.0").status, "current")
        self.server.release_overrides = {"prerelease": True}
        self.assertEqual(self.check().status, "current")
        self.server.release_overrides = {"draft": True}
        self.assertEqual(self.check().status, "current")

    def test_etag_gives_304(self):
        r = self.check(etag=ETAG)
        self.assertEqual((r.status, r.etag), ("not_modified", ETAG))
        self.assertEqual(self.server.requests[-1][2].get("If-None-Match"), ETAG)

    def test_errors_and_suspicious_answers(self):
        for mode, detail in (("404", "HTTP 404"), ("403", "HTTP 403"), ("garbage", "unexpected response"),
                             ("list", "unexpected response"), ("big", "response too large")):
            self.server.api_mode = mode
            r = self.check()
            self.assertEqual((r.status, r.detail), ("error", detail), mode)
        self.server.api_mode = "ok"
        self.server.release_overrides = {"html_url": "https://evil.example/ColeCraven/saylight/releases/tag/v1.3.0"}
        self.assertEqual(self.check().detail, "unexpected release URL")
        self.server.release_overrides = {"html_url": self.server.repo_url + "/../other/releases/tag/v9"}
        self.assertEqual(self.check().detail, "unexpected release URL")
        self.server.release_overrides = {}
        self.server.asset_overrides = {"browser_download_url": "https://evil.example/Saylight-Setup.exe"}
        r = self.check()
        self.assertEqual((r.status, r.release.setup_url, r.release.checksum_url, r.release.setup_size),
                         ("newer", "", "", 0))
        self.server.asset_overrides = {"size": "huge"}
        self.assertEqual(self.check().detail, "unexpected response")
        self.server.asset_overrides = {"size": float("inf")}  # json.dumps writes Infinity, json.loads reads it
        self.assertEqual(self.check().detail, "unexpected response")
        self.server.asset_overrides = {"digest": "md5:abc"}
        self.assertEqual(self.check().release.sha256, "")
        self.server.asset_overrides = {}
        self.server.tag = "v" + "9" * 5000
        self.assertEqual(self.check().status, "current")

    def test_api_redirects_only_to_the_expected_hosts(self):
        self.server.api_mode = "redirect"
        self.server.api_location = "https://evil.example/repos/ColeCraven/saylight/releases/latest"
        r = self.check()
        self.assertEqual((r.status, r.detail), ("error", "RedirectRefused"))
        self.server.api_location = f"http://127.0.0.1:{self.server.base.rsplit(':', 1)[1]}{API_PATH}?moved"
        self.assertEqual(self.check().detail, "RedirectRefused")  # no downgrade to plain http
        self.assertEqual(self.server.hits(API_PATH), 2)  # only the redirecting answers; nothing followed
        self.server.api_location = self.server.base + API_PATH + "?moved"  # same host, https: followed
        self.assertEqual(self.check().status, "newer")

    def test_timeout(self):
        self.server.slow_s = 1.5
        start = time.perf_counter()
        r = self.check(timeout=0.4)
        self.assertEqual(r.status, "error")
        self.assertLess(time.perf_counter() - start, 1.3)

    def test_untrusted_certificate_is_refused(self):
        r = U.check("1.2.0", site=self.site, context=ssl.create_default_context())  # system roots only
        self.assertEqual((r.status, r.detail), ("error", "URLError"))
        self.assertEqual(self.server.hits(API_PATH), 0)  # refused during the handshake

    def test_offline(self):
        class Offline:
            def open(self, *args, **kwargs):
                raise urllib.error.URLError(OSError(10051, "network unreachable"))
        self.assertEqual(U.check("1.2.0", opener=Offline()).detail, "URLError")
        self.assertEqual(U.check_redirect("1.2.0", opener=Offline()).detail, "URLError")


class Fallback(_Server):
    def test_tag_from_the_latest_redirect(self):
        r = U.check_redirect("1.2.0", site=self.site, context=self.context)
        download = f"{self.server.repo_url}/releases/download/v1.3.0/"
        self.assertEqual((r.status, r.release.version, r.release.setup_url, r.release.checksum_url),
                         ("newer", "1.3.0", download + "Saylight-Setup.exe", download + "Saylight-Setup.exe.sha256"))
        self.assertEqual(r.release.setup_size, 0)
        self.server.tag = "v1.2.0"
        self.assertEqual(U.check_redirect("1.2.0", site=self.site, context=self.context).status, "current")
        self.server.latest_location = "https://evil.example/ColeCraven/saylight/releases/tag/v9.0.0"
        self.assertEqual(U.check_redirect("1.2.0", site=self.site, context=self.context).status, "error")
        self.server.latest_location = self.server.repo_url + "/releases"  # no releases yet
        self.assertEqual(U.check_redirect("1.2.0", site=self.site, context=self.context).status, "error")
        self.assertEqual(self.server.hits(REPO_PATH, "HEAD"), 4)
        self.assertEqual(self.server.hits("/storage"), 0)  # the redirect is read, never followed

    def test_check_any_falls_back_when_rate_limited(self):
        self.server.api_mode = "403"
        r = U.check_any("1.2.0", site=self.site, context=self.context)
        self.assertEqual((r.status, r.release.version, r.release.setup_size), ("newer", "1.3.0", 0))
        self.assertEqual((self.server.hits(API_PATH), self.server.hits(REPO_PATH, "HEAD")), (1, 1))
        self.server.requests.clear()
        self.server.api_mode = "404"  # no release yet: nothing to fall back to
        self.assertEqual(U.check_any("1.2.0", site=self.site, context=self.context).detail, "HTTP 404")
        self.assertEqual(self.server.hits(REPO_PATH, "HEAD"), 0)

    def test_checker_gates(self):
        got = []
        c = U.UpdateChecker("1.2.0", got.append, site=self.site, context=self.context)
        self.assertFalse(c.maybe_check(False, True, 0, ""))           # switched off
        self.assertFalse(c.maybe_check(True, False, 0, ""))           # offline
        self.assertFalse(c.maybe_check(True, True, time.time(), ""))  # checked recently
        self.assertTrue(c.maybe_check(True, True, time.time(), "", force=True))
        end = time.monotonic() + 5
        while not got and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(got[0].status, "newer")
        self.assertEqual(self.server.hits(API_PATH), 1)


# --- Download, checks and launch -----------------------------------------------

class Download(_Server):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix="sl_update_test_")
        self.dest = os.path.join(self.folder, U.SETUP_NAME)
        self.addCleanup(shutil.rmtree, self.folder, True)

    def download(self, release: U.Release | None = None, **kw) -> int:
        release = release or self.server.release()
        args = dict(sha256=self.server.sha256, expected_size=release.setup_size, site=self.site,
                    context=self.context)
        args.update(kw)
        return U.download(release.setup_url, self.dest, **args)

    def assert_fails(self, kind: str, **kw) -> None:
        with self.assertRaises(U.DownloadError) as cm:
            self.download(**kw)
        self.assertEqual(cm.exception.kind, kind)
        self.assertEqual(os.listdir(self.folder), [])  # no installer and no .part left behind

    def test_downloads_through_the_storage_redirect_and_checks(self):
        seen = []
        self.assertEqual(self.download(progress=lambda done, total: seen.append((done, total))),
                         len(self.server.payload))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.server.payload)
        self.assertEqual(os.listdir(self.folder), [U.SETUP_NAME])
        size = len(self.server.payload)
        self.assertEqual((seen[0], seen[-1]), ((0, size), (size, size)))
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))
        self.assertEqual(self.server.hits("/storage/Saylight-Setup.exe"), 1)
        for _, _, sent in self.server.requests:
            self.assertNotIn("Cookie", sent)

    def test_checksum_mismatch_keeps_nothing(self):
        self.server.served_setup = b"\xff" + self.server.payload[1:]  # same size, one byte changed
        self.assert_fails("checksum")
        self.assert_fails("checksum", sha256="")

    def test_size_checks(self):
        release = self.server.release()
        self.assert_fails("size", expected_size=release.setup_size - 1)       # Content-Length disagrees
        self.server.setup_mode = "nolength"
        self.assert_fails("size", expected_size=release.setup_size - 1)       # stopped once it ran over
        self.assert_fails("size", expected_size=U.MAX_SETUP_BYTES + 1)
        self.server.setup_mode = "truncated"
        self.assert_fails("network")
        self.server.setup_mode = "nolength"  # unknown size (the redirect fallback): the hash still decides
        self.assertEqual(self.download(expected_size=0), release.setup_size)

    def test_redirects_only_to_the_expected_hosts(self):
        self.server.asset_location = "https://evil.example/Saylight-Setup.exe"
        self.assert_fails("network")
        self.server.asset_location = f"http://127.0.0.1:{self.server.base.rsplit(':', 1)[1]}/storage/Saylight-Setup.exe"
        self.assert_fails("network")  # no downgrade to plain http
        self.assertEqual(self.server.hits("/storage"), 0)
        with self.assertRaises(U.DownloadError) as cm:
            U.download("https://evil.example/ColeCraven/saylight/releases/download/v1/Saylight-Setup.exe",
                       self.dest, sha256=self.server.sha256, site=self.site, context=self.context)
        self.assertEqual(cm.exception.kind, "network")
        self.server.setup_mode = "missing"
        self.assert_fails("network")

    def test_installer_hash_sources(self):
        release = self.server.release()
        site, ctx = self.site, self.context
        self.assertEqual(U.installer_sha256(release, site=site, context=ctx), self.server.sha256)
        self.server.checksum_text = "missing"  # no .sha256 asset: GitHub's own digest is enough
        self.assertEqual(U.installer_sha256(release, site=site, context=ctx), self.server.sha256)
        no_digest = U.Release(release.version, release.url, release.setup_url, release.setup_size,
                              release.checksum_url, "")
        for text in ("missing", "garbage", "cd" * 32 + "  Other.zip", self.server.sha256 + " " * U.MAX_CHECKSUM_BODY):
            self.server.checksum_text = text
            with self.assertRaises(U.DownloadError) as cm:
                U.installer_sha256(no_digest, site=site, context=ctx)
            self.assertEqual(cm.exception.kind, "checksum", text)
        self.server.checksum_text = "cd" * 32  # the asset and GitHub's digest disagree
        with self.assertRaises(U.DownloadError) as cm:
            U.installer_sha256(release, site=site, context=ctx)
        self.assertEqual(cm.exception.kind, "checksum")
        self.server.checksum_text = None
        self.assertEqual(U.installer_sha256(no_digest, site=site, context=ctx), self.server.sha256)

    def test_launch(self):
        calls = []

        def spawn(cmd, **kw):
            calls.append((cmd, kw))
            if len(calls) == 1:
                raise PermissionError(5, "Access is denied")  # a job that refuses breakaway
            return object()

        U.launch_setup(r"C:\Temp\saylight_update_x\Saylight-Setup.exe", cwd=r"C:\Temp", spawn=spawn)
        self.assertEqual(len(calls), 2)
        cmd, kw = calls[1]
        self.assertEqual(cmd, [r"C:\Temp\saylight_update_x\Saylight-Setup.exe", "/SILENT", "/NORESTART"])
        self.assertEqual(kw["cwd"], r"C:\Temp")
        self.assertTrue(calls[0][1]["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(kw["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (subprocess.DEVNULL,) * 3)

        def missing(cmd, **kw):
            raise FileNotFoundError(2, "not found")
        with self.assertRaises(U.DownloadError) as cm:
            U.launch_setup("nowhere.exe", cwd=".", spawn=missing)
        self.assertEqual(cm.exception.kind, "launch")


if __name__ == "__main__":
    unittest.main()

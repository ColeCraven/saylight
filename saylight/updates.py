"""Update check: asks GitHub, at most once a day, whether a newer Saylight release exists, and fetches, checks
and starts its installer when the person clicks Install.

Privacy: one HTTPS GET to api.github.com with only a User-Agent (and the ETag GitHub returned last time, so an
unchanged answer is a cheap 304). No cookies, no account, no IDs, nothing about the person or what they read.
It can be switched off ("updates.check"), never downloads the installer by itself, and stays quiet when offline.
GitHub sees the request's IP address, like any web request; that is stated next to the setting.

Every function here blocks. UpdateService (update_service.py) runs them on worker threads.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("saylight.updates")

REPO = "ColeCraven/saylight"
REPO_URL = f"https://github.com/{REPO}"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
LATEST_PAGE = f"{REPO_URL}/releases/latest"
SETUP_NAME = "Saylight-Setup.exe"
CHECKSUM_NAME = SETUP_NAME + ".sha256"
# github.com answers an asset download with a redirect to GitHub's storage host: release-assets.githubusercontent.com
# today (seen on 2026-10-20), objects.githubusercontent.com before 2025.
ASSET_HOSTS = ("release-assets.githubusercontent.com", "objects.githubusercontent.com")

CHECK_INTERVAL_S = 24 * 3600
TIMEOUT_S = 10                         # per network operation, not for the whole download
MAX_BODY = 512 * 1024                  # a release JSON is a few KB; never read an unbounded body
MAX_CHECKSUM_BODY = 4096
MAX_SETUP_BYTES = 256 * 1024 * 1024    # the installer is about 24 MB; refuse anything absurd
CHUNK_BYTES = 64 * 1024
PROGRESS_STEP = 256 * 1024             # report download progress about every 256 KB
SETUP_ARGS = ("/SILENT", "/NORESTART")
UPDATE_DIR_PREFIX = "saylight_update_"
FAIL_KINDS = ("network", "size", "checksum", "signature", "launch")

# At most 9 digits a part: int() refuses very long digit runs, and no real version needs more.
_VERSION = re.compile(r"^\s*v?(\d{1,9})(?:\.(\d{1,9}))?(?:\.(\d{1,9}))?(?:[-.]?(a|alpha|b|beta|rc|c)\.?(\d{0,9}))?\s*$",
                      re.I)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHECKSUM_LINE = re.compile(r"^\s*([0-9A-Fa-f]{64})(?:\s+\*?(\S.*?))?\s*$")


# --- Versions and throttling -------------------------------------------------

def parse_version(text: str) -> tuple[int, int, int, int, int] | None:
    """'v1.2', '1.2.0', '1.3.0-rc.1' -> sortable tuple; a final release sorts after its pre-releases.
    None for anything else (so a strangely named tag is ignored rather than announced)."""
    m = _VERSION.match(text or "")
    if not m:
        return None
    major, minor, patch = (int(g) if g else 0 for g in m.group(1, 2, 3))
    if m.group(4):
        return (major, minor, patch, _PRE_RANK[m.group(4).lower()], int(m.group(5) or 0))
    return (major, minor, patch, 9, 0)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def due(last_check: float, now: float | None = None, interval: float = CHECK_INTERVAL_S) -> bool:
    now = time.time() if now is None else now
    return not (0 < last_check <= now) or now - last_check >= interval  # a clock moved backwards counts as due


# --- Results -----------------------------------------------------------------

@dataclass(frozen=True)
class Release:
    version: str            # tag without the leading v, e.g. "1.3.0"
    url: str                # the release page, opened in the browser only when the person clicks
    setup_url: str          # the Saylight-Setup.exe asset, if the release has one ("" otherwise)
    setup_size: int         # bytes; 0 when unknown (the redirect fallback can't see it)
    checksum_url: str = ""  # the Saylight-Setup.exe.sha256 asset ("" when the release has none)
    sha256: str = ""        # GitHub's own digest of the installer, when the API reports one


@dataclass(frozen=True)
class CheckResult:
    status: str                  # "newer", "current", "not_modified", "skipped", "error"
    release: Release | None = None
    etag: str = ""
    detail: str = ""             # HTTP status or error class only, never a response body


class DownloadError(Exception):
    """A failed install step. `kind` is one of FAIL_KINDS; `detail` is an HTTP status or error class only."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind, detail)
        self.kind = kind
        self.detail = detail


class RedirectRefused(urllib.error.URLError):
    """A redirect left https or the expected hosts."""


@dataclass(frozen=True)
class Site:
    """Where releases are published. Tests point this at a local server."""
    api_url: str = API_URL
    latest_page: str = LATEST_PAGE
    repo_url: str = REPO_URL                     # release pages and assets must sit under this
    asset_hosts: tuple[str, ...] = ASSET_HOSTS   # hosts an asset download may be redirected to
    proxies: Mapping[str, str] | None = None     # None uses the system proxy settings

    @property
    def download_prefix(self) -> str:
        return f"{self.repo_url}/releases/download/"


GITHUB = Site()


# --- URL rules and HTTP plumbing ---------------------------------------------

def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def url_allowed(url: str, hosts: Iterable[str]) -> bool:
    """True for an https URL whose host is exactly one of `hosts` (no user info, no look-alike names)."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 -- raises ValueError for a malformed port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.username is None and parts.password is None
            and (parts.hostname or "").lower() in {h.lower() for h in hosts})


def _under(url: str, prefix: str) -> bool:
    """`url` starts with `prefix` and has no dot segments that would climb out of it."""
    if not url.startswith(prefix):
        return False
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).replace("\\", "/")
    return not any(part in (".", "..") for part in path.split("/"))


def _user_agent(version: str) -> str:
    return f"Saylight/{version} (+https://github.com/{REPO})" if version else f"Saylight (+https://github.com/{REPO})"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # read the Location header ourselves instead of following it


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to https on one of the given hosts."""

    max_redirections = 5

    def __init__(self, hosts: Iterable[str]) -> None:
        super().__init__()
        self.hosts = tuple(hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl, self.hosts):
            with contextlib.suppress(Exception):
                fp.close()
            raise RedirectRefused("redirect to an unexpected host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(site: Site, context: ssl.SSLContext | None, redirect: urllib.request.BaseHandler):
    proxy = urllib.request.ProxyHandler() if site.proxies is None else urllib.request.ProxyHandler(dict(site.proxies))
    https = urllib.request.HTTPSHandler(context=context or ssl.create_default_context())
    return urllib.request.build_opener(proxy, https, redirect)  # no cookie jar: cookies GitHub sets are dropped


# --- The check ---------------------------------------------------------------

def check(current_version: str, etag: str = "", *, url: str | None = None, timeout: float = TIMEOUT_S,
          opener=None, site: Site = GITHUB, context: ssl.SSLContext | None = None) -> CheckResult:
    """One blocking request to the releases API. Call it off the GUI thread (see UpdateChecker)."""
    headers = {"User-Agent": _user_agent(current_version),
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url or site.api_url, headers=headers, method="GET")
    open_ = opener or _opener(site, context, _SafeRedirect([_host(site.api_url), _host(site.repo_url)]))
    try:
        with open_.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY + 1)
            new_etag = resp.headers.get("ETag", "")
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 304:
            return CheckResult("not_modified", etag=etag, detail="304")
        return CheckResult("error", detail=f"HTTP {e.code}")  # 404: no release yet; 403/429: rate limited
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return CheckResult("error", detail=type(e).__name__)
    if len(body) > MAX_BODY:
        return CheckResult("error", detail="response too large")
    try:
        data = json.loads(body)
        tag = str(data["tag_name"])
        page = str(data.get("html_url") or site.latest_page)
        skip = bool(data.get("draft") or data.get("prerelease"))
        assets = {str(a.get("name", "")).lower(): a for a in data.get("assets") or [] if isinstance(a, dict)}
        setup = assets.get(SETUP_NAME.lower(), {})
        checksum = assets.get(CHECKSUM_NAME.lower(), {})
        setup_size = max(0, int(setup.get("size") or 0))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):  # OverflowError: a size of Infinity
        return CheckResult("error", etag=new_etag, detail="unexpected response")
    if skip:
        return CheckResult("current", etag=new_etag)
    if not _under(page, site.repo_url + "/"):
        return CheckResult("error", detail="unexpected release URL")
    setup_url = _asset_url(setup, site)
    checksum_url = _asset_url(checksum, site) if setup_url else ""
    digest = str(setup.get("digest") or "").lower()
    sha256 = digest[7:] if digest.startswith("sha256:") and _SHA256.fullmatch(digest[7:]) else ""
    release = Release(tag.lstrip("vV"), page, setup_url, setup_size if setup_url else 0, checksum_url,
                      sha256 if setup_url else "")
    return CheckResult("newer" if is_newer(tag, current_version) else "current", release, new_etag)


def _asset_url(asset: Mapping[str, Any], site: Site) -> str:
    url = str(asset.get("browser_download_url") or "")
    return url if _under(url, site.download_prefix) else ""


def check_redirect(current_version: str, *, url: str | None = None, timeout: float = TIMEOUT_S, opener=None,
                   site: Site = GITHUB, context: ssl.SSLContext | None = None) -> CheckResult:
    """The same answer without the API: github.com/<repo>/releases/latest answers 302 with the newest release's
    tag in Location. Not subject to the API's 60-requests-an-hour-per-IP limit, which a shared campus or office
    address can use up. Sends one HEAD request; cookies GitHub sets are discarded (no cookie jar)."""
    req = urllib.request.Request(url or site.latest_page, method="HEAD",
                                 headers={"User-Agent": _user_agent(current_version)})
    open_ = opener or _opener(site, context, _NoRedirect())
    try:
        with open_.open(req, timeout=timeout) as resp:
            location, code = resp.headers.get("Location", ""), resp.status
    except urllib.error.HTTPError as e:
        location, code = e.headers.get("Location", ""), e.code
        e.close()
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return CheckResult("error", detail=type(e).__name__)
    m = re.match(rf"^{re.escape(site.repo_url)}/releases/tag/([^/?#]+)$", location or "")
    if code not in (301, 302, 303, 307, 308) or not m:
        return CheckResult("error", detail=f"HTTP {code}")
    tag = urllib.parse.unquote(m.group(1))
    base = f"{site.download_prefix}{m.group(1)}/"
    setup_url = base + SETUP_NAME if _under(base + SETUP_NAME, site.download_prefix) else ""
    release = Release(tag.lstrip("vV"), location, setup_url, 0, base + CHECKSUM_NAME if setup_url else "")
    return CheckResult("newer" if is_newer(tag, current_version) else "current", release)


def check_any(current_version: str, etag: str = "", *, site: Site = GITHUB, context: ssl.SSLContext | None = None,
              timeout: float = TIMEOUT_S) -> CheckResult:
    """The API first (it also says whether the release has an installer, its size and digest), then the redirect
    when the API is rate limited or unreachable."""
    result = check(current_version, etag, site=site, context=context, timeout=timeout)
    if result.status == "error" and result.detail not in ("unexpected release URL", "HTTP 404"):
        fallback = check_redirect(current_version, site=site, context=context, timeout=timeout)
        if fallback.status != "error":
            return fallback
    return result


class UpdateChecker:
    """Runs check_any() on a daemon thread when it is due; hands the result to `on_result` (marshal it to the GUI
    thread yourself, e.g. with a queued Qt signal). The caller persists last_check/etag/skipped version."""

    def __init__(self, current_version: str, on_result: Callable[[CheckResult], Any], *, site: Site = GITHUB,
                 context: ssl.SSLContext | None = None) -> None:
        self.current = current_version
        self.on_result = on_result
        self.site = site
        self.context = context
        self._busy = threading.Lock()

    @property
    def running(self) -> bool:
        return self._busy.locked()

    def maybe_check(self, enabled: bool, online: bool, last_check: float, etag: str, *, force: bool = False) -> bool:
        if not enabled or not online or not (force or due(last_check)):
            return False
        if not self._busy.acquire(blocking=False):
            return False
        site, context = self.site, self.context

        def work():
            try:
                result = check_any(self.current, etag, site=site, context=context)
            except Exception as e:  # never leave the lock held
                result = CheckResult("error", detail=type(e).__name__)
            finally:
                # Released before the result is handed over, so a caller that sees running == True knows a
                # result is still to come.
                self._busy.release()
            log.info("Update check: %s %s", result.status, result.detail)
            self.on_result(result)
        threading.Thread(target=work, name="update-check", daemon=True).start()
        return True


# --- Download, checks and launch ---------------------------------------------

def parse_checksum(text: str, name: str = SETUP_NAME) -> str:
    """The SHA-256 in a .sha256 file: a bare hash, or "hash  name" lines (sha256sum style) where the name is the
    installer. Lower-case hex, or "" when the file holds no hash for it."""
    for line in (text or "").splitlines():
        m = _CHECKSUM_LINE.match(line.lstrip("\ufeff"))
        if not m:
            continue
        listed = (m.group(2) or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not listed or listed.lower() == name.lower():
            return m.group(1).lower()
    return ""


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # Windows PowerShell 5.1 writes UTF-16 with ">"
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8-sig", "replace")


def fetch_checksum(url: str, *, site: Site = GITHUB, context: ssl.SSLContext | None = None,
                   timeout: float = TIMEOUT_S, version: str = "") -> str:
    """The installer's SHA-256 from the release's .sha256 asset. Raises DownloadError("checksum" | "network")."""
    if not _under(url, site.download_prefix):
        raise DownloadError("checksum", "unexpected URL")
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(version)})
    opener = _opener(site, context, _SafeRedirect([_host(site.repo_url), *site.asset_hosts]))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_CHECKSUM_BODY + 1)
    except urllib.error.HTTPError as e:
        e.close()
        raise DownloadError("checksum" if e.code in (404, 410) else "network", f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        raise DownloadError("network", type(e).__name__) from None
    if len(raw) > MAX_CHECKSUM_BODY:
        raise DownloadError("checksum", "checksum file too large")
    digest = parse_checksum(_decode(raw))
    if not digest:
        raise DownloadError("checksum", "no checksum in file")
    return digest


def installer_sha256(release: Release, *, site: Site = GITHUB, context: ssl.SSLContext | None = None,
                     timeout: float = TIMEOUT_S, version: str = "") -> str:
    """The hash the installer must match: the published .sha256 asset, cross-checked against GitHub's own digest
    when the API gave one. Either source alone is enough; when both exist they must agree."""
    published = ""
    if release.checksum_url:
        try:
            published = fetch_checksum(release.checksum_url, site=site, context=context, timeout=timeout,
                                       version=version)
        except DownloadError:
            if not release.sha256:
                raise
    if published and release.sha256 and published != release.sha256:
        raise DownloadError("checksum", "published checksums differ")
    if not (published or release.sha256):
        raise DownloadError("checksum", "no checksum")
    return published or release.sha256


def download(url: str, dest: str, *, sha256: str, expected_size: int = 0, site: Site = GITHUB,
             context: ssl.SSLContext | None = None, timeout: float = TIMEOUT_S, version: str = "",
             progress: Callable[[int, int], Any] | None = None) -> int:
    """Stream `url` into `dest`, checking size and SHA-256 on the way. Returns the byte count.

    Bytes go to dest + ".part", which becomes `dest` only once every check passes; after a failure no file is
    left. Raises DownloadError("network" | "size" | "checksum")."""
    if not _under(url, site.download_prefix):
        raise DownloadError("network", "unexpected URL")
    if not _SHA256.fullmatch(sha256 or ""):
        raise DownloadError("checksum", "no checksum")
    if expected_size > MAX_SETUP_BYTES:
        raise DownloadError("size", "too large")
    limit = expected_size or MAX_SETUP_BYTES
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(version)})
    opener = _opener(site, context, _SafeRedirect([_host(site.repo_url), *site.asset_hosts]))
    digest = hashlib.sha256()
    done = 0
    try:
        with opener.open(req, timeout=timeout) as resp, open(part, "xb") as out:
            length = _content_length(resp)
            if length > limit or (expected_size and length and length != expected_size):
                raise DownloadError("size", "length differs")
            total = expected_size or length
            if progress:
                progress(0, total)
            reported = 0
            while chunk := resp.read(CHUNK_BYTES):
                done += len(chunk)
                if done > limit:
                    raise DownloadError("size", "too large")
                digest.update(chunk)
                out.write(chunk)
                if progress and done - reported >= PROGRESS_STEP:
                    progress(done, total)
                    reported = done
            if length and done != length:
                raise DownloadError("network", "incomplete")  # the connection closed early
            if progress and reported != done:
                progress(done, total)
        if expected_size and done != expected_size:
            raise DownloadError("size", "size differs")
        if digest.hexdigest() != sha256:
            raise DownloadError("checksum", "checksum differs")
        os.replace(part, dest)
    except DownloadError:
        _remove(part)
        raise
    except urllib.error.HTTPError as e:
        e.close()
        _remove(part)
        raise DownloadError("network", f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        _remove(part)
        raise DownloadError("network", type(e).__name__) from None
    log.info("Update installer downloaded and checked (%d bytes)", done)
    return done


def _content_length(resp) -> int:
    try:
        return max(0, int(resp.headers.get("Content-Length") or 0))
    except ValueError:
        return 0


def _remove(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def setup_command(path: str) -> list[str]:
    return [path, *SETUP_ARGS]


def launch_setup(path: str, *, cwd: str, spawn: Callable[..., Any] = subprocess.Popen) -> None:
    """Start the installer and return without waiting. Raises DownloadError("launch").

    `cwd` should be a short folder such as %TEMP%: Inno Setup fails to start from paths over 260 characters.
    Setup closes Saylight through its quit event and starts it again when it finishes."""
    options = dict(cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   close_fds=True)
    group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)  # outlive a job Saylight may run in
    try:
        try:
            spawn(setup_command(path), creationflags=group | breakaway, **options)
        except PermissionError:
            spawn(setup_command(path), creationflags=group, **options)  # that job doesn't allow breakaway
    except (OSError, ValueError) as e:
        raise DownloadError("launch", type(e).__name__) from None

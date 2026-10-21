"""One Saylight at a time: a second copy started with a file hands the path to the running one, then exits.

Windows' "Open with" and `Saylight.exe <path>` start a second copy. It connects to the running copy's local
server (a named pipe only this user can open), sends the path and exits; the running copy opens the file. The
server checks the path before anything else sees it: an existing file of a kind Saylight reads becomes
openRequested, anything else openRejected ("unsupported" or "missing"). Paths are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger("saylight.single_instance")

SERVER_BASE = "Saylight.Open"   # a test copy gets its own name (packaging/identity.iss, OpenPipe)
SUPPORTED = (".pdf", ".docx", ".txt", ".md", ".markdown")   # the file kinds documents.loader reads
MAX_MESSAGE = 64 * 1024         # bytes: one path, with room to spare
ACK = b"1\n"


def server_name(base: str = SERVER_BASE) -> str:
    """The server's name for this user: pipe names are shared by every session on the PC."""
    who = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lower()
    return f"{base}.{hashlib.sha256(who.encode('utf-8')).hexdigest()[:16]}"


def check_path(path: str) -> str:
    """"" for a file Saylight can open; else "missing" or "unsupported"."""
    if not path or not os.path.isabs(path):
        return "missing"
    if os.path.isdir(path):
        return "unsupported"
    if not os.path.isfile(path):
        return "missing"
    return "" if os.path.splitext(path)[1].lower() in SUPPORTED else "unsupported"


class OpenServer(QObject):
    """Listens for paths from a second copy (GUI thread)."""

    openRequested = Signal(str)        # an absolute path to an existing file Saylight reads
    openRejected = Signal(str, str)    # kind ("unsupported" or "missing"), the path (for the notice's title only)

    def __init__(self, name: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.name = name or server_name()
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_connection)
        self._buffers: dict[QLocalSocket, bytearray] = {}

    def listen(self) -> bool:
        if self._server.isListening():
            return True
        if not self._server.listen(self.name):
            QLocalServer.removeServer(self.name)  # a name a crashed copy left behind
            if not self._server.listen(self.name):
                log.warning("Couldn't listen for files from Open with (%s)", self._server.errorString())
                return False
        return True

    def close(self) -> None:
        self._server.close()

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._buffers.pop(s, None))
            sock.disconnected.connect(sock.deleteLater)

    def _on_ready(self, sock: QLocalSocket) -> None:
        buf = self._buffers.get(sock)
        if buf is None:
            return
        buf += bytes(sock.readAll().data())
        if b"\n" not in buf and len(buf) <= MAX_MESSAGE:
            return
        line = bytes(buf.split(b"\n", 1)[0])
        self._buffers.pop(sock, None)
        sock.write(ACK)
        sock.flush()
        sock.disconnectFromServer()
        try:
            path = os.path.normpath(line.decode("utf-8").strip()) if len(line) <= MAX_MESSAGE else ""
        except UnicodeDecodeError:
            path = ""
        kind = check_path(path)
        if kind:
            log.info("Open rejected: %s", kind)
            self.openRejected.emit(kind, path)
        else:
            self.openRequested.emit(path)


def send_open(path: str, *, name: str | None = None, timeout_ms: int = 2000) -> bool:
    """Hand `path` to the running copy; True once it has it. Retries while that copy is still starting."""
    _app = QCoreApplication.instance() or QCoreApplication([])  # the sockets need one; a second copy has none
    deadline = time.monotonic() + timeout_ms / 1000
    sock = QLocalSocket()
    while True:
        sock.connectToServer(name or server_name())
        if sock.waitForConnected(max(1, int((deadline - time.monotonic()) * 1000))):
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    sock.write(os.path.abspath(path).encode("utf-8") + b"\n")
    sock.flush()
    reply = b""
    while ACK not in reply and time.monotonic() < deadline:  # the running copy says it has the path
        if sock.waitForReadyRead(max(1, int((deadline - time.monotonic()) * 1000))):
            reply += bytes(sock.readAll().data())
        elif sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
            reply += bytes(sock.readAll().data())
            break
    sock.disconnectFromServer()
    return ACK in reply

"""Boot/teardown helper for the sanity net's scratch server.

Not part of the app; a harness utility. Boots this checkout's
run_server.py against a scratch DB copy, waits for it to answer, and
gives a clean shutdown. Never touches the repo-root testboard.db.
"""
import http.client
import os
import subprocess
import sys
import time
from typing import IO, Optional

#: The checkout this file lives in (tools/dev/net/ -> root).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
PORT = 8931
HOST = "127.0.0.1"


class NetServer(object):
    def __init__(self, db_path: str, log_path: str, port: int = PORT,
                 workers: int = 8) -> None:
        self.db_path = db_path
        self.log_path = log_path
        self.port = port
        self.workers = workers
        self.proc = None  # type: Optional[subprocess.Popen]
        self.log_fh = None  # type: Optional[IO[str]]

    def start(self) -> None:
        self.log_fh = open(self.log_path, "w", encoding="utf-8")
        cmd = [
            sys.executable, os.path.join(REPO_ROOT, "run_server.py"),
            "--host", HOST, "--port", str(self.port),
            "--db", self.db_path, "--workers", str(self.workers),
        ]
        self.proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, stdout=self.log_fh, stderr=subprocess.STDOUT,
        )
        self._wait_ready(timeout=60)

    def _wait_ready(self, timeout: float) -> None:
        assert self.proc is not None
        deadline = time.time() + timeout
        last_err = None  # type: Optional[Exception]
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "server process exited early (code %s); see %s"
                    % (self.proc.returncode, self.log_path))
            try:
                conn = http.client.HTTPConnection(HOST, self.port, timeout=2)
                conn.request("GET", "/api/environments")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status in (200, 404):
                    return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(0.3)
        raise RuntimeError(
            "server did not become ready within %ss (last error: %s); see %s"
            % (timeout, last_err, self.log_path))

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        if self.log_fh:
            self.log_fh.close()

"""qBit WebUI client — login + list torrents. Stateless, host-loopback.

Reads creds from ~/secrets/qbittorrent.{user,password,port}.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


class QbitClient:
    def __init__(self, secrets_dir: Optional[Path] = None):
        self.secrets = secrets_dir or Path(
            os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets"))
        )
        port = _read(self.secrets / "qbittorrent.port")
        self.host = f"http://127.0.0.1:{port}" if port else ""
        self._sid: str = ""

    def login(self) -> bool:
        if not self.host:
            return False
        user = _read(self.secrets / "qbittorrent.user") or "quadstronaut"
        password = _read(self.secrets / "qbittorrent.password")
        data = urllib.parse.urlencode({"username": user, "password": password}).encode()
        req = urllib.request.Request(
            f"{self.host}/api/v2/auth/login",
            data=data,
            headers={"Referer": self.host},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode().strip()
                cookie = resp.headers.get("Set-Cookie", "") or ""
        except urllib.error.URLError:
            return False
        if body != "Ok.":
            return False
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("SID="):
                self._sid = part.split("=", 1)[1]
                return True
        return False

    def list_torrents(self) -> list[dict]:
        if not self._sid:
            return []
        req = urllib.request.Request(
            f"{self.host}/api/v2/torrents/info",
            headers={"Cookie": f"SID={self._sid}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode() or "[]")
        except (urllib.error.URLError, json.JSONDecodeError):
            return []

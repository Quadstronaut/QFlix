"""Stateless *arr API client. Reads creds from ~/secrets/<slug>.{key,port,urlbase}.

Returns (status_code, parsed_body) tuples. Body is a dict on JSON 2xx, an str
on non-JSON, or {"error": "..."} on transport failure with status==0.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        return ""


class ArrClient:
    def __init__(self, slug: str, version: str, *,
                 secrets_dir: Optional[Path] = None,
                 timeout: Optional[int] = None):
        self.slug = slug
        self.version = version
        self.secrets = secrets_dir or Path(
            os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets"))
        )
        self.api_key = _read(self.secrets / f"{slug}.key")
        self.port = _read(self.secrets / f"{slug}.port")
        self.urlbase = _read(self.secrets / f"{slug}.urlbase") or slug
        self._default_timeout = timeout

    def _url(self, path: str, query: str = "") -> str:
        qs = f"?{query}" if query else ""
        return f"http://127.0.0.1:{self.port}/{self.urlbase}/api/{self.version}{path}{qs}"

    def _req(self, method: str, path: str, *, query: str = "",
             body: Optional[dict] = None, timeout: int = 20):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(path, query), data=data,
                                      method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode(errors="ignore")
                code = resp.status
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="ignore")[:600]
        except Exception as e:
            return 0, {"error": str(e)[:300]}
        try:
            return code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return code, raw

    def get(self, path: str, *, query: str = "", timeout: Optional[int] = None):
        return self._req("GET", path, query=query,
                          timeout=timeout if timeout is not None
                          else (self._default_timeout if self._default_timeout is not None else 20))

    def post(self, path: str, *, body: Optional[dict] = None,
             query: str = "", timeout: Optional[int] = None):
        return self._req("POST", path, body=body, query=query,
                          timeout=timeout if timeout is not None
                          else (self._default_timeout if self._default_timeout is not None else 30))

    def put(self, path: str, *, body: Optional[dict] = None,
            query: str = "", timeout: Optional[int] = None):
        return self._req("PUT", path, body=body, query=query,
                          timeout=timeout if timeout is not None
                          else (self._default_timeout if self._default_timeout is not None else 30))

    def delete(self, path: str, *, query: str = "", timeout: Optional[int] = None):
        return self._req("DELETE", path, query=query,
                          timeout=timeout if timeout is not None
                          else (self._default_timeout if self._default_timeout is not None else 30))

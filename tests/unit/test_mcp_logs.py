"""Tests for scripts/mcp/logs.py — log routing logic."""
from __future__ import annotations
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import logs  # noqa: E402


def test_route_for_ucc_app():
    plan = logs.route("sonarr")
    assert plan["kind"] == "file"
    assert ".apps" in plan["path"] and "sonarr" in plan["path"] and "sonarr.txt" in plan["path"]


def test_route_for_systemd_app():
    plan = logs.route("listmonk")
    assert plan["kind"] == "journalctl"
    assert plan["unit"] == "listmonk.service"


def test_route_for_nginx():
    plan = logs.route("nginx")
    assert plan["kind"] == "file"
    assert "logs" in plan["path"] and "error.log" in plan["path"]


def test_parse_line_iso():
    line = "2026-05-12T07:00:13Z [Info] Sonarr.Some.Class - Doing thing"
    parsed = logs.parse_line(line, source="sonarr.txt")
    assert parsed["ts"].startswith("2026-05-12")
    assert parsed["level"] in ("Info", "info")
    assert "Doing thing" in parsed["message"]


def test_parse_line_unknown_format():
    parsed = logs.parse_line("garbage", source="x")
    assert parsed["level"] == "unknown"
    assert parsed["message"] == "garbage"

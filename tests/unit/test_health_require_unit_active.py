"""health.require_unit_active + pending-swap + pusher parity wiring (QFLX-20)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lib import health, manifest
from lib.health import HealthResult
from lib.manifest import App, HealthConfig, Manifest


def _app(rua=True, **top):
    raw = {"class": "systemd", "unit": "qflix-sonarr.service", "ucc_slug": "sonarr"}
    raw.update(top)
    h = {"port_secret": "sonarr.port"}
    if rua is not None:
        h["require_unit_active"] = rua
    return App(name="sonarr", class_="systemd", kuma_monitor="Sonarr",
               health=HealthConfig(kind="port_listen", raw=h), defaults={}, raw=raw)


def _patch(probe_ok=True, unit_state="active", rc=None):
    probe = lambda app, t: HealthResult(ok=probe_ok, latency_ms=3, reason="ok" if probe_ok else "boom")
    cp = MagicMock(returncode=0 if unit_state == "active" else 3, stdout=unit_state + "\n")
    return (patch.dict(health._PROBES, {"port_listen": probe}),
            patch("lib.health.subprocess.run", return_value=cp))


def test_revived_container_answering_200_with_dead_unit_is_red():
    p1, p2 = _patch(probe_ok=True, unit_state="inactive")
    with p1, p2:
        r = health.probe(_app())
    assert r.ok is False
    assert "qflix-sonarr.service not active" in r.reason and "another runtime" in r.reason


def test_active_unit_keeps_the_probe_green():
    p1, p2 = _patch(probe_ok=True, unit_state="active")
    with p1, p2:
        assert health.probe(_app()).ok is True


def test_flag_off_or_absent_ignores_the_unit():
    for rua in (False, None):
        p1, p2 = _patch(probe_ok=True, unit_state="inactive")
        with p1, p2 as run:
            assert health.probe(_app(rua=rua)).ok is True
            run.assert_not_called()


def test_pending_swap_skips_the_check_ucc_still_serves():
    p1, p2 = _patch(probe_ok=True, unit_state="inactive")
    with p1, p2 as run:
        assert health.probe(_app(swap_state="pending-swap")).ok is True
        run.assert_not_called()


def test_a_red_probe_stays_red_with_its_own_reason():
    p1, p2 = _patch(probe_ok=False, unit_state="active")
    with p1, p2:
        r = health.probe(_app())
    assert (r.ok, r.reason) == (False, "boom")


# --- manifest validation ------------------------------------------------------

def _load(tmp_path, body):
    p = tmp_path / "apps.yaml"
    p.write_text("apps:\n" + body)
    return manifest.load(p)


def test_manifest_accepts_pending_swap_and_flag(tmp_path):
    m = _load(tmp_path, "  a:\n    class: systemd\n    unit: u.service\n    swap_state: pending-swap\n"
                        "    health: {kind: systemd_only, require_unit_active: true}\n")
    assert m.app("a").raw["swap_state"] == "pending-swap"


@pytest.mark.parametrize("body", [
    "  a:\n    class: systemd\n    unit: u.service\n    swap_state: swapped\n",
    "  a:\n    class: systemd\n    unit: u.service\n    health: {kind: systemd_only, require_unit_active: yes please}\n",
    "  a:\n    class: systemd\n    health: {kind: systemd_only, require_unit_active: true}\n",
])
def test_manifest_rejects_bad_swap_state_flag_or_missing_unit(tmp_path, body):
    with pytest.raises(manifest.ManifestError):
        _load(tmp_path, body)


# --- pusher wiring ------------------------------------------------------------

def _push(parity, probe_ok=True, strikes=1):
    from lib import pusher
    pusher.reset_strike_counter()
    app = _app()
    m = Manifest({app.name: app})
    resp = MagicMock(status_code=200)
    with patch("lib.pusher.health_mod.probe",
               return_value=HealthResult(ok=probe_ok, latency_ms=2, reason="ok" if probe_ok else "down")), \
         patch("lib.pusher.parity_mod.check", return_value=parity), \
         patch("lib.pusher.recovery_mod.trigger_async") as trig, \
         patch("lib.pusher.suppression_mod.in_maintenance_window", return_value=False), \
         patch("lib.pusher.suppression_mod.push_suppressed", return_value=None), \
         patch("lib.pusher.suppression_mod.in_pause_window", return_value=False), \
         patch("lib.pusher._STRIKE_THRESHOLD", strikes), \
         patch("lib.pusher.requests.get", return_value=resp) as get:
        pusher.push_once(manifest=m, tokens={"sonarr": "tok"})
    params = get.call_args.kwargs["params"]
    return params, trig


def test_pusher_pushes_down_on_parity_violation_even_when_probe_is_green():
    params, _ = _push(["dormant container woken: sonarr pid(s) 7"])
    assert params["status"] == "down"
    assert params["msg"].startswith("runtime-parity: dormant container woken")


def test_pusher_never_auto_restarts_for_a_parity_violation():
    params, trig = _push(["2 process trees match ExecStart /x"], strikes=1)
    trig.assert_not_called()
    assert "operator needed" in params["msg"]


def test_pusher_still_auto_heals_ordinary_failures():
    _, trig = _push([], probe_ok=False, strikes=1)
    trig.assert_called_once()


def test_pusher_is_green_when_parity_holds():
    params, _ = _push([])
    assert params["status"] == "up"

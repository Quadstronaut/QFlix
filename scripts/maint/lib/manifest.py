"""lib/manifest.py — load and validate manifest/apps.yaml.

Pure stdlib + pyyaml. No SSH, no network, no secrets resolution.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import yaml

VALID_CLASSES = {"ucc", "systemd", "cron", "library"}

VALID_CANARY_SCHEDULES = {
    "hourly", "daily-0430", "daily", "every-15min", "every-30min", "every-10min",
    # weekly-mon-send: fires 3x around the Monday 15:00 UTC newsletter send
    # (14:20/14:50/15:20 UTC — see manitoba-maint-canary-newsletter-digest.timer).
    # The schedule NAME is weekly for staleness-map purposes (cli.py
    # _CANARY_INTERVAL_MIN); the timer itself has 3 OnCalendar lines, not 1.
    "weekly-mon-send",
}


class ManifestError(Exception):
    """Raised when apps.yaml fails validation."""


# ---------------------------------------------------------------------------
# Nested data holders
# ---------------------------------------------------------------------------

@dataclass
class HealthConfig:
    kind: str
    raw: dict = field(repr=False)


@dataclass
class VersionPin:
    source: Optional[str] = None
    key: Optional[str] = None
    max: Optional[str] = None
    max_reason: Optional[str] = None

    @property
    def max_version(self) -> Optional[str]:
        return self.max


@dataclass
class Canary:
    name: str
    kuma_monitor: str
    script: str
    schedule: str


@dataclass
class PauseWindow:
    """An app's intentional daily downtime, expressed in UTC hours.

    Used by the pusher to avoid treating a deliberately-stopped unit as a
    fault. NOTE: NO app declares one as of 2026-08-20 — tdarr-node was the only
    holder and went 24/7 (fair-use is its `throttle` now). The mechanism is
    kept, tested and load-bearing for the next app that needs it; do not read
    the examples below as describing live config. The window is
    [start_hour_utc, end_hour_utc) — start inclusive, end exclusive — so it
    lines up exactly with the systemd OnCalendar pause/resume timers and the
    heartbeat's `HOUR_UTC >= start && < end` guard.
    """
    start_hour_utc: int
    end_hour_utc: int

    def contains(self, hour_utc: int) -> bool:
        s, e = self.start_hour_utc, self.end_hour_utc
        if s == e:
            return False  # zero-width window — never paused
        if s < e:
            return s <= hour_utc < e
        # wrap-around window that spans midnight (e.g. 22..6)
        return hour_utc >= s or hour_utc < e


@dataclass
class Throttle:
    """An app's concurrency cap — how many jobs it may run at once.

    Added 2026-08-20 when tdarr-node's fair-use policy moved off the clock
    (see PauseWindow, retired for that app) and onto the worker cap. On a
    SHARED seedbox slot the cap is the whole social contract, so it gets a
    validated manifest field rather than a literal restated per surface: it
    already has to agree with 50b-tdarr-config.py's NODE_WORKER_LIMITS and
    with whatever the live node reports, and the 2026-08-07 drift (global set
    to 1/1, node still running four workers) is what those two disagreeing
    looks like.

    transcode/health_check are counted SEPARATELY because Tdarr runs them
    concurrently — a 2/1 cap is three simultaneous jobs, not two.
    """
    transcode: int
    health_check: int

    @property
    def total(self) -> int:
        return self.transcode + self.health_check


@dataclass
class UpgradeConfig:
    kind: str
    version_pin: Optional[VersionPin] = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def max_version(self) -> Optional[str]:
        if self.version_pin is not None:
            return self.version_pin.max_version
        return None


# ---------------------------------------------------------------------------
# App record
# ---------------------------------------------------------------------------

@dataclass
class App:
    name: str
    class_: str
    kuma_monitor: Optional[str]
    health: HealthConfig
    defaults: dict
    upgrade: Optional[UpgradeConfig] = None
    parked: bool = False
    pause_window: Optional["PauseWindow"] = None
    throttle: Optional["Throttle"] = None
    raw: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Manifest container
# ---------------------------------------------------------------------------

class Manifest:
    def __init__(
        self,
        apps: dict[str, App],
        canaries: dict[str, Canary] | None = None,
        external_monitors: list[str] | None = None,
    ) -> None:
        self._apps = apps
        self._canaries: dict[str, Canary] = canaries or {}
        self._external_monitors: set[str] = set(external_monitors or [])

    def external_monitors(self) -> set[str]:
        """Kuma monitor names that exist outside this manifest's scope —
        e.g., monitors owned by a separate project living in the same Kuma
        instance. The audit uses this to suppress kuma_only false-positives."""
        return set(self._external_monitors)

    def app(self, name: str) -> App:
        try:
            return self._apps[name]
        except KeyError:
            raise KeyError(f"No app named '{name}' in manifest")

    def apps(self) -> Iterator[App]:
        return iter(self._apps.values())

    def canary(self, name: str) -> Canary:
        try:
            return self._canaries[name]
        except KeyError:
            raise KeyError(f"No canary named '{name}' in manifest")

    def canaries(self) -> Iterator[Canary]:
        return iter(self._canaries.values())

    def resolve_kuma_monitor(self, monitor_name: str) -> Optional[str]:
        for name, app in self._apps.items():
            if app.kuma_monitor == monitor_name:
                return name
        return None

    def resolve_canary_monitor(self, monitor_name: str) -> Optional[str]:
        """Like resolve_kuma_monitor but for canaries. Returns the canary
        name (e.g. 'movie' for 'Canary Movie'), or None if no match."""
        for name, canary in self._canaries.items():
            if canary.kuma_monitor == monitor_name:
                return name
        return None

    def all_kuma_monitor_names(self) -> set[str]:
        """Return all kuma_monitor names from both apps and canaries."""
        names: set[str] = set()
        for app in self._apps.values():
            if app.kuma_monitor:
                names.add(app.kuma_monitor)
        for c in self._canaries.values():
            names.add(c.kuma_monitor)
        return names


# ---------------------------------------------------------------------------
# Loader + validator
# ---------------------------------------------------------------------------

def _parse_upgrade(raw_upgrade: dict) -> UpgradeConfig:
    vp_raw = raw_upgrade.get("version_pin")
    vp = None
    if vp_raw is not None:
        vp = VersionPin(
            source=vp_raw.get("source"),
            key=vp_raw.get("key"),
            max=vp_raw.get("max"),
            max_reason=vp_raw.get("max_reason"),
        )
    return UpgradeConfig(
        kind=raw_upgrade.get("kind", ""),
        version_pin=vp,
        raw=raw_upgrade,
    )


def _parse_pause_window(raw_pw, *, app_name: str) -> "PauseWindow":
    if not isinstance(raw_pw, dict):
        raise ManifestError(
            f"App '{app_name}' pause_window must be a mapping with "
            f"start_hour_utc + end_hour_utc"
        )
    try:
        start = int(raw_pw["start_hour_utc"])
        end = int(raw_pw["end_hour_utc"])
    except KeyError as exc:
        raise ManifestError(
            f"App '{app_name}' pause_window missing required key {exc}"
        )
    except (TypeError, ValueError) as exc:
        raise ManifestError(
            f"App '{app_name}' pause_window hours must be integers: {exc}"
        )
    for label, h in (("start_hour_utc", start), ("end_hour_utc", end)):
        if not (0 <= h <= 23):
            raise ManifestError(
                f"App '{app_name}' pause_window {label}={h} out of range 0..23"
            )
    return PauseWindow(start_hour_utc=start, end_hour_utc=end)


def _parse_throttle(raw_t, *, app_name: str) -> "Throttle":
    if not isinstance(raw_t, dict):
        raise ManifestError(
            f"App '{app_name}' throttle must be a mapping with "
            f"transcode_workers + health_check_workers"
        )
    try:
        transcode = int(raw_t["transcode_workers"])
        health = int(raw_t["health_check_workers"])
    except KeyError as exc:
        raise ManifestError(
            f"App '{app_name}' throttle missing required key {exc}"
        )
    except (TypeError, ValueError) as exc:
        raise ManifestError(
            f"App '{app_name}' throttle workers must be integers: {exc}"
        )
    # Zero is legal (that IS a full stop, expressed as a throttle rather than
    # as a pause window). Negative is not, and neither is a cap so wide it
    # stops being one — on a shared slot an unbounded worker count is the
    # failure this field exists to prevent, so it is rejected at parse time
    # rather than politely honoured.
    for label, n in (("transcode_workers", transcode),
                     ("health_check_workers", health)):
        if not (0 <= n <= 8):
            raise ManifestError(
                f"App '{app_name}' throttle {label}={n} out of range 0..8"
            )
    return Throttle(transcode=transcode, health_check=health)


# Mirror of health._PROBES keys, duplicated here to avoid a circular import
# at module load. Update both lists when adding a new probe kind.
VALID_HEALTH_KINDS: frozenset[str] = frozenset({
    "http_api", "http_root", "systemd_only", "systemd_oneshot",
    "port_listen", "import_check", "process_pattern",
})


def _parse_health(raw_health: dict, *, app_name: str = "<unknown>") -> HealthConfig:
    kind = raw_health.get("kind", "")
    # Empty string used to be tolerated for apps that omitted health entirely
    # (library class). Keep that allowance; only validate when a kind is set,
    # so a typo like 'http-api' (vs 'http_api') fails at load time instead of
    # raising a KeyError every push cycle from inside the pusher loop.
    if kind and kind not in VALID_HEALTH_KINDS:
        raise ManifestError(
            f"App '{app_name}' has unknown health.kind '{kind}'; "
            f"must be one of {sorted(VALID_HEALTH_KINDS)}"
        )
    return HealthConfig(kind=kind, raw=raw_health)


def load(path: str | Path) -> Manifest:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ManifestError(f"Manifest file not found: {path}")
    except yaml.YAMLError as exc:
        raise ManifestError(f"YAML parse error in {path}: {exc}")

    if not isinstance(data, dict):
        raise ManifestError("Manifest must be a YAML mapping at the top level")

    defaults = data.get("defaults", {}) or {}
    raw_apps = data.get("apps", {}) or {}

    if not isinstance(raw_apps, dict):
        raise ManifestError("'apps' must be a YAML mapping")

    apps: dict[str, App] = {}
    seen_monitors: dict[str, str] = {}  # monitor_name -> app_name

    for app_name, app_data in raw_apps.items():
        if not isinstance(app_data, dict):
            raise ManifestError(f"App '{app_name}' must be a YAML mapping")

        # Required: class
        if "class" not in app_data:
            raise ManifestError(
                f"App '{app_name}' is missing required field 'class'"
            )

        class_ = app_data["class"]
        if class_ not in VALID_CLASSES:
            raise ManifestError(
                f"App '{app_name}' has unknown class '{class_}'; "
                f"must be one of {sorted(VALID_CLASSES)}"
            )

        kuma_monitor = app_data.get("kuma_monitor")

        # Duplicate kuma_monitor check (null/None excluded)
        if kuma_monitor is not None:
            if kuma_monitor in seen_monitors:
                raise ManifestError(
                    f"duplicate kuma_monitor '{kuma_monitor}' found in "
                    f"'{app_name}' and '{seen_monitors[kuma_monitor]}'"
                )
            seen_monitors[kuma_monitor] = app_name

        raw_health = app_data.get("health", {}) or {}
        health = _parse_health(raw_health, app_name=app_name)

        raw_upgrade = app_data.get("upgrade")
        upgrade = _parse_upgrade(raw_upgrade) if raw_upgrade else None

        raw_pw = app_data.get("pause_window")
        pause_window = (
            _parse_pause_window(raw_pw, app_name=app_name)
            if raw_pw is not None else None
        )

        raw_throttle = app_data.get("throttle")
        throttle = (
            _parse_throttle(raw_throttle, app_name=app_name)
            if raw_throttle is not None else None
        )

        apps[app_name] = App(
            name=app_name,
            class_=class_,
            kuma_monitor=kuma_monitor,
            health=health,
            defaults=defaults,
            upgrade=upgrade,
            parked=bool(app_data.get("parked", False)),
            pause_window=pause_window,
            throttle=throttle,
            raw=app_data,
        )

    # Parse canaries section
    raw_canaries = data.get("canaries", {}) or {}
    if not isinstance(raw_canaries, dict):
        raise ManifestError("'canaries' must be a YAML mapping")

    canaries: dict[str, Canary] = {}
    seen_canary_monitors: dict[str, str] = {}

    for canary_name, canary_data in raw_canaries.items():
        if not isinstance(canary_data, dict):
            raise ManifestError(f"Canary '{canary_name}' must be a YAML mapping")

        for required in ("kuma_monitor", "script", "schedule"):
            if required not in canary_data:
                raise ManifestError(
                    f"Canary '{canary_name}' is missing required field '{required}'"
                )

        kuma_monitor = canary_data["kuma_monitor"]
        schedule = canary_data["schedule"]

        if schedule not in VALID_CANARY_SCHEDULES:
            raise ManifestError(
                f"Canary '{canary_name}' has unknown schedule '{schedule}'; "
                f"must be one of {sorted(VALID_CANARY_SCHEDULES)}"
            )

        # Duplicate canary name check
        if canary_name in canaries:
            raise ManifestError(f"duplicate canary name '{canary_name}'")

        # Duplicate kuma_monitor check across canaries (and against apps)
        if kuma_monitor in seen_monitors:
            raise ManifestError(
                f"canary '{canary_name}' kuma_monitor '{kuma_monitor}' "
                f"conflicts with app '{seen_monitors[kuma_monitor]}'"
            )
        if kuma_monitor in seen_canary_monitors:
            raise ManifestError(
                f"duplicate kuma_monitor '{kuma_monitor}' found in canaries "
                f"'{canary_name}' and '{seen_canary_monitors[kuma_monitor]}'"
            )
        seen_canary_monitors[kuma_monitor] = canary_name

        canaries[canary_name] = Canary(
            name=canary_name,
            kuma_monitor=kuma_monitor,
            script=canary_data["script"],
            schedule=schedule,
        )

    raw_external = data.get("kuma_external_monitors", []) or []
    if not isinstance(raw_external, list):
        raise ManifestError("'kuma_external_monitors' must be a YAML list of strings")
    external_monitors = [str(m) for m in raw_external]

    return Manifest(apps, canaries, external_monitors=external_monitors)

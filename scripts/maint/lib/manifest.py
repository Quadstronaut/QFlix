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

VALID_CANARY_SCHEDULES = {"hourly", "daily-0430", "every-15min"}


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


def _parse_health(raw_health: dict) -> HealthConfig:
    return HealthConfig(kind=raw_health.get("kind", ""), raw=raw_health)


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
        health = _parse_health(raw_health)

        raw_upgrade = app_data.get("upgrade")
        upgrade = _parse_upgrade(raw_upgrade) if raw_upgrade else None

        apps[app_name] = App(
            name=app_name,
            class_=class_,
            kuma_monitor=kuma_monitor,
            health=health,
            defaults=defaults,
            upgrade=upgrade,
            parked=bool(app_data.get("parked", False)),
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

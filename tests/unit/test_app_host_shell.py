"""Shell/template surfaces of the net.app_host conversion (QFLX-23)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run_with_home(script: str, home: Path):
    env = dict(os.environ, HOME=home.as_posix())
    return subprocess.run([BASH, (REPO / script).as_posix()], env=env, capture_output=True,
                          text=True, timeout=30)


@pytest.mark.parametrize("script", [
    "scripts/ops/tautulli-gate-watch.sh",
    "scripts/maint/flaresolverr-unsuppress-watch.sh",
])
def test_box_watchers_refuse_to_guess_without_the_secret(script, tmp_path):
    (tmp_path / "secrets").mkdir()
    r = _run_with_home(script, tmp_path)
    assert r.returncode == 78, (r.returncode, r.stderr)
    assert "net.app_host" in r.stderr


def test_unpackerr_template_takes_the_host_from_a_placeholder():
    tmpl = (REPO / "scripts/data/unpackerr.conf.tmpl").read_text(encoding="utf-8")
    assert tmpl.count("{{APP_HOST}}") == 4
    render = (REPO / "scripts/configure/31-unpackerr.sh").read_text(encoding="utf-8")
    assert "{{APP_HOST}}" in render and "secret_read net.app_host" in render


def test_unpackerr_render_substitutes_the_secret(tmp_path):
    """Run the real sed chain from 31-unpackerr.sh against a fake secrets dir."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, val in {"net.app_host": "10.9.9.9", "sonarr.port": "1", "sonarr.key": "k",
                      "sonarr2.port": "2", "sonarr2.key": "k", "radarr.port": "3",
                      "radarr.key": "k", "radarr2.port": "4", "radarr2.key": "k"}.items():
        (secrets / name).write_text(val + "\n", encoding="utf-8")
    src = (REPO / "scripts/configure/31-unpackerr.sh").read_text(encoding="utf-8")
    # keep only the sed rendering block (up to the redirect into $OUT)
    head, _, rest = src.partition('"$TMPL" > "$OUT"')
    block = head.split("TMPL=", 1)[1]
    script = (
        "set -euo pipefail\n"
        "die() { echo \"$*\" >&2; exit 1; }\n"
        'source "%s"\n'
        'TMPL=%s"$TMPL" > "$OUT"\ncat "$OUT"\n'
    ) % ((REPO / "scripts/lib/secrets.sh").as_posix(), block.replace('"$HERE/data/unpackerr.conf.tmpl"', '"%s"' % (REPO / "scripts/data/unpackerr.conf.tmpl").as_posix()))
    env = dict(os.environ, SECRETS_DIR=secrets.as_posix())
    sh = tmp_path / "render.sh"
    # a file, not -c: Windows argv quoting mangles embedded quotes; bytes keep LF
    sh.write_bytes(script.encode("utf-8"))
    r = subprocess.run([BASH, sh.as_posix()], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "http://10.9.9.9:1/" in r.stdout
    assert "{{APP_HOST}}" not in r.stdout

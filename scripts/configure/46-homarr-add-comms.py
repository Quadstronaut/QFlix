#!/usr/bin/env python3
"""Phase 21 — Add the Recently-Added (mediaReleases) widget to the public board.

Idempotent. Runs server-side. Reads ~/secrets/plex.token at runtime so the
integration carries its API key.

What it does:
  1. Ensures a Plex `integration` row exists (kind='plex'), pointing at the
     local Plex on 127.0.0.1:<plex.port>.
  2. Ensures the integration's `integrationSecret` carries kind='apiKey' with
     the Plex token (read from ~/secrets/plex.token), encrypted with Homarr's
     SECRET_ENCRYPTION_KEY in the AES-256-CBC `<ciphertext_hex>.<iv_hex>`
     format Homarr's decryptSecret() expects.
  3. Inserts a widget item (kind='mediaReleases') with default options, links
     it to the Plex integration via integration_item, and places it on the
     public board's default section spanning the full width at the top.

Widget kind, options shape, and Plex integration secret kind ('apiKey') are
all sourced from homarr-labs/homarr v1 source verified 2026-05-08.

Original plan also added a "Get notified" tile pointing at /alerts/ — that
is moot since Phase 18 is skipped (see plan annotation 2026-05-08).

Encryption format reference: packages/common/src/encryption.ts in
homarr-labs/homarr — `aes-256-cbc`, output is `${hex(ct)}.${hex(iv)}`.
"""
import glob
import json
import os
import secrets
import sqlite3
import sys
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

DB = os.path.expanduser("~/.apps/homarr-upstream/data/db/db.sqlite")
PUBLIC_BOARD_NAME = "public"
ADMIN_BOARD_NAME = "admin"
PLEX_INTEGRATION_NAME = "Plex (Manitoba)"
# Homarr runs in a docker container on the default bridge (172.17.0.0/16).
# It cannot reach Plex on the host's 127.0.0.1, but Plex *also* listens on
# 172.17.0.1 (the docker0 gateway IP) per Ultra.cc's app-plex bind config,
# so we point the integration there. Verified 2026-05-09 with `ss -tln`.
PLEX_LOCAL_URL_TEMPLATE = "http://172.17.0.1:{port}"


def newid() -> str:
    return secrets.token_hex(12)


def secret(name: str) -> str:
    with open(os.path.expanduser(f"~/secrets/{name}")) as f:
        return f.read().strip()


def homarr_encryption_key() -> bytes:
    """Read SECRET_ENCRYPTION_KEY from the running Homarr container's env.

    Homarr's docker-compose generates this on first run and keeps it only in
    the container's environment + on the existing encrypted secrets in the DB.
    Re-encrypting with a new key would invalidate every other secret already
    in the DB, so we MUST use the same one the running process has.
    """
    for environ_path in glob.glob("/proc/*/environ"):
        try:
            with open(environ_path, "rb") as f:
                env = f.read().split(b"\x00")
        except (OSError, PermissionError):
            continue
        kv = {e.split(b"=", 1)[0]: e.split(b"=", 1)[1] for e in env if b"=" in e}
        if kv.get(b"DB_URL") == b"/appdata/db/db.sqlite" and b"SECRET_ENCRYPTION_KEY" in kv:
            return bytes.fromhex(kv[b"SECRET_ENCRYPTION_KEY"].decode())
    raise SystemExit(
        "FATAL: could not find SECRET_ENCRYPTION_KEY in any /proc/*/environ — "
        "is the Homarr container running? Try `docker ps | grep homarr`."
    )


def encrypt_homarr_secret(plaintext: str, key: bytes) -> str:
    """Match `encryptSecret` in homarr-labs/homarr packages/common/src/encryption.ts:
    aes-256-cbc, PKCS7 padding, output = `${hex(ct)}.${hex(iv)}`.
    """
    iv = os.urandom(16)
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return f"{ct.hex()}.{iv.hex()}"


def upsert_plex_integration(cur, enc_key: bytes):
    encrypted_token = encrypt_homarr_secret(secret("plex.token"), enc_key)
    url = PLEX_LOCAL_URL_TEMPLATE.format(port=secret("plex.port"))
    cur.execute("SELECT id FROM integration WHERE kind='plex' AND name=?", (PLEX_INTEGRATION_NAME,))
    row = cur.fetchone()
    if row:
        iid = row[0]
        # Refresh url + secret in case Plex moved
        cur.execute("UPDATE integration SET url=? WHERE id=?", (url, iid))
        cur.execute(
            "INSERT INTO integrationSecret (kind, value, updated_at, integration_id) VALUES ('apiKey', ?, ?, ?) "
            "ON CONFLICT (integration_id, kind) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (encrypted_token, int(time.time()), iid),
        )
        return iid

    iid = newid()
    cur.execute(
        "INSERT INTO integration (id, name, url, kind, app_id) VALUES (?, ?, ?, 'plex', NULL)",
        (iid, PLEX_INTEGRATION_NAME, url),
    )
    cur.execute(
        "INSERT INTO integrationSecret (kind, value, updated_at, integration_id) VALUES ('apiKey', ?, ?, ?)",
        (encrypted_token, int(time.time()), iid),
    )
    return iid


def get_board_id(cur, name):
    cur.execute("SELECT id FROM board WHERE name=?", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def get_default_section_and_layout(cur, board_id):
    cur.execute(
        "SELECT id FROM section WHERE board_id=? AND kind='empty' ORDER BY x_offset, y_offset LIMIT 1",
        (board_id,),
    )
    sid = cur.fetchone()[0]
    cur.execute("SELECT id FROM layout WHERE board_id=? ORDER BY breakpoint LIMIT 1", (board_id,))
    lid = cur.fetchone()[0]
    return sid, lid


def add_mediareleases_widget(cur, board_id, integration_id):
    section_id, layout_id = get_default_section_and_layout(cur, board_id)

    options_json = json.dumps(
        {"json": {
            "layout": "backdrop",
            "showDescriptionTooltip": True,
            "showType": True,
            "showSource": True,
        }},
        separators=(",", ":"),
    )
    advanced_json = '{"json": {}}'

    # Idempotency: one mediaReleases widget per board.
    cur.execute(
        "SELECT id FROM item WHERE board_id=? AND kind='mediaReleases' LIMIT 1", (board_id,)
    )
    row = cur.fetchone()
    if row:
        iid = row[0]
        # Refresh options in case widget defaults change
        cur.execute("UPDATE item SET options=? WHERE id=?", (options_json, iid))
    else:
        iid = newid()
        cur.execute(
            "INSERT INTO item (id, board_id, kind, options, advanced_options) VALUES (?, ?, 'mediaReleases', ?, ?)",
            (iid, board_id, options_json, advanced_json),
        )

    # Link to integration
    cur.execute(
        "INSERT OR IGNORE INTO integration_item (item_id, integration_id) VALUES (?, ?)",
        (iid, integration_id),
    )

    # Place at top: span the full 10-column width, 4 rows tall.
    # Existing tiles will be pushed down because this is at y=0.
    # First, shift all existing item_layout entries on this section down by 4 rows.
    cur.execute(
        "SELECT 1 FROM item_layout WHERE section_id=? AND item_id=?",
        (section_id, iid),
    )
    if cur.fetchone() is None:
        cur.execute(
            "UPDATE item_layout SET y_offset = y_offset + 4 WHERE section_id=?",
            (section_id,),
        )
        cur.execute(
            "INSERT INTO item_layout (item_id, section_id, layout_id, x_offset, y_offset, width, height) VALUES (?, ?, ?, 0, 0, 10, 4)",
            (iid, section_id, layout_id),
        )
    return iid


def main() -> int:
    enc_key = homarr_encryption_key()
    con = sqlite3.connect(DB)
    cur = con.cursor()

    plex_iid = upsert_plex_integration(cur, enc_key)
    print(f"plex integration id={plex_iid}")

    pub_board = get_board_id(cur, PUBLIC_BOARD_NAME)
    if pub_board is None:
        print("FATAL: public board not found", file=sys.stderr)
        return 2
    widget_id = add_mediareleases_widget(cur, pub_board, plex_iid)
    print(f"mediaReleases widget id={widget_id} on public board")

    # Mirror onto admin board too — admins want to see the same.
    adm_board = get_board_id(cur, ADMIN_BOARD_NAME)
    if adm_board:
        adm_widget = add_mediareleases_widget(cur, adm_board, plex_iid)
        print(f"mediaReleases widget id={adm_widget} on admin board")

    con.commit()
    con.close()
    print("[OK] Phase 21 complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

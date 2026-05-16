#!/usr/bin/env python3
"""Seed Homarr public + admin boards with app tiles.

Runs server-side. Idempotent: if a board with the same name already has items,
this script skips re-seeding it.

Layout: 10-column grid; tiles are 2 wide x 2 tall; 5 tiles per row.

Tile URLs are built from ~/secrets/seedbox.host so re-running on a fresh box
doesn't seed dead tiles. Dies hard if the secret is missing rather than
silently writing the placeholder into the Homarr DB.
"""
import json, os, secrets, sqlite3, sys

DB = os.path.expanduser("~/.apps/homarr-upstream/data/db/db.sqlite")

PUBLIC_BOARD_NAME = "public"
ADMIN_BOARD_NAME = "admin"


def _read_secret(name: str) -> str:
    """Read ~/secrets/<name>, stripped. Hard-die if missing — better to abort
    the install than seed broken tiles into the public board."""
    path = os.path.expanduser(f"~/secrets/{name}")
    if not os.path.isfile(path):
        sys.exit(
            f"FATAL: ~/secrets/{name} not set; refusing to seed Homarr with "
            f"placeholder URLs. Create the file with the operator's real "
            f"public FQDN (e.g. `quadstronaut.<seedbox-fqdn>`)."
        )
    with open(path) as f:
        return f.read().strip()


PUBLIC_HOST = _read_secret("seedbox.host")
# Optional: a direct-access Plex hostname (Ultra.cc shared seedboxes route a
# dedicated direct-IP endpoint). Fall back to standard /web/ on the public
# host if the operator hasn't configured one.
try:
    PLEX_DIRECT = _read_secret("plex.direct_host")
except SystemExit:
    PLEX_DIRECT = f"{PUBLIC_HOST}/web/"

ICON = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg"

# Public-facing services. Jellyfin / Jellystat / Readarr / Mylar3 / autobrr
# were purged 2026-05-11 and are intentionally absent.
PUBLIC_APPS = [
    ("Plex",             f"{ICON}/plex.svg",            f"https://{PLEX_DIRECT}"),
    ("Seerr",            f"{ICON}/seerr.svg",           f"https://{PUBLIC_HOST}/seerr/"),
    ("Komga (Comics)",   f"{ICON}/komga.svg",           f"https://{PUBLIC_HOST}/komga"),
    ("Kavita (Manga)",   f"{ICON}/kavita.svg",          f"https://{PUBLIC_HOST}/kavita"),
    ("Calibre-Web",      f"{ICON}/calibre-web.svg",     f"https://{PUBLIC_HOST}/calibre-web/"),
    ("Audiobookshelf",   f"{ICON}/audiobookshelf.svg",  f"https://audiobookshelf-{PUBLIC_HOST}/"),
    ("Tautulli (Stats)", f"{ICON}/tautulli.svg",        f"https://{PUBLIC_HOST}/tautulli"),
    ("FAQ",              f"{ICON}/homeassistant.svg",   f"https://{PUBLIC_HOST}/faq/"),
]

# Admin-board adds (operator-only). Public apps appear on admin board too.
# Removed: Readarr, Mylar3, autobrr, Jellystat (all purged 2026-05-11).
ADMIN_EXTRA_APPS = [
    ("Sonarr",            f"{ICON}/sonarr.svg",       f"https://{PUBLIC_HOST}/sonarr/"),
    ("Sonarr2 (Anime)",   f"{ICON}/sonarr.svg",       f"https://{PUBLIC_HOST}/sonarr2/"),
    ("Radarr",            f"{ICON}/radarr.svg",       f"https://{PUBLIC_HOST}/radarr"),
    ("Radarr2 (AnimeMov)",f"{ICON}/radarr.svg",       f"https://{PUBLIC_HOST}/radarr2"),
    ("Prowlarr",          f"{ICON}/prowlarr.svg",     f"https://{PUBLIC_HOST}/prowlarr"),
    ("qBittorrent",       f"{ICON}/qbittorrent.svg",  f"https://{PUBLIC_HOST}/qbittorrent/"),
    ("Bazarr",            f"{ICON}/bazarr.svg",       f"https://{PUBLIC_HOST}/bazarr/"),
    # Bazarr 2 (anime *arr pair) intentionally has no tile — it's internal-only
    # (loopback 127.0.0.1:17032/bazarr2/, no nginx proxy) so there's no public
    # URL to link from a browser. Reach it via the manitoba-tunnel daemon.
    ("Maintainerr",       f"{ICON}/maintainerr.svg",  f"https://maintainerr-{PUBLIC_HOST}/"),
    ("Notifiarr",         f"{ICON}/notifiarr.svg",    "https://notifiarr.com/"),
]


def newid() -> str:
    """Match Homarr's CUID-ish id pattern: ~24 lowercase alnum chars."""
    return secrets.token_hex(12)


def upsert_app(cur, name, icon, href):
    cur.execute("SELECT id FROM app WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    aid = newid()
    cur.execute(
        "INSERT INTO app (id, name, description, icon_url, href, ping_url) VALUES (?, ?, '', ?, ?, ?)",
        (aid, name, icon, href, href),
    )
    return aid


def get_or_create_board(cur, name, *, is_public=False, creator_id=None):
    cur.execute("SELECT id FROM board WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0], False
    bid = newid()
    cur.execute(
        """INSERT INTO board
           (id, name, is_public, creator_id, background_image_attachment, background_image_repeat,
            background_image_size, primary_color, secondary_color, opacity, disable_status, item_radius)
           VALUES (?, ?, ?, ?, 'fixed', 'no-repeat', 'cover', '#fa5252', '#fd7e14', 100, 0, 'lg')""",
        (bid, name, 1 if is_public else 0, creator_id),
    )
    # default Base layout
    lid = newid()
    cur.execute(
        "INSERT INTO layout (id, name, board_id, column_count, breakpoint) VALUES (?, 'Base', ?, 10, 0)",
        (lid, bid),
    )
    # default empty section
    sid = newid()
    cur.execute(
        "INSERT INTO section (id, board_id, kind, x_offset, y_offset, name, options) VALUES (?, ?, 'empty', 0, 0, '', '{\"json\": {}}')",
        (sid, bid),
    )
    return bid, True


def get_default_section_and_layout(cur, board_id):
    cur.execute("SELECT id FROM section WHERE board_id = ? AND kind = 'empty' ORDER BY x_offset, y_offset LIMIT 1", (board_id,))
    sid = cur.fetchone()[0]
    cur.execute("SELECT id FROM layout WHERE board_id = ? ORDER BY breakpoint LIMIT 1", (board_id,))
    lid = cur.fetchone()[0]
    return sid, lid


def board_has_items(cur, board_id):
    cur.execute("SELECT COUNT(*) FROM item WHERE board_id = ?", (board_id,))
    return cur.fetchone()[0] > 0


def add_app_tiles(cur, board_id, apps_with_ids, *, tile_w=2, tile_h=2, cols=10):
    section_id, layout_id = get_default_section_and_layout(cur, board_id)
    # already-occupied (item, layout) pairs
    cur.execute("SELECT item_id FROM item_layout WHERE section_id = ?", (section_id,))
    placed = {r[0] for r in cur.fetchall()}

    # find next free slot
    cur.execute("SELECT MAX(y_offset + height) FROM item_layout WHERE section_id = ?", (section_id,))
    next_y = cur.fetchone()[0] or 0
    x = 0
    y = next_y
    for app_id in apps_with_ids:
        # one item per app; existing items reused
        cur.execute(
            "SELECT id FROM item WHERE board_id = ? AND options = ?",
            (board_id, json.dumps({"json": {"appId": app_id}}, separators=(",", ":"))),
        )
        row = cur.fetchone()
        if row:
            iid = row[0]
            if iid in placed:
                continue
        else:
            iid = newid()
            cur.execute(
                "INSERT INTO item (id, board_id, kind, options, advanced_options) VALUES (?, ?, 'app', ?, '{\"json\": {}}')",
                (iid, board_id, json.dumps({"json": {"appId": app_id}}, separators=(",", ":"))),
            )

        # advance grid (left-to-right, top-to-bottom)
        if x + tile_w > cols:
            x = 0
            y += tile_h
        cur.execute(
            "INSERT INTO item_layout (item_id, section_id, layout_id, x_offset, y_offset, width, height) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iid, section_id, layout_id, x, y, tile_w, tile_h),
        )
        x += tile_w


def set_home_board(cur, board_id):
    cur.execute("SELECT value FROM serverSetting WHERE setting_key = 'board'")
    row = cur.fetchone()
    if row:
        v = json.loads(row[0])
        v["json"]["homeBoardId"] = board_id
        v["json"]["mobileHomeBoardId"] = board_id
        cur.execute("UPDATE serverSetting SET value = ? WHERE setting_key = 'board'", (json.dumps(v, separators=(",", ":")),))


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # --- Apps ---
    print("== Apps ==")
    public_app_ids = []
    for name, icon, href in PUBLIC_APPS:
        aid = upsert_app(cur, name, icon, href)
        public_app_ids.append(aid)
        print(f"  app '{name}' -> {aid}")
    admin_app_ids = list(public_app_ids)
    for name, icon, href in ADMIN_EXTRA_APPS:
        aid = upsert_app(cur, name, icon, href)
        admin_app_ids.append(aid)
        print(f"  app '{name}' -> {aid}")

    # creator id from the first user (admin)
    cur.execute("SELECT id FROM user ORDER BY ROWID LIMIT 1")
    user_row = cur.fetchone()
    creator = user_row[0] if user_row else None

    # --- Public board ---
    pub_id, pub_created = get_or_create_board(cur, PUBLIC_BOARD_NAME, is_public=True, creator_id=creator)
    print(f"== Public board: {pub_id} (created={pub_created}) ==")
    if board_has_items(cur, pub_id):
        print("  already has items — skipping tile seed")
    else:
        add_app_tiles(cur, pub_id, public_app_ids)
        print(f"  seeded {len(public_app_ids)} app tiles")

    # --- Admin board ---
    adm_id, adm_created = get_or_create_board(cur, ADMIN_BOARD_NAME, is_public=False, creator_id=creator)
    print(f"== Admin board: {adm_id} (created={adm_created}) ==")
    if board_has_items(cur, adm_id):
        print("  already has items — skipping tile seed")
    else:
        add_app_tiles(cur, adm_id, admin_app_ids)
        print(f"  seeded {len(admin_app_ids)} app tiles")

    # --- Set public as home board ---
    set_home_board(cur, pub_id)
    print(f"== home board set -> {pub_id} (public)")

    con.commit()
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()

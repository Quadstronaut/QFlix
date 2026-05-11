#!/usr/bin/env python3
"""Seed Homarr public + admin boards with app tiles.

Runs server-side. Idempotent: if a board with the same name already has items,
this script skips re-seeding it.

Layout: 10-column grid; tiles are 2 wide x 2 tall; 5 tiles per row.
"""
import json, os, secrets, sqlite3, sys

DB = os.path.expanduser("~/.apps/homarr-upstream/data/db/db.sqlite")

PUBLIC_BOARD_NAME = "public"
ADMIN_BOARD_NAME = "admin"

# Public-facing services. (icon URLs use the dashboard-icons CDN.)
PUBLIC_APPS = [
    ("Plex",            "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/plex.svg",            "https://seedbox-direct.example.com:17025"),
    ("Jellyfin",        "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/jellyfin.svg",        "https://quadstronaut.seedbox.example.com/jellyfin"),
    ("Seerr",           "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/seerr.svg",      "https://quadstronaut.seedbox.example.com/seerr/"),
    ("Komga (Comics)",  "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/komga.svg",           "https://quadstronaut.seedbox.example.com/komga"),
    ("Kavita (Manga)",  "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/kavita.svg",          "https://quadstronaut.seedbox.example.com/kavita"),
    ("Calibre-Web",     "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/calibre-web.svg",     "https://quadstronaut.seedbox.example.com/calibre-web/"),
    ("Audiobookshelf",  "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/audiobookshelf.svg",  "https://audiobookshelf-quadstronaut.seedbox.example.com/"),
    ("Tautulli (Stats)","https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/tautulli.svg",        "https://quadstronaut.seedbox.example.com/tautulli"),
]

# Admin-board adds (operator-only). Public apps appear on admin board too.
ADMIN_EXTRA_APPS = [
    ("Sonarr",          "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/sonarr.svg",          "https://quadstronaut.seedbox.example.com/sonarr/"),
    ("Sonarr2 (Anime)", "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/sonarr.svg",          "https://quadstronaut.seedbox.example.com/sonarr2/"),
    ("Radarr",          "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/radarr.svg",          "https://quadstronaut.seedbox.example.com/radarr"),
    ("Radarr2 (AnimeMov)","https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/radarr.svg",        "https://quadstronaut.seedbox.example.com/radarr2"),
    ("Readarr",         "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/readarr.svg",         "https://quadstronaut.seedbox.example.com/readarr"),
    ("Mylar3",          "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/mylar.svg",           "https://quadstronaut.seedbox.example.com/mylar/"),
    ("Prowlarr",        "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/prowlarr.svg",        "https://quadstronaut.seedbox.example.com/prowlarr"),
    ("qBittorrent",     "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/qbittorrent.svg",     "https://quadstronaut.seedbox.example.com/qbittorrent/"),
    ("autobrr",         "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/autobrr.svg",         "https://quadstronaut.seedbox.example.com/autobrr/"),
    ("Bazarr",          "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/bazarr.svg",          "https://quadstronaut.seedbox.example.com/bazarr/"),
    # Bazarr 2 (anime *arr pair) intentionally has no tile — it's internal-only
    # (loopback 127.0.0.1:17032/bazarr2/, no nginx proxy) so there's no public
    # URL to link from a browser. Reach it via the manitoba-tunnel daemon.
    ("Maintainerr",     "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/maintainerr.svg",     "https://maintainerr-quadstronaut.seedbox.example.com/"),
    ("Jellystat",       "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/jellyfin.svg",        "https://jellystat-quadstronaut.seedbox.example.com/"),
    ("Notifiarr",       "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/notifiarr.svg",       "https://notifiarr.com/"),
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

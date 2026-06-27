#!/usr/bin/env python3
"""Replace the retired Homarr public board with a single 'QFlix has moved' notice.

Runs server-side on the seedbox. A true 301 redirect off the Homarr subdomain is
NOT possible on Ultra.cc (Homarr is a rootless container holding its own port; the
container can't be made to vacate it, and uninstalling kills the outer-nginx route
to the subdomain — see the 2026-06-27 cutover). Homarr also has no native redirect.
So the best forward we can manage is to gut its public board down to one big tile
that says "QFlix has moved — tap to open the new site" and links to the dashboard.

Idempotent-ish: clears all items on the public board, deletes the category
sections, and leaves one notice tile in the root grid. Backs up the DB first.
"""
import json
import os
import secrets
import shutil
import sqlite3
import time

DB = os.path.expanduser("~/.apps/homarr-upstream/data/db/db.sqlite")
TARGET = "https://qflix.quadstronix.dev/"
Q_ICON = "https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png"


def newid() -> str:
    return secrets.token_hex(12)


def main() -> None:
    bak = f"{DB}.bak.qflixmoved.{int(time.time())}"
    for ext in ("", "-wal", "-shm"):
        if os.path.isfile(DB + ext):
            shutil.copy2(DB + ext, bak + ext)
    print("backup:", bak)

    con = sqlite3.connect(DB, timeout=15)
    con.execute("PRAGMA busy_timeout=15000")
    cur = con.cursor()

    bid = cur.execute("SELECT id FROM board WHERE name='public'").fetchone()[0]
    layout_id = cur.execute("SELECT id FROM layout WHERE board_id=? ORDER BY breakpoint LIMIT 1", (bid,)).fetchone()[0]
    # the root 'empty' section (no category title) — notice goes here
    empty = cur.execute("SELECT id FROM section WHERE board_id=? AND kind='empty' LIMIT 1", (bid,)).fetchone()[0]

    # 1) the notice app
    app_id = newid()
    cur.execute(
        "INSERT INTO app (id,name,description,icon_url,href,ping_url) VALUES (?,?,?,?,?,?)",
        (app_id, "QFlix has moved — tap to open the new site", "", Q_ICON, TARGET, TARGET),
    )
    # 2) clear every existing tile on the board
    cur.execute("DELETE FROM item_layout WHERE item_id IN (SELECT id FROM item WHERE board_id=?)", (bid,))
    cur.execute("DELETE FROM item WHERE board_id=?", (bid,))
    # 3) delete the category (dynamic) sections + their section_layout rows
    dyn = [r[0] for r in cur.execute("SELECT id FROM section WHERE board_id=? AND kind!='empty'", (bid,))]
    for s in dyn:
        cur.execute("DELETE FROM section_layout WHERE section_id=?", (s,))
        cur.execute("DELETE FROM section WHERE id=?", (s,))
    # 4) the notice tile, centered in the root grid (10-col layout: x=2 w=6)
    item_id = newid()
    opts = {"json": {"appId": app_id, "openInNewTab": False, "showTitle": True,
                     "descriptionDisplayMode": "hidden", "layout": "column", "pingEnabled": False}}
    cur.execute("INSERT INTO item (id,board_id,kind,options,advanced_options) VALUES (?,?,?,?,?)",
                (item_id, bid, "app", json.dumps(opts, separators=(",", ":")), '{"json":{}}'))
    cur.execute("INSERT INTO item_layout (item_id,section_id,layout_id,x_offset,y_offset,width,height) VALUES (?,?,?,?,?,?,?)",
                (item_id, empty, layout_id, 2, 0, 6, 4))
    # 5) header
    cur.execute("UPDATE board SET page_title=?, meta_title=? WHERE id=?", ("QFlix has moved", "QFlix has moved", bid))

    con.commit()
    print("sections left:", [r[0] for r in cur.execute("SELECT id FROM section WHERE board_id=?", (bid,))])
    print("items:", cur.execute("SELECT COUNT(*) FROM item WHERE board_id=?", (bid,)).fetchone()[0])
    con.close()
    print("Done. Restart Homarr (app-homarr restart) so it reloads the board.")


if __name__ == "__main__":
    main()

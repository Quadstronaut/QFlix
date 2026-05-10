# *arr stack audit — action items 2026-05-09

Generated from `scripts/smoke/arr-audit.py`. Full audit report at
`docs/arr-audit-2026-05-09.md`.

## Headline

**Most of the *arr stack is clean.** No manual indexers leaking through (the
user's "I see some indexers in radarr that aren't Prowlarr" worry was a
false alarm — every indexer in every *arr traces back to Prowlarr).
TRaSH custom formats are applied to all quality profiles. qBit is wired
correctly to all 5 *arrs and `testall` returns 200 across the board.

The fixes below are minor cleanup + one real gap (Readarr ↔ Prowlarr).

---

## P0 — fix soon

### A. Readarr is not registered with Prowlarr (no indexers syncing)

**Symptom:** `readarr` has 0 indexers configured. Prowlarr's Applications
page only lists Sonarr/Sonarr2/Radarr/Radarr2 — Readarr is absent.

**Fix:** in Prowlarr UI → Settings → Apps → "+ Add", choose Readarr,
provide:
- Prowlarr Server URL: `http://prowlarr:<port>` *(internal — Prowlarr
  reaches Readarr via the same nginx subpath as the *arr admin URL)*
- Readarr Server URL: `http://127.0.0.1:<readarr.port>/readarr`
- API Key: `secrets/readarr.key`
- Sync Profile: matches the existing pattern (Sonarr/Radarr both use
  `fullSync`)
- Tags: book-specific indexer tag if any (none currently exists)

After saving, hit Sync App in the row. Indexers that support `book` /
`audiobook` categories will populate Readarr's indexer list.

**Or scriptable**: write a `scripts/configure/04f-prowlarr-readarr-sync.py`
following the `04e-prowlarr-curated-add.py` pattern.

---

### B. Stale download clients (post-Phase 16)

Phase 16 uninstalled Transmission + Deluge; the *arrs still have them in
their download-client list (disabled, but visible in UI clutter):

| *arr     | Stale entries                          |
|----------|----------------------------------------|
| sonarr   | rTorrent (disabled)                    |
| radarr   | rTorrent (disabled), Transmission (disabled) |
| sonarr2  | (clean)                                |
| radarr2  | (clean)                                |
| readarr  | (clean)                                |

**Fix:** DELETE the disabled entries via API:
```bash
# Example — adjust ID per arr
curl -X DELETE -u "quadstronaut:$HTPW" -H "X-Api-Key: $KEY" \
  "https://quadstronaut.seedbox.example.com/sonarr/api/v3/downloadclient/<id>"
```

Or write a one-shot script alongside `rectify-qbit-and-cascade.py` to
prune disabled clients across all *arrs.

---

### C. Two Prowlarr indexers with 0 grabs (high latency)

| Indexer            | Avg response | Queries | Grabs | Action            |
|--------------------|--------------|---------|-------|-------------------|
| Internet Archive   | 40 345 ms    | 36      | 0     | Remove from Prowlarr |
| Magnet Cat         | 16 822 ms    | 47      | 0     | Remove from Prowlarr |
| 1337x (id=13)      | never tested | 0       | 0     | Test live; remove if still 0 |

These are slow + producing nothing. They contribute to search timeouts
without increasing hit rate. Recommend removing all three. If you want
"more sources" they're not actually adding any — 12 healthy indexers
already cover the catalog.

---

## P1 — nice to have

### D. Prowlarr update available (v2.3.5.5327)

Health check warns about new release. Will be picked up automatically by
the Monday 04:30 cp.ultra.cc clicker once that's verified, since
Prowlarr is one of the 12 UCC apps.

---

### E. Multi-user routing tags in Sonarr + Radarr (verify intent)

Sonarr and Radarr each carry 7 user-named tags applied to ~70% of series /
~55% of movies. These are legacy request-attribution labels from the
multi-user Ombi era. Treat them as historical metadata.

**No fix needed** — flagged for awareness so a future cleanup doesn't
delete them thinking they're orphans. They're load-bearing for which
user requested what.

---

## Already verified clean

- ✅ All 4 working *arrs (sonarr, sonarr2, radarr, radarr2) have only
  Prowlarr-managed indexers
- ✅ qBittorrent is the only enabled download client on every *arr
- ✅ All download-client `testall` calls return HTTP 200
- ✅ TRaSH custom formats applied (sonarr=36, sonarr2=55, radarr=39,
  radarr2=39 format items per profile — matches TRaSH-Guides
  recommendations)
- ✅ Prowlarr → Sonarr/Sonarr2/Radarr/Radarr2 sync is `fullSync` with
  correct tag routing (anime tag=1 → *2 *arrs, general tag=3 → main *arrs)
- ✅ Root folders accessible, ~11 TB free across all of them
- ✅ Anime branch routing intact (Sonarr2/Radarr2 receive anime-tag
  indexers only)

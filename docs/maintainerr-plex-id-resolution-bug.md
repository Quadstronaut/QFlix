# Upstream bug report draft — Maintainerr (jorenn92/Maintainerr)

**Title:** [Plex] Radarr/Sonarr ActionHandler: "Couldn't resolve any supported external IDs" — delete action never executes (v3.15.0)

**Body:**

### Summary
On Plex, every collection-handling run fails to delete any media. For **every** movie and show, the Radarr/Sonarr action handler logs `Couldn't resolve any supported external IDs` and aborts (`the configured action could not be completed` → `No data was altered`). Rule *matching* works (items are added to collections); only the delete *action* fails. Nothing is ever deleted.

### Environment
- Maintainerr **3.15.0** (commitTag `latest-98445d1`), Docker.
- Media server: **Plex** (new Plex Movie / Plex TV Series agents).
- Collections use arrAction = Delete (delete from Radarr/Sonarr + files). Radarr/Sonarr connected and working.

### Logs (representative)
```
[CollectionWorkerService] Handling collection 'QFlix Movies-60d'
[RadarrActionHandler] Couldn't resolve any supported external IDs for movie with media server ID 6128. Please check this movie manually.
[CollectionWorkerService] Failed to handle media with id 6128 ... the configured action could not be completed
[SonarrActionHandler] Couldn't resolve any supported external IDs for media server item 6151. No action was taken.
[CollectionWorkerService] All collections handled. No data was altered
```

### Why this is a bug (not bad data / config)
The Plex items DO have full external IDs, and Maintainerr can retrieve them with its own stored token:
- `GET http://<plex>/library/metadata/6128?includeGuids=1` (using Maintainerr's stored `plex_auth_token`) returns:
  `Guid[] = ['imdb://tt15678738', 'tmdb://1167307', 'tvdb://357032']`
- The items exist in Radarr/Sonarr.
- Verified for both movies (ratingKeys 6128/6019/6051/6219 …) and shows (6151/5935/6012 …) — 100% of items fail.

So token, Plex agent, `includeGuids`, and the data are all fine; Maintainerr 3.15.0 fails to use IDs that are available to it. This is **not** the Emby fix in #3100 / v3.15.1 — that PR explicitly states "Plex is unaffected (its top-level movies already had no parent, so they already used their own ids)." This is a separate Plex-side regression in the action-handler ID resolution.

### Repro
1. Plex library on the new Plex agents; items have tmdb/imdb/tvdb GUIDs.
2. Rule with a Delete (Radarr/Sonarr) action; let items age past the delete window.
3. On handle, every item fails with "Couldn't resolve any supported external IDs."

### Impact
Autodelete is completely non-functional on Plex in 3.15.0 (silent — only WARN-level, "No data was altered"). Backlog grows unbounded; operators may believe cleanup is working.

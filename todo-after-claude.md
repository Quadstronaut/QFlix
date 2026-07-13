# post execution

### tdarr
* tune worker cap to this server's architecture — 128 cores available; current
  cap is 2/2 per inventory.md (Section B). Conservative on a shared seedbox
  but plenty of headroom. Decide whether to raise based on real transcoder
  contention vs. Plex's own playback budget.

### Other
* Nothing else outstanding as of 2026-05-16. See `docs/operator-deferred.md`
  for items that still need human judgment (Notifiarr client daemon,
  the post-Phase-16 uninstalls). (Homarr decommissioned 2026-07-13.)

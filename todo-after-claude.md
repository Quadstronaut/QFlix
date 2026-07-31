# post execution

**Nothing outstanding as of 2026-07-30.**

This file is deliberately kept empty of open work. It was a second, unwatched
backlog living beside `docs/operator-deferred.md`, which is how the Tdarr
worker-cap question sat here from 2026-05-16 to 2026-07-30 while the real
registry never mentioned it. One registry, watched by a test, or none.

### tdarr worker cap — CLOSED 2026-07-30, decision recorded

Was: *"tune worker cap to this server's architecture — 128 cores available;
current cap is 2/2."* **Resolved: keep 2/2.** Measured live while clients were
streaming — 2 Plex transcoders active, 946/2000 threads used (47% of the slot's
`RLIMIT_NPROC`), load average 26 on a **shared** box. Thread exhaustion is a
proven crash class here (it crash-looped VictoriaLogs). Tdarr is a background
optimiser; playback is the product. Full rationale in
[`docs/operator-deferred.md`](docs/operator-deferred.md).

### Everything else

Resolved — see `docs/operator-deferred.md`, the single registry, enforced by
`tests/unit/test_operator_deferred.py`: every open item must carry an owner and a
dated adjudication, and the list cannot grow unnoticed.

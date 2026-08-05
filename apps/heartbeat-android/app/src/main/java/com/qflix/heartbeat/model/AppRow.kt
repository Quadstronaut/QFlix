package com.qflix.heartbeat.model

/**
 * One row of the Apps screen's lifecycle list: an app's slug (e.g.
 * "sonarr") and its lifecycle class - `"ucc"` (driven through Ultra's
 * approved command layer) or `"systemd"` (driven directly by systemctl).
 *
 * The class travels all the way to the UI as a visible badge (see
 * `ui/AppsScreen.kt`) rather than being collapsed into one generic "app"
 * row: the design spec requires the operator be able to see, at a glance,
 * which lifecycle path a restart/stop/start actually takes.
 */
data class AppRow(val slug: String, val klass: String) {
    companion object {
        /**
         * Reads `app.list`'s `lines`, each formatted `"<slug> <class>"`.
         * A line that isn't exactly two whitespace-separated tokens is
         * skipped rather than crashing the screen - matches
         * [Envelope.parse]'s own "never throws" contract one layer up.
         */
        fun parseList(envelope: Envelope): List<AppRow> =
            envelope.lines.mapNotNull { line ->
                val parts = line.trim().split(Regex("\\s+"))
                if (parts.size == 2) AppRow(slug = parts[0], klass = parts[1]) else null
            }
    }
}

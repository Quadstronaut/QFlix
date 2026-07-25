package com.qflix.heartbeat.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * kotlinx-serialization mirror of the status-doc contract produced by
 * `scripts/mcp/app_status.py` (see docs/superpowers/plans/2026-07-15-heartbeat-android.md
 * "Output JSON contract"). One JSON document, seven top-level keys.
 *
 * Every section is nullable with a `null`/empty default so a doc that is
 * missing a whole section (key absent), or carries a section with
 * `"ok": false` and no data, still parses instead of throwing. Section-level
 * failure isolation is the server's contract too ("one dead source never
 * kills the doc") - this mirror just refuses to weaken that guarantee on the
 * way in.
 */
@Serializable
data class StatusDoc(
    val meta: Meta? = null,
    val quota: QuotaSection? = null,
    val kuma: KumaSection? = null,
    val streams: StreamsSection? = null,
    val top5: Top5Section? = null,
    val downloads: DownloadsSection? = null,
    val maint: MaintSection? = null,
    val alerts: List<Alert> = emptyList(),
) {
    companion object {
        // ignoreUnknownKeys: the server can grow new fields without breaking
        // an already-installed app. coerceInputValues: a field of the wrong
        // JSON type (e.g. a null where we don't expect one) falls back to
        // the property's default instead of throwing - the whole point of a
        // "degraded doc" contract is that the client never crashes on it.
        private val format = Json {
            ignoreUnknownKeys = true
            coerceInputValues = true
        }

        /** Parses one status-doc JSON string. Throws only on genuinely unparseable JSON. */
        fun parse(json: String): StatusDoc = format.decodeFromString(serializer(), json)
    }
}

@Serializable
data class Meta(
    @SerialName("generated_at") val generatedAt: String? = null,
    @SerialName("elapsed_ms") val elapsedMs: Long? = null,
    val host: String? = null,
    val version: Int? = null,
)

@Serializable
data class QuotaSection(
    val ok: Boolean = false,
    val error: String? = null,
    val disk: Disk? = null,
    val bandwidth: Bandwidth? = null,
)

@Serializable
data class Disk(
    @SerialName("used_gb") val usedGb: Double? = null,
    @SerialName("total_gb") val totalGb: Double? = null,
    val pct: Double? = null,
)

@Serializable
data class Bandwidth(
    @SerialName("used_pct") val usedPct: Double? = null,
    @SerialName("available_pct") val availablePct: Double? = null,
    @SerialName("last_reset") val lastReset: String? = null,
    @SerialName("next_reset") val nextReset: String? = null,
)

@Serializable
data class KumaSection(
    val ok: Boolean = false,
    val error: String? = null,
    val total: Int? = null,
    val up: Int? = null,
    val down: Int? = null,
    val red: List<KumaRed> = emptyList(),
)

@Serializable
data class KumaRed(
    val name: String? = null,
    val msg: String? = null,
    val since: String? = null,
)

@Serializable
data class StreamsSection(
    val ok: Boolean = false,
    val error: String? = null,
    val streams: Int? = null,
    val users: Int? = null,
    val transcodes: Int? = null,
    @SerialName("wan_kbps") val wanKbps: Int? = null,
)

@Serializable
data class Top5Section(
    val ok: Boolean = false,
    val error: String? = null,
    @SerialName("requests_30d") val requests30d: List<RequestEntry> = emptyList(),
    @SerialName("watch_30d") val watch30d: List<WatchEntry> = emptyList(),
)

@Serializable
data class RequestEntry(
    val user: String? = null,
    val count: Int? = null,
)

@Serializable
data class WatchEntry(
    val user: String? = null,
    val hours: Double? = null,
    val plays: Int? = null,
)

@Serializable
data class DownloadsSection(
    val ok: Boolean = false,
    val error: String? = null,
    val qbit: QbitStats? = null,
    val sab: SabStats? = null,
    val stuck: List<StuckTorrent> = emptyList(),
    @SerialName("recent_unsticks") val recentUnsticks: List<UnstickEvent> = emptyList(),
)

@Serializable
data class QbitStats(
    val total: Int? = null,
    val active: Int? = null,
    @SerialName("stalled_dl") val stalledDl: Int? = null,
    val errored: Int? = null,
    val seeding: Int? = null,
)

@Serializable
data class SabStats(
    val queued: Int? = null,
    val paused: Boolean? = null,
    @SerialName("mb_left") val mbLeft: Double? = null,
    @SerialName("mb_total") val mbTotal: Double? = null,
    val kbps: Int? = null,
)

@Serializable
data class StuckTorrent(
    val hash8: String? = null,
    val name: String? = null,
    val hours: Double? = null,
    val rule: String? = null,
    val acted: Boolean? = null,
)

@Serializable
data class UnstickEvent(
    val ts: String? = null,
    val hash8: String? = null,
    val result: String? = null,
)

@Serializable
data class Alert(
    val level: String? = null,
    val text: String? = null,
)

@Serializable
data class MaintSection(
    val ok: Boolean = false,
    val error: String? = null,
    @SerialName("failed_units") val failedUnits: List<String> = emptyList(),
    @SerialName("anime_janitor") val animeJanitor: AnimeJanitor? = null,
)

@Serializable
data class AnimeJanitor(
    @SerialName("recent_moves") val recentMoves: Int? = null,
    @SerialName("last_move") val lastMove: LastMove? = null,
)

@Serializable
data class LastMove(
    val title: String? = null,
    val from: String? = null,
    val to: String? = null,
    val ts: String? = null,
)

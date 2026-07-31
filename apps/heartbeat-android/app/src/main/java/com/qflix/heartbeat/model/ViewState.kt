package com.qflix.heartbeat.model

import java.time.Duration
import java.time.Instant
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.util.Locale
import kotlin.math.roundToLong

/**
 * Render state for one dashboard section. Exactly one of three shapes, so
 * the UI layer (A4) can `when`-exhaust it instead of null-checking:
 *  - [Ok]: section parsed with `ok: true` - render [Ok.data].
 *  - [SectionError]: section present with `ok: false` - render an inline
 *    error chip with [SectionError.message], not a crash.
 *  - [Missing]: the section key was absent from the doc entirely.
 */
sealed class SectionState<out T> {
    data class Ok<T>(val data: T) : SectionState<T>()
    data class SectionError(val message: String) : SectionState<Nothing>()
    object Missing : SectionState<Nothing>()
}

/** Everything the dashboard screen needs, pre-formatted - no formatting logic belongs in Compose code. */
data class DashboardState(
    val dataAge: String,
    val quota: SectionState<QuotaView>,
    val kuma: SectionState<KumaView>,
    val streams: SectionState<StreamsView>,
    val top5: SectionState<Top5View>,
    val downloads: SectionState<DownloadsView>,
    val alerts: List<AlertView>,
    val maint: SectionState<MaintView> = SectionState.Missing,
)

data class QuotaView(
    val diskPct: Double,
    val diskLabel: String,
    val bandwidthUsedPct: Double,
    val bandwidthLabel: String,
)

data class KumaView(
    val summary: String,
    val red: List<KumaRedView>,
)

data class KumaRedView(val name: String, val msg: String, val since: String)

data class StreamsView(
    val fraction: String,
    val transcodes: Int,
    val wanKbps: Int,
)

data class Top5View(
    val requests: List<RequestEntryView>,
    val watch: List<WatchEntryView>,
)

data class RequestEntryView(val user: String, val count: Int)
data class WatchEntryView(val user: String, val hoursLabel: String, val plays: Int)

data class DownloadsView(
    val qbitSummary: String,
    val sabSummary: String,
    val stuck: List<StuckView>,
    val recentUnsticks: List<UnstickView>,
)

data class StuckView(val hash8: String, val hoursLabel: String, val acted: Boolean, val rule: String)
data class UnstickView(val ts: String, val hash8: String, val result: String)

data class AlertView(val level: String, val text: String)

data class MaintView(
    val failedUnits: List<String>,
    val janitorLabel: String?,
)

/** Pure StatusDoc -> DashboardState mapping. No I/O, no Android types - trivially unit-testable. */
object ViewState {

    fun from(doc: StatusDoc, now: Instant): DashboardState = DashboardState(
        dataAge = dataAge(doc.meta?.generatedAt, now),
        quota = mapQuota(doc.quota),
        kuma = mapKuma(doc.kuma),
        streams = mapStreams(doc.streams),
        top5 = mapTop5(doc.top5),
        downloads = mapDownloads(doc.downloads),
        alerts = orderAlerts(doc.alerts),
        maint = mapMaint(doc.maint),
    )

    // ---- maint: failed units + anime-janitor activity ----

    private fun mapMaint(section: MaintSection?): SectionState<MaintView> = when {
        section == null -> SectionState.Missing
        !section.ok -> SectionState.SectionError(section.error ?: "maint unavailable")
        else -> SectionState.Ok(
            MaintView(
                failedUnits = section.failedUnits,
                janitorLabel = janitorLabel(section.animeJanitor),
            ),
        )
    }

    /** "1 re-home(s) in 7d - last: Cowboy Bebop (2021) (sonarr2->sonarr)", or null when idle. */
    fun janitorLabel(aj: AnimeJanitor?): String? {
        if (aj == null) return null
        val n = aj.recentMoves ?: 0
        val last = aj.lastMove
        val base = "$n re-home(s) in 7d"
        return if (last?.title != null) {
            "$base - last: ${last.title} (${last.from}→${last.to})"
        } else {
            base
        }
    }

    // ---- quota: disk + bandwidth bars ----

    private fun mapQuota(section: QuotaSection?): SectionState<QuotaView> = when {
        section == null -> SectionState.Missing
        !section.ok -> SectionState.SectionError(section.error ?: "quota unavailable")
        else -> SectionState.Ok(
            QuotaView(
                diskPct = section.disk?.pct ?: 0.0,
                diskLabel = diskLabel(section.disk?.usedGb ?: 0.0, section.disk?.totalGb ?: 0.0),
                bandwidthUsedPct = section.bandwidth?.usedPct ?: 0.0,
                bandwidthLabel = bandwidthLabel(section.bandwidth?.usedPct ?: 0.0, section.bandwidth?.nextReset),
            ),
        )
    }

    /** "2073 / 2794 GB" - whole numbers print bare, fractional GB keeps one decimal. */
    fun diskLabel(usedGb: Double, totalGb: Double): String =
        "${formatGb(usedGb)} / ${formatGb(totalGb)} GB"

    private fun formatGb(v: Double): String =
        if (v == v.roundToLong().toDouble()) v.roundToLong().toString() else String.format(Locale.US, "%.1f", v)

    /** "3.4% used - resets Jul 28" (or without the reset clause if next_reset is absent/unparseable). */
    fun bandwidthLabel(usedPct: Double, nextReset: String?): String {
        val pctStr = String.format(Locale.US, "%.1f", usedPct)
        val resetStr = nextReset?.let { formatResetDate(it) }
        return if (resetStr != null) "$pctStr% used - resets $resetStr" else "$pctStr% used"
    }

    private fun formatResetDate(iso: String): String? = try {
        LocalDateTime.parse(iso).format(DateTimeFormatter.ofPattern("MMM d", Locale.US))
    } catch (e: DateTimeParseException) {
        null
    }

    // ---- kuma ----

    private fun mapKuma(section: KumaSection?): SectionState<KumaView> = when {
        section == null -> SectionState.Missing
        !section.ok -> SectionState.SectionError(section.error ?: "kuma unavailable")
        else -> SectionState.Ok(
            KumaView(
                summary = "${section.up ?: 0}/${section.total ?: 0} up",
                red = section.red.map { KumaRedView(it.name ?: "", it.msg ?: "", it.since ?: "") },
            ),
        )
    }

    // ---- streams ----

    private fun mapStreams(section: StreamsSection?): SectionState<StreamsView> = when {
        section == null -> SectionState.Missing
        !section.ok -> SectionState.SectionError(section.error ?: "streams unavailable")
        else -> SectionState.Ok(
            StreamsView(
                fraction = "${section.streams ?: 0}/${section.users ?: 0}",
                transcodes = section.transcodes ?: 0,
                wanKbps = section.wanKbps ?: 0,
            ),
        )
    }

    // ---- top5 ----

    private fun mapTop5(section: Top5Section?): SectionState<Top5View> = when {
        section == null -> SectionState.Missing
        !section.ok -> SectionState.SectionError(section.error ?: "top5 unavailable")
        else -> SectionState.Ok(
            Top5View(
                requests = section.requests30d.map { RequestEntryView(it.user ?: "", it.count ?: 0) },
                watch = section.watch30d.map {
                    WatchEntryView(it.user ?: "", formatHours(it.hours ?: 0.0), it.plays ?: 0)
                },
            ),
        )
    }

    /** "78.2 h" - one decimal, always the " h" suffix (already hours, not seconds, per the server contract). */
    fun formatHours(hours: Double): String = String.format(Locale.US, "%.1f h", hours)

    // ---- downloads ----

    private fun mapDownloads(section: DownloadsSection?): SectionState<DownloadsView> = when {
        section == null -> SectionState.Missing
        !section.ok -> SectionState.SectionError(section.error ?: "downloads unavailable")
        else -> SectionState.Ok(
            DownloadsView(
                qbitSummary = qbitSummary(section.qbit),
                sabSummary = sabSummary(section.sab),
                stuck = section.stuck.map {
                    StuckView(
                        hash8 = it.hash8 ?: "",
                        hoursLabel = formatStuckHours(it.hours ?: 0.0),
                        acted = it.acted ?: false,
                        rule = it.rule ?: "",
                    )
                },
                recentUnsticks = section.recentUnsticks.map {
                    UnstickView(ts = it.ts ?: "", hash8 = it.hash8 ?: "", result = it.result ?: "")
                },
            ),
        )
    }

    // ---- downloads: say WHICH client, and how fast ----
    //
    // The card used to render two bare, unlabelled lines:
    //
    //     2 total - 0 active - 2 seeding
    //     0 queued - 0.0/0.0 MB left
    //
    // Nothing said which line was torrents and which was Usenet, and neither
    // carried a transfer rate -- so the one question a glance is actually asking,
    // "is anything moving right now", could not be answered from this screen at
    // all. Each line now names its client and leads with the rate.

    /** Client display names. The protocol is the useful half for an operator
     *  ("is my Usenet path working"), the product name is what the box calls it. */
    const val QBIT_LABEL = "qBittorrent"
    const val SAB_LABEL = "SABnzbd"

    private fun qbitSummary(qbit: QbitStats?): String {
        if (qbit == null) return "$QBIT_LABEL (torrent): 0 total"
        val rate = formatRate(qbit.dlBps, qbit.upBps)
        return "$QBIT_LABEL (torrent): ${qbit.total ?: 0} total - " +
            "${qbit.active ?: 0} active - ${qbit.seeding ?: 0} seeding - $rate"
    }

    private fun sabSummary(sab: SabStats?): String {
        if (sab == null) return "$SAB_LABEL (usenet): 0 queued"
        val paused = if (sab.paused == true) " - PAUSED" else ""
        return "$SAB_LABEL (usenet): ${sab.queued ?: 0} queued - " +
            "${formatMb(sab.mbLeft ?: 0.0)}/${formatMb(sab.mbTotal ?: 0.0)} MB left - " +
            "${formatKbps(sab.kbps)}$paused"
    }

    /**
     * qBit rates, bytes/sec. Down is shown always; up only when non-zero, so a
     * pure-leeching line stays short.
     *
     * A null rate is NOT rendered as "0 B/s". Null means the box has not had the
     * collector redeployed and reported no rate at all; printing 0 would assert
     * "nothing is downloading", which is a different and possibly false claim.
     */
    fun formatRate(dlBps: Long?, upBps: Long?): String {
        if (dlBps == null && upBps == null) return "rate n/a"
        val down = formatBps(dlBps ?: 0L)
        val up = upBps ?: 0L
        return if (up > 0L) "$down down, ${formatBps(up)} up" else "$down down"
    }

    /** SAB reports kB/s directly. Same null-vs-zero rule as above. */
    fun formatKbps(kbps: Int?): String {
        if (kbps == null) return "rate n/a"
        return formatBps(kbps.toLong() * 1000L)
    }

    /** Decimal units, matching what qBit and SAB themselves display. */
    fun formatBps(bps: Long): String = when {
        bps <= 0L -> "0 B/s"
        bps < 1_000L -> "$bps B/s"
        bps < 1_000_000L -> String.format(Locale.US, "%.1f kB/s", bps / 1_000.0)
        bps < 1_000_000_000L -> String.format(Locale.US, "%.1f MB/s", bps / 1_000_000.0)
        else -> String.format(Locale.US, "%.2f GB/s", bps / 1_000_000_000.0)
    }

    private fun formatMb(v: Double): String = String.format(Locale.US, "%.1f", v)

    private fun formatStuckHours(hours: Double): String =
        if (hours == hours.roundToLong().toDouble()) {
            "${hours.roundToLong()}h"
        } else {
            String.format(Locale.US, "%.1fh", hours)
        }

    // ---- alerts: crit before warn, stable within each level ----

    fun orderAlerts(alerts: List<Alert>): List<AlertView> =
        alerts
            .map { AlertView(it.level ?: "warn", it.text ?: "") }
            .sortedBy { if (it.level == "crit") 0 else 1 }

    // ---- data age: "45s ago" / "2m ago" / "3h ago" / "1d ago" ----

    fun dataAge(generatedAt: String?, now: Instant): String {
        val ts = generatedAt?.let { runCatching { Instant.parse(it) }.getOrNull() } ?: return "unknown"
        val seconds = Duration.between(ts, now).seconds.coerceAtLeast(0)
        return when {
            seconds < 60 -> "${seconds}s ago"
            seconds < 3600 -> "${seconds / 60}m ago"
            seconds < 86_400 -> "${seconds / 3600}h ago"
            else -> "${seconds / 86_400}d ago"
        }
    }
}

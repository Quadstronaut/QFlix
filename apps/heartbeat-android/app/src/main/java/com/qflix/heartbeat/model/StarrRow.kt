package com.qflix.heartbeat.model

import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * One row of the stARR screen: one *arr instance's title-completion summary
 * plus disk usage, sourced from a SINGLE `starr` round trip (see
 * `ui/StarrScreen.kt` - the server deliberately fans out to all four
 * instances server-side so the phone never has to fire four verbs over a
 * flaky mobile link).
 *
 * Deliberately shaped to hold nothing that could carry a member identity:
 * slug + kind + coarse counts + a disk figure + a health flag. No per-title
 * requester, no watch/view data - [StarrRowTest]'s structural test enforces
 * that at the field-name level, not just by convention, so this class must
 * never grow a field whose name could plausibly hold one.
 */
data class StarrRow(
    val slug: String,
    val kind: String,
    val titleCount: Int,
    val completeCount: Int,
    val human: String,
    val ok: Boolean,
    val error: String,
) {
    companion object {
        /**
         * Walks `envelope.raw["arrs"]` (dispatch.py's `_verb_starr`: slug ->
         * `{peek, usage}` for each of the four ARR_SLUGS). A row's `ok` is
         * peek.ok AND usage.ok, mirroring the server's own per-slug degraded
         * check - so a *arr that answered peek but failed usage (or vice
         * versa) still renders as degraded rather than falsely healthy.
         *
         * An instance missing from `arrs` entirely (shouldn't happen given
         * the server's contract, but never trusted blindly) is simply absent
         * from the result rather than crashing the screen.
         */
        fun parseAll(envelope: Envelope): List<StarrRow> {
            val arrs = envelope.raw["arrs"]?.jsonObject ?: return emptyList()
            return arrs.entries.map { (slug, entry) ->
                val obj = entry.jsonObject
                val peek = obj["peek"]?.jsonObject
                val usage = obj["usage"]?.jsonObject

                val titles = peek?.get("titles")?.jsonArray ?: emptyList()
                val completeCount = titles.count { title ->
                    title.jsonObject["complete"]?.jsonPrimitive?.boolean == true
                }

                val peekOk = peek?.get("ok")?.jsonPrimitive?.boolean ?: false
                val usageOk = usage?.get("ok")?.jsonPrimitive?.boolean ?: false
                val peekError = peek?.get("error")?.jsonPrimitive?.contentOrNull.orEmpty()
                val usageError = usage?.get("error")?.jsonPrimitive?.contentOrNull.orEmpty()

                StarrRow(
                    slug = slug,
                    kind = peek?.get("kind")?.jsonPrimitive?.contentOrNull ?: "?",
                    titleCount = titles.size,
                    completeCount = completeCount,
                    human = usage?.get("human")?.jsonPrimitive?.contentOrNull ?: "0.0 B",
                    ok = peekOk && usageOk,
                    error = if (!peekOk) peekError else if (!usageOk) usageError else "",
                )
            }
        }
    }
}

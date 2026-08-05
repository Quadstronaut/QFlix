package com.qflix.heartbeat.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * The one shape every dispatcher verb returns, success or failure.
 *
 * Six fields are common to all 11 verbs. Three verbs attach extra top-level
 * keys (`status` -> doc, `starr` -> arrs, `quota` -> used_gb/total_gb/percent),
 * which is why [raw] is kept: screens reach those through it rather than
 * Envelope growing a nullable field per verb.
 *
 * `ok == false` is a NORMAL answer - a refused verb, an unreachable app, a
 * privacy allowlist rejection. It is data to render, never an exception.
 */
data class Envelope(
    val ok: Boolean,
    val verb: String,
    val target: String?,
    val verdict: String,
    val lines: List<String>,
    val elapsedS: Double,
    val raw: JsonObject,
) {
    companion object {
        private val json = Json { ignoreUnknownKeys = true; isLenient = true }

        /** Never throws; a malformed body is [Result.failure]. */
        fun parse(body: String): Result<Envelope> = runCatching {
            val o = json.parseToJsonElement(body).jsonObject
            Envelope(
                ok = o["ok"]!!.jsonPrimitive.content.toBooleanStrict(),
                verb = o["verb"]!!.jsonPrimitive.content,
                target = o["target"]?.jsonPrimitive?.contentOrNull,
                verdict = o["verdict"]?.jsonPrimitive?.contentOrNull ?: "",
                lines = o["lines"]?.jsonArray?.map { it.jsonPrimitive.content } ?: emptyList(),
                elapsedS = o["elapsed_s"]?.jsonPrimitive?.double ?: 0.0,
                raw = o,
            )
        }
    }
}

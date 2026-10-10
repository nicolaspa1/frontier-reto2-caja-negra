package cn

import io.vertx.core.json.JsonObject
import java.io.File

/** One line of experiments/log.jsonl: the request sent to the original service and the response it gave. */
data class LogLine(val n: Int, val section: String, val method: String, val path: String, val bodyBytes: ByteArray?, val contentType: String?, val status: Int, val body: String)

/** Reads the recorded log (`-Dlog.path`, default ../experiments/log.jsonl) used as the fidelity oracle. */
object LogCorpus {
    val file: File = File(System.getProperty("log.path") ?: "../experiments/log.jsonl")

    /** Lines before which another client (same token) reset the original; the replay inserts a synthetic reset there. */
    private val externalResets: Set<Int> = setOf(
        1727, // 14:21:40 ALTA;Lucia -> AC-0001 right after an E302 of the same battery: a differential fuzzer reset the service out of band.
    )

    /** Bodies compacted by the client (> 2000 chars) beyond reconstruction; covered by RobustnessTest instead. */
    private val compactedBodies: Set<Int> = setOf(
        3382, // 4096-byte truncation of a {"m":"PING;XX","p":"aaa..."} body (invalid JSON) -> E100
        3740, // 6000-byte valid body with a padding key -> 200 pong
        3741, // 8192-byte valid body with a padding key -> 200 pong
    )

    /** First log line recorded against version 2 of the original (incident of 15:55; the 15:55:17 monitor round, L5527-L5554, was still v1). */
    const val V2_FROM = 5555

    fun lines(): List<LogLine> = rawLines()
        .filter { it.status != 0 } // status 0 = network error recorded by the client, nothing to reproduce
        .filter { it.n !in compactedBodies }
        .flatMap { l ->
            if (l.n in externalResets)
                listOf(l.copy(method = "POST", path = "/__reset", bodyBytes = null, contentType = null, status = 200, body = "{\"ok\":true}"), l)
            else listOf(l)
        }

    private fun rawLines(): List<LogLine> = file.readLines(Charsets.UTF_8).filter { it.isNotBlank() }.map { raw ->
        val j = JsonObject(raw)
        val req = j.getJsonObject("req"); val res = j.getJsonObject("res")
        LogLine(
            n = j.getInteger("n"), section = j.getString("section", ""),
            method = req.getString("method"), path = req.getString("path"),
            bodyBytes = body(req), contentType = req.getJsonObject("headers")?.getString("Content-Type"),
            status = res.getInteger("status"), body = res.getString("body"),
        )
    }

    /** Rebuilds the request body: JSON object, raw string, or the synthetic {"m":"AAA..."} body of body_bytes bytes. */
    private fun body(req: JsonObject): ByteArray? {
        val b = req.getValue("body") ?: return null
        if (b is JsonObject) return b.encode().toByteArray(Charsets.UTF_8)
        val s = b.toString()
        val synthetic = s.startsWith("<cuerpo sintetico") || Regex("<A x \\d+>").containsMatchIn(s)
        if (synthetic) {
            val total = req.getInteger("body_bytes")
            return ("{\"m\":\"" + "A".repeat(total - 8) + "\"}").toByteArray(Charsets.UTF_8)
        }
        return s.toByteArray(Charsets.UTF_8)
    }

    /** Sections: every successful POST /__reset opens a new block (log sections without a reset continue the previous state). */
    fun sections(): List<List<LogLine>> = lines().fold(mutableListOf<MutableList<LogLine>>()) { acc, l ->
        if (l.path == "/__reset" && l.method == "POST" && l.status == 200 || acc.isEmpty()) acc.add(mutableListOf())
        acc.last().add(l); acc
    }

    /** Evaluator rule: JSON bodies compare semantically (key order ignored); anything else compares as exact text. */
    fun jsonEquals(a: String, b: String): Boolean = try { JsonObject(a) == JsonObject(b) } catch (e: Exception) { a == b }
}

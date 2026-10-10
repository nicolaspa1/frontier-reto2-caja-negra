package cn

import cn.adapters.json.VertxJsonCodec
import cn.domain.MsgPipeline
import kotlin.test.Test
import kotlin.test.assertTrue

/** Replays every POST /msg line of the log (valid token) against the pure domain and compares status + normalised JSON. */
class DomainFidelityTest {
    @Test
    fun domainReproducesRecordedLog() {
        val failures = mutableListOf<String>(); var total = 0; var byteMismatch = 0
        for (section in LogCorpus.sections()) {
            val policy = TestSupport.policyFor(section.first().n)
            val h = MsgPipeline(policy, VertxJsonCodec)
            for (l in section) {
                if (l.path == "/__reset" && l.status == 200) { h.reset(); continue }
                if (l.method != "POST" || l.path != "/msg" || l.status == 401) continue
                total++
                // Same rule as HttpVerticle: body > bodyMaxBytes -> E101 before parsing.
                val bytes = l.bodyBytes ?: ByteArray(0)
                val r = if (bytes.size > policy.http.bodyMaxBytes) policy.error("E101") else h.handle(bytes.toString(Charsets.UTF_8))
                if (r.status != l.status || !LogCorpus.jsonEquals(r.body, l.body)) {
                    failures += "L${l.n} [${l.section}] expected ${l.status} ${l.body} | got ${r.status} ${r.body}"
                } else if (r.body != l.body) byteMismatch++
            }
        }
        println("DomainFidelity: ${total - failures.size}/$total lines equal (${"%.1f".format(100.0 * (total - failures.size) / total)} %), $byteMismatch differ only in bytes")
        failures.forEach(::println)
        assertTrue(failures.isEmpty(), "Domain fidelity failures:\n" + failures.joinToString("\n"))
    }
}

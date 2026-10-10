package cn

import cn.TestSupport.text
import cn.adapters.http.HttpVerticle
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Replays ALL log lines over real HTTP (random port), including auth and routing (L151-L196). */
class HttpFidelityTest {
    private val token = TestSupport.TOKEN

    /** The log does not record the Authorization header: 401 lines rotate through the variants documented in the facts (11:02). */
    private fun authHeader(l: LogLine): String? = if (l.status != 401) "Bearer $token" else when (l.n % 3) {
        0 -> "Bearer ftr_foreign_token"
        1 -> null
        else -> "bearer $token"
    }

    private fun send(client: HttpClient, base: String, l: LogLine): HttpResponse<String> {
        val b = HttpRequest.newBuilder(URI.create(base + l.path))
        val body = l.bodyBytes?.let { HttpRequest.BodyPublishers.ofByteArray(it) } ?: HttpRequest.BodyPublishers.noBody()
        b.method(l.method, body)
        authHeader(l)?.let { b.header("Authorization", it) }
        l.contentType?.let { b.header("Content-Type", it) }
        return try {
            client.send(b.build(), HttpResponse.BodyHandlers.ofString(Charsets.UTF_8))
        } catch (e: java.io.IOException) {
            // keep-alive race (server closed the pooled connection): retry once on a fresh connection
            println("[http-fidelity] reconnect at L${l.n} after ${e.message}")
            TestSupport.newClient().send(b.build(), HttpResponse.BodyHandlers.ofString(Charsets.UTF_8))
        }
    }

    @Test
    fun httpReproducesRecordedLog() {
        val failures = mutableListOf<String>(); var total = 0
        // One server per policy version: lines before V2_FROM replay against v1, the rest against v2.
        for ((v2, lines) in LogCorpus.lines().groupBy { it.n >= LogCorpus.V2_FROM }) TestSupport.withServer(if (v2) TestSupport.policyV2() else TestSupport.policyV1()) { base ->
            val client = TestSupport.newClient()
            for (l in lines) {
                total++
                val r = send(client, base, l)
                if (r.statusCode() != l.status || !LogCorpus.jsonEquals(r.body(), l.body)) {
                    failures += "L${l.n} [${l.section}] ${l.method} ${l.path} expected ${l.status} ${l.body} | got ${r.statusCode()} ${r.body()}"
                }
                assertEquals("application/json; charset=utf-8", r.headers().firstValue("content-type").orElse(null), "L${l.n} content-type")
            }
        }
        println("HttpFidelity: ${total - failures.size}/$total lines equal (${"%.1f".format(100.0 * (total - failures.size) / total)} %)")
        failures.forEach(::println)
        assertTrue(failures.isEmpty(), "HTTP fidelity failures:\n" + failures.joinToString("\n"))
    }

    @Test
    fun routingAuthAndBodyLimitEdgeCases() = TestSupport.withServer { base ->
        val client = TestSupport.newClient()
        fun req(method: String, path: String, body: String? = null, auth: String? = "Bearer $token") =
            TestSupport.send(client, base, method, path, body?.toByteArray(Charsets.UTF_8), auth)
        assertEquals(200, req("GET", HttpVerticle.DEFAULT_HEALTH_PATH, auth = null).statusCode())
        assertEquals(404, req("GET", "/__up", auth = null).statusCode())
        assertEquals(404, req("GET", "/__up-k7f3a9c2/", auth = null).statusCode())
        assertEquals(404, req("HEAD", "/health").statusCode())
        assertEquals(404, req("GET", "/health/").statusCode())
        assertEquals(401, req("GET", "/health", auth = "Bearer  $token").statusCode())
        assertEquals(401, req("GET", "/health", auth = token).statusCode())
        val bad = req("POST", "/msg", "{\"m\":")
        assertEquals(400, bad.statusCode()); assertEquals("{\"error\":\"E100\",\"mensaje\":\"json inválido\"}", bad.text())
        val empty = req("POST", "/msg", "")
        assertEquals(400, empty.statusCode()); assertEquals("E100", io.vertx.core.json.JsonObject(empty.text()).getString("error"))
        val big = req("POST", "/msg", "{\"m\":\"" + "A".repeat(3_000_000) + "\"}")
        assertEquals(413, big.statusCode())
        // literal UTF-8 accents in the body
        val e202 = req("POST", "/msg", "{\"m\":\"ALTA;Lucia;83\"}")
        assertTrue(e202.text().contains("inválido"), e202.text())
    }
}

package cn

import cn.TestSupport.text
import cn.domain.Iso7064
import io.vertx.core.json.JsonObject
import java.net.http.HttpResponse
import java.util.Collections
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * 50 parallel clients against the in-process server, mixing /__reset and /msg.
 * Oracles: no 5xx, every body is policy JSON, `x-req` values are unique (requests were serialised),
 * sequential ids are handed out exactly once under contention, and a deterministic script after the storm
 * produces exactly the expected ledger (no residue across resets).
 */
class ConcurrencyTest {
    private val clients = 50
    private val requestsPerClient = 40

    private fun m(payload: String) = "$payload;${Iso7064.control(payload)}"

    private fun <T> parallel(n: Int, task: (Int) -> T): List<T> {
        val pool = Executors.newFixedThreadPool(n)
        try {
            val futures = (0 until n).map { i -> pool.submit(Callable { task(i) }) }
            return futures.map { it.get(60, TimeUnit.SECONDS) }
        } finally { pool.shutdownNow() }
    }

    @Test
    fun parallelClientsNeverSee5xxAndStateStaysConsistent() = TestSupport.withServer { base ->
        val policy = TestSupport.policyV1()
        val knownStatuses = (policy.errors.values + policy.responses.values).map { it.status }.toSet()
        val xreq = Collections.synchronizedSet(HashSet<String>())
        val problems = Collections.synchronizedList(mutableListOf<String>())

        fun record(r: HttpResponse<ByteArray>, what: String) {
            val body = r.text()
            if (r.statusCode() >= 500) problems += "5xx for $what: ${r.statusCode()} $body"
            if (r.statusCode() !in knownStatuses) problems += "unknown status for $what: ${r.statusCode()}"
            try { JsonObject(body) } catch (e: Exception) { problems += "non-JSON body for $what: $body" }
            val id = r.headers().firstValue("x-req").orElse(null)
            if (id == null || !xreq.add(id)) problems += "missing or duplicated x-req '$id' for $what"
        }

        // Phase 1: storm of mixed resets, commands and health checks.
        val total = parallel(clients) { c ->
            val client = TestSupport.newClient()
            val r = Random(1000L + c)
            repeat(requestsPerClient) {
                when (r.nextInt(10)) {
                    0, 1 -> record(TestSupport.send(client, base, "POST", "/__reset"), "reset")
                    2 -> record(TestSupport.send(client, base, "GET", "/health"), "health")
                    else -> {
                        val msg = listOf(m("ALTA;Lucia"), m("ALTA;Mateo"), m("CONSULTA;AC-0001"), m("DEPOSITO;AC-0001;250000"),
                            m("TRANSFER;AC-0001;AC-0002;100000"), m("TRANSFER;AC-0001;AC-0002;100001"), m("ANULA;OP-0001"), "PING;64", "NOPE;1", m("HALT;AC-0001")).random(r)
                        record(TestSupport.postMsg(client, base, msg), "msg $msg")
                    }
                }
            }
            requestsPerClient
        }.sum()
        assertTrue(problems.isEmpty(), problems.take(20).joinToString("\n"))
        assertEquals(total, xreq.size, "every one of the $total responses must carry a distinct x-req")

        // Phase 2: id allocation under contention is exactly-once.
        val setup = TestSupport.newClient()
        assertEquals(200, TestSupport.send(setup, base, "POST", "/__reset").statusCode())
        val ids = parallel(clients) { _ ->
            val client = TestSupport.newClient()
            List(20) { JsonObject(TestSupport.postMsg(client, base, "ALTA;Lucia;82").text()).getString("cuentaId") }
        }.flatten()
        assertEquals((1..clients * 20).map { "AC-" + it.toString().padStart(4, '0') }.toSet(), ids.toSet())
        assertEquals(clients * 20, ids.size)

        // Phase 3: a deterministic script after the storm matches the policy byte for byte (no residue across resets).
        assertEquals(200, TestSupport.send(setup, base, "POST", "/__reset").statusCode())
        fun send(msg: String) = TestSupport.postMsg(setup, base, msg).text()
        assertEquals("{\"cuentaId\":\"AC-0001\"}", send("ALTA;Lucia;82"))
        assertEquals("{\"cuentaId\":\"AC-0002\"}", send("ALTA;Mateo;3"))
        assertEquals("{\"status\":\"recieved\",\"operacionId\":\"OP-0001\"}", send("DEPOSITO;AC-0001;250000;86"))
        assertEquals("{\"status\":\"recieved\",\"operacionId\":\"OP-0002\"}", send(m("TRANSFER;AC-0001;AC-0002;100000")))
        assertEquals("{\"cuenta_id\":\"AC-0002\",\"saldo\":100000}", send(m("CONSULTA;AC-0002")))
        assertEquals("{\"ok\":true,\"anulada\":\"OP-0002\"}", send(m("ANULA;OP-0002")))
        assertEquals("{\"ok\":false,\"motivo\":\"ya anulada\"}", send(m("ANULA;OP-0002")))
        assertEquals("{\"cuenta_id\":\"AC-0002\",\"saldo\":0}", send(m("CONSULTA;AC-0002")))
        assertEquals("{\"cuenta_id\":\"AC-0001\",\"saldo\":250000}", send("CONSULTA;AC-0001;14"))
        println("Concurrency: $clients clients, ${total + clients * 20} requests, no 5xx, ${xreq.size} distinct x-req")
    }
}

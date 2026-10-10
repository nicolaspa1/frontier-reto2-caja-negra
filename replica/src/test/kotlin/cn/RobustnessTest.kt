package cn

import cn.TestSupport.text
import cn.adapters.json.VertxJsonCodec
import cn.domain.Iso7064
import cn.domain.MsgPipeline
import cn.domain.Policy
import io.vertx.core.json.JsonObject
import java.io.IOException
import java.net.URI
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import kotlin.test.fail

/**
 * "Never 5xx" fuzzing: 500 seeded hostile messages plus raw envelope garbage, through the domain and over real HTTP.
 * Oracles: no exception escapes, status < 500, body is policy JSON, a well-formed envelope never yields E100.
 */
class RobustnessTest {
    private val seed = 20261010L
    private val messageCount = 500

    // ---- generators ------------------------------------------------------------------------------------------

    private val validMessages = listOf("PING;64", "ALTA;Lucia;82", "ALTA;Mateo;3", "CONSULTA;AC-0001;14", "DEPOSITO;AC-0001;250000;86",
        "TRANSFER;AC-0001;AC-0002;100000;${Iso7064.control("TRANSFER;AC-0001;AC-0002;100000")}", "ANULA;OP-0001;${Iso7064.control("ANULA;OP-0001")}",
        "HALT;AC-0001;82", "NOTAS;11")
    private val commands = listOf("PING", "ALTA", "CONSULTA", "DEPOSITO", "TRANSFER", "ANULA", "HALT", "NOTAS", "ping", "Alta", "TRANSFERIR", "")
    private val numbers = listOf("0", "1", "-1", "+5", "00", "007", "1.5", "1e5", "0x10", " 5", "5 ", "9999999999", "10000000000", "100000", "100001",
        "9223372036854775807", "9223372036854775808", "-9223372036854775808", "99999999999999999999999999999999", "9".repeat(400), "١٢٣", "６４", "NaN", "Infinity")
    private val controls = listOf("0", "00", "3", "03", "64", "82", "99", "100", "-1", "", " 64", "64 ", "6 4", "٦٤", "６４", "XX", "9".repeat(50), "\u0000", "64\n")
    private val unicodeBits = listOf("😀", "é", "ñ", "ß", "İ", "\u0301", "\u200B", "\uFEFF", "\u202E", "漢字", "🇦🇷", "\uD800", "\uDC00", "\uD83D\uDE00", "\u0000", "\u001F", "\u007F", "\t", "\r\n", "Ⅻ", "𝟙𝟚")

    private fun randomBytesMessage(r: Random): String = String(CharArray(r.nextInt(0, 200)) { r.nextInt(0, 256).toChar() })
    private fun randomUnicodeMessage(r: Random): String = (0 until r.nextInt(1, 40)).joinToString("") { if (r.nextBoolean()) unicodeBits.random(r) else if (r.nextBoolean()) ";" else "A" }
    private fun veryLongMessage(r: Random): String = when (r.nextInt(4)) {
        0 -> "A".repeat(r.nextInt(8_000, 20_000)) + ";12"
        1 -> "ALTA;" + "x".repeat(r.nextInt(8_000, 20_000)) + ";5"
        2 -> ";".repeat(r.nextInt(2_000, 9_000))
        else -> (0 until r.nextInt(500, 3_000)).joinToString(";") { commands.random(r) }
    }
    private fun nestedSeparators(r: Random): String = listOf(";;;", "A;;B;;C", ";PING;64;", "PING;64;", ";64", ";", ";;", "PING;;64", ";;PING;;64;;",
        "ALTA;;82", "TRANSFER;AC-0001;;100;1", "a;b;c;d;e;f;g;h;i;j;k;12", "||", "ALTA|Lucia|82", "PING,64", "PING 64", "PING;64;;").random(r)
    private fun hugeNumbers(r: Random): String = when (r.nextInt(3)) {
        0 -> "DEPOSITO;AC-0001;${numbers.random(r)}"
        1 -> "TRANSFER;AC-0001;AC-0002;${numbers.random(r)}"
        else -> "CONSULTA;AC-${numbers.random(r)}"
    }.let { "$it;${if (r.nextBoolean()) Iso7064.control(it).toString() else controls.random(r)}" }
    private fun controlEdge(r: Random): String = "${validMessages.random(r).substringBeforeLast(';')};${controls.random(r)}"
    private fun mutated(r: Random): String {
        val sb = StringBuilder(validMessages.random(r))
        repeat(r.nextInt(1, 4)) {
            if (sb.isEmpty()) { sb.append(unicodeBits.random(r)); return@repeat }
            val i = r.nextInt(sb.length)
            when (r.nextInt(5)) {
                0 -> sb.deleteCharAt(i)
                1 -> sb.insert(i, if (r.nextBoolean()) ";" else unicodeBits.random(r))
                2 -> sb.setCharAt(i, r.nextInt(0x20, 0x7F).toChar())
                3 -> sb.setCharAt(i, if (sb[i].isUpperCase()) sb[i].lowercaseChar() else sb[i].uppercaseChar())
                else -> sb.insert(i, sb.substring(0, i))
            }
        }
        return sb.toString()
    }

    /** 500 deterministic messages spread across the generator families. */
    fun messages(): List<String> {
        val r = Random(seed)
        val gens = listOf(::randomBytesMessage, ::randomUnicodeMessage, ::veryLongMessage, ::nestedSeparators, ::hugeNumbers, ::controlEdge, ::mutated)
        return List(messageCount) { i -> gens[i % gens.size](r) }
    }

    /** Raw envelope bodies that are not `{"m": <string>}`. */
    fun rawBodies(): List<ByteArray> {
        val text = listOf("", " ", "\n", "{", "}", "[]", "[1]", "{}", "null", "true", "123", "\"PING;64\"", "{\"m\":123}", "{\"m\":null}", "{\"m\":[\"PING;64\"]}",
            "{\"m\":{\"m\":\"PING;64\"}}", "{\"M\":\"PING;64\"}", "{\"m\":\"PING;64\"}garbage", "{\"m\":\"PING;64\",\"m\":\"X\"}", "{\"x\":1e999999,\"m\":\"PING;64\"}",
            "{\"x\":${"9".repeat(5000)},\"m\":\"PING;64\"}", "\uFEFF{\"m\":\"PING;64\"}", "{\"m\":\"\\ud800;64\"}", "{\"m\":\"\\u0000;64\"}", "{\"m\":\"PING;64\\",
            "{\"m\":\"PING;64\"", "{'m':'PING;64'}", "m=PING;64", "<xml/>", "[" .repeat(4000) + "]".repeat(4000), "{\"a\":".repeat(1500) + "1" + "}".repeat(1500),
            "{\"m\":" + "[".repeat(3000) + "]".repeat(3000) + "}", "{\"m\":\"" + "A".repeat(8_170) + "\"}", "{\"m\":\"" + "A".repeat(8_200) + "\"}")
        val bytes = listOf(byteArrayOf(0x7B, 0x22, 0x6D, 0x22, 0x3A, 0x22, 0xFF.toByte(), 0xFE.toByte(), 0x22, 0x7D),   // {"m":"<invalid utf8>"}
            "{\"m\":\"PING\u0000;64\"}".toByteArray(), byteArrayOf(0x00, 0x00, 0x00), byteArrayOf(0xC3.toByte(), 0x28),
            ByteArray(300) { it.toByte() })
        return text.map { it.toByteArray(Charsets.UTF_8) } + bytes
    }

    // ---- oracles --------------------------------------------------------------------------------------------

    private fun knownStatuses(p: Policy) = (p.errors.values + p.responses.values).map { it.status }.toSet()

    /** Error codes a reply may carry: every `errors` key plus codes embedded in fixed responses (HALT answers 409 E520). */
    private fun knownErrorCodes(p: Policy): Set<String> = p.errors.keys +
        p.responses.values.mapNotNull { r -> try { JsonObject(r.body).getString("error") } catch (e: Exception) { null } }

    private fun assertWellFormed(p: Policy, status: Int, body: String, what: String) {
        assertTrue(status < 500, "5xx for $what: $status $body")
        assertTrue(status in knownStatuses(p), "status $status not defined by the policy for $what")
        val json = try { JsonObject(body) } catch (e: Exception) { fail("body is not JSON for $what: $body") }
        json.getString("error")?.let { code -> assertTrue(code in knownErrorCodes(p), "unknown error code $code for $what") }
    }

    @Test
    fun domainSurvivesHostileMessages() {
        val policy = TestSupport.policyV1()
        val pipeline = MsgPipeline(policy, VertxJsonCodec)
        val e100 = policy.error("E100")
        var count = 0
        for (m in messages()) {
            val envelope = JsonObject().put("m", m).encode()
            val r = try { pipeline.handle(envelope) } catch (t: Throwable) { fail("exception escaped for message ${m.take(80)}: $t") }
            assertWellFormed(policy, r.status, r.body, "message ${m.take(80).replace("\n", "\\n")}")
            assertTrue(r != e100, "well-formed envelope answered E100 (swallowed exception) for message ${m.take(80)}")
            count++
        }
        for (raw in rawBodies()) {
            val r = try { pipeline.handle(raw.toString(Charsets.UTF_8)) } catch (t: Throwable) { fail("exception escaped for raw body ${raw.size}B: $t") }
            assertWellFormed(policy, r.status, r.body, "raw body ${raw.toString(Charsets.UTF_8).take(60)}")
            count++
        }
        // The pipeline is still healthy afterwards.
        pipeline.reset()
        assertEquals(policy.response("PING"), pipeline.handle("{\"m\":\"PING;64\"}"))
        assertEquals(201, pipeline.handle("{\"m\":\"ALTA;Lucia;82\"}").status)
        println("Robustness(domain): $count inputs, no exception, no 5xx")
    }

    @Test
    fun httpSurvivesHostileRequests() = TestSupport.withServer { base ->
        val policy = TestSupport.policyV1()
        val client = TestSupport.newClient()
        var count = 0
        fun check(r: HttpResponse<ByteArray>, what: String) {
            assertWellFormed(policy, r.statusCode(), r.text(), what)
            assertEquals("application/json; charset=utf-8", r.headers().firstValue("content-type").orElse(null), "content-type for $what")
            assertTrue(r.headers().firstValue("x-req").isPresent, "x-req header for $what")
            count++
        }
        for (m in messages()) check(TestSupport.postMsg(client, base, m), "message ${m.take(60).replace("\n", "\\n")}")
        for (raw in rawBodies()) check(TestSupport.send(client, base, "POST", "/msg", raw), "raw body ${raw.size}B")
        // Oversized bodies: declared (Content-Length) and streamed (chunked, no Content-Length).
        assertEquals(413, TestSupport.send(client, base, "POST", "/msg", ByteArray(3_000_000) { 'A'.code.toByte() }).statusCode())
        val chunked = HttpRequest.newBuilder(URI.create("$base/msg")).header("Authorization", "Bearer ${TestSupport.TOKEN}")
            .POST(HttpRequest.BodyPublishers.ofInputStream { ByteArray(20_000) { 'A'.code.toByte() }.inputStream() }).build()
        assertEquals(413, client.send(chunked, HttpResponse.BodyHandlers.ofString()).statusCode())
        val hugeChunked = HttpRequest.newBuilder(URI.create("$base/msg")).header("Authorization", "Bearer ${TestSupport.TOKEN}")
            .POST(HttpRequest.BodyPublishers.ofInputStream { ByteArray(3_000_000) { 'A'.code.toByte() }.inputStream() }).build()
        try { assertEquals(413, client.send(hugeChunked, HttpResponse.BodyHandlers.ofString()).statusCode()) }
        catch (e: IOException) { /* the server rejected early and closed the connection while we were still sending: acceptable */ }
        // Odd routing/method combinations never leave the policy's error set.
        for ((method, path) in listOf("GET" to "/", "PUT" to "/msg", "DELETE" to "/__reset", "PATCH" to "/health", "GET" to "/msg?x=1", "POST" to "/msg/", "POST" to "/MSG", "GET" to "/%6dsg", "GET" to "/a/../health"))
            check(TestSupport.send(client, base, method, path, "{\"m\":\"PING;64\"}".toByteArray(), headers = emptyMap()), "$method $path")
        // Still healthy afterwards.
        assertEquals(200, TestSupport.send(client, base, "POST", "/__reset").statusCode())
        assertEquals(policy.response("PING").body, TestSupport.postMsg(client, base, "PING;64").text())
        assertEquals("{\"cuentaId\":\"AC-0001\"}", TestSupport.postMsg(client, base, "ALTA;Lucia;82").text())
        println("Robustness(http): $count requests, no 5xx")
    }

    /** Large but valid bodies with an extra padding key are still parsed: 6000 and exactly 8192 bytes -> 200 pong (L3740, L3741). */
    @Test
    fun largeValidBodyWithPaddingKeyIsAccepted() = TestSupport.withServer<Unit> { base ->
        val client = TestSupport.newClient()
        for (total in listOf(6000, 8192)) {
            val head = "{\"m\":\"PING;XX\",\"p\":\""
            val body = head + "a".repeat(total - head.length - 2) + "\"}"
            assertEquals(total, body.toByteArray(Charsets.UTF_8).size)
            val r = TestSupport.send(client, base, "POST", "/msg", body.toByteArray(Charsets.UTF_8))
            assertEquals(200, r.statusCode(), "bytes=$total"); assertEquals("{\"pong\":true}", r.text())
        }
        // A body cut in the middle of a string is invalid JSON -> E100 (L3382).
        val cut = ("{\"m\":\"PING;XX\",\"p\":\"" + "a".repeat(5000)).take(4096)
        val r = TestSupport.send(client, base, "POST", "/msg", cut.toByteArray(Charsets.UTF_8))
        assertEquals(400, r.statusCode()); assertTrue(r.text().contains("E100"), r.text())
    }
}

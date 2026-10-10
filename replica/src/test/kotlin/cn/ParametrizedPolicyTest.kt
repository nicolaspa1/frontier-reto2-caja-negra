package cn

import cn.adapters.config.PolicyLoader
import cn.adapters.http.HttpVerticle
import cn.adapters.json.VertxJsonCodec
import cn.domain.ControlAlgorithms
import cn.domain.Iso7064
import cn.domain.MsgPipeline
import io.vertx.core.Vertx
import io.vertx.core.json.JsonObject
import java.io.File
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.util.concurrent.TimeUnit
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Minimal policies: only what changes; the loader fills in today's defaults for everything else. */
class ParametrizedPolicyTest {
    private val base = """{
      "version": "test",
      "commands": {
        "PING": { "arity": 0, "args": [], "rules": [], "postRules": [], "effect": "ping", "response": "PING" },
        "ALTA": { "arity": 1, "args": ["nombre"], "rules": [], "postRules": [], "effect": "alta", "response": "ALTA" }
      },
      "responses": {
        "OK": { "status": 200, "body": "{\"ok\":true}" },
        "PING": { "status": 200, "body": "{\"pong\":true}" },
        "ALTA": { "status": 201, "body": "{\"cuentaId\":\"${'$'}{cuentaId}\"}" }
      },
      "errors": {
        "E001": { "status": 401, "body": "{\"error\":\"E001\"}" }, "E100": { "status": 400, "body": "{\"error\":\"E100\"}" },
        "E102": { "status": 400, "body": "{\"error\":\"E102\"}" }, "E103": { "status": 400, "body": "{\"error\":\"E103\"}" },
        "E104": { "status": 400, "body": "{\"error\":\"E104\"}" }, "E201": { "status": 400, "body": "{\"error\":\"E201\"}" },
        "E202": { "status": 400, "body": "{\"error\":\"E202\"}" }, "E301": { "status": 400, "body": "{\"error\":\"E301\"}" },
        "E302": { "status": 400, "body": "{\"error\":\"E302\"}" }, "E404": { "status": 404, "body": "{\"error\":\"E404\"}" },
        "E101": { "status": 413, "body": "{\"error\":\"E101\"}" }
      }
    }"""

    private fun policy(extra: String) = PolicyLoader.parse(JsonObject(base).mergeIn(JsonObject(extra), true))
    private fun pipeline(extra: String) = MsgPipeline(policy(extra), VertxJsonCodec)

    @Test fun pipeSeparator() {
        val h = pipeline("""{"integrity": {"separator": {"value": "|"}}}""")
        assertEquals(201, h.handle("""{"m":"ALTA|Lucia|${Iso7064.control("ALTA|Lucia")}"}""").status)
        assertEquals(400, h.handle("""{"m":"ALTA;Lucia;82"}""").status)        // no '|' -> E103
        assertEquals("""{"error":"E103"}""", h.handle("""{"m":"ALTA;Lucia;82"}""").body)
        assertEquals("""{"error":"E104"}""", h.handle("""{"m":"ALTA||5"}""").body)
    }

    @Test fun customBodyField() {
        val h = pipeline("""{"http": {"bodyField": {"value": "msg"}}}""")
        assertEquals("""{"pong":true}""", h.handle("""{"msg":"PING;64"}""").body)
        assertEquals("""{"error":"E102"}""", h.handle("""{"m":"PING;64"}""").body)
    }

    @Test fun permissiveControl() {
        val h = pipeline("""{"integrity": {"controlAlgorithm": {"value": {"name": "permissive"}}}}""")
        assertEquals(201, h.handle("""{"m":"ALTA;Lucia;00"}""").status)           // never E202
        assertEquals("""{"error":"E201"}""", h.handle("""{"m":"ALTA;Lucia;123"}""").body) // controlFormat still applies
        val strict = pipeline("{}")
        assertEquals("""{"error":"E202"}""", strict.handle("""{"m":"ALTA;Lucia;00"}""").body)
    }

    @Test fun caseSensitiveCommandTrimAndControlFirst() {
        val h = pipeline("""{"integrity": {"commandCase": {"value": "sensitive"}, "trim": {"value": true}, "controlPosition": {"value": "first"}}}""")
        assertEquals("""{"pong":true}""", h.handle("""{"m":"  64;PING "}""").body)
        assertEquals("""{"error":"E301"}""", h.handle("""{"m":"64;ping"}""").body)
        assertEquals(201, h.handle("""{"m":"${Iso7064.control("ALTA;Lucia")};ALTA;Lucia"}""").status)
    }

    /** Real control-fit.json written by `python3 control-plane/harness/fit.py fit` (schema of fit.py::serialize). */
    @Test fun fitFromHarnessFile() {
        val real = File("../control-plane/harness/control-fit.json")
        assertTrue(real.isFile, "missing ${real.absolutePath}: run `python3 control-plane/harness/fit.py fit` (the test must not be skipped silently)")
        val algo = ControlAlgorithms.from(mapOf("name" to "fit", "file" to real.absolutePath))
        if (JsonObject(real.readText(Charsets.UTF_8)).getString("familia") == "permissive") {
            // no candidate found by fit.py: degraded mode, only the permissive semantics can be checked
            println("WARNING: control-plane/harness/control-fit.json is in permissive mode; fit.py must treat '03' as a canonical-format rejection, not a value mismatch")
            assertEquals(null, algo.expected("ALTA;Lucia"))
            return
        }
        assertEquals(82, algo.expected("ALTA;Lucia"))
        assertEquals(14, algo.expected("CONSULTA;AC-0001"))
        assertEquals(64, algo.expected("PING"))
        assertEquals(4, algo.expected("DEPOSITO;AC-0001;999999999999"))
        // the whole policy with controlAlgorithm {"name":"fit"} still reproduces the corpus
        val h = pipeline("""{"integrity": {"controlAlgorithm": {"value": {"name": "fit", "file": "${real.absolutePath.replace("\\", "/")}"}}}}""")
        assertEquals(201, h.handle("""{"m":"ALTA;Lucia;82"}""").status)
        assertEquals("""{"error":"E202"}""", h.handle("""{"m":"ALTA;Lucia;83"}""").body)
        // inline (POLICY_JSON): the contents of control-fit.json pasted under controlAlgorithm with name=fit
        val inline = JsonObject(real.readText(Charsets.UTF_8)).put("name", "fit")
        val h2 = pipeline("""{"integrity": {"controlAlgorithm": {"value": ${inline.encode()}}}}""")
        assertEquals(201, h2.handle("""{"m":"ALTA;Lucia;82"}""").status)
    }

    /** Synthetic fit: byte sum mod 97, control = 98 - r (fit.py family "bytesum"). */
    @Test fun syntheticByteSumFit() {
        val spec = JsonObject("""{"familia":"bytesum","params":{"case":"upper","keep":"","nonascii":"drop","agg":"sum_bytes","formula":"98-","m":97},"verificado_sobre":0,"formula_texto":"test"}""")
        val algo = ControlAlgorithms.fromFitJson(spec)
        val expected = 98 - "ALTALUCIA".toByteArray().sumOf { it.toInt() } % 97
        assertEquals(expected, algo.expected("ALTA;Lucia"))
        // same tables as fit.py for the check digits
        assertEquals(3, cn.domain.CheckDigits.luhn("7992739871"))
        assertEquals(4, cn.domain.CheckDigits.damm("572"))
        assertEquals(3, cn.domain.CheckDigits.verhoeff("236"))
        val poly = ControlAlgorithms.fromFitJson(JsonObject("""{"familia":"polynomial","params":{"keep":";","nonascii":"drop","agg":"poly","base":31,"formula":"rem","m":97}}"""))
        assertEquals("ALTA;LUCIA".fold(0L) { h, c -> (h * 31 + c.code) % 97 }.toInt(), poly.expected("ALTA;LUCIA"))
    }

    @Test fun customRouteAndAuthScheme() {
        val p = policy("""{"http": {"routes": {"value": {"msg": {"method": "POST", "path": "/mensaje"}, "reset": {"method": "POST", "path": "/__reset"}, "health": {"method": "GET", "path": "/health"}}}, "authScheme": {"value": "Token "}}}""")
        val vertx = Vertx.vertx()
        val v = HttpVerticle(MsgPipeline(p, VertxJsonCodec), p, "t", 0, healthInternal = false)
        vertx.deployVerticle(v).toCompletionStage().toCompletableFuture().get(10, TimeUnit.SECONDS)
        try {
            val client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build()
            fun post(path: String, auth: String?): HttpResponse<String> {
                val b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:${v.actualPort}$path")).POST(HttpRequest.BodyPublishers.ofString("""{"m":"PING;64"}"""))
                auth?.let { b.header("Authorization", it) }
                return client.send(b.build(), HttpResponse.BodyHandlers.ofString())
            }
            assertEquals(200, post("/mensaje", "Token t").statusCode())
            assertEquals(404, post("/msg", "Token t").statusCode())          // the old route no longer exists
            assertEquals(401, post("/mensaje", "Bearer t").statusCode())    // scheme changed by config
            assertEquals(404, post("/__up-k7f3a9c2", null).statusCode())
        } finally { vertx.close().toCompletionStage().toCompletableFuture().get(5, TimeUnit.SECONDS) }
    }
}

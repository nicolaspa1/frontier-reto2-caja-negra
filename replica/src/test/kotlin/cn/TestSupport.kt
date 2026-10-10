package cn

import cn.adapters.config.PolicyLoader
import cn.adapters.http.HttpVerticle
import cn.adapters.json.VertxJsonCodec
import cn.domain.MsgPipeline
import cn.domain.Policy
import io.vertx.core.Vertx
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.util.concurrent.TimeUnit

/** Shared helpers: an in-process server on a random port and a tiny HTTP client wrapper. */
object TestSupport {
    const val TOKEN = "ftr_test_token"

    fun policyV1(): Policy = PolicyLoader.load("v1", null)

    /** Deploys one HttpVerticle (random port, internal health enabled) and runs [block] against its base URL. */
    fun <T> withServer(policy: Policy = policyV1(), token: String = TOKEN, block: (base: String) -> T): T {
        val vertx = Vertx.vertx()
        val verticle = HttpVerticle(MsgPipeline(policy, VertxJsonCodec), policy, token, 0, healthInternal = true)
        vertx.deployVerticle(verticle).toCompletionStage().toCompletableFuture().get(10, TimeUnit.SECONDS)
        try { return block("http://127.0.0.1:${verticle.actualPort}") }
        finally { vertx.close().toCompletionStage().toCompletableFuture().get(5, TimeUnit.SECONDS) }
    }

    fun newClient(): HttpClient = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build()

    fun send(client: HttpClient, base: String, method: String, path: String, body: ByteArray? = null, auth: String? = "Bearer $TOKEN", headers: Map<String, String> = emptyMap()): HttpResponse<ByteArray> {
        val b = HttpRequest.newBuilder(URI.create(base + path))
            .method(method, body?.let { HttpRequest.BodyPublishers.ofByteArray(it) } ?: HttpRequest.BodyPublishers.noBody())
        auth?.let { b.header("Authorization", it) }
        headers.forEach { (k, v) -> b.header(k, v) }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofByteArray())
    }

    fun postMsg(client: HttpClient, base: String, m: String): HttpResponse<ByteArray> =
        send(client, base, "POST", "/msg", io.vertx.core.json.JsonObject().put("m", m).encode().toByteArray(Charsets.UTF_8))

    fun HttpResponse<ByteArray>.text(): String = body().toString(Charsets.UTF_8)
}

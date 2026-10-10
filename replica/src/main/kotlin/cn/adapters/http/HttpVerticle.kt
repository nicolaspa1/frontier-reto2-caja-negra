package cn.adapters.http

import cn.domain.MessageHandler
import cn.domain.Policy
import cn.domain.Response
import io.vertx.core.AbstractVerticle
import io.vertx.core.Promise
import io.vertx.core.buffer.Buffer
import io.vertx.core.http.HttpServer
import io.vertx.core.http.HttpServerOptions
import io.vertx.core.http.HttpServerRequest
import io.vertx.ext.web.Router
import io.vertx.ext.web.RoutingContext

/**
 * HTTP adapter. One verticle = one event loop: `handle()`/`reset()` run serialised, the ledger needs no locking.
 * Route match (path + method) comes BEFORE auth: unknown route -> E404 regardless of token (L151-L196).
 * Never 5xx: every failure path ends in a policy-defined 4xx.
 */
class HttpVerticle(
    private val handler: MessageHandler,
    private val policy: Policy,
    private val token: String,
    private val port: Int,
    private val healthInternal: Boolean,
    private val healthPath: String = DEFAULT_HEALTH_PATH,
) : AbstractVerticle() {
    var actualPort: Int = -1; private set
    private var reqCounter = 0L
    private lateinit var server: HttpServer

    override fun start(startPromise: Promise<Void>) {
        val router = Router.router(vertx)
        router.route().handler(::dispatch)
        // Statuses Vert.x Web may produce on its own (no matching route for "*" paths, bad request line, handler exception).
        router.errorHandler(404) { it.replyOnce(policy.error(policy.http.unknownRouteError)) }
        router.errorHandler(400) { it.replyOnce(policy.error("E100")) }
        router.errorHandler(500) { it.replyOnce(policy.error("E100")) }
        server = vertx.createHttpServer(HttpServerOptions().setHandle100ContinueAutomatically(true).setPort(port))
        server.requestHandler(router).listen().onSuccess { actualPort = it.actualPort(); startPromise.complete() }.onFailure(startPromise::fail)
    }

    private fun RoutingContext.reply(r: Response) {
        response().setStatusCode(r.status)
            .putHeader("content-type", "application/json; charset=utf-8")
            .putHeader("x-req", (++reqCounter).toString())
            .end(Buffer.buffer(r.body.toByteArray(Charsets.UTF_8)))
    }

    /** Reply unless something already answered this request (error handlers may run after a partial write). */
    private fun RoutingContext.replyOnce(r: Response) { if (!response().ended() && !response().headWritten()) reply(r) }

    private fun authed(req: HttpServerRequest) = req.getHeader("Authorization") == policy.http.authScheme + token

    /** Routes, methods, auth scheme and codes come from policy.http; only the order route -> auth -> handler lives here. */
    private fun dispatch(ctx: RoutingContext) {
        val req = ctx.request()
        val path = req.path()
        val method = req.method().name()
        val http = policy.http
        if (healthInternal && method == "GET" && path == healthPath) { ctx.reply(policy.response("OK")); return }
        val route = http.routes.entries.firstOrNull { (_, r) -> r.method.equals(method, ignoreCase = true) && r.path == path }
        if (route == null) { ctx.reply(policy.error(http.unknownRouteError)); return }
        if (!authed(req)) { ctx.reply(policy.error(http.authError)); return }
        when (route.key) {
            "health" -> ctx.reply(policy.response("OK"))
            "reset" -> { handler.reset(); ctx.reply(policy.response("OK")) }
            "msg" -> readBody(ctx) { body -> ctx.reply(guarded { handler.handle(body.toString(Charsets.UTF_8)) }) }
            else -> ctx.reply(policy.error(http.unknownRouteError))
        }
    }

    /**
     * Bounded body read: at most `bodyMaxBytes` are buffered, larger bodies answer the too-large error at end of
     * request; beyond [HARD_BODY_CAP] (declared or streamed) the connection is closed at once. Aborted body -> E100.
     */
    private fun readBody(ctx: RoutingContext, onBody: (Buffer) -> Unit) {
        val req = ctx.request()
        val http = policy.http
        val tooLarge = policy.error(http.bodyTooLargeError)
        val declared = req.getHeader("Content-Length")?.toLongOrNull()
        if (declared != null && declared > HARD_BODY_CAP) { ctx.reply(tooLarge); req.connection().close(); return }
        val body = Buffer.buffer()
        var received = 0L
        var done = false
        fun finish(block: () -> Unit) { if (!done) { done = true; safely(ctx, block) } }
        req.handler { chunk ->
            if (done) return@handler
            received += chunk.length()
            if (received > HARD_BODY_CAP) { finish { ctx.reply(tooLarge); req.connection().close() }; return@handler }
            if (received <= http.bodyMaxBytes) body.appendBuffer(chunk)
        }
        req.endHandler { finish { if (received > http.bodyMaxBytes) ctx.reply(tooLarge) else onBody(body) } }
        req.exceptionHandler { finish { ctx.reply(policy.error("E100")) } }
        req.resume()   // no-op unless an upstream handler paused the stream
    }

    /** Runs [block] on the event loop; if it throws, answer E100 rather than leaving the request hanging. */
    private fun safely(ctx: RoutingContext, block: () -> Unit) {
        try { block() } catch (e: Exception) { ctx.replyOnce(policy.error("E100")) } catch (e: StackOverflowError) { ctx.replyOnce(policy.error("E100")) }
    }

    /** Domain call guard: an exception (or a stack overflow on pathological input) becomes E100, never a 5xx. */
    private fun guarded(block: () -> Response): Response =
        try { block() } catch (e: Exception) { policy.error("E100") } catch (e: StackOverflowError) { policy.error("E100") }

    companion object {
        const val DEFAULT_HEALTH_PATH = "/__up-k7f3a9c2"
        /** Above this many bytes (declared or streamed) the request is rejected without reading the rest. */
        const val HARD_BODY_CAP = 1_000_000L
    }
}

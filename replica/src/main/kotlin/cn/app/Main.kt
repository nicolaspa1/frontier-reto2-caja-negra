package cn.app

import cn.adapters.config.PolicyLoader
import cn.adapters.http.HttpVerticle
import cn.adapters.json.VertxJsonCodec
import cn.domain.MsgPipeline
import io.vertx.core.Vertx

/**
 * Entry point. Environment: PORT (8080), TEAM_TOKEN, POLICY (v1 | path), POLICY_JSON (inline policy, wins over POLICY),
 * HEALTH_INTERNAL (any value enables the unauthenticated health route), HEALTH_PATH (its path).
 * Exactly one verticle instance is deployed: the ledger is in-memory and single-threaded by design.
 */
fun main() {
    val port = System.getenv("PORT")?.toIntOrNull() ?: 8080
    // "ftr_PENDIENTE" is the documented placeholder token; a real deployment always sets TEAM_TOKEN.
    val token = System.getenv("TEAM_TOKEN") ?: "ftr_PENDIENTE".also { System.err.println("WARN: TEAM_TOKEN is not set, using placeholder $it") }
    val healthInternal = !System.getenv("HEALTH_INTERNAL").isNullOrBlank()
    val healthPath = System.getenv("HEALTH_PATH")?.takeIf { it.isNotBlank() } ?: HttpVerticle.DEFAULT_HEALTH_PATH
    val policy = PolicyLoader.load()
    val pipeline = MsgPipeline(policy, VertxJsonCodec)
    val vertx = Vertx.vertx()
    vertx.deployVerticle(HttpVerticle(pipeline, policy, token, port, healthInternal, healthPath))
        .onSuccess { println("caja-negra replica policy=${policy.version} port=$port healthInternal=$healthInternal healthPath=$healthPath") }
        .onFailure { it.printStackTrace(); System.exit(1) }
}

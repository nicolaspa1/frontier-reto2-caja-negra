package cn.adapters.config

import cn.domain.*
import io.vertx.core.json.JsonArray
import io.vertx.core.json.JsonObject
import java.io.File

/**
 * Loads `policy-<version>.json` from the classpath, from a file when POLICY is an existing path,
 * or from POLICY_JSON (inline JSON) for a hot fix without a rebuild. Missing optional sections fall back
 * to today's production values, so a minimal policy only needs `commands`, `responses` and `errors`.
 * [validate] fails fast at start-up when a name the runtime will need cannot be resolved.
 */
object PolicyLoader {
    private val defaultIntegrityOrder = listOf("FaltaControl", "SegmentoVacio", "ControlMalFormado", "ControlInvalido", "ComandoDesconocido", "Aridad")

    /** Error codes emitted by the envelope/integrity stage; a policy must define them all. */
    private val requiredErrors = listOf("E100", "E102", "E103", "E104", "E201", "E202", "E301", "E302")
    private val knownEffects = setOf("ping", "static", "alta", "consulta", "deposito", "transfer", "anula")

    fun load(policyEnv: String = System.getenv("POLICY") ?: "v1", inline: String? = System.getenv("POLICY_JSON")): Policy {
        val text = when {
            !inline.isNullOrBlank() -> inline
            File(policyEnv).isFile -> File(policyEnv).readText(Charsets.UTF_8)
            else -> {
                val res = "/policy-$policyEnv.json"
                PolicyLoader::class.java.getResourceAsStream(res)?.use { it.readBytes().toString(Charsets.UTF_8) }
                    ?: error("$res not found on the classpath (POLICY=$policyEnv)")
            }
        }
        return parse(JsonObject(text))
    }

    /** Each policy entry is `{ "value": ..., "evidencia": [...] }`; only `value` matters at runtime. */
    private fun JsonObject.v(key: String): Any? = getJsonObject(key)?.getValue("value")
    private fun JsonObject.vLongOr(key: String, def: Long): Long = (v(key) as Number?)?.toLong() ?: def
    private fun JsonObject.vLongOrNull(key: String): Long? = (v(key) as Number?)?.toLong()
    private fun JsonArray?.strings(): List<String> = this?.map { it as String } ?: emptyList()
    private fun JsonObject.objects(): Map<String, JsonObject> = map.keys.associateWith { getJsonObject(it) }

    fun parse(j: JsonObject): Policy {
        val http = j.getJsonObject("http") ?: JsonObject()
        val integrity = j.getJsonObject("integrity") ?: JsonObject()
        val ids = j.getJsonObject("ids") ?: JsonObject()
        val limits = j.getJsonObject("limits") ?: JsonObject()
        val httpDefaults = HttpSpec(8192, "Bearer "); val parserDefaults = ParserSpec()
        fun idSpec(k: String, prefix: String) = (ids.v(k) as JsonObject?)?.let { IdSpec(it.getString("prefix"), it.getInteger("width")) } ?: IdSpec(prefix, 4)
        val routes = (http.v("routes") as JsonObject?)?.objects()?.mapValues { (_, r) -> RouteSpec(r.getString("method"), r.getString("path")) } ?: httpDefaults.routes
        val algoSpec: Map<String, Any?> = when (val a = integrity.v("controlAlgorithm")) {
            null -> emptyMap(); is JsonObject -> a.map; is String -> mapOf("name" to a.lowercase().replace('_', '-')); else -> throw IllegalArgumentException("Invalid controlAlgorithm")
        }
        val commands = j.getJsonObject("commands").objects().map { (name, c) ->
            name to CommandSpec(
                name = name,
                arity = c.getInteger("arity"),
                args = c.getJsonArray("args").strings(),
                rules = c.getJsonArray("rules").strings(),
                postRules = c.getJsonArray("postRules").strings(),
                effect = c.getString("effect"),
                response = c.getString("response"),
                failureStrikes = c.getJsonObject("failureStrikes")?.let { StrikeSpec(it.getString("role"), it.getJsonArray("errors").strings().toSet()) },
            )
        }.toMap()
        fun specs(section: String) = j.getJsonObject(section).objects().mapValues { (_, r) -> ResponseSpec(r.getInteger("status"), r.getString("body")) }
        val policy = Policy(
            version = j.getString("version"),
            http = HttpSpec(
                bodyMaxBytes = (http.v("bodyMaxBytes") as Number?)?.toInt() ?: httpDefaults.bodyMaxBytes,
                authScheme = http.v("authScheme") as String? ?: httpDefaults.authScheme,
                bodyField = http.v("bodyField") as String? ?: httpDefaults.bodyField,
                routes = routes,
                unknownRouteError = http.v("unknownRouteError") as String? ?: httpDefaults.unknownRouteError,
                authError = http.v("authError") as String? ?: httpDefaults.authError,
                bodyTooLargeError = http.v("bodyTooLargeError") as String? ?: httpDefaults.bodyTooLargeError,
            ),
            parser = ParserSpec(
                separator = integrity.v("separator") as String? ?: parserDefaults.separator,
                trim = integrity.v("trim") as Boolean? ?: parserDefaults.trim,
                commandCase = integrity.v("commandCase") as String? ?: parserDefaults.commandCase,
                controlPosition = integrity.v("controlPosition") as String? ?: parserDefaults.controlPosition,
            ),
            integrityOrder = (integrity.v("order") as JsonArray?)?.strings() ?: defaultIntegrityOrder,
            controlFormat = Regex(integrity.v("controlFormat") as String? ?: "^[0-9]{1,2}$"),
            controlAlgorithm = ControlAlgorithms.from(algoSpec),
            controlCompare = integrity.v("controlCompare") as String? ?: "canonical-string",
            suggestion = (integrity.v("suggestion") as JsonObject?)?.let { SuggestionSpec(it.getJsonArray("candidates").strings(), it.getInteger("maxDistance")) },
            controlExempt = (integrity.v("controlExempt") as JsonArray?)?.strings()?.toSet() ?: setOf("PING"),
            accountId = idSpec("account", "AC-"),
            operationId = idSpec("operation", "OP-"),
            limits = Limits(
                initialBalance = limits.vLongOr("initialBalance", 0),
                amountMin = limits.vLongOr("amountMin", 1),
                amountMax = limits.vLongOr("amountMax", 9999999999L),
                transferMax = limits.vLongOr("transferMax", 100000),
                blockAfterFailures = limits.vLongOr("blockAfterFailures", 3).toInt(),
                resetStrikesOnSuccess = limits.v("resetStrikesOnSuccess") as Boolean? ?: true,
                accumulatedTransferMax = limits.vLongOrNull("accumulatedTransferMax"),
                feeFixed = limits.vLongOr("feeFixed", 0),
                feePercent = limits.vLongOr("feePercent", 0),
            ),
            commands = commands,
            responses = specs("responses"),
            errors = specs("errors"),
        )
        validate(policy)
        return policy
    }

    /**
     * Fails at load time (not with a 5xx at request time) when the policy references something the runtime
     * cannot resolve: rule names, effects, response keys and the error codes the HTTP and integrity stages emit.
     */
    fun validate(p: Policy) {
        val missing = mutableListOf<String>()
        fun needError(code: String, why: String) { if (code !in p.errors) missing += "error $code ($why)" }
        fun needResponse(key: String, why: String) { if (key !in p.responses) missing += "response $key ($why)" }
        (requiredErrors + p.http.authError + p.http.unknownRouteError + p.http.bodyTooLargeError).forEach { needError(it, "http/integrity stage") }
        if (p.suggestion != null) needError("E301_SUGERENCIA", "integrity.suggestion is set")
        needResponse("OK", "health/reset")
        p.integrityOrder.forEach { if (it !in IntegrityRegistry.rules) missing += "integrity rule $it" }
        for (c in p.commands.values) {
            c.rules.forEach { if (it !in RuleRegistry.rules) missing += "rule $it (command ${c.name})" }
            c.postRules.forEach { if (it !in RuleRegistry.postRules) missing += "postRule $it (command ${c.name})" }
            if (c.effect !in knownEffects) missing += "effect ${c.effect} (command ${c.name})"
            needResponse(c.response, "command ${c.name}")
            if (c.effect == "anula") needResponse(c.response + "_REPETIDA", "command ${c.name}")
            if (c.args.size != c.arity) missing += "command ${c.name}: args (${c.args.size}) and arity (${c.arity}) differ"
        }
        require(missing.isEmpty()) { "policy ${p.version} cannot be loaded: " + missing.joinToString("; ") }
    }
}

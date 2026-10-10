package cn.domain

/** Immutable, parsed view of `policy-<version>.json`: everything that can change without touching code. */

/** Status plus body template; `${name}` placeholders are replaced literally by [render]. */
data class ResponseSpec(val status: Int, val body: String) {
    fun render(vars: Map<String, Any>): Response {
        var out = body
        for ((k, v) in vars) out = out.replace("\${$k}", v.toString())
        return Response(status, out)
    }
}

data class IdSpec(val prefix: String, val width: Int) {
    fun format(n: Long) = prefix + n.toString().padStart(width, '0')
}

/** Which argument role gets a strike when the command fails with one of [errors]. */
data class StrikeSpec(val role: String, val errors: Set<String>)

data class CommandSpec(
    val name: String,
    val arity: Int,
    val args: List<String>,
    val rules: List<String>,
    val postRules: List<String>,
    val effect: String,
    val response: String,
    val failureStrikes: StrikeSpec?,
)

data class Limits(
    val initialBalance: Long,
    val amountMin: Long,
    val amountMax: Long,
    val transferMax: Long,
    val blockAfterFailures: Int,
    val resetStrikesOnSuccess: Boolean,
    val accumulatedTransferMax: Long?,
    val feeFixed: Long,
    val feePercent: Long,
    /** Transfers between accounts whose holder names are identical (case-sensitive) skip the per-operation cap (L2301-L2303). */
    val transferMaxSameOwnerExempt: Boolean = false,
)

data class RouteSpec(val method: String, val path: String)

/**
 * Confirmed halt (`policy.halt`): when [command] receives one of [confirmValues] as its argument the service enters
 * a halted state and every message answers [error] until the next reset (L2272-L2278).
 */
data class HaltSpec(val command: String, val confirmValues: Set<String>, val error: String)

/** Everything the HTTP adapter reads from the policy: changing a route, method, auth scheme or error code is config only. */
data class HttpSpec(
    val bodyMaxBytes: Int,
    val authScheme: String,
    val bodyField: String = "m",
    val routes: Map<String, RouteSpec> = mapOf("msg" to RouteSpec("POST", "/msg"), "reset" to RouteSpec("POST", "/__reset"), "health" to RouteSpec("GET", "/health")),
    val unknownRouteError: String = "E404",
    val authError: String = "E001",
    val bodyTooLargeError: String = "E101",
)

data class ParserSpec(
    val separator: String = ";",
    val trim: Boolean = false,
    val commandCase: String = "insensitive",
    val controlPosition: String = "last",
)

/** E301 carries a `sugerencia` when the command is within [maxDistance] (Levenshtein) of a candidate. */
data class SuggestionSpec(val candidates: List<String>, val maxDistance: Int)

data class Policy(
    val version: String,
    val http: HttpSpec,
    val parser: ParserSpec,
    val integrityOrder: List<String>,
    val controlFormat: Regex,
    val controlAlgorithm: ControlAlgorithm,
    val controlCompare: String = "canonical-string",
    val suggestion: SuggestionSpec?,
    val controlExempt: Set<String>,
    val accountId: IdSpec,
    val operationId: IdSpec,
    val limits: Limits,
    val commands: Map<String, CommandSpec>,
    val responses: Map<String, ResponseSpec>,
    val errors: Map<String, ResponseSpec>,
    val halt: HaltSpec? = null,
) {
    fun error(code: String, vars: Map<String, Any> = emptyMap()): Response =
        (errors[code] ?: throw IllegalStateException("policy $version: error $code is not defined")).render(vars)

    fun response(key: String, vars: Map<String, Any> = emptyMap()): Response =
        (responses[key] ?: throw IllegalStateException("policy $version: response $key is not defined")).render(vars)
}

package cn.domain

/** Business rules, addressed by the names used in `policy.commands.<CMD>.rules` / `postRules` (policy data). */

/** Context of a command that already passed the integrity checks (control, arity). */
class CommandContext(val spec: CommandSpec, val args: List<String>, val ledger: Ledger, val policy: Policy) {
    /** Argument carrying the given role, or null when the command has no such role. */
    fun arg(role: String): String? = spec.args.indexOf(role).takeIf { it >= 0 }?.let { args[it] }
    /** All arguments whose role is one of [roles], in positional order. */
    fun argsWithRole(vararg roles: String): List<String> = spec.args.withIndex().filter { it.value in roles }.map { args[it.index] }
    /** Amount already validated by `MontoValido` (null when the command has no amount). */
    var amount: Long? = null
    /** Fee set by the latent `Comision` rule (0 by default). */
    var fee: Long = 0
}

/** A business rule returns the error code that stops the pipeline, or null to let it continue. */
fun interface Rule { fun check(ctx: CommandContext): String? }

/** Hook that runs after a successful effect (must not fail). */
fun interface PostRule { fun after(ctx: CommandContext, op: Operation?) }

object RuleRegistry {
    private val amountRegex = Regex("^[1-9][0-9]*$")

    val rules: Map<String, Rule> = mapOf(
        // Positive integer, no leading zeros, within [amountMin, amountMax]. L44-L47, L53, L61
        "MontoValido" to Rule { ctx ->
            val raw = ctx.arg("monto") ?: return@Rule null
            if (!amountRegex.matches(raw)) return@Rule "E303"
            val v = raw.toLongOrNull() ?: return@Rule "E303"
            if (v < ctx.policy.limits.amountMin || v > ctx.policy.limits.amountMax) return@Rule "E303"
            ctx.amount = v; null
        },
        // L102, L145
        "OrigenDestinoDistintos" to Rule { ctx -> if (ctx.arg("origen") == ctx.arg("destino")) "E304" else null },
        // Every referenced account (cuenta, origen, destino) must exist. L16, L48, L104, L106
        "CuentaExiste" to Rule { ctx -> if (ctx.argsWithRole("cuenta", "origen", "destino").any { ctx.ledger.account(it) == null }) "E401" else null },
        // Origin blocked after N blocking failures. L72, L75, L87, L139
        "OrigenNoBloqueada" to Rule { ctx ->
            val acc = ctx.arg("origen")?.let(ctx.ledger::account) ?: return@Rule null
            if (acc.strikes >= ctx.policy.limits.blockAfterFailures) "E510" else null
        },
        // Per-operation cap, inclusive (L68-L71); same holder name (exact text) is exempt (L2301 OK, L2320 E506).
        "LimitePorOperacion" to Rule { ctx ->
            if (ctx.policy.limits.transferMaxSameOwnerExempt) {
                val o = ctx.arg("origen")?.let(ctx.ledger::account)
                val d = ctx.arg("destino")?.let(ctx.ledger::account)
                if (o != null && d != null && o.name == d.name) return@Rule null
            }
            if ((ctx.amount ?: 0) > ctx.policy.limits.transferMax) "E506" else null
        },
        // L17, L136
        "SaldoSuficiente" to Rule { ctx ->
            val acc = ctx.arg("origen")?.let(ctx.ledger::account) ?: return@Rule null
            if (acc.balance < (ctx.amount ?: 0) + ctx.fee) "E507" else null
        },
        // L119
        "OperacionExiste" to Rule { ctx -> if (ctx.arg("operacion")?.let(ctx.ledger::operation) == null) "E402" else null },

        // ---- Latent rules: implemented but not listed in policy-v1 ----
        // Accumulated outgoing-transfer cap per account (limits.accumulatedTransferMax).
        "LimiteAcumulado" to Rule { ctx ->
            val max = ctx.policy.limits.accumulatedTransferMax ?: return@Rule null
            val acc = ctx.arg("origen")?.let(ctx.ledger::account) ?: return@Rule null
            if (acc.transferredOut + (ctx.amount ?: 0) > max) "E506" else null
        },
        // Fixed and/or percentage fee on TRANSFER (limits.feeFixed, limits.feePercent). Must run before SaldoSuficiente.
        "Comision" to Rule { ctx ->
            val l = ctx.policy.limits
            ctx.fee = l.feeFixed + ((ctx.amount ?: 0) * l.feePercent) / 100
            null
        },
    )

    val postRules: Map<String, PostRule> = mapOf(
        // Reset the failure counter after a successful transfer as origin (limits.resetStrikesOnSuccess, L679-L688).
        "DesbloqueoTrasExito" to PostRule { ctx, _ ->
            if (ctx.policy.limits.resetStrikesOnSuccess) ctx.arg("origen")?.let(ctx.ledger::account)?.let { it.strikes = 0; it.blockedAttempts = 0 }
        },
        // A successful DEPOSITO on the account clears its strikes; nothing else does (L3663-L3731).
        "DesbloqueoTrasDeposito" to PostRule { ctx, _ ->
            ctx.arg("cuenta")?.let(ctx.ledger::account)?.let { it.strikes = 0; it.blockedAttempts = 0 }
        },
    )

    fun rule(name: String): Rule = rules[name] ?: error("Unknown rule in policy: $name")
    fun postRule(name: String): PostRule = postRules[name] ?: error("Unknown postRule in policy: $name")
}

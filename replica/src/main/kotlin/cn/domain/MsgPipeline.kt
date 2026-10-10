package cn.domain

/**
 * Message pipeline: envelope -> syntax -> integrity rules -> business rules -> ledger effect -> response template.
 * Pure Kotlin; the only state is the [Ledger].
 */

class ParsedMessage(raw: String, val spec: ParserSpec) {
    val raw: String = if (spec.trim) raw.trim() else raw
    val parts: List<String> = this.raw.split(spec.separator)
    private val controlFirst = spec.controlPosition == "first"
    private val hasControl get() = parts.size >= 2
    val command: String = (parts.getOrNull(if (controlFirst) 1 else 0) ?: "").let { if (spec.commandCase == "sensitive") it else asciiUpper(it) }
    val control: String get() = if (controlFirst) parts.first() else parts.last()
    val args: List<String> get() = when {
        !hasControl -> emptyList()
        controlFirst -> parts.drop(2)
        else -> parts.subList(1, parts.size - 1)
    }
    val payload: String get() = when {
        !hasControl -> this.raw
        controlFirst -> this.raw.substring(this.raw.indexOf(spec.separator) + spec.separator.length)
        else -> this.raw.substring(0, this.raw.lastIndexOf(spec.separator))
    }
}

/** Integrity check: returns an error code or null. The order comes from `policy.integrity.order`. */
fun interface IntegrityRule { fun check(m: ParsedMessage, p: Policy): String? }

object IntegrityRegistry {
    val rules: Map<String, IntegrityRule> = mapOf(
        "FaltaControl" to IntegrityRule { m, _ -> if (m.parts.size < 2) "E103" else null },
        "SegmentoVacio" to IntegrityRule { m, _ -> if (m.parts.any { it.isEmpty() }) "E104" else null },
        // policy.integrity.controlFormat (1-2 ASCII digits): "+5", "014", "123" -> E201 (L731-L733)
        "ControlMalFormado" to IntegrityRule { m, p -> if (m.command in p.controlExempt || p.controlFormat.matches(m.control)) null else "E201" },
        "ControlInvalido" to IntegrityRule { m, p ->
            if (m.command in p.controlExempt) return@IntegrityRule null
            // Canonical text comparison: "03" -> E202 even though 3 is the right value (L1070, L730)
            if (p.controlAlgorithm.accepts(m.payload, m.control, p.controlCompare)) null else "E202"
        },
        "ComandoDesconocido" to IntegrityRule { m, p -> if (m.command in p.commands) null else "E301" },
        "Aridad" to IntegrityRule { m, p -> p.commands[m.command]?.let { if (it.arity != m.args.size) "E302" else null } },
    )
    fun rule(name: String) = rules[name] ?: error("Unknown integrity rule in policy: $name")
}

class MsgPipeline(val policy: Policy, private val codec: JsonCodec) : MessageHandler {
    val ledger = Ledger(policy)
    private val integrity = policy.integrityOrder.map(IntegrityRegistry::rule)

    override fun reset() = ledger.reset()

    /** Never throws: any unexpected failure degrades to the envelope error (E100) rather than a 5xx. */
    override fun handle(raw: String): Response = try {
        val halt = policy.halt
        if (halt != null && ledger.halted) return policy.error(halt.error)
        when (val env = codec.extract(raw, policy.http.bodyField)) {
            Envelope.Invalid -> policy.error("E100")
            Envelope.MissingField -> policy.error("E102")
            is Envelope.Message -> process(env.m)
        }
    } catch (e: Exception) {
        policy.error("E100")
    } catch (e: StackOverflowError) {
        policy.error("E100")
    }

    private fun process(m: String): Response {
        val parsed = ParsedMessage(m, policy.parser)
        for (r in integrity) r.check(parsed, policy)?.let { code -> return integrityError(code, parsed) }
        val spec = policy.commands[parsed.command] ?: return integrityError("E301", parsed)
        val ctx = CommandContext(spec, parsed.args, ledger, policy)
        for (name in spec.rules) {
            RuleRegistry.rule(name).check(ctx)?.let { code ->
                strike(ctx, code)
                return policy.error(code, errorVars(ctx, code))
            }
        }
        val (response, op) = apply(ctx)
        for (name in spec.postRules) RuleRegistry.postRule(name).after(ctx, op)
        return response
    }

    /** E301 carries `sugerencia` when a candidate is within Levenshtein distance <= maxDistance (L837, L843, L881). Ties: first in list. */
    private fun integrityError(code: String, parsed: ParsedMessage): Response {
        val suggestion = policy.suggestion
        if (code == "E301" && suggestion != null) {
            val best = suggestion.candidates.map { it to Levenshtein.distance(parsed.command, it) }
                .filter { it.second <= suggestion.maxDistance }.minByOrNull { it.second }
            if (best != null) return policy.error("E301_SUGERENCIA", mapOf("sugerencia" to best.first))
        }
        return policy.error(code)
    }

    /** E510 carries the attempts made while blocked, from 1, cleared when the block lifts (v2 `intentos`, L5562-L5564). */
    private fun errorVars(ctx: CommandContext, code: String): Map<String, Any> {
        if (code != "E510") return emptyMap()
        val acc = ctx.arg("origen")?.let(ledger::account) ?: return emptyMap()
        acc.blockedAttempts += 1
        return mapOf("intentos" to acc.blockedAttempts)
    }

    /** Blocking failures (E506/E507) as origin add one strike (L84-L87, L136-L139). */
    private fun strike(ctx: CommandContext, code: String) {
        val s = ctx.spec.failureStrikes ?: return
        if (code !in s.errors) return
        ctx.arg(s.role)?.let(ledger::account)?.let { it.strikes++ }
    }

    private fun apply(ctx: CommandContext): Pair<Response, Operation?> = when (ctx.spec.effect) {
        // Fixed response, ledger untouched: PING, HALT (409 E520, L894), NOTAS (L896).
        "ping", "static" -> {
            // Confirmed HALT (HALT;1) -> 503 E599, halted until reset (L2272-L2278)
            val halt = policy.halt
            if (halt != null && ctx.spec.name == halt.command && ctx.args.any { it in halt.confirmValues }) {
                ledger.halted = true
                policy.error(halt.error) to null
            } else policy.response(ctx.spec.response) to null
        }
        "alta" -> {
            val acc = ledger.openAccount(ctx.arg("nombre") ?: "")
            policy.response(ctx.spec.response, mapOf("cuentaId" to acc.id)) to null
        }
        "consulta" -> {
            val acc = requireAccount(ctx, "cuenta")
            policy.response(ctx.spec.response, mapOf("cuentaId" to acc.id, "saldo" to acc.balance, "saldoDecimal" to decimal(acc.balance))) to null
        }
        "deposito" -> {
            val op = ledger.deposit(requireAccount(ctx, "cuenta"), requireAmount(ctx))
            policy.response(ctx.spec.response, mapOf("operacionId" to op.id)) to op
        }
        "transfer" -> {
            val op = ledger.transfer(requireAccount(ctx, "origen"), requireAccount(ctx, "destino"), requireAmount(ctx), ctx.fee)
            policy.response(ctx.spec.response, mapOf("operacionId" to op.id)) to op
        }
        "anula" -> {
            val op = ctx.arg("operacion")?.let(ledger::operation) ?: error("effect anula requires rule OperacionExiste")
            if (ledger.annul(op)) policy.response(ctx.spec.response, mapOf("operacionId" to op.id)) to op
            else policy.response(ctx.spec.response + "_REPETIDA") to op
        }
        else -> error("Unknown effect: ${ctx.spec.effect}")
    }

    private fun requireAccount(ctx: CommandContext, role: String): Account =
        ctx.arg(role)?.let(ledger::account) ?: error("effect ${ctx.spec.effect} requires rule CuentaExiste for role $role")

    private fun requireAmount(ctx: CommandContext): Long = ctx.amount ?: error("effect ${ctx.spec.effect} requires rule MontoValido")
}

object Levenshtein {
    fun distance(a: String, b: String): Int {
        var prev = IntArray(b.length + 1) { it }
        var cur = IntArray(b.length + 1)
        for (i in 1..a.length) {
            cur[0] = i
            for (j in 1..b.length) {
                val cost = if (a[i - 1] == b[j - 1]) 0 else 1
                cur[j] = minOf(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            }
            val t = prev; prev = cur; cur = t
        }
        return prev[b.length]
    }
}

/** ASCII-only case folding: `pıng`, `PİNG`, `ſalta`, `Kalta` (Kelvin) are NOT accepted by the original (L3732-L3739). */
internal fun asciiUpper(s: String): String = buildString(s.length) { for (c in s) append(if (c in 'a'..'z') c - 32 else c) }

/** Two-decimal balance, unit = 1/100 of the stored amount (v2 CONSULTA: 22 -> "0.22", -100 -> "-1.00"; L5565, L5605). */
internal fun decimal(cents: Long): String {
    val a = if (cents < 0) -cents else cents
    return (if (cents < 0) "-" else "") + (a / 100) + "." + (a % 100).toString().padStart(2, '0')
}

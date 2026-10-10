package cn.domain

/**
 * Core domain types shared by the pipeline, the ledger and the adapters.
 *
 * The domain never sees Vert.x: HTTP and JSON enter through the [MessageHandler] and [JsonCodec] ports.
 * Spanish identifiers (`MontoValido`, `monto`, `cuentaId`, ...) are policy data keys and response-body
 * variables replicated from the original; they are not translated.
 */

data class Response(val status: Int, val body: String)

/** Inbound port: one envelope body in, one [Response] out; `reset` wipes all in-memory state. */
interface MessageHandler {
    fun handle(raw: String): Response
    fun reset()
}

/** Result of decoding the JSON envelope `{"<field>": "<message>"}`. */
sealed interface Envelope {
    data object Invalid : Envelope
    data object MissingField : Envelope
    data class Message(val m: String) : Envelope
}

fun interface JsonCodec { fun extract(raw: String, field: String): Envelope }

/**
 * Mutable account state. [strikes] counts consecutive blocking failures as transfer origin;
 * [transferredOut] feeds the latent accumulated-transfer limit rule.
 */
data class Account(val id: String, val name: String, var balance: Long, var strikes: Int = 0, var blockedAttempts: Int = 0, var transferredOut: Long = 0)

data class Operation(val id: String, val type: String, val from: String?, val to: String, val amount: Long, val fee: Long, var annulled: Boolean = false)

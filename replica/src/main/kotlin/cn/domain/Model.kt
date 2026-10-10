package cn.domain

/**
 * Core domain types shared by the pipeline, the ledger and the adapters.
 *
 * The domain never sees Vert.x: HTTP and JSON enter through the [MessageHandler] and [JsonCodec] ports.
 * Spanish strings that appear in this package (rule names such as `MontoValido`, argument roles such as
 * `monto`/`origen`, template variables such as `cuentaId`) are *data* keys defined by `rules/policy-v1.json`
 * and the response bodies replicated byte for byte from the original service; they are intentionally not translated.
 */

/** HTTP status and exact body (already rendered) to send back. */
data class Response(val status: Int, val body: String)

/** Inbound port: one envelope body in, one [Response] out; `reset` wipes all in-memory state. */
interface MessageHandler {
    fun handle(raw: String): Response
    fun reset()
}

/** Result of decoding the JSON envelope `{"<field>": "<message>"}`. */
sealed interface Envelope {
    /** Body is not valid JSON -> E100. */
    data object Invalid : Envelope
    /** Valid JSON but not an object, or the field is missing / not a string -> E102. */
    data object MissingField : Envelope
    data class Message(val m: String) : Envelope
}

/** Port used by the pipeline to decode the envelope; the adapter decides which JSON library to use. */
fun interface JsonCodec { fun extract(raw: String, field: String): Envelope }

/**
 * Mutable account state. [strikes] counts consecutive blocking failures as transfer origin;
 * [transferredOut] feeds the latent accumulated-transfer limit rule.
 */
data class Account(val id: String, val name: String, var balance: Long, var strikes: Int = 0, var transferredOut: Long = 0)

/** A recorded deposit or transfer; [annulled] flips once when ANULA reverts it. */
data class Operation(val id: String, val type: String, val from: String?, val to: String, val amount: Long, val fee: Long, var annulled: Boolean = false)

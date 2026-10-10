package cn.adapters.json

import cn.domain.Envelope
import cn.domain.JsonCodec
import io.vertx.core.json.DecodeException
import io.vertx.core.json.Json
import io.vertx.core.json.JsonObject

/**
 * Envelope decoder (field = policy.http.bodyField): invalid JSON -> E100; not an object / missing / not a string -> E102.
 * Jackson's default stream constraints turn pathological nesting into [DecodeException], not stack overflows.
 */
object VertxJsonCodec : JsonCodec {
    override fun extract(raw: String, field: String): Envelope {
        val value: Any? = try {
            if (raw.isBlank()) return Envelope.Invalid
            Json.decodeValue(raw)
        } catch (e: DecodeException) {
            return Envelope.Invalid
        }
        if (value !is JsonObject) return Envelope.MissingField
        val m = value.getValue(field)
        return if (m is String) Envelope.Message(m) else Envelope.MissingField
    }
}

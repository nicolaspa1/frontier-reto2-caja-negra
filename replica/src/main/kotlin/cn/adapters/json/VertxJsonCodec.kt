package cn.adapters.json

import cn.domain.Envelope
import cn.domain.JsonCodec
import io.vertx.core.json.DecodeException
import io.vertx.core.json.Json
import io.vertx.core.json.JsonObject

/**
 * Decodes the envelope `{"<field>": "..."}` (field = policy.http.bodyField) with Vert.x/Jackson:
 * invalid JSON -> E100; not an object / field missing / field not a string -> E102.
 * Jackson's default stream constraints (nesting depth 1000, number length 1000) turn pathological
 * documents into [DecodeException] instead of stack overflows.
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

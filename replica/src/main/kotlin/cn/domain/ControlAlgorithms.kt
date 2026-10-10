package cn.domain

import java.io.File

/**
 * Control-field validation strategy, selected by policy.integrity.controlAlgorithm:
 *  - {"name":"iso7064-mod97-10", alphabet=a1|a10|ascii, dropNonAlnum, dropNonAscii, caseFold, suffix, modulus, base}  (current)
 *  - {"name":"permissive"}  accepts any control that matches controlFormat; never E202
 *  - {"name":"fit","file":"harness/control-fit.json"}  reads the {familia, params} written by harness/fit.py (FitControl)
 */
interface ControlAlgorithm {
    /** Expected control for "CMD;arg1;...;argN", or null when there is no single value (permissive). */
    fun expected(payload: String): Int?

    /** policy.integrity.controlCompare: "canonical-string" = exact str(expected), "03" != 3 (L1070); "numeric" = int compare. */
    fun accepts(payload: String, control: String, compare: String = "canonical-string"): Boolean {
        val exp = expected(payload) ?: return true
        return when (compare) {
            "numeric" -> control.toIntOrNull() == exp
            else -> control == exp.toString()
        }
    }
}

object PermissiveControl : ControlAlgorithm { override fun expected(payload: String): Int? = null }

/** Turns the payload into a digit string according to the alphabet and filters. */
class DigitMapper(val alphabet: String = "a1", val dropNonAlnum: Boolean = true, val dropNonAscii: Boolean = true, val caseFold: Boolean = true) {
    fun letterValue(ch: Char): String? {
        val upper = ch in 'A'..'Z'; val lower = ch in 'a'..'z'
        if (!upper && !lower) return null
        if (lower && !caseFold && alphabet != "ascii") return null          // no case folding: lowercase is outside the alphabet
        val idx = if (upper) ch - 'A' else ch - 'a'
        return when (alphabet) {
            "a1" -> (idx + 1).toString()
            "a10" -> (idx + 10).toString()
            "ascii" -> (if (caseFold) ch.uppercaseChar() else ch).code.toString()
            else -> throw IllegalArgumentException("Unknown alphabet: $alphabet")
        }
    }
    fun digits(payload: String): String {
        val sb = StringBuilder()
        for (ch in payload) {
            when {
                ch in '0'..'9' -> sb.append(ch)
                ch.code < 128 -> letterValue(ch)?.let { sb.append(it) } ?: run { if (!dropNonAlnum) sb.append(ch.code) }
                else -> if (!dropNonAscii) sb.append(ch.code)
            }
        }
        return sb.toString()
    }
}

/** Generalised ISO 7064 MOD 97-10: result = base - (digits(payload)+suffix mod modulus). */
class Iso7064Control(val mapper: DigitMapper = DigitMapper(), val suffix: String = "00", val modulus: Long = 97, val base: Long = 98) : ControlAlgorithm {
    override fun expected(payload: String): Int {
        var mod = 0L
        for (c in mapper.digits(payload) + suffix) mod = (mod * 10 + (c - '0')) % modulus
        return (base - mod).toInt()
    }
}

/**
 * Exact replica of harness/fit.py::make_control over the schema fit.py writes to control-fit.json:
 * params = {case: upper|lower|raw|absent, keep: ""|";"|"-"|";-", nonascii: drop|map, agg: iso_concat|sum_bytes|sum_weighted|poly,
 *           alphabet: a1|a10|ascii (iso_concat), suffix: "00"|"0"|"" (iso_concat), base: int (poly),
 *           formula: rem|98-|97-|m-|m+1-|luhn|verhoeff|damm, m: int (numeric formulas)}
 * familia (label): iso7064 | luhn | verhoeff | damm | bytesum | weighted | polynomial | permissive.
 */
class FitControl(val p: Map<String, Any?>) : ControlAlgorithm {
    private val case = p["case"] as String?
    private val keep = p["keep"] as String? ?: ""
    private val nonascii = p["nonascii"] as String? ?: "drop"
    private val agg = p["agg"] as String? ?: "iso_concat"
    private val alphabet = p["alphabet"] as String? ?: "a1"
    private val suffix = p["suffix"] as String? ?: "00"
    private val base = (p["base"] as Number?)?.toLong() ?: 31
    private val formula = p["formula"] as String? ?: "98-"
    private val m = (p["m"] as Number?)?.toLong() ?: 97
    private val digitFormula = formula in setOf("luhn", "verhoeff", "damm")

    private fun normalize(payload: String): String {
        val src = when (case) { "upper" -> payload.uppercase(); "lower" -> payload.lowercase(); else -> payload }
        val sb = StringBuilder()
        for (ch in src) {
            if (ch.code < 128) { if (ch.isLetterOrDigit() || ch in keep) sb.append(ch) }
            else if (nonascii == "map") sb.append(ch)
        }
        return sb.toString()
    }

    private fun isoDigits(s: String): String {
        val sb = StringBuilder()
        for (ch in s) {
            if (ch in '0'..'9') sb.append(ch)
            else if (ch.isLetter()) {
                val o = ch.uppercaseChar().code
                when (alphabet) {
                    "a1" -> sb.append(if (o in 65..90) (o - 64).toString() else ch.code.toString())
                    "a10" -> sb.append(if (o in 65..90) (o - 55).toString() else ch.code.toString())
                    else -> sb.append(ch.code.toString())
                }
            } else sb.append(ch.code.toString())   // kept separator or mapped non-ASCII -> code point
        }
        return sb.toString()
    }

    private fun numFormula(r: Long): Int = when (formula) {
        "rem" -> r; "98-" -> 98 - r; "97-" -> 97 - r; "m-" -> Math.floorMod(m - r, m); "m+1-" -> m + 1 - r
        else -> throw IllegalArgumentException("Unknown formula: $formula")
    }.toInt()

    private fun digitFormulaOn(digits: String): Int = when (formula) {
        "luhn" -> CheckDigits.luhn(digits); "verhoeff" -> CheckDigits.verhoeff(digits); "damm" -> CheckDigits.damm(digits)
        else -> throw IllegalArgumentException(formula)
    }

    override fun expected(payload: String): Int? {
        val s = normalize(payload)
        if (agg == "iso_concat") {
            val digits = isoDigits(s)
            if (digits.isEmpty()) return null
            if (digitFormula) return digitFormulaOn(digits)
            var mod = 0L
            for (c in digits + suffix) mod = (mod * 10 + (c - '0')) % m
            return numFormula(mod)
        }
        val b = s.toByteArray(Charsets.UTF_8)
        if (b.isEmpty()) return null
        var n = java.math.BigInteger.ZERO
        when (agg) {
            "sum_bytes" -> for (x in b) n += java.math.BigInteger.valueOf((x.toInt() and 0xff).toLong())
            "sum_weighted" -> b.forEachIndexed { i, x -> n += java.math.BigInteger.valueOf((i + 1L) * (x.toInt() and 0xff)) }
            "poly" -> { val bb = java.math.BigInteger.valueOf(base); for (x in b) n = n * bb + java.math.BigInteger.valueOf((x.toInt() and 0xff).toLong()) }
            else -> throw IllegalArgumentException("Unknown agg: $agg")
        }
        if (digitFormula) return digitFormulaOn(n.toString())
        return numFormula(n.mod(java.math.BigInteger.valueOf(m)).toLong())
    }
}

object CheckDigits {
    fun luhn(s: String): Int {
        var total = 0
        s.reversed().forEachIndexed { i, ch -> var d = ch - '0'; if (i % 2 == 0) { d *= 2; if (d > 9) d -= 9 }; total += d }
        return (10 - total % 10) % 10
    }
    private val damm = arrayOf(
        intArrayOf(0,3,1,7,5,9,8,6,4,2), intArrayOf(7,0,9,2,1,5,4,8,6,3), intArrayOf(4,2,0,6,8,7,1,3,5,9), intArrayOf(1,7,5,0,9,8,3,4,2,6), intArrayOf(6,1,2,3,0,4,5,9,7,8),
        intArrayOf(3,6,7,4,2,0,9,5,8,1), intArrayOf(5,8,6,9,7,2,0,1,3,4), intArrayOf(8,9,4,5,3,6,2,0,1,7), intArrayOf(9,4,3,8,6,1,7,2,0,5), intArrayOf(2,5,8,1,4,3,6,7,9,0))
    fun damm(s: String): Int { var interim = 0; for (ch in s) interim = damm[interim][ch - '0']; return interim }
    private val vd = arrayOf(
        intArrayOf(0,1,2,3,4,5,6,7,8,9), intArrayOf(1,2,3,4,0,6,7,8,9,5), intArrayOf(2,3,4,0,1,7,8,9,5,6), intArrayOf(3,4,0,1,2,8,9,5,6,7), intArrayOf(4,0,1,2,3,9,5,6,7,8),
        intArrayOf(5,9,8,7,6,0,4,3,2,1), intArrayOf(6,5,9,8,7,1,0,4,3,2), intArrayOf(7,6,5,9,8,2,1,0,4,3), intArrayOf(8,7,6,5,9,3,2,1,0,4), intArrayOf(9,8,7,6,5,4,3,2,1,0))
    private val vp = arrayOf(
        intArrayOf(0,1,2,3,4,5,6,7,8,9), intArrayOf(1,5,7,6,2,8,3,0,9,4), intArrayOf(5,8,0,3,7,9,6,1,4,2), intArrayOf(8,9,1,6,0,4,3,5,2,7),
        intArrayOf(9,4,5,3,1,2,6,8,7,0), intArrayOf(4,2,8,6,5,7,3,9,0,1), intArrayOf(2,7,9,3,8,0,6,4,1,5), intArrayOf(7,0,4,6,9,1,3,2,5,8))
    private val vinv = intArrayOf(0,4,3,2,1,5,6,7,8,9)
    fun verhoeff(s: String): Int { var c = 0; s.reversed().forEachIndexed { i, ch -> c = vd[c][vp[(i + 1) % 8][ch - '0']] }; return vinv[c] }
}

object ControlAlgorithms {
    /** Builds the strategy from the policy.integrity.controlAlgorithm object (already converted to a Map). */
    fun from(spec: Map<String, Any?>): ControlAlgorithm = when (val name = (spec["name"] as String?)?.lowercase() ?: "iso7064-mod97-10") {
        "iso7064-mod97-10", "iso7064" -> iso7064(spec)
        "permissive" -> PermissiveControl
        "fit" -> fit(spec)
        else -> throw IllegalArgumentException("Unknown controlAlgorithm: $name")
    }

    private fun mapper(p: Map<String, Any?>) = DigitMapper(
        alphabet = p["alphabet"] as String? ?: "a1",
        dropNonAlnum = p["dropNonAlnum"] as Boolean? ?: true,
        dropNonAscii = p["dropNonAscii"] as Boolean? ?: true,
        caseFold = p["caseFold"] as Boolean? ?: true,
    )
    private fun num(p: Map<String, Any?>, k: String, def: Long) = (p[k] as Number?)?.toLong() ?: def

    private fun iso7064(p: Map<String, Any?>) = Iso7064Control(mapper(p), p["suffix"] as String? ?: "00", num(p, "modulus", 97), num(p, "base", 98))

    /** harness/control-fit.json as written by fit.py: {"familia", "params", "verificado_sobre", "formula_texto"}. */
    fun fit(spec: Map<String, Any?>): ControlAlgorithm {
        // inline form (POLICY_JSON): the contents of control-fit.json under {"name":"fit", ...}
        if (spec["params"] != null) {
            val params = spec["params"].let { if (it is io.vertx.core.json.JsonObject) it.map else @Suppress("UNCHECKED_CAST") (it as Map<String, Any?>) }
            return if (spec["familia"] == "permissive") PermissiveControl else FitControl(params)
        }
        val path = spec["file"] as String? ?: "harness/control-fit.json"
        val candidates = listOf(File(path), File("..", path), File(System.getProperty("user.dir"), path))
        val file = candidates.firstOrNull { it.isFile } ?: throw IllegalStateException("control-fit file not found: $path")
        return fromFitJson(io.vertx.core.json.JsonObject(file.readText(Charsets.UTF_8)))
    }

    fun fromFitJson(json: io.vertx.core.json.JsonObject): ControlAlgorithm =
        if (json.getString("familia") == "permissive") PermissiveControl else FitControl(json.getJsonObject("params")?.map ?: emptyMap())
}

package cn

import cn.adapters.config.PolicyLoader
import cn.domain.Iso7064
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Unit checks: control algorithm, resources in sync with ../rules, hidden commands, canonical control compare. */
class UnitTest {
    @Test fun iso7064ControlValues() {
        assertEquals(64, Iso7064.control("PING"))
        assertEquals(82, Iso7064.control("ALTA;Lucia"))
        assertEquals(3, Iso7064.control("ALTA;Mateo"))
        assertEquals(14, Iso7064.control("CONSULTA;AC-0001"))
        assertEquals(86, Iso7064.control("DEPOSITO;AC-0001;250000"))
        assertEquals(4, Iso7064.control("DEPOSITO;AC-0001;999999999999"))
        assertEquals(25, Iso7064.control("ALTA;Juan Perez"))
    }

    @Test fun policyResourceMatchesRules() {
        val rules = File("../rules/policy-v1.json")
        if (!rules.isFile) return
        val res = PolicyLoader::class.java.getResourceAsStream("/policy-v1.json")!!.readBytes().toString(Charsets.UTF_8)
        assertEquals(rules.readText(Charsets.UTF_8), res, "src/main/resources/policy-v1.json is out of sync with ../rules (run gradle syncRules)")
    }

    /** HALT/NOTAS follow the order E103>E104>E201>E202>E301>E302 (invalid/malformed control = assumption, no log evidence). */
    @Test fun hiddenCommandsFollowIntegrityOrder() {
        val p = PolicyLoader.load("v1", null)
        val h = cn.domain.MsgPipeline(p, cn.adapters.json.VertxJsonCodec)
        fun send(m: String) = h.handle("{\"m\":\"$m\"}")
        assertEquals(p.error("E103"), send("HALT"))
        assertEquals(p.error("E104"), send("HALT;;71"))
        assertEquals(p.error("E201"), send("HALT;AC-0001;XX"))
        assertEquals(p.error("E202"), send("HALT;AC-0001;83"))     // assumption: the control is validated (82 is the valid one, L894)
        assertEquals(p.error("E302"), send("HALT;71"))              // L893
        assertEquals(p.response("HALT"), send("HALT;AC-0001;82"))   // L894
        assertEquals(p.response("HALT"), send("halt;ZZ-9999;${cn.domain.Iso7064.control("halt;ZZ-9999")}")) // assumption: account is not validated
        assertEquals(p.error("E201"), send("NOTAS;1X"))
        assertEquals(p.error("E202"), send("NOTAS;12"))             // assumption
        assertEquals(p.response("NOTAS"), send("NOTAS;11"))         // L896
        assertEquals(p.error("E302"), send("NOTAS;AC-0001;14"))     // L897
        assertEquals(p.error("E302"), send("PING;64;64"))           // L190
    }

    /** L1064-L1073: control with a leading zero -> E202 even if the value matches; order E202 > E301; HALT validates the control. */
    @Test fun canonicalControlCompare() {
        val p = PolicyLoader.load("v1", null)
        val h = cn.domain.MsgPipeline(p, cn.adapters.json.VertxJsonCodec)
        fun send(m: String) = h.handle("{\"m\":\"$m\"}")
        assertEquals(201, send("ALTA;Mateo;3").status)                 // L1067
        assertEquals(p.error("E202"), send("ALTA;Mateo;03"))           // L1070
        assertEquals(p.error("E202"), send("NOEXISTE;AC-0001;99"))     // L1064: control is checked before the command
        assertEquals(p.error("E201"), send("ALTA;Lucia;Gomez;XX"))     // L1065
        assertEquals(p.error("E303"), send("TRANSFER;AC-0001;AC-0001;0;74"))   // L1068
        assertEquals(p.error("E304"), send("TRANSFER;AC-0099;AC-0099;100;19")) // L1069
        assertEquals(p.error("E202"), send("HALT;AC-0001;99"))         // L1073
        assertEquals(p.error("E102"), h.handle("{\"m\":null}"))        // L1071
        assertEquals(p.error("E100"), h.handle(""))                     // L1072
    }

    @Test fun everyRuleNameResolves() {
        val p = PolicyLoader.load("v1", null)
        p.integrityOrder.forEach { cn.domain.IntegrityRegistry.rule(it) }
        p.commands.values.forEach { c -> c.rules.forEach { cn.domain.RuleRegistry.rule(it) }; c.postRules.forEach { cn.domain.RuleRegistry.postRule(it) } }
        assertTrue(p.errors.keys.containsAll(listOf("E001","E100","E101","E102","E103","E104","E201","E202","E301","E302","E303","E304","E401","E402","E404","E506","E507","E510")))
    }
}

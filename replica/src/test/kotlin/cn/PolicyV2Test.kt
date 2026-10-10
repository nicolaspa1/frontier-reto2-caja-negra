package cn

import cn.adapters.json.VertxJsonCodec
import cn.domain.Iso7064
import cn.domain.MsgPipeline
import cn.domain.Response
import cn.domain.decimal
import io.vertx.core.json.JsonObject
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * Explicit coverage of version 2 of the original (ticket FR-0153, `rules/policy-v2.json`), independent of the recorded
 * log: transfer limit 50000 (L5760, L5559), E510 as 423 with `intentos` (L5562-L5564, L5660), the two-decimal balance
 * string of CONSULTA (L5565, L5566, L5580, L5605) and the `decimal()` formatter behind it. The last test runs the same
 * code with policy v1 to show that the difference is data.
 */
class PolicyV2Test {
    private val v2 = TestSupport.policyV2()

    private fun pipeline(policy: cn.domain.Policy = v2) = MsgPipeline(policy, VertxJsonCodec)
    private fun MsgPipeline.send(payload: String): Response = handle("""{"m":"$payload;${Iso7064.control(payload)}"}""")
    private fun operationId(r: Response): String = JsonObject(r.body).getString("operacionId")

    /** AC-0001 (Lucia) funded with 1000000, AC-0002 (Mateo) empty. */
    private fun MsgPipeline.twoAccounts() {
        assertEquals("""{"cuentaId":"AC-0001"}""", send("ALTA;Lucia").body)
        assertEquals("""{"cuentaId":"AC-0002"}""", send("ALTA;Mateo").body)
        assertEquals(200, send("DEPOSITO;AC-0001;1000000").status)
    }

    @Test fun transferLimitIs50000Inclusive() {
        val h = pipeline(); h.twoAccounts()
        assertEquals(200, h.send("TRANSFER;AC-0001;AC-0002;50000").status)                       // L5760
        val over = h.send("TRANSFER;AC-0001;AC-0002;50001")                                       // L5559
        assertEquals(422, over.status)
        assertEquals("""{"error":"E506","mensaje":"límite excedido"}""", over.body)
        assertEquals(v2.error("E506"), h.send("TRANSFER;AC-0001;AC-0002;100000"))                 // L5560
    }

    @Test fun blockedAccountAnswers423WithAttemptCounter() {
        val h = pipeline(); h.twoAccounts()
        repeat(3) { assertEquals(422, h.send("TRANSFER;AC-0001;AC-0002;50001").status) }        // three E506 as origin
        val first = h.send("TRANSFER;AC-0001;AC-0002;10")
        assertEquals(423, first.status)                                                            // L5562
        assertEquals("""{"error":"E510","mensaje":"cuenta bloqueada","intentos":1}""", first.body)
        assertEquals("""{"error":"E510","mensaje":"cuenta bloqueada","intentos":2}""", h.send("TRANSFER;AC-0001;AC-0002;10").body) // L5563
        // A successful deposit to the blocked account lifts the block (v1 rule, L3669-L3671) and clears the counter.
        assertEquals(200, h.send("DEPOSITO;AC-0001;1").status)
        assertEquals(200, h.send("TRANSFER;AC-0001;AC-0002;10").status)
        // A new block starts counting again at 1.
        repeat(3) { assertEquals(422, h.send("TRANSFER;AC-0001;AC-0002;50001").status) }
        val again = h.send("TRANSFER;AC-0001;AC-0002;10")
        assertEquals(423, again.status)
        assertEquals("""{"error":"E510","mensaje":"cuenta bloqueada","intentos":1}""", again.body)
    }

    @Test fun consultaRendersBalanceWithTwoDecimals() {
        val h = pipeline()
        assertEquals("""{"cuentaId":"AC-0001"}""", h.send("ALTA;Lucia").body)
        repeat(22) { assertEquals(200, h.send("DEPOSITO;AC-0001;1").status) }
        val consulta = h.send("CONSULTA;AC-0001")
        assertEquals(200, consulta.status)
        assertEquals("""{"cuenta_id":"AC-0001","saldo":"0.22"}""", consulta.body)                 // L5565
        // Annulling a deposit whose funds already left the account drives the balance negative: -100 -> "-1.00" (L5605).
        assertEquals("""{"cuentaId":"AC-0002"}""", h.send("ALTA;Mateo").body)
        val deposit = h.send("DEPOSITO;AC-0002;100")
        assertEquals(200, deposit.status)
        assertEquals(200, h.send("TRANSFER;AC-0002;AC-0001;100").status)
        assertEquals(200, h.send("ANULA;${operationId(deposit)}").status)
        assertEquals("""{"cuenta_id":"AC-0002","saldo":"-1.00"}""", h.send("CONSULTA;AC-0002").body)
        assertEquals("""{"cuenta_id":"AC-0002","saldo":"-1.00"}""", h.send("CONSULTA;AC-0002").body) // ANULA twice does not double it
        // 1000000 -> "10000.00" (L5566 shape)
        assertEquals("""{"cuentaId":"AC-0003"}""", h.send("ALTA;Ana").body)
        assertEquals(200, h.send("DEPOSITO;AC-0003;1000000").status)
        assertEquals("""{"cuenta_id":"AC-0003","saldo":"10000.00"}""", h.send("CONSULTA;AC-0003").body)
    }

    @Test fun decimalFormatter() {
        assertEquals("0.00", decimal(0))
        assertEquals("0.05", decimal(5))
        assertEquals("0.22", decimal(22))
        assertEquals("1.00", decimal(100))
        assertEquals("-1.00", decimal(-100))
        assertEquals("-0.01", decimal(-1))
        assertEquals("10000.00", decimal(1_000_000))
        assertEquals("199999999.98", decimal(19_999_999_998))
    }

    /** Same code, policy v1: 100000 still passes, the block is a 409 without `intentos`, the balance is an integer. */
    @Test fun sameCodeWithPolicyV1AnswersAsVersion1() {
        val v1 = TestSupport.policyV1()
        val h = pipeline(v1); h.twoAccounts()
        assertEquals(200, h.send("TRANSFER;AC-0001;AC-0002;100000").status)                      // L68-L71
        repeat(3) { assertEquals(422, h.send("TRANSFER;AC-0001;AC-0002;100001").status) }
        val blocked = h.send("TRANSFER;AC-0001;AC-0002;10")
        assertEquals(409, blocked.status)                                                          // L87
        assertEquals("""{"error":"E510","mensaje":"cuenta bloqueada"}""", blocked.body)
        assertEquals("""{"cuenta_id":"AC-0002","saldo":100000}""", h.send("CONSULTA;AC-0002").body)
    }
}

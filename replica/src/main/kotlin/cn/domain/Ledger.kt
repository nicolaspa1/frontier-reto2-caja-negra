package cn.domain

/**
 * In-memory state. A single instance exists and it is only mutated from the Vert.x event loop
 * (one verticle), so no synchronisation is needed; `reset` restores the initial state.
 */
class Ledger(private val policy: Policy) {
    val accounts = LinkedHashMap<String, Account>()
    val operations = LinkedHashMap<String, Operation>()
    private var accountSeq = 0L
    private var operationSeq = 0L
    /** Set by a confirmed HALT; every message answers the halt error until [reset] (L2272-L2278). */
    var halted = false

    fun reset() { accounts.clear(); operations.clear(); accountSeq = 0; operationSeq = 0; halted = false }

    fun account(id: String): Account? = accounts[id]
    fun operation(id: String): Operation? = operations[id]

    fun openAccount(name: String): Account {
        val id = policy.accountId.format(++accountSeq)
        return Account(id, name, policy.limits.initialBalance).also { accounts[id] = it }
    }

    fun deposit(to: Account, amount: Long): Operation {
        to.balance += amount
        return record("DEPOSITO", null, to.id, amount, 0)
    }

    fun transfer(from: Account, to: Account, amount: Long, fee: Long): Operation {
        from.balance -= amount + fee
        to.balance += amount
        from.transferredOut += amount
        return record("TRANSFER", from.id, to.id, amount, fee)
    }

    /**
     * Reverts unconditionally, exactly like the original: the balance may go negative (L130).
     * Returns false if the operation had already been annulled (-> `ANULA_REPETIDA`).
     */
    fun annul(op: Operation): Boolean {
        if (op.annulled) return false
        op.annulled = true
        val to = accounts[op.to]
        val from = op.from?.let { accounts[it] }
        if (to != null) to.balance -= op.amount
        if (from != null) { from.balance += op.amount + op.fee; from.transferredOut -= op.amount }
        return true
    }

    private fun record(type: String, from: String?, to: String, amount: Long, fee: Long): Operation {
        val id = policy.operationId.format(++operationSeq)
        return Operation(id, type, from, to, amount, fee).also { operations[id] = it }
    }
}

package cn.domain

/** Shortcut for the default control algorithm (ISO 7064 MOD 97-10, letters A=1..26, non-alphanumerics dropped). */
object Iso7064 {
    private val default = Iso7064Control()
    fun control(payload: String): Int = default.expected(payload)
}

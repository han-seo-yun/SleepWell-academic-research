package test

import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/**
 * ★ 최소 테스트 프레임워크 (앱 빌드에 포함하지 않음)
 *
 * 왜 JUnit 을 안 쓰는가:
 *   이 저장소에는 Gradle 프로젝트가 없다. 검증 하네스처럼 kotlinc 로 개별 컴파일해서
 *   돌리는 구조라, 의존성 없이 컴파일·실행되는 게 이득이다.
 *   (ktest/junit 을 붙이면 jar 를 더 Chaplin 해야 하고, 뒤에 "왜 이거 안 되지?" 가 생긴다)
 *
 * 규칙은 하나. 실패하면 메시지에 **왜 그 값이 나와야 하는지**를 같이 적는다.
 * 숫자만 나열한 테스트는 나중에 깨져도 왜 깨졌는지 못 읽는다.
 */
object T {

    var passed = 0
    private val failures = mutableListOf<String>()
    private var currentGroup = "(none)"

    fun group(name: String) {
        currentGroup = name
        println()
        println("── $name ──")
    }

    fun ok(desc: String) {
        passed++
        println("  PASS  $desc")
    }

    fun fail(desc: String, detail: String) {
        failures += "[$currentGroup] $desc\n         $detail"
        println("  FAIL  $desc")
        println("        $detail")
    }

    fun assertTrue(desc: String, cond: Boolean, why: String = "") {
        if (cond) ok(desc) else fail(desc, why.ifEmpty { "조건이 false" })
    }

    fun assertFalse(desc: String, cond: Boolean, why: String = "") =
        assertTrue(desc, !cond, why.ifEmpty { "조건이 true" })

    fun assertEquals(desc: String, expected: Any?, actual: Any?, why: String = "") {
        if (expected == actual) {
            ok("$desc  = $actual")
        } else {
            fail(desc, "기대: $expected\n         실제: $actual" + if (why.isEmpty()) "" else "\n         이유: $why")
        }
    }

    fun assertClose(desc: String, expected: Double, actual: Double, eps: Double = 1e-9, why: String = "") {
        if (Math.abs(expected - actual) <= eps) {
            ok("$desc  = ${fmt(actual)} (±$eps)")
        } else {
            fail(desc, "기대: ${fmt(expected)} (±$eps)\n         실제: ${fmt(actual)}" +
                    if (why.isEmpty()) "" else "\n         이유: $why")
        }
    }

    /** 서로 달라야 하는데 같으면 실패 — 회귀 버그를 잡는 용도. */
    fun assertNotSame(desc: String, a: Any?, b: Any?, why: String) {
        if (a != b) {
            ok("$desc  ($a != $b)")
        } else {
            fail(desc, "둘 다 $a 로 같다\n         이유: $why")
        }
    }

    private fun fmt(v: Double): String = String.format("%.8f", v)

    fun summary(): Int {
        println()
        println("=".repeat(70))
        if (failures.isEmpty()) {
            println("전부 통과: $passed 개")
            println("=".repeat(70))
            return 0
        }
        println("실패 ${failures.size} 개 / 통과 $passed 개")
        println("=".repeat(70))
        failures.forEach { println("  · $it"); println() }
        return 1
    }
}

/** KST 문자열 → epoch ms. 테스트 입력은 전부 KST 로 적는다. */
object Kst {
    private val fmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("Asia/Seoul") }

    fun ms(text: String): Long = fmt.parse(text)!!.time

    fun text(ms: Long): String = fmt.format(java.util.Date(ms))

    /** a 와 b 사이를 stepMs 간격으로 순회 (양 끝 포함). */
    fun range(fromMs: Long, toMs: Long, stepMs: Long): List<Long> {
        val out = mutableListOf<Long>()
        var t = fromMs
        while (t <= toMs) { out += t; t += stepMs }
        return out
    }
}
package verify

import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.DirectInput
import app.sleepwell.health.HealthResult
import app.sleepwell.health.TimeFmt
import java.io.File
import java.lang.reflect.Method
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/*
 * ★ 검증용 공통 코드 (앱 빌드에 포함하지 않음)
 *
 * 30초 버전과 1분 버전은 AnalysisResult 필드가 하나 다르다.
 *   - 30초: epochIntervalMs / epochIntervalLabel / inputPeriodStartMs / inputPeriodEndMs 없음
 *   - 1분 : 위 4개 추가
 * 그래서 1분 전용 필드는 리플렉션으로 "있으면 읽고 없으면 빈칸"으로 처리한다.
 * 같은 Report.kt를 두 버전 jar에 각각 넣으면 출력이 같은 형식이라 바로 비교가 된다.
 */

object Kst {
    private val fmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("Asia/Seoul") }

    fun parse(text: String): Long = fmt.parse(text)!!.time

    /** 인자로 넘어온 DirectInput 시각을 AnalysisResult 에 넣는다. */
    fun directInputOf(args: Array<String>): DirectInput? =
        if (args.size >= 4) DirectInput(
            sleepStartMs = parse(args[2]),
            sleepEndMs = parse(args[3]),
            targetWakeMs = null,
            plannedBedMs = null
        ) else null

    fun header(di: DirectInput?): String =
        if (di == null) "directInput=none"
        else "directInput=${TimeFmt.full(di.sleepStartMs!!)}~${TimeFmt.full(di.sleepEndMs!!)}"
}

object Report {

    fun write(tag: String, result: HealthResult<AnalysisResult>, outPath: String, extraHeader: String) {
        val text = build(tag, extraHeader, result)
        File(outPath).writeText(text, Charsets.UTF_8)
        println(text)
    }

    fun build(tag: String, extraHeader: String, result: HealthResult<AnalysisResult>): String {
        val sb = StringBuilder()
        sb.appendLine("tag=$tag")
        sb.appendLine(extraHeader)
        when (result) {
            is HealthResult.Success -> sb.append(buildSuccess(result.data))
            is HealthResult.Unavailable -> {
                sb.appendLine("status=Unavailable")
                sb.appendLine("reason=${result.reason}")
            }
            is HealthResult.PermissionDenied -> sb.appendLine("status=PermissionDenied")
            is HealthResult.Failure -> {
                sb.appendLine("status=Failure")
                sb.appendLine("error=${result.error}")
                result.error.stackTraceToString().lines().take(20).forEach { sb.appendLine("at=$it") }
            }
        }
        return sb.toString()
    }

    private fun buildSuccess(r: AnalysisResult): String {
        val sb = StringBuilder()
        sb.appendLine("status=Success")
        sb.appendLine("modelVersion=${r.modelVersion}")
        sb.appendLine("epochIntervalMs=${r.optLong("EpochIntervalMs") ?: "-"}")
        sb.appendLine("epochIntervalLabel=${r.optString("EpochIntervalLabel") ?: "-"}")
        val periodStart = r.optLong("InputPeriodStartMs")
        val periodEnd = r.optLong("InputPeriodEndMs")
        sb.appendLine("inputPeriodStartMs=${periodStart ?: "-"}")
        sb.appendLine("inputPeriodEndMs=${periodEnd ?: "-"}")
        if (periodStart != null) sb.appendLine("inputPeriodStartText=${TimeFmt.full(periodStart)}")
        if (periodEnd != null) sb.appendLine("inputPeriodEndText=${TimeFmt.full(periodEnd)}")
        sb.appendLine("epochCount=${r.scHistory.size}")
        sb.appendLine("futurePointCount=${r.futureS?.size ?: -1}")
        sb.appendLine("sessionCount=${r.sleepRecords.size}")
        sb.appendLine("currentS=${fmt(r.currentS)}")
        sb.appendLine("currentC=${fmt(r.currentC)}")
        sb.appendLine("currentPropensity=${fmt(r.currentPropensity)}")
        sb.appendLine("Smin=${fmt(r.scHistory.minOf { it.S })}")
        sb.appendLine("Smax=${fmt(r.scHistory.maxOf { it.S })}")
        sb.appendLine("Cmin=${fmt(r.scHistory.minOf { it.C })}")
        sb.appendLine("Cmax=${fmt(r.scHistory.maxOf { it.C })}")
        sb.appendLine("propensityMin=${fmt(r.scHistory.minOf { it.propensity })}")
        sb.appendLine("propensityMax=${fmt(r.scHistory.maxOf { it.propensity })}")
        sb.appendLine("firstEpochText=${TimeFmt.full(r.scHistory.first().timestampMs)}")
        sb.appendLine("lastEpochText=${TimeFmt.full(r.scHistory.last().timestampMs)}")

        // 15분 간격 샘플 — 그래프 전달 간격과 동일. 시각 좌표를 남겨야
        // 30초/1분이 같은 물리량을 말하는지 눈으로 확인할 수 있다.
        sb.appendLine("--- graph-15min ---")
        var lastBucket = Long.MIN_VALUE
        r.scHistory.forEach { p ->
            val bucket = p.timestampMs / (15 * 60_000L)
            if (bucket != lastBucket) {
                lastBucket = bucket
                sb.appendLine(
                    "${TimeFmt.full(p.timestampMs)},${fmt(p.S)},${fmt(p.C)},${fmt(p.propensity)}"
                )
            }
        }

        // S가 실제로 출렁이는지. 단조로 붙으면 epoch 반영이 깨진 것.
        val diffs = r.scHistory.zipWithNext { a, b -> b.S - a.S }
        sb.appendLine("--- sanity ---")
        sb.appendLine("sRiseCount=${diffs.count { it > 1e-12 }}")
        sb.appendLine("sFallCount=${diffs.count { it < -1e-12 }}")
        sb.appendLine("sFlatCount=${diffs.count { kotlin.math.abs(it) <= 1e-12 }}")

        sb.appendLine("--- sessions ---")
        r.sleepRecords.forEach { s ->
            sb.appendLine(
                "session,${TimeFmt.full(s.startMs)},${TimeFmt.full(s.endMs)}," +
                    "${fmt(s.totalSleepMin)}," +
                    s.stageSummary.entries.sortedBy { it.key }
                        .joinToString("|") { "${it.key}=${fmt(it.value)}" }
            )
        }
        return sb.toString()
    }

    private fun fmt(v: Double): String = String.format("%.6f", v)

    private fun AnalysisResult.optLong(field: String): Long? = optCall(field) as? Long

    private fun AnalysisResult.optString(field: String): String? = optCall(field) as? String

    private fun AnalysisResult.optCall(field: String): Any? = try {
        val getter = "get" + field.replaceFirstChar { it.uppercase() }
        val m: Method? = this::class.java.methods.firstOrNull { it.name == getter && it.parameterCount == 0 }
        m?.invoke(this)
    } catch (e: Exception) {
        null
    }
}
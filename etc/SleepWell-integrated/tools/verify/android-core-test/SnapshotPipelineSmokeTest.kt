package test

import app.sleepwell.health.*
import java.time.LocalDateTime
import java.time.ZoneId

private fun ms(value: String): Long = LocalDateTime.parse(value)
    .atZone(ZoneId.of("Asia/Seoul")).toInstant().toEpochMilli()

fun main() {
    val sessions = (1..5).map { day ->
        val start = ms("2026-10-0${day}T23:00:00")
        val end = start + 8 * 3_600_000L
        SleepSession(
            sessionId = "S$day",
            startMs = start,
            endMs = end,
            stages = listOf(SleepStageSegment(start, end, SleepStage.N2))
        )
    }
    val snapshot = HealthSnapshot(
        subjectId = "test",
        queriedFrom = ms("2026-10-01T20:00:00"),
        queriedTo = ms("2026-10-06T20:00:00"),
        sourceTag = "fake",
        sleepSessions = sessions
    )
    val fallback = SnapshotAnalysisPipeline.analyze(snapshot)
    check(fallback is HealthResult.Success)
    check(fallback.data.recommendation?.modelStatus == "physics_only_untrained")

    val ridge = RidgeResidualModel(
        1, "smoke-ridge", "trained", listOf("current_s"),
        listOf(0.5), listOf(0.0), listOf(1.0),
        20.0, listOf(0.0), -10.0, listOf(0.0)
    )
    val hybrid = SnapshotAnalysisPipeline.analyze(snapshot, residualModel = ridge)
    check(hybrid is HealthResult.Success)
    check(hybrid.data.recommendation?.modelStatus == "hybrid_trained")
    check(hybrid.data.recommendation?.bedtimeResidualMin == 20.0)
    println("ANDROID_CORE_RESULT=PASS")
}

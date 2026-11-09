package verify

import app.sleepwell.health.EpochInterval
import app.sleepwell.health.SleepAnalysisPipeline
import java.io.File

/*
 * ★ 검증용 러너 — 1분 버전 (앱 빌드에 포함하지 않음)
 *
 * 1분 소스를 빌드해 같은 CSV·같은 기준 시각에 돌린다.
 * 30초 러너와 출력이 같은 형식이라 그대로 비교가 된다.
 *
 * 인자: <csv> <출력> [<directInput 취침> <directInput 기상>]
 */

fun main(args: Array<String>) {
    val directInput = Kst.directInputOf(args)

    val result = SleepAnalysisPipeline.analyze(
        input = File(args[0]).inputStream(),
        directInput = directInput,
        referenceTimeMs = null,
        epochInterval = EpochInterval.MIN1,
        alignToEpochBoundary = true
    )

    val tag = if (directInput == null) "1MIN" else "1MIN+DI"
    Report.write(tag, result, args[1], Kst.header(directInput))
}
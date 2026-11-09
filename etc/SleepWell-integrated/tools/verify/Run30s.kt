package verify

import app.sleepwell.health.SleepAnalysisPipeline
import java.io.File

/*
 * ★ 검증용 러너 — 30초 버전 (앱 빌드에 포함하지 않음)
 *
 * 현행(30초) 소스를 그대로 빌드해 실제 CSV에 돌린다.
 * epochInterval 인자가 없는 게 30초 버전의 특징이며, 같은 물리량을 말하는지
 * (즉 1분 리팩터링이 30초 결과를 안 바꾸었는지) 확인하는 게 목적이다.
 *
 * 인자: <csv> <출력> [<directInput 취침> <directInput 기상>]
 */

fun main(args: Array<String>) {
    val directInput = Kst.directInputOf(args)

    val result = SleepAnalysisPipeline.analyze(
        input = File(args[0]).inputStream(),
        directInput = directInput,
        referenceTimeMs = null
    )

    val tag = if (directInput == null) "30S" else "30S+DI"
    Report.write(tag, result, args[1], Kst.header(directInput))
}
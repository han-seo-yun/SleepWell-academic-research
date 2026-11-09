package test

/**
 * ★ 테스트 러너 — EpochInterval 이 없는 버전 (src/ · src-epoch-30s/)
 *
 * EpochInterval 타입을 참조하는 테스트(EpochGridTest, SleepEpochConverterTest,
 * SleepAnalysisPipelineTest)는 이 빌드에 포함하지 않는다. 30초 버전은
 * `EPOCH_INTERVAL_MS = 30_000L` 상수 하나라 간격 인자가 없기 때문이다.
 *
 * 대신 **버전과 무관하게 통과해야 하는 것**만 돌린다. 지금은 clockHours 회귀 하나.
 * 그게 30초 경로에도 버그가 없다는 뜻이라, 두 폴더가 같은 상태인지 확인하는 용도로도 쓴다.
 */
fun main() {
    println("SleepWell 회귀 테스트 — 30초 버전 (EpochInterval 없음)")
    println("=".repeat(70))

    T.group("시작")
    T.ok("테스트 러너 기동")

    TwoProcessModelTest.run()

    val code = T.summary()
    if (code == 0) {
        println("RESULT=PASS")
    } else {
        println("RESULT=FAIL")
    }
    // ★ 종료 코드를 반드시 0 이 아닌 값으로 끝내야 스크립트가 실패를 알린다.
    //   Unit 을 돌려주는 main 은 JVM exit code 가 항상 0 이 된다.
    kotlin.system.exitProcess(code)
}
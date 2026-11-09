package test

/**
 * ★ 테스트 러너 — EpochInterval 이 있는 버전 (src-epoch-1min/)
 *
 * 빌드: kotlinc 로 src-epoch-1min + 스텁 + 이 폴더를 함께 컴파일해 돌린다.
 * 실행 인자 없이 돈다. (검증 하네스처럼 CSV 경로를 넘기지 않는다 — 합성 CSV 를 쓴다)
 */
fun main() {
    println("SleepWell 회귀 테스트 — src-epoch-1min (1분 버전)")
    println("=".repeat(70))

    T.group("시작")
    T.ok("테스트 러너 기동")

    EpochGridTest.run()
    SleepEpochConverterTest.run()
    TwoProcessModelTest.run()
    RidgeResidualModelTest.run()
    HybridRecommendationEngineTest.run()
    SleepAnalysisPipelineTest.run()

    val code = T.summary()
    if (code == 0) {
        println("RESULT=PASS")
    } else {
        println("RESULT=FAIL")
    }
    // ★ exitProcess 가 없으면 JVM 종료 코드가 항상 0 이 된다.
    //   그러면 스크립트/CI 가 "전부 통과"로 오인한다.
    //   실제로 이 버그가 있어서 RESULT=FAIL 인데 요약은 "전체 통과" 로 출력됐었다.
    kotlin.system.exitProcess(code)
}

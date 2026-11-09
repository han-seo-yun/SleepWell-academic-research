package app.sleepwell.health

import kotlin.math.ceil

/**
 * SleepSession 목록을 30초 epoch 타임라인으로 변환.
 *
 * 중요: 세션 사이 깨어 있던 시간(WAKE)까지 포함한 전체 타임라인을 만들어야 한다.
 * 수면 구간만 넣으면 세션 사이를 모두 수면으로 간주하여 S 계산이 틀어진다.
 *
 * 예: 세션 A 종료 06:48, 세션 B 시작 18:24 → 사이 11시간 36분도 isSleep=false로 포함.
 */
object SleepEpochConverter {

    /** 30초 (ms) */
    private const val EPOCH_INTERVAL_MS: Long = 30_000L

    /**
     * SleepSession 목록과 시간 범위를 받아 30초 epoch 타임라인을 생성.
     *
     * @param sessions  파싱된 SleepSession 목록
     * @param startTimeMs 타임라인 시작 시각 (KST epoch ms). 가장 이른 세션 시작 또는 사용자 지정 시각.
     * @param endTimeMs   타임라인 종료 시각 (KST epoch ms). 가장 늦은 세션 종료 또는 현재 시각.
     * @return 30초 간격 epoch 리스트 (startMs 기준 정렬)
     */
    fun toEpochTimeline(
        sessions: List<SleepSession>,
        startTimeMs: Long,
        endTimeMs: Long
    ): List<EpochInput> {
        if (sessions.isEmpty()) return emptyList()
        if (endTimeMs <= startTimeMs) return emptyList()

        // 세션 구간들을 startMs 기준 정렬
        val sortedSegments = sessions.flatMap { session ->
            session.stages.map { stage ->
                Segment(startMs = stage.startMs, endMs = stage.endMs, stage = stage.stage)
            }
        }.sortedBy { it.startMs }

        val result = mutableListOf<EpochInput>()

        // startTimeMs부터 endTimeMs까지 30초 간격으로 순회
        var cursor = startTimeMs
        while (cursor < endTimeMs) {
            val epochEnd = cursor + EPOCH_INTERVAL_MS
            val isSleep = isSleepAt(sortedSegments, cursor, epochEnd)
            result.add(EpochInput(
                timestampMs = cursor,
                isSleep = isSleep
            ))
            cursor = epochEnd
        }

        return result
    }

    /**
     * 주어진 epoch 구간 [epochStart, epochEnd)가 어떤 수면 단계 구간에 속하는지 판별.
     *
     * - 어떤 SleepStageSegment와라도 겹치면 해당 stage의 isSleep 값을 사용
     * - 여러 구간과 겹치면 더 긴 겹침을 기준으로 함
     * - 어떤 구간에도 속하지 않으면 WAKE → isSleep = false
     */
    private fun isSleepAt(
        segments: List<Segment>,
        epochStart: Long,
        epochEnd: Long
    ): Boolean {
        val overlapping = segments.filter { segment ->
            segment.startMs < epochEnd && segment.endMs > epochStart
        }

        if (overlapping.isEmpty()) {
            return false // WAKE (세션 사이에 있는 시간)
        }

        // 가장 긴 겹침을 기준으로 isSleep 결정
        val best = overlapping.maxByOrNull { minOf(it.endMs, epochEnd) - maxOf(it.startMs, epochStart) }
        return when (best?.stage) {
            SleepStage.WAKE, SleepStage.UNKNOWN -> false
            SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM -> true
            else -> false
        }
    }

    /**
     * epoch 리스트를 두 배열로 분리 (Two-Process 모델 입력 형식).
     */
    fun toModelInputArrays(epochTimeline: List<EpochInput>): Pair<List<Long>, List<Boolean>> {
        return Pair(
            epochTimeline.map { it.timestampMs },
            epochTimeline.map { it.isSleep }
        )
    }

    // ────────────────────────────────────────────────
    // 내부 클래스
    // ────────────────────────────────────────────────

    /** epoch 한 조각 */
    data class EpochInput(
        val timestampMs: Long,  // epoch 시작 시각 (KST epoch ms)
        val isSleep: Boolean     // 해당 epoch가 수면 중이면 true
    )

    /** 평탄화된 수면 단계 구간 */
    private data class Segment(
        val startMs: Long,
        val endMs: Long,
        val stage: SleepStage
    )
}

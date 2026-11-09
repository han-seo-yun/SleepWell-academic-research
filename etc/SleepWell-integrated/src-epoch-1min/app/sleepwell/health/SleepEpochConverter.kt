package app.sleepwell.health

/**
 * SleepSession 목록을 epoch 타임라인으로 변환. (1분 버전)
 *
 * 30초 버전(`src-epoch-30s/`)은 `EPOCH_INTERVAL_MS = 30_000L` 로 고정돼 있었다.
 * 여기서는 간격을 `EpochInterval` 인자로 받는다. 기본값은 2026-10-02 부터 `EpochInterval.MIN1`
 * (팀 공통 기준). 30초 동작은 `EpochInterval.SEC30` + `alignToEpochBoundary = false` 로
 * 그대로 재현할 수 있다.
 *
 * 중요: 세션 사이 깨어 있던 시간(WAKE)까지 포함한 전체 타임라인을 만들어야 한다.
 * 수면 구간만 넣으면 세션 사이를 모두 수면으로 간주하여 S 계산이 틀어진다.
 *
 * 예: 세션 A 종료 06:48, 세션 B 시작 18:24 → 사이 11시간 36분도 isSleep=false로 포함.
 *
 * 성능: isSleepAt을 두 포인터 방식으로 O(epoch 수 + 구간 수)에 수행.
 *   - sortedSegments는 startMs 기준 정렬됨
 *   - epoch cursor가 진행할 때 pointer도 함께 앞으로만 이동
 *
 * 정렬(alignToEpochBoundary):
 *   true면 타임라인 시작 시각을 epoch 경계로 내림해 맞춘다. 기본값 true.
 *   기준 코드 TwoProcessModel이 epoch 시각의 시계 시각(분/60, 초/3600)을 그대로
 *   C_sleep에 쓰기 때문에, 경계에서 어긋나면 C가 최대 interval/2 만큼 밀린다.
 *   30초를 그대로 재현하려면 false를 넘기면 된다(기존 동작과 동일).
 */
object SleepEpochConverter {

    /**
     * SleepSession 목록과 시간 범위를 받아 epoch 타임라인을 생성.
     *
     * @param sessions  파싱된 SleepSession 목록
     * @param startTimeMs 타임라인 시작 시각 (KST epoch ms). 참조 시간 - 7일 또는 직접 입력.
     * @param endTimeMs   타임라인 종료 시각 (KST epoch ms). 참조 시간 또는 마지막 세션 endMs.
     * @param epochInterval epoch 간격. 기본값 EpochInterval.MIN1 (팀 공통 기준). 30초는 SEC30.
     * @param alignToEpochBoundary 시작 시각을 epoch 경계로 내림할지. 기본 true.
     * @return epoch 리스트 (timestampMs 오름차순). 마지막 epoch는 interval보다 짧아도 포함.
     */
    fun toEpochTimeline(
        sessions: List<SleepSession>,
        startTimeMs: Long,
        endTimeMs: Long,
        epochInterval: EpochInterval = EpochInterval.DEFAULT,
        alignToEpochBoundary: Boolean = true
    ): List<EpochInput> {
        if (sessions.isEmpty()) return emptyList()
        if (endTimeMs <= startTimeMs) return emptyList()

        val intervalMs = epochInterval.intervalMs

        // 세션 구간들을 startMs 기준 정렬
        val sortedSegments = sessions.flatMap { session ->
            session.stages.map { stage ->
                Segment(startMs = stage.startMs, endMs = stage.endMs, stage = stage.stage)
            }
        }.sortedBy { it.startMs }

        val result = mutableListOf<EpochInput>()

        // 두 포인터: cursor = epoch 진행, segIdx = 다음 검사할 구간
        var cursor = if (alignToEpochBoundary) EpochGrid.floorTo(startTimeMs, epochInterval)
                     else startTimeMs
        var segIdx = 0
        val n = sortedSegments.size

        while (cursor < endTimeMs) {
            val epochStart = cursor
            val epochEnd = cursor + intervalMs

            // segIdx를 현재 epoch와 겹치는 첫 구간으로 이동
            while (segIdx < n && sortedSegments[segIdx].endMs <= epochStart) {
                segIdx++
            }

            val isSleep = isSleepAtTwoPointer(
                sortedSegments, segIdx, n, epochStart, epochEnd
            )
            result.add(EpochInput(
                timestampMs = epochStart,
                isSleep = isSleep
            ))
            cursor = epochEnd
        }

        return result
    }

    /**
     * 두 포인터 방식 isSleepAt.
     *
     * - segIdx 이전 구간은 이미 epochStart 이전 종료 → 검사 불필요
     * - 현재 segIdx부터 처음으로 epochEnd 이후 종료하는 구간까지 검사
     * - 겹치는 구간이 없거나 모두 WAKE/UNKNOWN이면 false
     *
     * 한 epoch 내에서 여러 구간이 겹칠 수 있으므로 처음 겹치는 구간부터
     * epochEnd를 넘어가는 구간까지 모두 검사. 가장 긴 겹침 기준.
     */
    private fun isSleepAtTwoPointer(
        segments: List<Segment>,
        segIdx: Int,
        n: Int,
        epochStart: Long,
        epochEnd: Long
    ): Boolean {
        var bestOverlap = 0L
        var bestStage: SleepStage? = null
        var cur = segIdx

        while (cur < n && segments[cur].startMs < epochEnd) {
            val seg = segments[cur]
            if (seg.endMs > epochStart) {
                // 겹침: [max(epochStart, seg.startMs), min(epochEnd, seg.endMs)]
                val overlapStart = epochStart.coerceAtLeast(seg.startMs)
                val overlapEnd = epochEnd.coerceAtMost(seg.endMs)
                val overlap = overlapEnd - overlapStart
                if (overlap > bestOverlap) {
                    bestOverlap = overlap
                    bestStage = seg.stage
                }
            }
            cur++
        }

        return when (bestStage) {
            SleepStage.WAKE, SleepStage.UNKNOWN -> false
            SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM -> true
            null -> false
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
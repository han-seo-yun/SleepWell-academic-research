package app.sleepwell.analysis

import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.DisplaySettings
import app.sleepwell.health.DirectInput
import app.sleepwell.health.HealthResult
import app.sleepwell.health.LimitCode
import app.sleepwell.health.PersonalizationStage
import app.sleepwell.health.ReasonCode
import app.sleepwell.health.PredictedPoint
import app.sleepwell.health.PropensityPoint
import app.sleepwell.health.SleepRecord
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/*
 * 분석 결과(AnalysisResult) → 화면에 그릴 값으로 정리하는 곳. (소빈 · 2026-09-25)
 *
 * 화면 코드(AnalysisScreen)와 분리한 이유
 *  - 계산·해석 규칙을 Android 없이 단위 테스트로 확인하기 위해서.
 *  - 채윤 쪽 AnalysisResult 가 바뀌어도 이 파일만 맞추면 화면은 그대로 둘 수 있게.
 *
 * ★ (2026-09-26) "높음/보통/낮음" 임의 경계는 없앴다. 값은 실제 숫자와 본인 기록(최근 7일)에 비교한 문장으로 설명한다.
 *   모델(서윤 #7)과 파라미터가 확정되지 않았으므로, 화면에 "임시 모델로 계산"이라고 함께 표시한다.
 *   값의 방향은 교정 보고서 기준: C_sleep 은 수면 중간 시각에 +최대, propensity = S + C_sleep (클수록 잠들기 쉬움).
 *
 * ★ (2026-10-24 A안) 날짜 하나 = **그날 아침 일어난 시각**이 기준 (아래 "○○ 밤" 설명은 이전 방식, 기록용)
 *   - 고른 날의 기준 시각으로 한 번 계산 → 지난밤 수면 분석(그래프 + 해석 ①②③) + 그날 밤 수면 일정(예상·권장, 지난 날은 실제 참고)
 *   - 기상 기록(3시간 이상 잠)이 없는 날은 그날 낮 12시가 기준
 *
 * ★ (2026-10) "○○ 밤" 단위 조회 + 표시 시간대 (이전 방식)
 *   - 화면은 사용자가 고른 **밤 하나**(그날 12:00 ~ 다음 날 12:00, 표시 시간대 기준)를 보여준다.
 *     export 의 night 규칙(정오~정오)과 같은 경계라, 자정을 넘는 잠이 잘리지 않는다.
 *   - 모델 계산은 그대로(전체 기간 연속). 여기서는 그 결과에서 밤 하나만큼 잘라 보여줄 뿐이다.
 *   - "지금 상태"·"앞으로 예측"·개인 경향은 **가장 최근 밤**(마지막 기록이 들어 있는 밤)에서만 의미가 있어 그때만 채운다.
 *   - 고를 수 있는 밤 = 데이터(수면 기록) 전체 범위의 밤. (2026-10 변경: 이전엔 계산된 최근 7일 안만)
 *     지난 밤을 고르면 화면(AnalysisScreen)이 그 밤의 끝을 기준 시각으로 다시 계산한다 → 그 밤 앞 7일이 계산 구간.
 *     기록이 시작된 첫 밤은 그 앞 기록이 없어 수면 압력이 덜 정확하다(isPartial).
 *   - 시각·날짜 글자는 DisplaySettings.zone(표시 시간대)을 따른다.
 */

/** 그래프 한 점 */
data class ChartPoint(val timeMs: Long, val s: Double, val c: Double, val propensity: Double)

/**
 * 화면에서 고르는 "밤" 하나 = startMs(그날 12:00) ~ endMs(다음 날 12:00), 표시 시간대 기준.
 * @param isLatest 마지막 기록(기준 시각)이 들어 있는 밤 → "지금 상태"·예측을 보여준다
 * @param isPartial 데이터(수면 기록)가 시작된 직후의 밤 → 그 앞 기록이 없어 수면 압력이 덜 정확하다
 */
data class NightInfo(
    val startMs: Long,
    val endMs: Long,
    val label: String,
    val rangeText: String,
    val isLatest: Boolean,
    val isPartial: Boolean,
    /** (2026-10-24 A안) 이 날의 기준 시각 = 그날 아침 일어난 시각 (3시간 이상 잔 잠 중 가장 긴 것의 끝). 없으면 그날 낮 12시 */
    val basisMs: Long = 0L,
    /** 기준 시각이 실제 기상 시각인가 (false = 기상 기록이 없어 낮 12시로 대신함) */
    val hasWake: Boolean = true
)

/*
 * (2026-10-24 A안) 「지난밤 수면 분석」 해석 문장 ①②③.
 *  - 지시서 Ⅲ-3 "수치가 어떤지만이 아니라 그 수치가 무엇을 뜻하는지, 쉽게" → 문장마다 [숫자(무슨 일)] + [뜻(MEANING)] 순서.
 *  - 화면은 Seg 의 모양(style)대로 색·굵기·배경만 입힌다. 글은 전부 여기서 만든다.
 */
enum class SegStyle { PLAIN, BOLD, S, C, MEANING, MEANING_BOLD, SLEEP_SWATCH }
data class Seg(val text: String, val style: SegStyle = SegStyle.PLAIN)
data class AnalysisLine(val title: String, val segs: List<Seg>)

/** 지난밤 수면 한 줄 요약 (분석 카드 맨 위) */
data class LastSleepInfo(val rangeText: String, val detailText: String)

/**
 * (2026-10-24) 그래프 해설 문장과 그래프 위 표시를 잇는 번호.
 *  ① 평소 잠든·일어난 시각 (그래프 아래 눈금 위 ①)  ② 일주기 리듬 꼭대기 (▲)  ③ 지난번만큼 잠들기 쉬워지는 때 (예측선 위 점)
 */
data class ChartMarks(val onsetMs: Long? = null, val wakeMs: Long? = null, val reachMs: Long? = null)
// (2026-10-24 A안) ① = 지난밤 잠든 시각(수면 압력 선 위), ③ = 다시 잠들 때만큼 피로가 쌓이는 때(수면 압력 예측선 위 점). wakeMs 는 쓰지 않음

/** 그래프 배경에 칠할 수면 구간 */
data class SleepBand(val startMs: Long, val endMs: Long)

/** 값이 범위 안 어디쯤인지 보여주는 막대 */
data class Gauge(val min: Double, val max: Double, val value: Double, val leftLabel: String, val rightLabel: String)

enum class ReadingKind { S, C, P }

/**
 * 한 줄 해석: 제목 · 값 · 쉬운 설명.
 * (2026-09-26) 높음/보통/낮음 같은 임의 경계 대신, 실제 값·본인 기록과 비교한 문장으로 설명한다.
 */
data class Reading(
    val title: String,
    val value: String,
    val meaning: String,
    val kind: ReadingKind = ReadingKind.S,
    val gauge: Gauge? = null,
    val details: List<String> = emptyList(),
    val note: String? = null
)

/*
 * (2026-10-24 소빈) 「다음 수면 일정」 카드 — 작업 지시서 Ⅲ-3 "예측값과 권장값을 반드시 구분 + 의미 설명"
 *
 *  - 예상(모델) : 권장 엔진(HybridRecommendationEngine, 서윤)의 predictedBed/Wake 를 그대로 보여준다.
 *  - 예상(내 기록): 최근 7일 주 수면(3시간 이상)의 중간 취침·기상. 모델 값이 아니라 "본인 기록 요약"이다.
 *    → 둘을 나란히 두어, 엔진 값이 이상할 때(현재 새벽 4~5시 문제, 서윤 언니에게 보고 예정) 사용자가 비교할 수 있게.
 *  - 권장       : 목표 기상이 있을 때만 엔진의 recommendedBed/Wake/SleepMin.
 *    목표 기상이 없으면 숫자 대신 입력을 권한다 (목표 없이 나오는 권장값은 현재 새벽 4~5시로 치우침).
 *  - 기록 단계  : 14일 / 30일 기준은 서윤 미래예측모델 PERSONALIZATION_PROTOCOL 의 후보값 (확정 전).
 *    앱은 아직 단계별 개인화를 하지 않으므로 "(준비 중)"으로 표시한다.
 */
enum class ScheduleLevel { INFO, WARN, LOW }

data class ScheduleStatus(val text: String, val level: ScheduleLevel, val progress: Float? = null)

/** 시각 한 쌍. Epoch 값은 저장 후 표시 시간대를 다시 적용할 때 쓴다. */
data class ScheduleTimes(
    val bedText: String,
    val wakeText: String,
    val bedMs: Long? = null,
    val wakeMs: Long? = null
)

data class ScheduleRecommendation(
    val targetWakeText: String,
    val bedText: String,
    val sleepText: String?,
    val compareText: String?,
    val targetWakeMs: Long? = null,
    val bedMs: Long? = null
)

data class Schedule(
    val basisText: String,
    val status: ScheduleStatus,
    val modelLabel: String,
    val model: ScheduleTimes?,
    val modelNote: String,
    val habit: ScheduleTimes?,
    val habitNote: String,
    val recommended: ScheduleRecommendation?,
    val needsTargetWake: Boolean,
    val unavailableText: String?,
    val note: String,
    /** (2026-10-24) 카드 제목. 가장 최근 = "다음 수면 일정", 지난 날 = "이 날 수면 일정 (20:00 기준)" */
    val title: String = "다음 수면 일정",
    /** 지난 날: 그 밤에 실제로 잔 시각 (3시간 이상 중 가장 긴 수면). 가장 최근 밤은 null */
    val actual: ScheduleTimes? = null,
    val actualNote: String? = null,
    val isPast: Boolean = false,
    /** (2026-10-24) 지난 날: 예상과 실제의 차이를 말로 ("실제로 5시간 22분 일찍 잠들고, 15분 늦게 일어났어요.") */
    val diffText: String? = null,
    /** (2026-10-24) 결과는 나왔지만 제한이 있는 이유 (채윤 LimitCode → 문장). 상태 줄 아래에 작게 */
    val notices: List<String> = emptyList(),
    val basisMs: Long? = null
)

/** (2026-10-24) 계산을 못 했을 때 보여 줄 안내 카드 (채윤 ReasonCode → 제목 + 문장) */
data class Notice(val title: String, val lines: List<String>)

data class AnalysisDisplay(
    val modelLabel: String,
    val isTemporaryModel: Boolean,
    /** 기준 시각 = 마지막 계산 시점 (과거 그래프와 미래 그래프의 경계) */
    val referenceMs: Long,
    val readings: List<Reading>,
    /** 과거 구간 (다운샘플) */
    val history: List<ChartPoint>,
    /** 미래 구간 (다운샘플). 비어 있으면 "준비 중" */
    val future: List<ChartPoint>,
    val futureIsTemporary: Boolean,
    val sleepBands: List<SleepBand>,
    /** 앞으로 24시간 중 propensity 가 가장 큰 시각 (임시 예측). 미래 값이 없으면 null */
    val easiestSleepMs: Long?,
    val recentSleeps: List<SleepRecord>,
    val recommendationText: String,
    /** 직접 입력한 수면 구간 (그래프에 다른 색으로 칠함). 없으면 null */
    val manualSleep: SleepBand? = null,
    /** 직접 입력 요약 문구 (화면에 그대로 표시) */
    val inputNotes: List<String> = emptyList(),
    /** 업로드 기록의 마지막 기상 시각 (직접 입력 가능 범위 안내용) */
    val lastRecordEndMs: Long? = null,
    /** 그래프 아래 "개인 경향" 문장. 기록이 2번 미만이면 비어 있음 → 화면에서 숨김 */
    val patternLines: List<String> = emptyList(),
    /** 하루 넘게 수면 기록이 없는 구간 (그래프에 빗금). '깨어 있음'으로 계산돼 값이 높게 보이는 곳 */
    val gapBands: List<SleepBand> = emptyList(),
    /** 지금 보고 있는 밤 (그래프 범위 = night.startMs ~ night.endMs) */
    val night: NightInfo? = null,
    /** 고를 수 있는 밤 목록 (오래된 순) */
    val nights: List<NightInfo> = emptyList(),
    /** 이 밤에 잠든 수면 기록 (잠든 시각이 이 밤 범위 안) */
    val nightSleeps: List<SleepRecord> = emptyList(),
    /** 이 결과의 계산 구간(앞 7일)에 걸친 수면 기록 수. 적으면 '깨어 있음'으로 채워져 수면 압력이 높게 나온다 */
    val windowSleepCount: Int = 0,
    /** (2026-10-24) 다음 수면 일정 카드. 가장 최근 밤에서만 채운다 */
    val schedule: Schedule? = null,
    /** (2026-10-24) 해설 문장 ①②③ 에 해당하는 그래프 위 표시 */
    val marks: ChartMarks = ChartMarks(),
    /** (2026-10-24) 그래프 가로 범위. 0 이면 night 범위를 쓴다 */
    val graphStartMs: Long = 0L,
    val graphEndMs: Long = 0L,
    /** (2026-10-24) 그래프 위 예상·권장 수면 막대 */
    val planBands: List<PlanBand> = emptyList(),
    /** (2026-10-24 A안) 지난밤 수면 요약. 기상 기록이 없는 날은 null */
    val lastSleep: LastSleepInfo? = null,
    /** (2026-10-24 A안) 해석 ①②③ (+ ◆ 안내는 planBands 로 화면이 붙인다) */
    val analysisLines: List<AnalysisLine> = emptyList(),
    /** 기준 시각이 기상 시각인가. false 면 화면에 "기상 기록이 없어 낮 12시 기준" 안내 */
    val basisIsWake: Boolean = true,
    /**
     * (2026-10-24) 기록 일수 · 개인화 단계 · 제한 이유 코드 (채윤 PredictionStatus 기준).
     * 엔진은 기록 일수를 파일 전체로 세서, 여기서 기준 시각까지 끝난 기록으로 다시 센다 (지난 날짜도 그날 기준).
     * 화면 안내문 연결은 다음 작업.
     */
    val recordDays: Int = 0,
    val personalizationStage: String = "",
    val limitations: List<String> = emptyList()
)

enum class PlanKind { PREDICTED, RECOMMENDED }
data class PlanBand(val startMs: Long, val endMs: Long, val kind: PlanKind)

object AnalysisPresenter {

    /** (2026-10 이전) 그래프에 보여주던 과거 기간. 지금 화면은 "○○ 밤"(24시간) 단위라 쓰지 않는다 */
    const val DEFAULT_WINDOW_DAYS = 3
    /** 그래프 간격 (합의: 계산 1분(2026-10, 이전 30초), 전달·그래프 10~15분) */
    const val DEFAULT_STEP_MIN = 15

    private const val MIN_MS = 60_000L
    private const val HOUR_MS = 60 * MIN_MS
    private const val DAY_MS = 24 * 60 * MIN_MS
    /** 이보다 짧은 수면은 경향 문장에서 뺀다 (낮잠이 "잠드는 시각" 평균을 흐리지 않게) */
    private const val MAIN_SLEEP_MIN = 180.0
    /** 이보다 길게 수면 기록이 없으면 "기록 없음"으로 본다 (하루 넘게 한 번도 안 잤을 가능성은 낮음) */
    private const val GAP_MS = DAY_MS

    /**
     * (2026-10-24 A안) 날짜 하나 = 그날 아침 일어난 시각이 기준.
     * @param r 그 기준 시각으로 계산한 결과 (가장 최근 = 업로드 직후 계산, 지난 날 = 화면이 runFileAt(기상 시각)으로 다시 계산)
     * @param dayMs 보고 싶은 날(그날 안의 아무 시각). null 이거나 범위 밖이면 가장 최근 날
     * @param latestRefMs 가장 최근 계산의 기준 시각(마지막 기록). 지난 날 결과를 넘길 때 "가장 최근"을 알기 위해 필요
     * @param direct 직접 입력 (지난 날은 화면이 그날 설문 알람을 목표 기상으로 넣어 넘긴다)
     */
    fun build(
        r: AnalysisResult,
        dayMs: Long? = null,
        stepMin: Int = DEFAULT_STEP_MIN,
        direct: DirectInput? = null,
        condition: Int? = null,
        latestRefMs: Long? = null,
        dataStartMs: Long? = null,
        survey: SurveyDay? = null,
        recommendationReferenceMs: Long? = null
    ): AnalysisDisplay {
        val tz = zone()
        val refMs = r.scHistory.lastOrNull()?.timestampMs ?: r.calculationTimeMs
        val stepMs = stepMin * MIN_MS
        val latestRef = latestRefMs ?: refMs
        val days = days(r.sleepRecords, direct, latestRef, dataStartMs, tz)
        val latest = days.last()
        val day = dayMs?.let { k -> dayStartOf(k, tz).let { st -> days.firstOrNull { it.startMs == st } } } ?: latest

        // 그래프 = 기준 시각 12시간 전(지난밤 잠) ~ 24시간 뒤(그날 하루 + 그날 밤) — 지시서 "현재 및 다음 날 수면압력 변화"
        val fromMs = refMs - (if (recommendationReferenceMs != null) 24 else 12) * HOUR_MS
        val toMs = refMs + 24 * HOUR_MS
        val history = downsample(
            r.scHistory.asSequence().filter { it.timestampMs in fromMs..refMs }
                .map { ChartPoint(it.timestampMs, it.S, it.C, it.propensity) },
            stepMs
        )
        val future = downsample(
            (r.futureS ?: emptyList()).asSequence().filter { it.timestampMs <= toMs }
                .map { ChartPoint(it.timestampMs, it.predictedS, it.predictedC, it.predictedPropensity) },
            stepMs
        )
        // 지난 날은 기준 시각 뒤의 실제 수면도 칠한다 (그날 밤 실제로 잔 시간 → 예상과 비교)
        val bands = r.sleepRecords
            .filter { it.endMs > fromMs && it.startMs < toMs }
            .map { SleepBand(maxOf(it.startMs, fromMs), minOf(it.endMs, toMs)) }
        val manual = direct?.let { dd ->
            val s0 = dd.sleepStartMs; val e0 = dd.sleepEndMs
            if (s0 != null && e0 != null && e0 > s0) Sleep(s0, e0) else null
        }
        val allSleeps = (r.sleepRecords.map { Sleep(it.startMs, it.endMs, it.totalSleepMin) } + listOfNotNull(manual)).sortedBy { it.startMs }
        val sleeps = allSleeps.filter { it.endMs <= refMs + MIN_MS }
        val lastMain = sleeps.filter { it.minutes >= MAIN_SLEEP_MIN && it.endMs > refMs - 24 * HOUR_MS }.maxByOrNull { it.endMs }
        // 그날 밤 실제로 잔 잠 (지난 날만 있음): 기준 시각 1시간 뒤 ~ 30시간 안에 시작한 3시간 이상 잠 중 가장 긴 것
        val actualNext = allSleeps
            .filter { it.minutes >= MAIN_SLEEP_MIN && it.startMs > refMs + HOUR_MS && it.startMs < refMs + 30 * HOUR_MS }
            .maxByOrNull { it.minutes }
        // 지난 날은 권장을 보여주지 않으므로 그래프에도 예상 막대만
        val plan = planBands(r, direct, fromMs, toMs).filter { day.isLatest || it.kind == PlanKind.PREDICTED }
        // 화면에 쓰는 기준 시각: 계산은 1분 경계로 맞춰져 기상보다 1분쯤 빠를 수 있어(07:00 → 06:59), 글자는 실제 기상 시각으로
        val shownRef = if (day.hasWake && lastMain != null && kotlin.math.abs(lastMain.endMs - refMs) <= 5 * MIN_MS) lastMain.endMs else refMs
        val analysis = analysisLines(r, refMs, lastMain, allSleeps, if (day.isLatest) null else actualNext)
        // 모델 이름에 "임시"가 없어져도 아직 임시 모델이다 (파라미터 확정 전). 항상 표시 (합의안 5-1 #4)
        val temporary = true
        val recDays = AnalysisRunner.recordDaysUntil(r.sleepRecords, refMs)
        // 제한 이유 코드: 엔진 값 그대로.
        // (v6) 옛 엔진 때 "기록 14일 미만 → 기본 모델" 을 그날 기준으로 다시 붙이던 줄은 뺐다 —
        //      새 모델은 첫 기록부터 개인화해서 "14일부터 내 패턴에 맞춘 계산을 할 예정" 안내가 틀린 말이 됨 (NextNightModel 이 이 코드를 뺀다)
        val limits = r.predictionLimitations.filterNot { it == LimitCode.FEW_RECORDS }

        return AnalysisDisplay(
            modelLabel = r.modelVersion,
            isTemporaryModel = temporary,
            referenceMs = refMs,
            readings = if (r.scHistory.isEmpty()) emptyList() else readings(r, refMs, sleeps, day.isLatest),
            history = history,
            future = future,
            futureIsTemporary = r.futureS != null,
            sleepBands = bands,
            easiestSleepMs = null,                                    // (A안) "가장 졸린 때" 문장은 ◆ 「예상」으로 합침
            recentSleeps = if (day.isLatest) r.sleepRecords.sortedByDescending { it.startMs }.take(7) else emptyList(),
            recommendationText = recommendationText(r),
            manualSleep = manual?.let { m -> if (m.endMs > fromMs && m.startMs < toMs) SleepBand(maxOf(m.startMs, fromMs), minOf(m.endMs, toMs)) else null },
            inputNotes = if (day.isLatest) inputNotes(direct, condition, r.recommendation != null, survey) else emptyList(),
            lastRecordEndMs = r.sleepRecords.maxOfOrNull { it.endMs },
            patternLines = emptyList(),
            marks = analysis.second,
            gapBands = gapBands(sleeps, fromMs, refMs),
            night = day,
            nights = days,
            nightSleeps = emptyList(),
            windowSleepCount = r.scHistory.firstOrNull()?.timestampMs?.let { ws ->
                r.sleepRecords.count { it.endMs > ws && it.startMs < refMs }
            } ?: 0,
            schedule = schedule(r, recommendationReferenceMs ?: refMs, sleeps, direct, past = !day.isLatest,
                wakeBasis = recommendationReferenceMs == null && day.hasWake, actual = actualNext,
                shownRefMs = recommendationReferenceMs ?: shownRef)
                .copy(notices = limitNotices(limits, recDays, past = !day.isLatest)),
            graphStartMs = fromMs,
            graphEndMs = toMs,
            planBands = plan,
            lastSleep = lastMain?.let { lastSleepInfo(r, it) },
            analysisLines = analysis.first,
            basisIsWake = recommendationReferenceMs == null && day.hasWake,
            recordDays = recDays,
            personalizationStage = app.sleepwell.health.PersonalizationStage.of(recDays),
            limitations = limits
        )
    }

    // ── 날짜 (A안: 그날 아침 일어난 시각 기준) ──────────────────────
    /** 표시 시간대의 그날 0시 */
    fun dayStartOf(ms: Long, tz: TimeZone = zone()): Long = Calendar.getInstance(tz).apply {
        timeInMillis = ms
        set(Calendar.HOUR_OF_DAY, 0); set(Calendar.MINUTE, 0); set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0)
    }.timeInMillis

    /**
     * 고를 수 있는 날짜 목록 (오래된 순). 첫 날 = 처음으로 3시간 이상 자고 일어난 날, 마지막 = 가장 최근 기준 시각의 날.
     *  - 기준 시각: 그날 끝난 3시간 이상 잠 중 가장 긴 것의 끝(= 아침 기상). 없으면 그날 낮 12시 (hasWake = false)
     *  - 가장 최근 날: 기준 시각 = 마지막 기록(또는 직접 입력한 더 늦은 기상)
     */
    fun days(records: List<SleepRecord>, direct: DirectInput?, latestRefMs: Long, dataStartMs: Long? = null, tz: TimeZone = zone()): List<NightInfo> {
        val manual = direct?.let { d -> val s0 = d.sleepStartMs; val e0 = d.sleepEndMs; if (s0 != null && e0 != null && e0 > s0) Sleep(s0, e0) else null }
        val mains = (records.map { Sleep(it.startMs, it.endMs, it.totalSleepMin) } + listOfNotNull(manual))
            .filter { it.minutes >= MAIN_SLEEP_MIN && it.endMs <= latestRefMs + MIN_MS }
        val dataStart = dataStartMs ?: records.minOfOrNull { it.startMs } ?: latestRefMs
        val last = dayStartOf(latestRefMs, tz)
        val first = minOf(dayStartOf(mains.minOfOrNull { it.endMs } ?: latestRefMs, tz), last)
        val out = mutableListOf<NightInfo>()
        var st = first
        while (st <= last && out.size < 5000) {      // 5000 일 ≈ 13년 (안전 상한)
            val en = dayStartOf(st + DAY_MS + 2 * HOUR_MS, tz)   // 다음 날 0시 (서머타임이 있어도 맞게)
            val isLatest = st == last
            val wake = mains.filter { it.endMs >= st && it.endMs < en }.maxByOrNull { it.minutes }?.endMs
            val noon = Calendar.getInstance(tz).apply { timeInMillis = st; set(Calendar.HOUR_OF_DAY, 12) }.timeInMillis
            val basis = if (isLatest) latestRefMs else wake ?: noon
            val hasWake = if (isLatest) mains.any { kotlin.math.abs(it.endMs - latestRefMs) <= MIN_MS } else wake != null
            out += NightInfo(
                startMs = st, endMs = en,
                label = fmt("M/d(E)", tz).format(Date(st)) + if (hasWake) " 기상" else "",
                rangeText = "",
                isLatest = isLatest,
                isPartial = basis < dataStart + DAY_MS,
                basisMs = basis,
                hasWake = hasWake
            )
            st = en
        }
        return out
    }

    /** 지난밤 수면 한 줄: "10/19(월) 23:24 → 10/20(화) 07:00" / "잔 시간 7시간 26분 · LIGHT 49% · …" */
    private fun lastSleepInfo(r: AnalysisResult, s: Sleep): LastSleepInfo {
        val rec = r.sleepRecords.firstOrNull { it.startMs == s.startMs && it.endMs == s.endMs }
        val detail = if (rec != null) {
            val tot = rec.totalSleepMin
            "잔 시간 ${duration(tot)} · " + listOf("LIGHT", "DEEP", "REM").joinToString(" · ") { k ->
                "$k ${(if (tot > 0) (rec.stageSummary[k] ?: 0.0) * 100 / tot else 0.0).toInt()}%"
            }
        } else "잔 시간 ${duration(s.minutes)} · 직접 입력한 수면"
        return LastSleepInfo("${dayTime(s.startMs)} → ${dayTime(s.endMs)}", detail)
    }

    /**
     * 해석 ①②③ (A안). 숫자는 "무슨 일이 있었나", MEANING 은 "그게 무슨 뜻인가".
     *  ① 자는 동안 수면 압력이 얼마나 내려갔나 (잠들 때 → 일어날 때). 줄어든 비율 70% / 40% 경계는 소빈 판단(임시)
     *  ② 일주기 리듬 꼭대기(▲, 몸이 가장 자고 싶어 하는 때)에 자고 있었나
     *  ③ 일어난 뒤 언제 다시 지난밤 잠들 때만큼 피로가 쌓이나 (+ 지난 날은 실제로 잠든 시각)
     */
    private fun analysisLines(
        r: AnalysisResult, refMs: Long, lastMain: Sleep?, allSleeps: List<Sleep>, actualNext: Sleep?
    ): Pair<List<AnalysisLine>, ChartMarks> {
        val h = r.scHistory
        if (lastMain == null || h.isEmpty()) return emptyList<AnalysisLine>() to ChartMarks()
        val out = mutableListOf<AnalysisLine>()
        val onsetS = nearest(h, lastMain.startMs)?.S
        val wakeS = nearest(h, lastMain.endMs)?.S ?: r.currentS

        // ①
        if (onsetS != null && onsetS > 0.0) {
            val drop = (onsetS - wakeS) / onsetS
            val (title, meaning) = when {
                drop >= 0.7 -> "자는 동안 피로가 풀렸어요" to listOf(
                    Seg("하루 동안 쌓인 피로가 잠으로 ", SegStyle.MEANING), Seg("대부분 풀린 채", SegStyle.MEANING_BOLD), Seg(" 일어났다는 뜻이에요.", SegStyle.MEANING))
                drop >= 0.4 -> "자는 동안 피로가 절반 넘게 풀렸어요" to listOf(
                    Seg("피로가 절반 넘게 풀렸지만, ", SegStyle.MEANING), Seg("조금 남은 채", SegStyle.MEANING_BOLD), Seg(" 일어났다는 뜻이에요.", SegStyle.MEANING))
                else -> "피로가 덜 풀린 채 일어났어요" to listOf(
                    Seg("잠든 시간이 짧아 ", SegStyle.MEANING), Seg("피로가 다 풀리지 않았다", SegStyle.MEANING_BOLD), Seg("는 뜻이에요.", SegStyle.MEANING))
            }
            out += AnalysisLine(title, listOf(
                Seg("수면 압력", SegStyle.S), Seg("이 잠들 때 "), Seg(fmt(onsetS), SegStyle.S), Seg("에서 일어날 때 "),
                Seg(fmt(wakeS), SegStyle.S), Seg("까지 내려갔어요. ")
            ) + meaning)
        }

        // ② 기준 시각 앞 24시간 중 일주기 리듬이 가장 높은 때
        h.filter { it.timestampMs > refMs - DAY_MS }.maxByOrNull { it.C }?.let { peak ->
            val asleep = allSleeps.any { peak.timestampMs in it.startMs..it.endMs }
            val head = listOf(
                Seg("일주기 리듬", SegStyle.C), Seg("이 가장 높은 곳은 몸이 하루 중 가장 자고 싶어 하는 때예요. 이날은 ${clockWords(minuteOfDay(peak.timestampMs))}"),
                Seg("▲", SegStyle.C), Seg("이었고, 이 시각에 ")
            )
            out += if (asleep) AnalysisLine("몸의 리듬과 잠 시간이 잘 맞았어요", head + listOf(
                Seg("자고 있었어요", SegStyle.BOLD), Seg(". "), Seg("몸이 원하는 때에 맞춰 잤다는 뜻이에요.", SegStyle.MEANING)))
            else AnalysisLine("몸의 리듬과 잠 시간이 어긋났어요", head + listOf(
                Seg("깨어 있었어요", SegStyle.BOLD), Seg(". "),
                Seg("몸은 자고 싶은 때에 깨어 있었다는 뜻이에요. 이런 날이 잦으면 낮에 더 피곤하게 느낄 수 있어요.", SegStyle.MEANING)))
        }

        // ③ 일어난 뒤 다시 지난밤 잠들 때만큼 쌓이는 때 (수면 압력 예측선)
        var reachMs: Long? = null
        if (onsetS != null) {
            val reach = r.futureS?.firstOrNull { it.predictedS >= onsetS }
            val actualSegs = actualNext?.let { a -> listOf(Seg(" 실제로는 ${clockExact(a.startMs)}에 잠들었어요 "), Seg("■", SegStyle.SLEEP_SWATCH), Seg(".")) } ?: emptyList()
            if (reach != null) {
                reachMs = reach.timestampMs
                out += AnalysisLine("${clockExact(reach.timestampMs)}쯤 다시 잘 때가 돼요", listOf(
                    Seg("일어난 뒤에는 깨어 있는 만큼 피로가 다시 쌓여요. ${clockExact(reach.timestampMs)}쯤 지난밤 잠들 때만큼("),
                    Seg("● ${fmt(onsetS)}", SegStyle.S), Seg(") 쌓여요. "),
                    Seg("이때쯤이면 몸이 다시 잘 준비가 된다는 뜻이에요.", SegStyle.MEANING)
                ) + actualSegs)
            } else {
                out += AnalysisLine("하루 동안 피로가 천천히 쌓여요", listOf(
                    Seg("일어난 뒤 24시간 안에는 지난밤 잠들 때만큼("), Seg("● ${fmt(onsetS)}", SegStyle.S), Seg(") 쌓이지 않아요. "),
                    Seg("지난밤 잠들 때 피로가 평소보다 많이 쌓여 있었다는 뜻이에요.", SegStyle.MEANING)
                ) + actualSegs)
            }
        }
        return out to ChartMarks(onsetMs = lastMain.startMs, reachMs = reachMs)
    }

    /** "밤 11시 14분" / "아침 7시" (분까지) */
    fun clockExact(ms: Long): String {
        val c = cal(ms); val hh = c.get(Calendar.HOUR_OF_DAY); val mm = c.get(Calendar.MINUTE)
        val p = if (hh == 0) "밤" else period(hh)
        return "$p ${h12(hh)}시" + if (mm > 0) " ${mm}분" else ""
    }

    // ── 밤 경계 (표시 시간대의 정오~다음 날 정오) ──────────────────
    /** ms 가 속한 밤의 시작 = (ms − 12시간)의 날짜의 정오. export 의 night 규칙과 같은 방식 (시간대만 표시 시간대) */
    fun nightStartOf(ms: Long, tz: TimeZone = zone()): Long = Calendar.getInstance(tz).apply {
        timeInMillis = ms - 12 * HOUR_MS
        set(Calendar.HOUR_OF_DAY, 12); set(Calendar.MINUTE, 0); set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0)
    }.timeInMillis

    /** 이 밤의 끝 = 다음 날 정오 (지난 밤을 다시 계산할 때 기준 시각으로 쓴다) */
    fun nightEndOf(startMs: Long, tz: TimeZone = zone()): Long = nightStartOf(startMs + DAY_MS + HOUR_MS, tz)

    /** 데이터 범위 [dataStart, refMs] 에 걸치는 밤 목록 (오래된 순). 최소 1개. */
    fun nightsBetween(dataStart: Long, refMs: Long, tz: TimeZone = zone()): List<NightInfo> {
        val first = nightStartOf(dataStart, tz)
        val last = nightStartOf(refMs, tz)
        val out = mutableListOf<NightInfo>()
        var s = first
        while (s <= last && out.size < 5000) {      // 5000 밤 ≈ 13년 (안전 상한)
            val e = nightStartOf(s + DAY_MS + HOUR_MS, tz)     // 다음 날 정오 (서머타임이 있는 시간대에서도 맞게 다시 잡는다)
            out += NightInfo(
                startMs = s, endMs = e,
                label = fmt("M/d(E)", tz).format(Date(s)) + " 밤",
                rangeText = fmt("M/d HH:mm", tz).format(Date(s)) + " ~ " + fmt("M/d HH:mm", tz).format(Date(e)),
                isLatest = s == last,
                isPartial = s < dataStart + DAY_MS       // 기록이 시작된 첫 1~2밤: 그 앞 기록이 없어 수면 압력이 덜 정확
            )
            s = e
        }
        return out
    }

    /** 업로드 기록 + 직접 입력을 같은 모양으로 다루기 위한 내부 타입 */
    private data class Sleep(val startMs: Long, val endMs: Long, val sleptMin: Double? = null) {
        /** 누워 있던 시간 (시작~끝) */
        val minutes: Double get() = (endMs - startMs) / 60_000.0
        /** 실제로 잔 시간 (중간에 깬 시간 제외). 직접 입력은 단계 정보가 없어 시작~끝 */
        val slept: Double get() = sleptMin ?: minutes
    }

    /** 일정 간격마다 첫 점만 남긴다. 원본이 수백만 점이어도 그래프에는 수백 점만 넘긴다. */
    fun downsample(points: Sequence<ChartPoint>, stepMs: Long): List<ChartPoint> {
        val out = ArrayList<ChartPoint>()
        var nextMs = Long.MIN_VALUE
        for (p in points) {
            if (p.timeMs >= nextMs) {
                out += p
                nextMs = p.timeMs + stepMs
            }
        }
        return out
    }

    /**
     * 값 3개. (2026-10-24 소빈) 수면 압력·일주기 리듬은 값과 막대만 (설명은 위 상자 + 그래프 해설 ①②③ 으로),
     * 잠들기 쉬운 정도만 최근 7일과 비교한 한 줄. 지난 밤은 그 밤 끝(다음 날 정오) 기준 값이라 "지금" 대신 "그때".
     */
    private fun readings(r: AnalysisResult, refMs: Long, sleeps: List<Sleep>, latest: Boolean): List<Reading> {
        val h = r.scHistory
        val last = sleeps.lastOrNull { it.endMs <= refMs + MIN_MS }
        val now = if (latest) "지금은" else "그때는"

        // 잠들기 쉬운 정도: 최근 7일 중 어디쯤인지 (본인 기록과 비교 → 임의 경계 없음)
        val below = h.count { it.propensity < r.currentPropensity }
        val pct = if (h.isEmpty()) 50.0 else below * 100.0 / h.size
        val (rank, mood) = when {
            pct < 5 -> "최근 7일 중 가장 낮아요." to "$now 잠들기 어려운 때예요."
            pct < 33 -> "최근 7일 중 낮은 편이에요." to "$now 잠들기 어려운 때예요."
            pct < 67 -> "최근 7일 중 중간쯤이에요." to ""
            pct < 95 -> "최근 7일 중 높은 편이에요." to "$now 잠들기 쉬운 때예요."
            else -> "최근 7일 중 가장 높아요." to "$now 잠들기 쉬운 때예요."
        }
        val justWoke = last != null && refMs - last.endMs <= HOUR_MS
        val pMood = if (justWoke && pct < 33) "막 일어나서 $mood" else mood

        return listOf(
            Reading("수면 압력", fmt(r.currentS), "", ReadingKind.S, gauge = Gauge(0.0, 1.0, r.currentS, "0 푹 잔 직후", "1 한계")),
            Reading("일주기 리듬", signed(r.currentC), "", ReadingKind.C, gauge = Gauge(-0.15, 0.15, r.currentC, "−0.15 또렷한 쪽", "+0.15 잠을 원하는 쪽")),
            Reading("잠들기 쉬운 정도", fmt(r.currentPropensity), listOf(rank, pMood).filter { it.isNotEmpty() }.joinToString(" "), ReadingKind.P)
        )
    }

    /**
     * 그래프 아래 "개인 경향" 문장 + 그래프 위 번호 표시 (2026-10-24 ①②③).
     *  ① 최근 N번 기록의 잠든 시각 범위 · 일어난 시각 → 그래프에 평소 잠든·일어난 시각 표시
     *  ② 일주기 리듬이 가장 잠을 원하는 시각 → ▲
     *  ③ 지금부터 깨어 있으면 언제 지난번 잠들 때만큼 잠들기 쉬워지는지 (가장 최근 밤에서만, 예측 값 기준)
     * 3시간 이상 잔 기록이 2번 미만이면 ①은 비운다.
     * 지난 밤은 그 밤 끝(다음 날 정오)까지의 기록으로 같은 문장을 만든다.
     */
    private fun patternLines(
        r: AnalysisResult, refMs: Long, sleeps: List<Sleep>, fromMs: Long, toMs: Long, latest: Boolean
    ): Pair<List<String>, ChartMarks> {
        val out = mutableListOf<String>()
        var onsetMark: Long? = null; var wakeMark: Long? = null; var reachMark: Long? = null

        val recent = sleeps.filter { it.endMs > refMs - 7 * DAY_MS && it.endMs <= refMs + MIN_MS && it.minutes >= MAIN_SLEEP_MIN }
        if (recent.size >= 2) {
            // 잠든 시각: 정오 기준으로 돌려서(밤 11시~새벽 1시처럼 자정을 넘어도) 범위를 잡는다
            val onsetH = recent.map { (hourOf(it.startMs) + 12) % 24 }
            val lo = (onsetH.min() + 12) % 24; val hi = (onsetH.max() + 12) % 24
            val onsetText = if (lo == hi) "${period(lo)} ${h12(lo)}시쯤"
                else if (period(lo) == period(hi)) "${period(lo)} ${h12(lo)}~${h12(hi)}시에"
                else "${period(lo)} ${h12(lo)}시~${period(hi)} ${h12(hi)}시에"
            val wakeMin = circularMeanMinutes(recent.map { minuteOfDay(it.endMs) })
            out += "① 최근 ${recent.size}번의 기록에서 $onsetText 잠들어 ${clockWords(wakeMin)}쯤 일어났어요."
            onsetMark = atClock(circularMeanMinutes(recent.map { minuteOfDay(it.startMs) }), fromMs, toMs)
            wakeMark = atClock(wakeMin, fromMs, toMs)
        }

        r.scHistory.filter { it.timestampMs > refMs - DAY_MS }.maxByOrNull { it.C }?.let {
            out += "② 몸은 ${clockWords(minuteOfDay(it.timestampMs))}쯤(▲) 가장 잠을 원해요."
        }

        if (latest && sleeps.isNotEmpty()) {
            val last = sleeps.last()
            val onsetP = nearest(r.scHistory, last.startMs)?.propensity
            val reach = onsetP?.let { p -> r.futureS?.firstOrNull { it.predictedPropensity >= p } }
            if (reach != null) {
                val which = if (last.minutes >= MAIN_SLEEP_MIN) "지난밤" else "지난번"
                val inGraph = reach.timestampMs in fromMs..toMs
                out += "③ 지금부터 깨어 있으면 ${dayWord(refMs, reach.timestampMs)} ${clockWords(minuteOfDay(reach.timestampMs))}쯤, " +
                        "$which 잠들 때만큼 잠들기 쉬워져요." + if (inGraph) "" else " (그래프 범위 밖)"
                if (inGraph) reachMark = reach.timestampMs
            }
        }
        return out to ChartMarks(onsetMark, wakeMark, reachMark)
    }

    /** 그래프 범위 [fromMs, toMs] 안에서 시계 시각(분)이 처음 나오는 때. 범위가 하루라 보통 한 번 나온다 */
    private fun atClock(minute: Int, fromMs: Long, toMs: Long): Long? {
        val c = cal(fromMs).apply {
            set(java.util.Calendar.HOUR_OF_DAY, minute / 60); set(java.util.Calendar.MINUTE, minute % 60)
            set(java.util.Calendar.SECOND, 0); set(java.util.Calendar.MILLISECOND, 0)
        }
        if (c.timeInMillis < fromMs) c.add(java.util.Calendar.DAY_OF_MONTH, 1)
        return c.timeInMillis.takeIf { it <= toMs }
    }

    /**
     * 예상(모델)·권장 수면 구간 → 그래프 띠. 권장은 목표 기상이 있을 때만 (카드와 같은 규칙).
     * 그래프 범위 밖은 잘라서 넣는다.
     */
    private fun planBands(r: AnalysisResult, direct: DirectInput?, fromMs: Long, toMs: Long): List<PlanBand> {
        val rec = r.recommendation ?: return emptyList()
        fun band(s0: Long?, e0: Long?, k: PlanKind): PlanBand? =
            if (s0 != null && e0 != null && e0 > s0 && e0 > fromMs && s0 < toMs) PlanBand(maxOf(s0, fromMs), minOf(e0, toMs), k) else null
        return listOfNotNull(
            band(rec.predictedBedTimeMs, rec.predictedWakeTimeMs, PlanKind.PREDICTED),
            if (direct?.targetWakeMs != null || rec.modelStatus == "fusion_cmi_mesa") band(rec.recommendedBedTimeMs, rec.recommendedWakeTimeMs, PlanKind.RECOMMENDED) else null
        )
    }

    /** 표시 기간 안에서 하루 넘게 수면 기록이 없는 구간 */
    private fun gapBands(sleeps: List<Sleep>, fromMs: Long, refMs: Long): List<SleepBand> {
        val out = mutableListOf<SleepBand>()
        var cursor = fromMs
        for (s in sleeps) {
            if (s.endMs <= fromMs) continue
            if (s.startMs - cursor > GAP_MS) out += SleepBand(cursor, s.startMs)
            cursor = maxOf(cursor, s.endMs)
        }
        if (refMs - cursor > GAP_MS) out += SleepBand(cursor, refMs)
        return out
    }

    // ── 시각·문장 도우미 ─────────────────────────────────────
    private fun nearest(h: List<PropensityPoint>, ms: Long): PropensityPoint? {
        // (2026-10) 칸 크기를 숫자로 박지 않고 데이터 간격에서 읽는다 (30초든 1분이든 그대로 맞음)
        val step = if (h.size >= 2) h[1].timestampMs - h[0].timestampMs else MIN_MS
        if (h.isEmpty() || ms < h.first().timestampMs || ms > h.last().timestampMs + step) return null
        return h.minByOrNull { kotlin.math.abs(it.timestampMs - ms) }
    }

    private fun duration(min: Double): String {
        val m = min.toInt()
        return when { m < 60 -> "${m}분"; m % 60 == 0 -> "${m / 60}시간"; else -> "${m / 60}시간 ${m % 60}분" }
    }

    /** 기기 설정을 자동으로 따르는 표시 시간대. */
    private fun zone(): TimeZone = DisplaySettings.zone.tz
    private fun cal(ms: Long) = java.util.Calendar.getInstance(zone()).apply { timeInMillis = ms }
    private fun hourOf(ms: Long) = cal(ms).get(java.util.Calendar.HOUR_OF_DAY)
    private fun minuteOfDay(ms: Long) = cal(ms).let { it.get(java.util.Calendar.HOUR_OF_DAY) * 60 + it.get(java.util.Calendar.MINUTE) }

    /** 표시 시간대 날짜 기준 며칠 차이인지 ("오늘/내일"). 자정~1시는 말할 때 "오늘 밤"이라 그렇게 부른다 */
    private fun dayWord(refMs: Long, ms: Long): String {
        fun dayIndex(t: Long) = Math.floorDiv(t + zone().getOffset(t), DAY_MS)
        val diff = (dayIndex(ms) - dayIndex(refMs)).toInt()
        if (diff == 1 && hourOf(ms) == 0) return "오늘"
        return when (diff) { 0 -> "오늘"; 1 -> "내일"; else -> "${diff}일 뒤" }
    }

    private fun period(h: Int) = when (h) {
        in 0..6 -> "새벽"; in 7..8 -> "아침"; in 9..11 -> "오전"; in 12..17 -> "오후"; in 18..20 -> "저녁"; else -> "밤"
    }
    private fun h12(h: Int) = when { h == 0 -> 12; h > 12 -> h - 12; else -> h }

    /** 분(0~1439) → "새벽 5시 반" 같은 말. 30분 단위로 반올림 */
    fun clockWords(minuteOfDay: Int): String {
        val r = ((minuteOfDay + 15) / 30 * 30) % 1440
        val hh = r / 60; val half = r % 60 == 30
        if (hh == 0 && !half) return "자정"
        if (hh == 0) return "밤 12시 반"
        return "${period(hh)} ${h12(hh)}시" + if (half) " 반" else ""
    }

    private fun circularMeanMinutes(mins: List<Int>): Int {
        val ang = mins.map { it * 2 * Math.PI / 1440 }
        val a = kotlin.math.atan2(ang.sumOf { kotlin.math.sin(it) }, ang.sumOf { kotlin.math.cos(it) })
        return (((a * 1440 / (2 * Math.PI)) + 1440) % 1440).toInt()
    }

    private fun signed(v: Double) = (if (v >= 0) "+" else "") + fmt(v)

    private fun inputNotes(d: DirectInput?, fatigue: Int?, hasRecommendation: Boolean, survey: SurveyDay? = null): List<String> {
        val out = mutableListOf<String>()
        if (d?.sleepStartMs != null && d.sleepEndMs != null)
            out += "✓ 최근 수면 ${dayTime(d.sleepStartMs)} → ${hhmm(d.sleepEndMs)} : 계산에 반영됨 (업로드 기록과 겹치는 부분은 기록 우선)"
        d?.targetWakeMs?.let {
            out += if (hasRecommendation) "✓ 목표 기상 ${dayTime(it)} : 권장 취침 계산에 반영됨"
                   else "목표 기상 ${dayTime(it)} : 이번 권장을 계산하지 못해 반영 여부를 확인할 수 없어요."
        }
        d?.plannedBedMs?.let { out += "예정 취침 ${dayTime(it)} : 계산에 반영 안 됨 (저장만)" }
        // 생활 맥락과 수면에 연결한 주관 응답의 반영 범위를 구분한다.
        val surveyText = survey?.summaryIn(zone().toZoneId(), includeAlarm = false)?.takeIf { survey.hasItems() }
        if (surveyText != null) out += "설문 · $surveyText. 개운함·피로는 선택한 수면 기록과 연결하고 수면 측정 정보가 완전할 때 목표 보정 근거가 됩니다."
        else fatigue?.let { out += "피로도 $it/7 · 수면 기록과 연결해 저장하면 개인화 근거에 포함됩니다." }
        return out
    }

    // ── 다음 수면 일정 (2026-10-24) ────────────────────────────
    /** 개인화 단계 후보 (서윤 미래예측모델 PERSONALIZATION_PROTOCOL, 확정 전) */
    const val STAGE1_DAYS = 14
    const val STAGE2_DAYS = 30

    /**
     * (2026-10-24 A안) 그날 밤 수면 일정 — 가장 최근 날·지난 날 같은 규칙 (기준 = 그날 아침 기상).
     *  - 예상: 엔진 predictedBed/Wake 그대로. 권장: 목표 기상이 있을 때만 (지난 날 = 그날 설문 알람, 화면이 direct 로 넘김)
     *  - 지난 날은 그날 밤 실제로 잔 시각을 참고로 붙인다
     *  - 시각은 항상 날짜와 함께 ("오늘/내일"은 폰 날짜와 헷갈려 쓰지 않음)
     */
    private fun schedule(
        r: AnalysisResult, refMs: Long, sleeps: List<Sleep>, direct: DirectInput?,
        past: Boolean, wakeBasis: Boolean, actual: Sleep?, shownRefMs: Long = refMs
    ): Schedule {
        fun w(ms: Long) = "${dayOnly(ms)} ${hhmm(ms)}"
        val rec = r.recommendation
        val main = sleeps.filter { it.minutes >= MAIN_SLEEP_MIN && it.endMs <= refMs + MIN_MS }
        val trained = rec != null && !rec.usedFallback && rec.modelStatus == "hybrid_trained"
        val status = ScheduleStatus(
            if (rec?.modelStatus == "fusion_cmi_mesa") {
                "CMI·MESA · 점진 예측 개인화 · " +
                    if (rec.evidenceStatus == "adequate") "수면 기록 기반 개인화" else "설정한 수면 목표 유지"
            } else if (trained) "개인 보정 모델" + (rec?.residualModelVersion?.let { " ($it)" } ?: "") else "임시 모델",
            ScheduleLevel.INFO
        )

        val mb = rec?.predictedBedTimeMs; val mw = rec?.predictedWakeTimeMs
        val model = if (mb != null && mw != null) ScheduleTimes(w(mb), w(mw), mb, mw) else null
        val modelNote = when {
            rec == null -> "이번에는 예측을 계산하지 못했어요. 입력 상태를 확인해 주세요."
            model == null -> "모델이 이번에는 예상 시각을 내지 못했어요."
            else -> ""
        }

        // 참고: 내 평소 패턴 (최근 7일 주 수면의 중간값)
        val recent = main.filter { it.endMs > refMs - 7 * DAY_MS }
        val habitBed = if (recent.size >= 2) circularMedian(recent.map { minuteOfDay(it.startMs) }) else null
        val habitWake = if (recent.size >= 2) circularMedian(recent.map { minuteOfDay(it.endMs) }) else null
        val habit = if (habitBed != null && habitWake != null) ScheduleTimes(clock(habitBed), clock(habitWake)) else null
        val habitNote = when {
            habit != null -> "최근 7일 중간값"
            r.sleepRecords.isEmpty() -> "수면 단계 자료가 없어 이 요약은 표시하지 않아요. 누적 수면 시각은 예측·개인화에 반영합니다."
            else -> "최근 7일 기록이 적어 평소 패턴을 아직 알 수 없어요."
        }

        val target = direct?.targetWakeMs
        val recBed = rec?.recommendedBedTimeMs
        val recommended = if (rec != null && recBed != null && (target != null || rec.modelStatus == "fusion_cmi_mesa") && (rec.recommendedSleepMin ?: 0.0) > 0.0) ScheduleRecommendation(
            // (v6) 새 모델은 권장 기상 = 알람과 평소 기상 중 이른 쪽 → 알람이 아니라 실제로 계산에 쓴 기상 시각을 보여 준다
            //      (알람 09:00 · 평소 07:20 이면 "07:20에 일어나려면 …" — 취침 시각과 짝이 맞게)
            targetWakeText = w(rec.recommendedWakeTimeMs ?: target ?: recBed),
            bedText = w(recBed),
            sleepText = rec.recommendedSleepMin?.let { "권장 수면 기회 ${duration(it)}" + if (rec.planFeasible == false) " · 목표보다 부족해요" else "" },
            compareText = null,
            targetWakeMs = rec.recommendedWakeTimeMs ?: target ?: recBed,
            bedMs = recBed
        ) else null
        val needsTarget = !past && target == null && recommended == null
        val unavailable = when {
            past -> null                                   // 지난 날은 권장 상자를 보여주지 않는다 (예상 vs 실제만)
            recommended != null -> null
            needsTarget -> null
            target == null -> "이 날은 목표 기상(설문 알람) 기록이 없어 권장 시각은 계산하지 않았어요."
            rec == null -> "이번에는 권장을 계산하지 못했어요. 입력 상태를 확인해 주세요."
            recommended == null -> "이번에는 권장 시각을 계산하지 못했어요."
            else -> null
        }

        return Schedule(
            basisText = "기준 ${dayTime(shownRefMs)}" + if (wakeBasis) " 기상" else "",
            basisMs = shownRefMs,
            status = status,
            modelLabel = "예상",
            model = model,
            modelNote = modelNote,
            habit = habit,
            habitNote = habitNote,
            recommended = if (past) null else recommended,
            needsTargetWake = needsTarget,
            unavailableText = unavailable,
            note = rec?.explanation ?: "연구용 참고값이에요. 예측 정확도는 아직 검증 중이고, 의학적 진단이 아니에요.",
            title = "${dayOnly(shownRefMs)} 밤 수면 일정",
            actual = if (past) actual?.let { ScheduleTimes(w(it.startMs), w(it.endMs), it.startMs, it.endMs) } else null,
            actualNote = if (!past) null else actual?.let { "잔 시간 ${duration(it.slept)}" } ?: "이 밤에는 3시간 넘게 잔 기록이 없어요.",
            isPast = past,
            diffText = if (past && actual != null && mb != null && mw != null) diffText(mb, mw, actual) else null
        )
    }

    // ── 이유 안내 (2026-10-24) — 지시서 Ⅲ-2 "모델 입력값이 부족하거나 예측을 수행할 수 없는 경우 그 이유를 명확하게 안내"
    //    기준 = 채윤 health/PredictionStatus.kt 의 코드. 화면 문장은 여기 한 곳에서만 만든다 (웹도 같은 코드를 쓰면 안내가 같아짐)

    private const val CSV_HINT = "삼성 헬스 내보내기 파일 중 com.samsung.health.sleep_stage…csv (이름에 'stage'가 있는 파일)를 골랐는지 확인해 주세요."

    /** 결과는 나왔지만 제한이 있는 이유 → 문장 (중요한 순서) */
    fun limitNotices(codes: List<String>, recordDays: Int, past: Boolean): List<String> = buildList {
        if (LimitCode.FEW_RECORDS in codes)
            add("기록이 ${recordDays}일이라 기본 모델로 계산했어요. ${PersonalizationStage.STAGE1_DAYS}일부터 내 패턴에 맞춘 계산을 할 예정이에요 (준비 중).")
        if (LimitCode.RIDGE_UNTRAINED in codes)
            add("개인 보정 모델이 아직 학습 전이라, 예상 시각은 기본 모델(물리모델) 값이에요.")
        if (LimitCode.NO_TARGET_WAKE in codes)
            add(if (past) "이 날은 설문 알람(목표 기상)이 없어, 예상 기상은 예상 취침에 평소 수면시간을 더해 계산했어요."
                else "목표 기상이 없어 예상 기상은 예상 취침에 평소 수면시간을 더해 계산했고, 권장 취침은 계산하지 않았어요.")
        if (LimitCode.NO_SURVEY_FEATURES in codes)
            add(if (past) "이 날은 설문(카페인·낮잠·운동·피로도) 기록이 없어요."
                else "설문(카페인·낮잠·운동·피로도)을 입력하지 않았어요. 입력하면 기록으로 쌓여 개인 보정 학습에 쓰여요.")
    }

    /** 계산을 못 한 이유 → 안내 카드 */
    fun failureNotice(res: HealthResult<*>, pastDay: Boolean = false): Notice = when (res) {
        is HealthResult.Unavailable -> when (res.code) {
            ReasonCode.NO_SOURCE -> Notice("파일을 열 수 없어요", listOf("파일을 다시 선택해 주세요."))
            ReasonCode.NO_SLEEP_RECORDS -> Notice("수면 기록이 없어요", listOf("이 파일에는 수면 기록이 없어요.", CSV_HINT))
            ReasonCode.NEED_MORE_RECORDS -> Notice(
                if (pastDay) "이 날은 계산할 수 없어요" else "기록이 부족해 계산할 수 없어요",
                listOf(
                    (if (pastDay) "이 날" else "기준 시각") + " 앞 7일 동안 수면 기록이 없어요.",
                    "기록이 없으면 내내 깨어 있던 것으로 계산돼 결과가 의미 없어서, 계산하지 않았어요."
                )
            )
            ReasonCode.OUT_OF_MEMORY -> Notice("파일이 너무 커요", listOf("파일이 너무 커서 계산하지 못했어요. 기간을 나눠서 올려 주세요."))
            ReasonCode.WAKE_OUT_OF_RANGE -> Notice("입력한 기상 시각을 반영할 수 없어요", listOf(res.reason))
            else -> Notice("분석하지 못했어요", listOf(res.reason, CSV_HINT))
        }
        is HealthResult.Failure -> Notice("분석하지 못했어요", listOf(
            // 잘못된 파일(예: XML)을 고르면 오류 문구에 파일 첫 줄 전체가 들어와 화면을 덮는다 → 앞부분만
            (res.error.message ?: res.error::class.java.simpleName).let { if (it.length > 200) it.take(200) + " …" else it },
            CSV_HINT
        ))
        is HealthResult.PermissionDenied -> Notice("권한이 없어요", listOf("건강 데이터 접근 권한이 거부되었어요."))
        is HealthResult.Success -> Notice("", emptyList())
    }

    /** 예상과 실제의 차이 → "실제로 5시간 22분 일찍 잠들고, 15분 늦게 일어났어요." (10분 이내는 "예상과 비슷하게") */
    private fun diffText(predBed: Long, predWake: Long, actual: Sleep): String {
        fun part(actualMs: Long, predMs: Long, verb: String): String {
            val d = ((actualMs - predMs) / MIN_MS).toInt()
            return when {
                kotlin.math.abs(d) <= 10 -> "예상과 비슷하게 $verb"
                d < 0 -> "${duration(-d.toDouble())} 일찍 $verb"
                else -> "${duration(d.toDouble())} 늦게 $verb"
            }
        }
        return "실제로 " + part(actual.startMs, predBed, "잠들고") + ", " + part(actual.endMs, predWake, "일어났어요") + "."
    }

    /** "오늘 23:40" / "오늘 밤 02:10"(다음 날 새벽) / "내일 09:40" */
    private fun whenText(refMs: Long, ms: Long): String {
        fun dayIndex(t: Long) = Math.floorDiv(t + zone().getOffset(t), DAY_MS)
        val diff = (dayIndex(ms) - dayIndex(refMs)).toInt()
        val word = when {
            diff == 0 -> "오늘"
            diff == 1 && hourOf(ms) < 6 -> "오늘 밤"
            diff == 1 -> "내일"
            else -> dayOnly(ms)
        }
        return "$word ${hhmm(ms)}"
    }

    /** 분(0~1439) → "01:50" */
    private fun clock(m: Int) = String.format(Locale.US, "%02d:%02d", m / 60, m % 60)

    /** a − b 를 −720~+720 분으로 (자정을 넘어도 가까운 쪽 차이) */
    private fun circularDiff(a: Int, b: Int): Int { val d = Math.floorMod(a - b, 1440); return if (d > 720) d - 1440 else d }

    /** 시각(분)의 중간값. 원형 평균을 기준으로 펼친 뒤 중간값을 구한다 (23시·1시가 섞여도 자정 근처로 맞게) */
    private fun circularMedian(mins: List<Int>): Int {
        val center = circularMeanMinutes(mins)
        val offs = mins.map { circularDiff(it, center) }.sorted()
        val n = offs.size
        val med = if (n % 2 == 1) offs[n / 2].toDouble() else (offs[n / 2 - 1] + offs[n / 2]) / 2.0
        return Math.floorMod((center + Math.round(med)).toInt(), 1440)
    }

    private fun recommendationText(r: AnalysisResult): String {
        val rec = r.recommendation ?: return "준비 중 — 권장 시각 계산은 모델 확정 후 연결돼요."
        val parts = listOfNotNull(
            rec.recommendedBedTimeMs?.let { "권장 취침 ${hhmm(it)}" },
            rec.recommendedWakeTimeMs?.let { "권장 기상 ${hhmm(it)}" },
            rec.recommendedSleepMin?.let { "권장 수면 기회 ${(it / 60).toInt()}시간 ${(it % 60).toInt()}분" }
        )
        return if (parts.isEmpty()) "준비 중 — 권장 시각 계산은 모델 확정 후 연결돼요." else parts.joinToString(" · ")
    }

    fun fmt(v: Double): String = String.format(Locale.US, "%.2f", v)

    // 화면 시각은 기기 시간대를 반영한 DisplaySettings.zone으로 표시한다.
    private fun fmt(pattern: String, tz: TimeZone = zone()) = SimpleDateFormat(pattern, Locale.KOREA).apply { timeZone = tz }
    fun hhmm(ms: Long): String = fmt("HH:mm").format(Date(ms))
    fun dayHour(ms: Long): String = fmt("M/d HH시").format(Date(ms))
    fun dayOnly(ms: Long): String = fmt("M/d").format(Date(ms))
    fun dayTime(ms: Long): String = fmt("M/d(E) HH:mm").format(Date(ms))
}

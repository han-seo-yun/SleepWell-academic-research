package app.sleepwell.analysis

import java.io.File
import java.util.Properties

/*
 * 내 정보(프로필) 저장 (소빈 · 2026-10-31, 통합 v6)
 *
 * 사용자 희망 수면시간과 선언한 평소 시각을 개인화 초기값으로 보존한다.
 * 나이대·성별·활동량은 집단 참고값에 쓰며 희망 수면시간을 덮어쓰지 않는다.
 * 앱 전용 폴더의 profile.properties 한 파일. 서버로 보내지 않는다. Android 없이 시험할 수 있게 File 만 받는다.
 */
object ProfileStore {

    const val FILE_NAME = "profile.properties"

    /** 엔진이 아는 나이대 (UserModelEngine.normalizeAgeBand) */
    val AGE_BANDS = listOf("18-29", "30-44", "45-59", "60-80")

    /** 화면 표시 이름 → 엔진 값 (female / male / any) */
    val SEXES = listOf("female" to "여성", "male" to "남성", "any" to "응답 안 함")

    /** 연구 설문과 동일한 표시 이름 → 저장 값. 직업은 현재 모델 계수가 아니라 프로필 계약으로만 보존한다. */
    val OCCUPATIONS: List<Pair<String, String>> = listOf(
        "학생" to "student", "직장인" to "worker", "기타" to "other"
    )

    /**
     * 하루 활동량 구간 → 엔진에 넘길 대표 분 (엔진 구간: 30분 미만 low · 90분 미만 medium · 그 이상 high)
     */
    val ACTIVITY = listOf(15 to "30분 미만", 60 to "30분~1시간 30분", 120 to "1시간 30분 이상")

    fun load(file: File): NextNightModel.ModelProfile? {
        if (!file.isFile) return null
        return try {
            val p = Properties().apply { file.inputStream().use { load(it) } }
            val base = NextNightModel.ModelProfile()
            val rawAge = p.getProperty("ageBand")
            val rawSex = p.getProperty("sex")
            val rawOccupation = p.getProperty("occupation")
            val loaded = NextNightModel.ModelProfile(
                ageBand = rawAge?.takeIf { it in AGE_BANDS } ?: base.ageBand,
                sex = rawSex?.takeIf { s -> SEXES.any { it.first == s } } ?: base.sex,
                activityMinutes = p.getProperty("activityMinutes")?.toIntOrNull()?.coerceIn(0, 1440) ?: base.activityMinutes,
                desiredSleepMinutes = p.getProperty("desiredSleepMinutes")?.toIntOrNull()?.coerceIn(420, 540) ?: base.desiredSleepMinutes,
                usualBed = validClock(p.getProperty("usualBed")) ?: base.usualBed,
                usualWake = validClock(p.getProperty("usualWake")) ?: base.usualWake,
                occupation = rawOccupation?.takeIf(::validOccupation) ?: "",
                onboardingCompleted = false
            )
            loaded.copy(onboardingCompleted = p.getProperty("onboardingCompleted").toBoolean() &&
                rawAge == loaded.ageBand && rawSex == loaded.sex && rawOccupation == loaded.occupation &&
                validBasicFields(loaded))
        } catch (e: Exception) { null }
    }

    fun save(file: File, profile: NextNightModel.ModelProfile) {
        require(profile.desiredSleepMinutes in 420..540) { "희망 수면시간은 7~9시간으로 입력해 주세요." }
        require(validClock(profile.usualBed) != null && validClock(profile.usualWake) != null) { "평소 시각은 HH:mm 형식으로 입력해 주세요." }
        require(profile.activityMinutes in 0..1440) { "활동 시간은 하루 범위로 입력해 주세요." }
        if (profile.onboardingCompleted) {
            require(validBasicFields(profile)) { "나이대·성별·직업을 올바르게 선택해 주세요." }
        }
        val p = Properties().apply {
            setProperty("ageBand", profile.ageBand)
            setProperty("sex", profile.sex)
            setProperty("occupation", profile.occupation)
            setProperty("onboardingCompleted", profile.onboardingCompleted.toString())
            setProperty("activityMinutes", profile.activityMinutes.toString())
            setProperty("desiredSleepMinutes", profile.desiredSleepMinutes.toString())
            setProperty("usualBed", profile.usualBed)
            setProperty("usualWake", profile.usualWake)
        }
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.outputStream().use { p.store(it, "SleepWell profile") }
        if (!tmp.renameTo(file)) { tmp.copyTo(file, overwrite = true); tmp.delete() }
    }

    /** 화면 한 줄 요약: "18-29 · 선택 안 함 · 활동 30분~1시간 30분" */
    fun summary(p: NextNightModel.ModelProfile): String =
        "${p.ageBand}세 · ${SEXES.firstOrNull { it.first == p.sex }?.second ?: p.sex} · " +
            "${OCCUPATIONS.firstOrNull { it.second == p.occupation }?.first ?: "직업 미입력"} · " +
            "희망 ${p.desiredSleepMinutes / 60}시간 ${p.desiredSleepMinutes % 60}분 · 평소 ${p.usualBed}~${p.usualWake}"

    /** 파일 부재, legacy/incomplete 저장, 또는 필수 선택값 손상은 모두 최초 설정 화면으로 보낸다. */
    fun needsOnboarding(profile: NextNightModel.ModelProfile?): Boolean =
        profile == null || !profile.onboardingCompleted || !validBasicFields(profile)

    fun validClock(value: String?): String? = value?.takeIf {
        Regex("\\d{2}:\\d{2}").matches(it) && runCatching { java.time.LocalTime.parse(it) }.isSuccess
    }

    /** Manual duration input preserves minute precision within the existing adult target contract. */
    fun parseDesiredSleepMinutes(hoursText: String, minutesText: String): Int? {
        val hours = hoursText.trim().takeIf { it.matches(Regex("[0-9]{1,2}")) }?.toIntOrNull() ?: return null
        val minutes = minutesText.trim().takeIf { it.matches(Regex("[0-9]{1,2}")) }?.toIntOrNull() ?: return null
        if (hours !in 7..9 || minutes !in 0..59) return null
        return (hours * 60 + minutes).takeIf { it in 420..540 }
    }

    fun activityLabel(min: Int): String = when {
        min < 30 -> ACTIVITY[0].second
        min < 90 -> ACTIVITY[1].second
        else -> ACTIVITY[2].second
    }

    private fun validBasicFields(profile: NextNightModel.ModelProfile): Boolean =
        profile.ageBand in AGE_BANDS && SEXES.any { it.first == profile.sex } && validOccupation(profile.occupation)

    private fun validOccupation(value: String): Boolean = OCCUPATIONS.any { it.second == value }
}

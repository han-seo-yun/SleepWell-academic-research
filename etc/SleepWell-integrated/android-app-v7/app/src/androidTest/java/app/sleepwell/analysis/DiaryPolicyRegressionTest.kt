package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.DisplaySettings
import app.sleepwell.health.DisplayZone
import app.sleepwell.health.DirectInput
import app.sleepwell.health.SleepRecord as WatchRecord
import app.sleepwell.user.SleepRecord as DiaryRecord
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class DiaryPolicyRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")
    private lateinit var previousZone: DisplayZone
    private lateinit var previousDiary: List<DiaryRecord>
    private var previousDemoMode = false
    private lateinit var previousProfile: NextNightModel.ModelProfile

    override fun setUp() {
        super.setUp()
        previousZone = DisplaySettings.zone
        previousDiary = NextNightModel.diaryRecords
        previousDemoMode = NextNightModel.demoMode
        previousProfile = NextNightModel.profile
        DisplaySettings.zone = DisplayZone.KST
        NextNightModel.diaryRecords = emptyList()
        NextNightModel.demoMode = false
    }

    override fun tearDown() {
        DisplaySettings.zone = previousZone
        NextNightModel.diaryRecords = previousDiary
        NextNightModel.demoMode = previousDemoMode
        NextNightModel.profile = previousProfile
        super.tearDown()
    }

    fun testDirectInputCreatesCausalExplicitDiaryWithMeasurements() {
        val now = atSeoul(2026, 11, 3, 12, 0)
        val parsed = DirectInputParser.parse(
            DirectInputForm(
                bedText = "2026-11-02 23:00",
                wakeText = "2026-11-03 07:00",
                sleepLatencyText = "20",
                awakeMinutesText = "30",
                condition = 4,
                rested = 5,
                exerciseMinText = "45"
            ),
            nowMs = now.toEpochMilli()
        )

        assertTrue(parsed.errors.joinToString(), parsed.ok)
        assertNotNull(parsed.diary)
        val diary = parsed.diary!!
        assertEquals("diary:2026-11-03", diary.id)
        assertEquals("manual", diary.source)
        assertEquals(now.toString(), diary.modifiedAt)
        assertEquals(20, diary.sleepLatencyMinutes)
        assertEquals(30, diary.awakeMinutes)
        assertEquals(3, diary.fatigue)
        assertEquals(5, diary.rested)
        assertEquals(45.0, diary.exerciseMinutes ?: -1.0, 0.0)
    }

    fun testFutureCompletedSleepAndImpossibleMeasurementsAreRejected() {
        val now = atSeoul(2026, 11, 3, 6, 0)
        val future = DirectInputParser.parse(
            DirectInputForm(bedText = "2026-11-02 23:00", wakeText = "2026-11-03 07:00"),
            nowMs = now.toEpochMilli()
        )
        assertFalse(future.ok)
        assertNull(future.diary)
        assertTrue(future.errors.any { it.contains("현재 시각 이후") })

        val impossible = DirectInputParser.parse(
            DirectInputForm(
                bedText = "2026-11-02 23:00", wakeText = "2026-11-03 07:00",
                sleepLatencyText = "180", awakeMinutesText = "300"
            ),
            nowMs = atSeoul(2026, 11, 3, 12, 0).toEpochMilli()
        )
        assertFalse(impossible.ok)
        assertNull(impossible.diary)
        assertTrue(impossible.errors.any { it.contains("합은") })
    }

    fun testMeasurementBoundsMatchCanonicalPolicy() {
        val latency = DirectInputParser.parse(
            DirectInputForm(
                bedText = "2026-11-02 23:00", wakeText = "2026-11-03 07:00",
                sleepLatencyText = "181"
            ),
            nowMs = atSeoul(2026, 11, 3, 12, 0).toEpochMilli()
        )
        assertFalse(latency.ok)
        assertTrue(latency.errors.any { it.contains("0~180") })

        val awake = DirectInputParser.parse(
            DirectInputForm(
                bedText = "2026-11-02 23:00", wakeText = "2026-11-03 07:00",
                awakeMinutesText = "601"
            ),
            nowMs = atSeoul(2026, 11, 3, 12, 0).toEpochMilli()
        )
        assertFalse(awake.ok)
        assertTrue(awake.errors.any { it.contains("0~600") })
    }

    fun testLiveNightIdUsesEarlierOfUsualWakeAndNoon() {
        assertEquals("2026-11-02", NextNightModel.liveNightId(atSeoulZoned(2026, 11, 3, 6, 59), "07:00").toString())
        assertEquals("2026-11-03", NextNightModel.liveNightId(atSeoulZoned(2026, 11, 3, 7, 0), "07:00").toString())
        assertEquals("2026-11-02", NextNightModel.liveNightId(atSeoulZoned(2026, 11, 3, 11, 59), "13:00").toString())
        assertEquals("2026-11-03", NextNightModel.liveNightId(atSeoulZoned(2026, 11, 3, 12, 0), "13:00").toString())
    }

    fun testAfterMidnightContextAssociatesScheduleAndNapWithPreviousNight() {
        NextNightModel.profile = NextNightModel.ModelProfile(usualWake = "07:00")
        val now = atSeoulZoned(2026, 11, 3, 1, 0)
        val targetWake = atSeoul(2026, 11, 3, 7, 0)
        val napEnd = atSeoul(2026, 11, 2, 16, 0)
        val context = NextNightModel.dailyContext(
            direct = DirectInput(null, null, targetWake.toEpochMilli(), null),
            f = mapOf("napEndMs" to napEnd.toEpochMilli().toDouble(), "napMinutes" to 60.0),
            observedAt = now
        )
        assertNotNull(context)
        val actual = context!!

        assertEquals("2026-11-02", actual.nightId)
        assertEquals(targetWake.toString(), actual.firstStart)
        assertEquals(napEnd.toString(), actual.napEnd)
        assertEquals(napEnd.minusSeconds(3_600).toString(), actual.napStart)

        val parsed = DirectInputParser.parse(
            DirectInputForm(napMinText = "60", napEndText = "16:00"),
            nowMs = now.toInstant().toEpochMilli()
        )
        assertEquals("2026-11-02", parsed.survey?.date)
        val parsedContext = NextNightModel.dailyContext(
            direct = null,
            f = parsed.survey!!.featureValues(SurveyCsv.SURVEY_ZONE),
            observedAt = now
        )
        assertEquals("2026-11-02", parsedContext?.nightId)
        assertEquals(napEnd.toString(), parsedContext?.napEnd)
    }

    fun testLiveDiaryIsNotBlockedByWearableGraphWindow() {
        val parsed = DirectInputParser.parse(
            DirectInputForm(bedText = "2026-10-01 23:00", wakeText = "2026-10-02 07:00"),
            lastRecordEndMs = atSeoul(2026, 11, 1, 7, 0).toEpochMilli(),
            nowMs = atSeoul(2026, 11, 3, 12, 0).toEpochMilli()
        )
        assertTrue(parsed.errors.joinToString(), parsed.ok)
        assertNotNull(parsed.diary)
    }

    fun testDiaryStoreMigratesMissingMeasurementsAndPreservesOtherRows() {
        val dir = File(instrumentation.targetContext.cacheDir, "diary-policy-${System.nanoTime()}")
        assertTrue(dir.mkdirs())
        val file = File(dir, DiaryStore.FILE_NAME)
        try {
            file.writeText("""{"schema_version":1,"records":[{"id":"diary:2026-11-01","day":"2026-11-01","bedtime":"2026-10-31T14:00:00Z","wake_time":"2026-10-31T22:00:00Z","duration_minutes":480,"rested":null,"fatigue":null,"source":"manual","modified_at":"2026-10-31T22:01:00Z"}]}""")
            val migrated = DiaryStore.load(file).single()
            assertNull(migrated.sleepLatencyMinutes)
            assertNull(migrated.awakeMinutes)

            val added = diary("2026-11-02", "2026-11-01T14:00:00Z", "2026-11-01T22:00:00Z", "2026-11-01T22:01:00Z")
                .copy(sleepLatencyMinutes = 10, awakeMinutes = 20, steps = 9_000, exerciseMinutes = 35.0)
            val saved = DiaryStore.save(file, added)
            assertEquals(2, saved.size)
            val restored = DiaryStore.load(file).first { it.id == added.id }
            assertEquals(10, restored.sleepLatencyMinutes)
            assertEquals(20, restored.awakeMinutes)
            assertEquals(9_000, restored.steps)
            assertEquals(35.0, restored.exerciseMinutes ?: -1.0, 0.0)
        } finally {
            file.delete()
            dir.delete()
        }
    }

    fun testDiaryStoreRejectsOlderOverwriteAndMalformedInput() {
        val dir = File(instrumentation.targetContext.cacheDir, "diary-policy-${System.nanoTime()}")
        assertTrue(dir.mkdirs())
        val file = File(dir, DiaryStore.FILE_NAME)
        try {
            val newer = diary("2026-11-02", "2026-11-01T14:00:00Z", "2026-11-01T22:00:00Z", "2026-11-02T00:00:00Z")
            DiaryStore.save(file, newer)
            assertFails { DiaryStore.save(file, newer.copy(modifiedAt = "2026-11-01T23:00:00Z")) }
            file.writeText("not-json")
            assertFails { DiaryStore.load(file) }
        } finally {
            file.delete()
            dir.delete()
        }
    }

    fun testDiaryStoreRejectsCanonicalMeasurementOverflow() {
        val dir = File(instrumentation.targetContext.cacheDir, "diary-policy-${System.nanoTime()}")
        assertTrue(dir.mkdirs())
        val file = File(dir, DiaryStore.FILE_NAME)
        try {
            val base = diary("2026-11-02", "2026-11-01T14:00:00Z", "2026-11-01T22:00:00Z", "2026-11-01T22:01:00Z")
            assertFails { DiaryStore.save(file, base.copy(sleepLatencyMinutes = 181)) }
            assertFails { DiaryStore.save(file, base.copy(awakeMinutes = 601)) }
            assertFails { DiaryStore.save(file, base.copy(awakeMinutes = 480)) }
        } finally {
            file.delete()
            dir.delete()
        }
    }

    fun testWatchRemainsAuxiliaryAndPersistedDiaryOverridesWakeDay() {
        val start = atSeoul(2026, 11, 2, 23, 0)
        val wake = atSeoul(2026, 11, 3, 7, 0)
        val watch = WatchRecord("watch-1", start.toEpochMilli(), wake.toEpochMilli(), 450.0, emptyMap())

        val auxiliary = NextNightModel.nightlyRecords(listOf(watch), surveys = listOf(
            SurveyDay(
                participant = "me", date = "2026-11-03",
                caffeineUsed = null, caffeineMg = null, lastCaffeine = null,
                napMinutes = null, napEnd = null, exerciseMinutes = null, exerciseEnd = null,
                exerciseIntensity = null, fatigue = 7, steps = null, heartRate = null,
                alarmTime = null, firstCommitment = null,
                examTomorrow = false, alcohol = false, illness = false, rested = 5
            )
        ))
        assertEquals("samsung_health", auxiliary.single().source)
        assertNull(auxiliary.single().fatigue)
        assertNull(auxiliary.single().rested)
        assertEquals(wake.toString(), auxiliary.single().modifiedAt)

        val explicit = diary("2026-11-03", start.toString(), wake.toString(), atSeoul(2026, 11, 3, 12, 0).toString())
        NextNightModel.diaryRecords = listOf(explicit)
        val overridden = NextNightModel.nightlyRecords(listOf(watch))
        assertEquals(1, overridden.size)
        assertEquals(explicit, overridden.single())
    }

    fun testDemoRecordsAreExplicitlySyntheticAndNeverMixRealDiary() {
        val start = atSeoul(2026, 11, 2, 23, 0)
        val wake = atSeoul(2026, 11, 3, 7, 0)
        NextNightModel.diaryRecords = listOf(
            diary("2026-11-03", start.toString(), wake.toString(), atSeoul(2026, 11, 3, 12, 0).toString())
        )
        NextNightModel.demoMode = true

        val demo = NextNightModel.nightlyRecords(
            listOf(WatchRecord("synthetic", start.toEpochMilli(), wake.toEpochMilli(), 480.0, emptyMap()))
        )
        assertEquals(1, demo.size)
        assertEquals("manual", demo.single().source)
        assertTrue(demo.single().id.startsWith("demo:"))
        assertFalse(demo.any { it.id.startsWith("diary:") })
    }

    fun testProfileUsesDeclaredBaselineInsteadOfWatchMedian() {
        NextNightModel.profile = NextNightModel.ModelProfile(usualBed = "00:15", usualWake = "08:45")
        val profile = NextNightModel.userProfile(emptyList(), ZonedDateTime.of(2026, 11, 3, 20, 0, 0, 0, seoul))
        assertEquals("00:15", profile.usualBed)
        assertEquals("08:45", profile.usualWake)
    }

    private fun diary(day: String, bed: String, wake: String, observed: String) = DiaryRecord(
        id = "diary:$day", day = day, bedtime = bed, wakeTime = wake, durationMinutes = 480,
        rested = 4, fatigue = 2, source = "manual", modifiedAt = observed
    )

    private fun atSeoul(year: Int, month: Int, day: Int, hour: Int, minute: Int): Instant =
        atSeoulZoned(year, month, day, hour, minute).toInstant()

    private fun atSeoulZoned(year: Int, month: Int, day: Int, hour: Int, minute: Int): ZonedDateTime =
        ZonedDateTime.of(year, month, day, hour, minute, 0, 0, seoul)

    private fun assertFails(block: () -> Unit) {
        try {
            block()
            fail("예외가 발생해야 합니다.")
        } catch (_: IllegalArgumentException) {
            // expected
        }
    }
}

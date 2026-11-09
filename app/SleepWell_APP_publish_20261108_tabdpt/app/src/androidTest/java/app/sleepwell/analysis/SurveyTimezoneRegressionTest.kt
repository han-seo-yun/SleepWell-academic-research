package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class SurveyTimezoneRegressionTest : InstrumentationTestCase() {
    private val newYork = ZoneId.of("America/New_York")
    private lateinit var previousProfile: NextNightModel.ModelProfile

    override fun setUp() {
        super.setUp()
        previousProfile = NextNightModel.profile
        NextNightModel.profile = NextNightModel.ModelProfile(usualWake = "07:30")
    }

    override fun tearDown() {
        NextNightModel.profile = previousProfile
        super.tearDown()
    }

    fun testDirectInputRejectsNonexistentDstWallTime() {
        val parsed = DirectInputParser.parse(
            DirectInputForm(plannedBedText = "2026-03-08 02:30"),
            nowMs = Instant.parse("2026-03-07T12:00:00Z").toEpochMilli(),
            zoneId = newYork
        )

        assertFalse(parsed.ok)
        assertTrue(parsed.errors.any { it.contains("존재하지 않는 시각") })
    }

    fun testClockOnlyTargetRejectsUpcomingDstGapInsteadOfShifting() {
        val parsed = DirectInputParser.parse(
            DirectInputForm(targetWakeText = "02:30"),
            nowMs = Instant.parse("2026-03-08T05:00:00Z").toEpochMilli(), // 00:00 local
            zoneId = newYork
        )

        assertFalse(parsed.ok)
        assertTrue(parsed.errors.any { it.contains("존재하지 않는 시각") })
    }

    fun testPastDstGapDoesNotBlockNextDaysValidOccurrence() {
        val base = Instant.parse("2026-03-08T16:00:00Z") // 12:00 local, gap is already past
        val next = DirectInputParser.nextOccurrence(base.toEpochMilli(), 2, 30, zoneId = newYork)
        assertEquals(Instant.parse("2026-03-09T06:30:00Z").toEpochMilli(), next)
    }

    fun testSurveyEventRejectsNonexistentDstWallTime() {
        val parsed = DirectInputParser.parse(
            DirectInputForm(napMinText = "30", napEndText = "02:30"),
            nowMs = Instant.parse("2026-03-08T16:00:00Z").toEpochMilli(), // 12:00 local
            zoneId = newYork
        )
        assertFalse(parsed.ok)
        assertTrue(parsed.errors.any { it.startsWith("낮잠 끝난 시각:") && it.contains("존재하지 않는 시각") })
    }

    fun testDstOverlapDeterministicallyUsesEarlierInstant() {
        val parsed = DirectInputParser.parse(
            DirectInputForm(plannedBedText = "2026-11-01 01:30"),
            nowMs = Instant.parse("2026-10-31T12:00:00Z").toEpochMilli(),
            zoneId = newYork
        )

        assertTrue(parsed.errors.joinToString(), parsed.ok)
        assertEquals(Instant.parse("2026-11-01T05:30:00Z").toEpochMilli(), parsed.input?.plannedBedMs)
    }

    fun testSurveySourceZoneRoundTripsAndFeaturesKeepOriginalInstant() {
        val file = File(instrumentation.targetContext.cacheDir, "survey-zone-${System.nanoTime()}.csv")
        try {
            val row = survey(
                date = "2026-11-01", sourceDate = "2026-11-01",
                napEnd = "01:30", alarm = "07:00"
            )
            SurveyStore.save(file, row, nowMs = Instant.parse("2026-11-01T18:00:00Z").toEpochMilli())
            val restored = SurveyStore.load(file).single()

            assertEquals("America/New_York", restored.sourceZoneId)
            assertEquals("2026-11-01", restored.sourceDate)
            assertEquals(
                Instant.parse("2026-11-01T05:30:00Z").toEpochMilli().toDouble(),
                restored.featureValues()["napEndMs"] ?: -1.0,
                0.0
            )
        } finally {
            file.delete()
        }
    }

    fun testLegacySurveyDefaultsToKoreanSourceZone() {
        val parsed = SurveyCsv.parse(
            "participant,date,napMinutes,napEnd,alarmTime\nme,2026-11-01,30,15:00,07:00\n"
        )
        val row = parsed.days.single()

        assertEquals(SurveyCsv.LEGACY_SOURCE_ZONE, row.sourceZoneId)
        assertNull(row.sourceDate)
        assertEquals(Instant.parse("2026-11-01T06:00:00Z"), row.eventInstant(row.napEnd))
    }

    fun testAlarmKeepsAbsoluteOccurrenceAndRecursInSourceZone() {
        val row = survey(date = "2026-10-31", sourceDate = "2026-10-31", napEnd = null, alarm = "07:00")
        val first = row.alarmInstant()

        assertEquals(Instant.parse("2026-11-01T12:00:00Z"), first)
        assertEquals("21:00", row.alarmTextIn(ZoneId.of("Asia/Tokyo")))
        assertEquals(
            Instant.parse("2026-11-02T12:00:00Z"),
            row.nextAlarmAfter(Instant.parse("2026-11-01T13:00:00Z"))
        )
    }

    fun testParserCapturesSourceZoneAndLocalNightDate() {
        val now = ZonedDateTime.of(2026, 11, 3, 1, 0, 0, 0, newYork).toInstant()
        val parsed = DirectInputParser.parse(
            DirectInputForm(napMinText = "30", napEndText = "16:00", targetWakeText = "07:00"),
            nowMs = now.toEpochMilli(),
            zoneId = newYork
        )
        assertNotNull(parsed.survey)
        val survey = parsed.survey!!

        assertEquals("America/New_York", survey.sourceZoneId)
        assertEquals("2026-11-02", survey.sourceDate)
        assertEquals(Instant.parse("2026-11-03T12:00:00Z").toEpochMilli(), survey.targetWakeMs)
        assertEquals(Instant.parse("2026-11-02T21:00:00Z"), survey.eventInstant(survey.napEnd))
        assertEquals(Instant.parse("2026-11-03T12:00:00Z"), survey.alarmInstant())
    }

    fun testExplicitDatedTargetPersistsParsedAbsoluteInstant() {
        val now = ZonedDateTime.of(2026, 11, 3, 1, 0, 0, 0, newYork).toInstant()
        val parsed = DirectInputParser.parse(
            DirectInputForm(targetWakeText = "2026-11-04 07:00"),
            nowMs = now.toEpochMilli(),
            zoneId = newYork
        )
        val survey = parsed.survey!!

        assertNull(survey.alarmTime)
        assertEquals(Instant.parse("2026-11-04T12:00:00Z").toEpochMilli(), survey.targetWakeMs)
        assertEquals(Instant.parse("2026-11-04T12:00:00Z"), survey.alarmInstant())
        assertEquals(
            Instant.parse("2026-11-04T12:00:00Z"),
            survey.nextAlarmAfter(Instant.parse("2026-11-03T12:00:00Z"))
        )
    }

    fun testMidnightEventsUseLatestOccurrenceAndRoundTripAbsoluteInstants() {
        val seoul = ZoneId.of("Asia/Seoul")
        val now = ZonedDateTime.of(2026, 11, 5, 1, 0, 0, 0, seoul).toInstant()
        val parsed = DirectInputParser.parse(
            DirectInputForm(
                caffeineMgText = "100", lastCaffeineText = "16:00",
                napMinText = "30", napEndText = "00:30"
            ),
            nowMs = now.toEpochMilli(),
            zoneId = seoul
        )
        assertTrue(parsed.errors.joinToString(), parsed.ok)
        val survey = parsed.survey!!

        assertEquals("2026-11-04", survey.sourceDate)
        assertEquals(Instant.parse("2026-11-04T07:00:00Z").toEpochMilli(), survey.lastCaffeineMs)
        assertEquals(Instant.parse("2026-11-04T15:30:00Z").toEpochMilli(), survey.napEndMs)
        assertEquals(Instant.parse("2026-11-04T07:00:00Z"), survey.lastCaffeineInstant())
        assertEquals(Instant.parse("2026-11-04T15:30:00Z"), survey.napEndInstant())
        assertTrue(survey.summaryIn(seoul).contains("마지막 11/4 16:00"))
        assertTrue(survey.summaryIn(seoul).contains("11/5 00:30 종료"))

        val file = File(instrumentation.targetContext.cacheDir, "survey-midnight-${System.nanoTime()}.csv")
        try {
            SurveyStore.save(file, survey, nowMs = now.toEpochMilli())
            val restored = SurveyStore.load(file).single()
            assertEquals(survey.lastCaffeineMs, restored.lastCaffeineMs)
            assertEquals(survey.napEndMs, restored.napEndMs)
            assertEquals(
                Instant.parse("2026-11-04T15:30:00Z").toEpochMilli().toDouble(),
                restored.featureValues()["napEndMs"] ?: -1.0,
                0.0
            )
        } finally {
            file.delete()
        }
    }

    private fun survey(date: String, sourceDate: String, napEnd: String?, alarm: String?) = SurveyDay(
        participant = "me", date = date,
        caffeineUsed = null, caffeineMg = null, lastCaffeine = null,
        napMinutes = napEnd?.let { 30.0 }, napEnd = napEnd,
        exerciseMinutes = null, exerciseEnd = null, exerciseIntensity = null,
        fatigue = null, steps = null, heartRate = null,
        alarmTime = alarm, firstCommitment = null,
        examTomorrow = false, alcohol = false, illness = false,
        sourceZoneId = newYork.id, sourceDate = sourceDate
    )
}

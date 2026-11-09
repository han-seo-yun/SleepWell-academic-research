package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.SleepSession
import app.sleepwell.health.SleepStage
import app.sleepwell.health.SleepStageSegment
import app.sleepwell.user.SleepRecord
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class ImportedSleepStoreRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun testObservedStagesProduceMeasurementsAndUnchangedReimportKeepsFirstObservation() {
        val dir = isolatedDir("observed")
        try {
            val start = atSeoul(2026, 11, 1, 23, 0)
            val end = atSeoul(2026, 11, 2, 7, 0)
            val session = SleepSession("stable-id", start, end, listOf(
                segment(start, start + 10 * 60_000L, SleepStage.WAKE),
                segment(start + 10 * 60_000L, end - 20 * 60_000L, SleepStage.LIGHT),
                segment(end - 20 * 60_000L, end, SleepStage.WAKE)
            ))
            val firstObserved = atSeoul(2026, 11, 2, 8, 0)
            val metadata = mapOf("stable-id" to HealthConnectCsv.ModelMetadata(true, "samsung_health", firstObserved))
            val first = ImportedSleepStore.ingest(dir, listOf(session), "health_connect", firstObserved, metadata).single()
            assertEquals("samsung_health", first.source)
            assertEquals(10, first.sleepLatencyMinutes)
            assertEquals(20, first.awakeMinutes)
            assertEquals(Instant.ofEpochMilli(firstObserved).toString(), first.modifiedAt)

            val laterImport = atSeoul(2026, 11, 3, 12, 0)
            val repeated = ImportedSleepStore.ingest(
                dir, listOf(session), "health_connect", laterImport,
                mapOf("stable-id" to HealthConnectCsv.ModelMetadata(true, "samsung_health", laterImport))
            ).single()
            assertEquals(first.modifiedAt, repeated.modifiedAt)
            assertEquals(first, repeated)
            val supplemented = ImportedSleepStore.loadModelNights(
                dir, atSeoul(2026, 11, 4, 12, 0),
                listOf(survey("2026-11-02", "2026-11-02T02:00:00Z", rested = 5, latency = 25, awake = 55))
            ).single()
            assertEquals(10, supplemented.sleepLatencyMinutes)
            assertEquals(20, supplemented.awakeMinutes)
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testProviderTimestampCannotBackdateFirstImportAndStableIdCanCorrectDurationDown() {
        val dir = isolatedDir("correction")
        try {
            val start = atSeoul(2026, 11, 1, 23, 0)
            val originalEnd = atSeoul(2026, 11, 2, 7, 0)
            val importedAt = atSeoul(2026, 11, 2, 20, 0)
            val providerModified = atSeoul(2026, 11, 2, 8, 0)
            val metadata = mapOf("stable" to HealthConnectCsv.ModelMetadata(false, "health_connect", providerModified))
            val original = ImportedSleepStore.ingest(
                dir, listOf(SleepSession("stable", start, originalEnd)), "health_connect", importedAt, metadata
            ).single()
            assertEquals(Instant.ofEpochMilli(importedAt).toString(), original.modifiedAt)

            val correctedEnd = atSeoul(2026, 11, 2, 6, 0)
            val correctionImport = atSeoul(2026, 11, 2, 21, 0)
            val corrected = ImportedSleepStore.ingest(
                dir, listOf(SleepSession("stable", start, correctedEnd)), "health_connect", correctionImport, metadata
            ).single()
            assertEquals(420, corrected.durationMinutes)
            assertEquals(Instant.ofEpochMilli(correctionImport).toString(), corrected.modifiedAt)
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testMissingUnknownOrPartialStagesNeverFabricateZeroMeasurements() {
        val dir = isolatedDir("missing-stage")
        try {
            val start = atSeoul(2026, 11, 1, 23, 0)
            val end = atSeoul(2026, 11, 2, 7, 0)
            val empty = SleepSession("empty", start, end, emptyList())
            val unknown = SleepSession("unknown", start + 24 * 3_600_000L, end + 24 * 3_600_000L, listOf(
                segment(start + 24 * 3_600_000L, end + 24 * 3_600_000L, SleepStage.UNKNOWN)
            ))
            val partial = SleepSession("partial", start + 48 * 3_600_000L, end + 48 * 3_600_000L, listOf(
                segment(start + 48 * 3_600_000L + 60 * 60_000L, end + 48 * 3_600_000L, SleepStage.LIGHT)
            ))
            val observed = atSeoul(2026, 11, 5, 12, 0)
            val metadata = listOf("empty", "unknown", "partial").associateWith {
                HealthConnectCsv.ModelMetadata(true, "health_connect", observed)
            }
            val records = ImportedSleepStore.ingest(dir, listOf(empty, unknown, partial), "health_connect", observed, metadata)
            assertEquals(3, records.size)
            records.forEach { record ->
                assertNull(record.sleepLatencyMinutes)
                assertNull(record.awakeMinutes)
            }
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testAllSleepCoverageCanProveAwakeZeroButNotLatencyZeroAndOverlapIsRejected() {
        val dir = isolatedDir("measurement-bounds")
        try {
            val start = atSeoul(2026, 11, 1, 23, 0)
            val end = atSeoul(2026, 11, 2, 7, 0)
            val allSleep = SleepSession("all-sleep", start, end, listOf(segment(start, end, SleepStage.LIGHT)))
            val overlapStart = start + 24 * 3_600_000L
            val overlapEnd = end + 24 * 3_600_000L
            val overlap = SleepSession("overlap", overlapStart, overlapEnd, listOf(
                segment(overlapStart, overlapStart + 5 * 3_600_000L, SleepStage.LIGHT),
                segment(overlapStart + 4 * 3_600_000L, overlapEnd, SleepStage.WAKE)
            ))
            val observed = atSeoul(2026, 11, 4, 12, 0)
            val metadata = listOf("all-sleep", "overlap").associateWith {
                HealthConnectCsv.ModelMetadata(true, "health_connect", observed)
            }
            val records = ImportedSleepStore.ingest(dir, listOf(allSleep, overlap), "health_connect", observed, metadata)
            val allSleepRecord = records.single { it.id.endsWith(":all-sleep") }
            assertNull(allSleepRecord.sleepLatencyMinutes)
            assertEquals(0, allSleepRecord.awakeMinutes)
            val overlapRecord = records.single { it.id.endsWith(":overlap") }
            assertNull(overlapRecord.sleepLatencyMinutes)
            assertNull(overlapRecord.awakeMinutes)
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testGeneratedCsvPreservesSessionBoundsProvenanceAndFirstReceipt() {
        val dir = isolatedDir("csv-provenance")
        try {
            val start = atSeoul(2026, 11, 1, 23, 0)
            val end = atSeoul(2026, 11, 2, 7, 0)
            val providerModified = atSeoul(2026, 11, 2, 8, 0)
            val importedAt = atSeoul(2026, 11, 2, 9, 0)
            val csv = HealthConnectCsv.toCsv(listOf(HcSleepSession(
                id = "provider-id", startMs = start, endMs = end, offsetSeconds = 9 * 3600,
                stages = listOf(HcSleepStage(start + 60 * 60_000L, end - 30 * 60_000L, HealthConnectCsv.LIGHT)),
                source = "samsung_health", observedAtMs = providerModified
            )), idPrefix = "")
            SleepDataStore.saveHealthConnect(dir, csv, source = "health_connect", observedAtMs = importedAt)
            val record = ImportedSleepStore.loadModelNights(dir, importedAt).single()
            assertEquals("samsung_health", record.source)
            assertEquals(Instant.ofEpochMilli(start).toString(), record.bedtime)
            assertEquals(Instant.ofEpochMilli(end).toString(), record.wakeTime)
            assertEquals(Instant.ofEpochMilli(importedAt).toString(), record.modifiedAt)
            assertNull(record.sleepLatencyMinutes)
            assertNull(record.awakeMinutes)
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testFutureWakeIsNotOfferedBeforeCompletion() {
        val dir = isolatedDir("future")
        try {
            val now = atSeoul(2026, 11, 2, 6, 0)
            ImportedSleepStore.ingest(
                dir,
                listOf(SleepSession("future", atSeoul(2026, 11, 1, 23, 0), atSeoul(2026, 11, 2, 7, 0))),
                "health_connect", now
            )
            assertTrue(ImportedSleepStore.loadModelNights(dir, now).isEmpty())
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testMalformedExistingHealthConnectArchiveIsNeverOverwritten() {
        val dir = isolatedDir("corrupt-existing")
        try {
            val target = File(dir, SleepDataStore.HEALTH_CONNECT)
            target.writeText("not,a,health-connect-file", Charsets.UTF_8)
            val before = target.readText(Charsets.UTF_8)
            val start = atSeoul(2026, 11, 1, 23, 0)
            val end = atSeoul(2026, 11, 2, 7, 0)
            val incoming = HealthConnectCsv.toCsv(listOf(HcSleepSession(
                "new", start, end, 9 * 3600, emptyList()
            )))
            try {
                SleepDataStore.saveHealthConnect(dir, incoming, observedAtMs = atSeoul(2026, 11, 2, 9, 0))
                fail("손상된 기존 파일을 덮어쓰면 안 됩니다.")
            } catch (expected: IllegalArgumentException) {
                assertTrue(expected.message.orEmpty().contains("손상"))
            }
            assertEquals(before, target.readText(Charsets.UTF_8))
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testSameSourceWakeDateKeepsOneLongestMainSleepWithoutDroppingOlderDates() {
        val dir = isolatedDir("longest-night")
        try {
            val firstStart = atSeoul(2026, 10, 31, 23, 0)
            val firstEnd = atSeoul(2026, 11, 1, 7, 0)
            val longStart = atSeoul(2026, 11, 1, 22, 30)
            val sameWake = atSeoul(2026, 11, 2, 7, 0)
            val shortStart = atSeoul(2026, 11, 2, 1, 0)
            val observed = atSeoul(2026, 11, 2, 12, 0)
            ImportedSleepStore.ingest(
                dir,
                listOf(
                    SleepSession("older-day", firstStart, firstEnd),
                    SleepSession("long", longStart, sameWake),
                    SleepSession("short", shortStart, sameWake)
                ),
                "health_connect", observed
            )
            val loaded = ImportedSleepStore.loadModelNights(dir, observed)
            assertEquals(2, loaded.size)
            assertEquals(listOf("2026-11-01", "2026-11-02"), loaded.map { it.day })
            assertTrue(loaded.single { it.day == "2026-11-02" }.id.endsWith(":long"))

            ImportedSleepStore.ingest(
                dir, listOf(SleepSession("later-short", shortStart + 30 * 60_000L, sameWake)),
                "health_connect", observed + 3_600_000L
            )
            val unchanged = ImportedSleepStore.loadModelNights(dir, observed + 3_600_000L)
            assertEquals(2, unchanged.size)
            assertTrue(unchanged.single { it.day == "2026-11-02" }.id.endsWith(":long"))
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testQuestionnaireJoinIsExplicitCausalAndDoesNotRetimestampWearableTiming() {
        val dir = isolatedDir("questionnaire")
        try {
            val start = atSeoul(2026, 11, 1, 23, 0)
            val end = atSeoul(2026, 11, 2, 7, 0)
            val importedAt = atSeoul(2026, 11, 2, 8, 0)
            ImportedSleepStore.ingest(
                dir, listOf(SleepSession("watch", start, end, emptyList())),
                "health_connect", importedAt
            )
            val tooEarly = survey("2026-11-02", "2026-11-01T21:00:00Z", rested = 1, latency = 90)
            val valid = survey("2026-11-02", "2026-11-02T01:00:00Z", rested = 5, latency = 25)
            val futureRevision = survey("2026-11-02", "2026-11-03T01:00:00Z", rested = 2, latency = 50)
            val evaluation = Instant.parse("2026-11-02T12:00:00Z")

            val joined = ImportedSleepStore.loadModelNights(dir, evaluation.toEpochMilli(), listOf(tooEarly, valid, futureRevision)).single()
            assertEquals(Instant.ofEpochMilli(importedAt).toString(), joined.modifiedAt)
            assertEquals("questionnaire", joined.subjectiveSource)
            assertEquals("2026-11-02T01:00:00Z", joined.subjectiveObservedAt)
            assertEquals(5, joined.rested)
            assertEquals(25, joined.sleepLatencyMinutes)
            assertNull(joined.awakeMinutes)

            val raw = ImportedSleepStore.loadModelNights(dir, evaluation.toEpochMilli(), emptyList()).single()
            assertNull(raw.rested)
            assertNull(raw.subjectiveSource)
            assertNull(raw.sleepLatencyMinutes)
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testManualDiaryOverridesSameWakeDateWhileImportedSourceRemainsUnchanged() {
        val oldImported = NextNightModel.importedRecords
        val oldDiary = NextNightModel.diaryRecords
        val oldDemo = NextNightModel.demoMode
        try {
            val start = Instant.ofEpochMilli(atSeoul(2026, 11, 1, 23, 0))
            val end = Instant.ofEpochMilli(atSeoul(2026, 11, 2, 7, 0))
            val wearable = SleepRecord(
                id = "wearable:health_connect:watch", day = "2026-11-02", bedtime = start.toString(), wakeTime = end.toString(),
                durationMinutes = 480, rested = null, fatigue = null, source = "health_connect",
                modifiedAt = end.plusSeconds(3_600).toString()
            )
            val manual = wearable.copy(
                id = "diary:2026-11-02", source = "manual", modifiedAt = end.plusSeconds(7_200).toString(),
                sleepLatencyMinutes = 15, awakeMinutes = 10
            )
            NextNightModel.importedRecords = listOf(wearable)
            NextNightModel.diaryRecords = emptyList()
            NextNightModel.demoMode = false
            val importedOnly = NextNightModel.nightlyRecords(emptyList(), includeImportedRecords = true).single()
            assertEquals("health_connect", importedOnly.source)
            assertEquals(wearable.id, importedOnly.id)

            NextNightModel.diaryRecords = listOf(manual)
            val overridden = NextNightModel.nightlyRecords(emptyList(), includeImportedRecords = true).single()
            assertEquals(manual, overridden)
        } finally {
            NextNightModel.importedRecords = oldImported
            NextNightModel.diaryRecords = oldDiary
            NextNightModel.demoMode = oldDemo
        }
    }

    fun testWearablePolicyUsesOnlyCausallyAvailablePersistedImports() {
        val oldImported = NextNightModel.importedRecords
        val oldDiary = NextNightModel.diaryRecords
        val oldDemo = NextNightModel.demoMode
        try {
            val startMs = atSeoul(2026, 11, 1, 23, 0)
            val endMs = atSeoul(2026, 11, 2, 7, 0)
            val boundary = Instant.ofEpochMilli(atSeoul(2026, 11, 2, 10, 0))
            val imported = SleepRecord(
                id = "wearable:health_connect:persisted", day = "2026-11-02",
                bedtime = Instant.ofEpochMilli(startMs).toString(), wakeTime = Instant.ofEpochMilli(endMs).toString(),
                durationMinutes = 480, rested = null, fatigue = null, source = "health_connect",
                modifiedAt = boundary.minusSeconds(60).toString()
            )
            val lateCorrection = imported.copy(
                id = "diary:2026-11-02", source = "manual", modifiedAt = boundary.plusSeconds(60).toString()
            )
            NextNightModel.demoMode = false
            NextNightModel.importedRecords = listOf(imported)
            NextNightModel.diaryRecords = listOf(lateCorrection)

            val result = NextNightModel.nightlyRecords(
                records = listOf(app.sleepwell.health.SleepRecord("raw", startMs, endMs, 480.0, emptyMap())),
                observedAt = boundary,
                includeImportedRecords = true
            )
            assertEquals(listOf(imported), result)

            NextNightModel.importedRecords = emptyList()
            val noPersistedFallback = NextNightModel.nightlyRecords(
                records = listOf(app.sleepwell.health.SleepRecord("raw", startMs, endMs, 480.0, emptyMap())),
                observedAt = boundary,
                includeImportedRecords = true
            )
            assertTrue(noPersistedFallback.isEmpty())
        } finally {
            NextNightModel.importedRecords = oldImported
            NextNightModel.diaryRecords = oldDiary
            NextNightModel.demoMode = oldDemo
        }
    }

    private fun survey(wakeDate: String, savedAt: String, rested: Int, latency: Int, awake: Int? = null) = SurveyDay(
        participant = "me", date = "2026-11-02", caffeineUsed = null, caffeineMg = null, lastCaffeine = null,
        napMinutes = null, napEnd = null, exerciseMinutes = null, exerciseEnd = null, exerciseIntensity = null,
        fatigue = null, steps = null, heartRate = null, alarmTime = null, firstCommitment = null,
        examTomorrow = false, alcohol = false, illness = false, savedAt = savedAt, rested = rested,
        sleepWakeDate = wakeDate, sleepLatencyMinutes = latency, awakeMinutes = awake
    )

    private fun segment(start: Long, end: Long, stage: SleepStage) = SleepStageSegment(start, end, stage)

    private fun atSeoul(year: Int, month: Int, day: Int, hour: Int, minute: Int): Long =
        ZonedDateTime.of(year, month, day, hour, minute, 0, 0, seoul).toInstant().toEpochMilli()

    private fun isolatedDir(label: String): File =
        File(instrumentation.targetContext.cacheDir, "imported-sleep-$label-${System.nanoTime()}").also { assertTrue(it.mkdirs()) }
}

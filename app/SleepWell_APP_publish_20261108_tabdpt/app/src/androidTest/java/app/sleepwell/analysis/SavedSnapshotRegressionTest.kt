package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.DirectInput
import app.sleepwell.health.DisplaySettings
import app.sleepwell.health.DisplayZone
import app.sleepwell.health.PredictedPoint
import app.sleepwell.health.PredictionOrigin
import app.sleepwell.health.PropensityPoint
import app.sleepwell.health.Recommendation
import app.sleepwell.health.SleepRecord
import app.sleepwell.user.SleepRecord as PersonalizationRecord
import java.io.File
import java.time.Instant

@Suppress("DEPRECATION")
class SavedSnapshotRegressionTest : InstrumentationTestCase() {
    fun testFrozenResultRoundTripAndReplayDoNotReadOrMutateLiveModelState() {
        val base = tempBase()
        val oldProfile = NextNightModel.profile
        val oldSurveys = NextNightModel.surveyDays
        val oldDiary = NextNightModel.diaryRecords
        val oldImported = NextNightModel.importedRecords
        val oldZone = DisplaySettings.zone
        try {
            val calculated = result()
            val direct = DirectInput(calculated.sleepRecords.single().startMs, calculated.sleepRecords.single().endMs,
                epoch("2026-11-04T22:00:00Z"), null)
            val diary = PersonalizationRecord(
                id = "diary-1", day = "2026-11-02", bedtime = "2026-11-02T14:30:00Z",
                wakeTime = "2026-11-02T22:30:00Z", durationMinutes = 480, rested = 4, fatigue = 3,
                source = "manual", modifiedAt = "2026-11-03T00:00:00Z"
            )
            SavedSnapshot.save(base, 101L, calculated, direct, 4, null, null,
                profile = NextNightModel.ModelProfile(usualBed = "22:45", usualWake = "06:45", onboardingCompleted = true),
                diaryRecords = listOf(diary), importedRecords = listOf(diary.copy(id = "watch-1", source = "samsung_health")),
                wearablePersonalizationEnabled = true,
                basisMs = calculated.sleepRecords.single().endMs,
                latestRefMs = calculated.calculationTimeMs,
                dataStartMs = calculated.inputPeriodStartMs)

            NextNightModel.profile = NextNightModel.ModelProfile(desiredSleepMinutes = 420)
            NextNightModel.surveyDays = emptyList()
            NextNightModel.diaryRecords = emptyList()
            NextNightModel.importedRecords = emptyList()

            val snapshot = SavedSnapshot.load(base, 101L)!!
            assertEquals(calculated, snapshot.result)
            DisplaySettings.zone = DisplayZone.UTC
            val beforeProfile = NextNightModel.profile
            val replay = SavedSnapshot.replay(snapshot)
            assertNotNull(replay.display)
            assertEquals(calculated.sleepRecords.single().endMs, replay.display!!.night!!.basisMs)
            assertEquals("11/2(월) 기상", replay.display!!.night!!.label)
            assertEquals(calculated, replay.result)
            assertEquals(beforeProfile, NextNightModel.profile)
            assertTrue(NextNightModel.surveyDays.isEmpty())
            assertTrue(NextNightModel.diaryRecords.isEmpty())
            assertTrue(NextNightModel.importedRecords.isEmpty())
            assertEquals("gradual_v1", snapshot.result.recommendation!!.runtimePolicy)
            assertEquals("measured_complete", snapshot.result.recommendation!!.evidenceStatus)
            assertEquals(PredictionOrigin.CSV_REANALYSIS, snapshot.result.recommendation!!.predictionOrigin)
            assertEquals(epoch("2026-11-03T11:00:00Z"), snapshot.result.recommendation!!.historyCutoffMs)
            assertEquals(8, snapshot.result.recommendation!!.historyRecordCount)
            assertEquals(6, snapshot.result.recommendation!!.csvHistoryRecordCount)
            val meta = File(SavedSnapshot.dir(base, 101L), "meta.properties").readText()
            assertTrue(meta.contains("usualBed=22\\:45"))
            assertTrue(meta.contains("onboardingCompleted=true"))
            assertTrue(meta.contains("wearablePersonalizationEnabled=true"))
            val personalization = File(SavedSnapshot.dir(base, 101L), "personalization_records.json").readText()
            assertTrue(personalization.contains("diary-1"))
            assertTrue(personalization.contains("samsung_health"))
        } finally {
            NextNightModel.profile = oldProfile
            NextNightModel.surveyDays = oldSurveys
            NextNightModel.diaryRecords = oldDiary
            NextNightModel.importedRecords = oldImported
            DisplaySettings.zone = oldZone
            base.deleteRecursively()
        }
    }

    fun testCalculationCsvIsContentAddressedAndDeletedAfterLastSnapshot() {
        val base = tempBase()
        val source = File(base, "working.csv").apply { writeText("timestamp,stage\n1,LIGHT\n") }
        try {
            val captured = SavedSnapshot.captureInput(base, source)
            assertTrue(captured.file.isFile)
            assertEquals(source.readText(), captured.file.readText())
            assertEquals(captured.sha256, SavedSnapshot.inputSha256(captured.file))
            SavedSnapshot.save(base, 201L, result(), null, null, null, captured.file, captured.sha256)
            SavedSnapshot.save(base, 202L, result(), null, null, null, captured.file, captured.sha256)
            SavedSnapshot.releaseCapturedInput(base, captured)
            assertEquals(SavedSnapshot.load(base, 201L)!!.input!!.canonicalPath,
                SavedSnapshot.load(base, 202L)!!.input!!.canonicalPath)
            SavedSnapshot.delete(base, 201L)
            assertTrue(captured.file.isFile)
            SavedSnapshot.delete(base, 202L)
            assertFalse(captured.file.exists())
        } finally { base.deleteRecursively() }
    }

    fun testDiaryOnlySnapshotDoesNotInventCalculationCsv() {
        val base = tempBase()
        try {
            val diary = result().copy(sleepRecords = emptyList(), scHistory = emptyList(), futureS = null)
            SavedSnapshot.save(base, 301L, diary, null, null, null, null)
            val snapshot = SavedSnapshot.load(base, 301L)!!
            assertNull(snapshot.input)
            assertTrue(snapshot.result.sleepRecords.isEmpty())
        } finally { base.deleteRecursively() }
    }

    fun testLegacySnapshotWithoutPredictionOriginRemainsUnspecified() {
        val base = tempBase()
        try {
            SavedSnapshot.save(base, 302L, result(), null, null, null, null)
            val resultFile = File(SavedSnapshot.dir(base, 302L), "result.json")
            val root = org.json.JSONObject(resultFile.readText())
            root.getJSONObject("recommendation").apply {
                remove("predictionOrigin")
                remove("historyCutoffMs")
                remove("historyRecordCount")
                remove("csvHistoryRecordCount")
            }
            resultFile.writeText(root.toString())

            val restored = SavedSnapshot.load(base, 302L)!!.result.recommendation!!

            assertEquals(PredictionOrigin.LEGACY_UNSPECIFIED, restored.predictionOrigin)
            assertNull(restored.historyCutoffMs)
            assertNull(restored.historyRecordCount)
            assertNull(restored.csvHistoryRecordCount)
        } finally { base.deleteRecursively() }
    }

    fun testNewProcessCleanupRemovesStaleLeaseAndUnreferencedInput() {
        val base = tempBase()
        val source = File(base, "working.csv").apply { writeText("timestamp,stage\n1,LIGHT\n") }
        try {
            val captured = SavedSnapshot.captureInput(base, source)
            assertTrue(captured.lease.isFile)
            assertTrue(captured.file.isFile)

            SavedSnapshot.cleanupStaleLeases(File(base, SavedSnapshot.DIR), "new-process-session")

            assertFalse(captured.lease.exists())
            assertFalse(captured.file.exists())
        } finally { base.deleteRecursively() }
    }

    fun testNewProcessCleanupPreservesInputReferencedBySavedSnapshot() {
        val base = tempBase()
        val source = File(base, "working.csv").apply { writeText("timestamp,stage\n1,LIGHT\n") }
        try {
            val captured = SavedSnapshot.captureInput(base, source)
            SavedSnapshot.save(base, 351L, result(), null, null, null, captured.file, captured.sha256)

            SavedSnapshot.cleanupStaleLeases(File(base, SavedSnapshot.DIR), "new-process-session")

            assertFalse(captured.lease.exists())
            assertTrue(captured.file.isFile)
            assertEquals(captured.file.canonicalPath, SavedSnapshot.load(base, 351L)!!.input!!.canonicalPath)
        } finally { base.deleteRecursively() }
    }

    fun testRetainedInputSurvivesUntilEveryCurrentProcessLeaseIsReleased() {
        val base = tempBase()
        val source = File(base, "working.csv").apply { writeText("timestamp,stage\n1,LIGHT\n") }
        try {
            val owner = SavedSnapshot.captureInput(base, source)
            val reader = SavedSnapshot.retainCapturedInput(base, owner)
            assertTrue(owner.lease.isFile)
            assertTrue(reader.lease.isFile)
            assertFalse(owner.lease.canonicalPath == reader.lease.canonicalPath)

            SavedSnapshot.releaseCapturedInput(base, owner)
            assertTrue(reader.file.isFile)
            SavedSnapshot.releaseCapturedInput(base, reader)
            assertFalse(reader.file.exists())
        } finally { base.deleteRecursively() }
    }

    fun testSaveRejectsCsvChangedAfterAnalysisDigest() {
        val base = tempBase()
        val input = File(base, "working.csv").apply { writeText("old") }
        try {
            val expected = SavedSnapshot.inputSha256(input)
            input.writeText("new")
            try {
                SavedSnapshot.save(base, 401L, result(), null, null, null, input, expected)
                fail("changed calculation input must be rejected")
            } catch (expectedFailure: IllegalStateException) {
                assertTrue(expectedFailure.message!!.contains("바뀌었어요"))
            }
            assertFalse(SavedSnapshot.exists(base, 401L))
        } finally { base.deleteRecursively() }
    }

    fun testResultStoreUnionSchemaKeepsChangedResultsOnSameDay() {
        val base = tempBase()
        val file = File(base, "saved.csv")
        try {
            val first = savedResult(501L, "22:30 전에 취침")
            val changed = savedResult(502L, "23:00 전에 취침")
            ResultStore.save(file, first)
            val (items, replaced) = ResultStore.saveReplacing(file, changed)
            assertEquals(2, items.size)
            assertTrue(replaced.isEmpty())
            val (deduplicated, ids) = ResultStore.saveReplacing(file, changed.copy(savedAtMs = 503L))
            assertEquals(2, deduplicated.size)
            assertEquals(listOf(502L), ids)
            val restored = ResultStore.load(file).first { it.savedAtMs == 503L }
            assertEquals("2026-11-03", restored.dayDate)
            assertTrue(restored.isPast)
            assertEquals("실제 23:15 취침", restored.actualText)
            assertEquals("희망 수면 8시간", restored.profileText)
            assertEquals("Asia/Seoul", restored.sourceZoneId)

            val live = changed.copy(savedAtMs = 504L, predictionOrigin = PredictionOrigin.LIVE)
            val reanalysis = changed.copy(
                savedAtMs = 505L,
                predictionOrigin = PredictionOrigin.CSV_REANALYSIS,
                historyCutoffMs = epoch("2026-11-03T11:00:00Z")
            )
            ResultStore.saveReplacing(file, live)
            val (withBothOrigins, originReplacedIds) = ResultStore.saveReplacing(file, reanalysis)
            assertTrue(originReplacedIds.isEmpty())
            assertTrue(withBothOrigins.any { it.savedAtMs == 504L && it.predictionOrigin == PredictionOrigin.LIVE })
            assertTrue(withBothOrigins.any { it.savedAtMs == 505L && it.predictionOrigin == PredictionOrigin.CSV_REANALYSIS })
        } finally { base.deleteRecursively() }
    }

    private fun tempBase(): File = File(instrumentation.targetContext.cacheDir, "snapshot-${System.nanoTime()}").apply { mkdirs() }

    private fun result(): AnalysisResult {
        val bed = epoch("2026-11-02T14:30:00Z")
        val wake = epoch("2026-11-02T22:30:00Z")
        val calculated = epoch("2026-11-03T11:00:00Z")
        return AnalysisResult(
            currentS = 0.62, currentC = -0.08, currentPropensity = 0.54,
            sleepRecords = listOf(SleepRecord("watch-1", bed, wake, 455.0, mapOf("LIGHT" to 250.0, "UNKNOWN" to 25.0))),
            scHistory = listOf(PropensityPoint(wake, 0.2, 0.1, 0.3), PropensityPoint(calculated, 0.62, -0.08, 0.54)),
            futureS = listOf(PredictedPoint(calculated + 3_600_000L, 0.7, -0.1, 0.6)),
            recommendation = Recommendation(
                recommendedBedTimeMs = epoch("2026-11-03T13:30:00Z"), recommendedWakeTimeMs = epoch("2026-11-03T22:00:00Z"),
                recommendedSleepMin = 510.0, physicalBedTimeMs = epoch("2026-11-03T14:00:00Z"),
                physicalWakeTimeMs = epoch("2026-11-03T22:00:00Z"), predictedBedTimeMs = epoch("2026-11-03T14:15:00Z"),
                predictedWakeTimeMs = epoch("2026-11-03T22:15:00Z"), bedtimeResidualMin = 15.0, wakeResidualMin = 15.0,
                residualModelVersion = "personal-v5", modelStatus = "personalized", usedFallback = false,
                explanation = "saved", planFeasible = true, generatedAtMs = calculated,
                personalizationStatus = "measurement_complete", personalizationWeight = 0.75,
                evidenceStatus = "measured_complete", runtimePolicy = "gradual_v1",
                predictionOrigin = PredictionOrigin.CSV_REANALYSIS,
                historyCutoffMs = calculated, historyRecordCount = 8, csvHistoryRecordCount = 6
            ),
            modelVersion = "frozen-test", calculationTimeMs = calculated,
            inputPeriodStartMs = bed - 86_400_000L, inputPeriodEndMs = calculated,
            recordDays = 8, personalizationStage = "stage1", predictionLimitations = listOf("sample_limit")
        )
    }

    private fun savedResult(savedAt: Long, rec: String) = SavedResult(
        savedAtMs = savedAt, source = "health-connect", basisMs = epoch("2026-11-03T00:00:00Z"),
        basisText = "11/3", modelText = "model", recText = rec, habitText = "habit", statusText = "status",
        surveyText = "fatigue=3/5", sValue = "0.6", cValue = "-0.1", pValue = "0.5",
        sourceZoneId = "Asia/Seoul", recommendedBedMs = epoch("2026-11-03T13:30:00Z"),
        recommendedWakeMs = epoch("2026-11-03T22:00:00Z"), dayDate = "2026-11-03", isPast = true,
        actualText = "실제 23:15 취침", profileText = "희망 수면 8시간"
    )

    private fun epoch(value: String): Long = Instant.parse(value).toEpochMilli()
}

package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.DirectInput
import app.sleepwell.health.HealthResult
import app.sleepwell.health.PredictionOrigin
import app.sleepwell.user.SleepInputPolicy
import app.sleepwell.user.SleepRecord
import app.sleepwell.user.UserModelEngine
import app.sleepwell.user.UserProfile
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.io.File
import java.time.Duration
import java.time.ZonedDateTime

/** Locks the explicit retrospective CSV policy without changing stored receipt provenance. */
@Suppress("DEPRECATION")
class HistoricalCsvReanalysisRegressionTest : InstrumentationTestCase() {
    private val zone = ZoneId.of("Asia/Seoul")

    fun testProjectionIsCsvOnlyStrictlyBeforeCutoffAndNeverMutatesStoredRecord() {
        val cutoff = instant(10, 20)
        val stored = record(10, 0, 7, source = "samsung_health_csv")
        val originalReceipt = stored.modifiedAt

        val projected = HistoricalCsvReanalysis.prepare(stored, cutoff)

        assertNotNull(projected)
        assertEquals(stored.wakeTime, projected!!.modifiedAt)
        assertEquals(originalReceipt, stored.modifiedAt)
        assertNull(HistoricalCsvReanalysis.prepare(stored.copy(source = "health_connect"), cutoff))
        assertNull(HistoricalCsvReanalysis.prepare(recordAt(cutoff, cutoff.minusSeconds(8 * 3_600), "at-cutoff"), cutoff))
        assertNull(HistoricalCsvReanalysis.prepare(recordAt(cutoff.minusSeconds(179 * 60), cutoff.minusSeconds(358 * 60), "short"), cutoff))

        val acrossMidnight = recordAt(instant(10, 0), instant(9, 16), "midnight-no-stages")
        assertNotNull(HistoricalCsvReanalysis.prepare(acrossMidnight, cutoff))
    }

    fun testSelectedDateAccumulatesOnlyPriorCsvIntoData33AndPredictionMetadata() = withModelState {
        val imports = records(1..12)
        val earlyCutoff = instant(4, 20)
        val laterCutoff = instant(8, 20)

        val early = reanalyze(imports, earlyCutoff)
        val later = reanalyze(imports, laterCutoff)
        val earlyFeatures = data33(imports, earlyCutoff)
        val laterFeatures = data33(imports, laterCutoff)

        assertEquals(PredictionOrigin.CSV_REANALYSIS, early.recommendation!!.predictionOrigin)
        assertEquals(4, early.recommendation!!.historyRecordCount)
        assertEquals(4, early.recommendation!!.csvHistoryRecordCount)
        assertEquals(4.0, earlyFeatures.getDouble("history_count_7"), 0.0)
        assertEquals(8, later.recommendation!!.historyRecordCount)
        assertEquals(8, later.recommendation!!.csvHistoryRecordCount)
        assertEquals(7.0, laterFeatures.getDouble("history_count_7"), 0.0)
        assertEquals(laterCutoff.toEpochMilli(), later.recommendation!!.historyCutoffMs)
    }

    fun testRecordsAfterSelectedDateCannotChangePastData33OrPrediction() = withModelState {
        val cutoff = instant(8, 20)
        val baseline = records(1..12)
        val changedFuture = baseline.map { row ->
            if (LocalDate.parse(row.day).dayOfMonth <= 8) row
            else shift(row, bedMinutes = 300, wakeMinutes = 240)
        }

        val a = reanalyze(baseline, cutoff).recommendation!!
        val b = reanalyze(changedFuture, cutoff).recommendation!!
        val af = data33(baseline, cutoff)
        val bf = data33(changedFuture, cutoff)

        assertEquals(a.predictedBedTimeMs, b.predictedBedTimeMs)
        assertEquals(a.predictedWakeTimeMs, b.predictedWakeTimeMs)
        assertEquals(af.toString(), bf.toString())
        assertEquals(8, a.csvHistoryRecordCount)
        assertEquals(8, b.csvHistoryRecordCount)
    }

    fun testChangingEligibleCsvChangesPastData33AndPrediction() = withModelState {
        val cutoff = instant(8, 20)
        val baseline = records(1..12)
        val changedHistory = baseline.map { row ->
            if (LocalDate.parse(row.day).dayOfMonth <= 8) shift(row, bedMinutes = 180, wakeMinutes = 120) else row
        }

        val a = reanalyze(baseline, cutoff).recommendation!!
        val b = reanalyze(changedHistory, cutoff).recommendation!!
        val af = data33(baseline, cutoff)
        val bf = data33(changedHistory, cutoff)

        assertFalse(af.getDouble("recent_bed") == bf.getDouble("recent_bed"))
        assertFalse(af.getDouble("recent_wake") == bf.getDouble("recent_wake"))
        assertTrue(a.predictedBedTimeMs != b.predictedBedTimeMs || a.predictedWakeTimeMs != b.predictedWakeTimeMs)
    }

    fun testExplicitInputsDoNotRewriteGlobalImportsOrReceiptProvenance() = withModelState {
        val global = listOf(record(2, 23, 7, source = "health_connect", observedAt = instant(2, 9)))
        val bulk = records(1..8)
        val before = bulk.map { it.modifiedAt }
        NextNightModel.importedRecords = global

        reanalyze(bulk, instant(8, 20))

        assertEquals(global, NextNightModel.importedRecords)
        assertEquals(before, bulk.map { it.modifiedAt })
    }

    fun testNormalHealthConnectAndLivePathsKeepReceiptTimePolicy() = withModelState {
        val boundary = instant(8, 20)
        val dailyHealthConnect = record(7, 23, 7, source = "health_connect", observedAt = instant(8, 9))
        val lateHealthConnect = record(7, 23, 7, source = "health_connect", observedAt = instant(20, 12))
        val lateCsv = record(7, 23, 7, source = "samsung_health_csv", observedAt = instant(20, 12))

        assertEquals(
            listOf(dailyHealthConnect),
            NextNightModel.nightlyRecords(
                emptyList(), observedAt = boundary, includeImportedRecords = true,
                imports = listOf(dailyHealthConnect, lateHealthConnect, lateCsv)
            )
        )
        assertEquals(
            3,
            NextNightModel.nightlyRecords(
                emptyList(), observedAt = instant(21, 20), includeImportedRecords = true,
                imports = listOf(
                    dailyHealthConnect,
                    record(8, 23, 7, source = "health_connect", observedAt = instant(20, 12)),
                    record(9, 23, 7, source = "samsung_health_csv", observedAt = instant(20, 12))
                )
            ).size
        )

        NextNightModel.importedRecords = listOf(dailyHealthConnect)
        val live = AnalysisRunner.runDiary(null, emptyMap(), boundary.toEpochMilli())
        assertEquals(PredictionOrigin.LIVE, live.recommendation!!.predictionOrigin)
    }

    fun testCsvReanalysisCannotBeEnabledOnLiveCalculation() = withModelState {
        try {
            AnalysisRunner.runDiary(
                null, emptyMap(), instant(8, 20).toEpochMilli(),
                historicalReconstruction = false,
                historicalCsvReanalysis = true,
                historicalImports = records(1..8)
            )
            fail("실시간 계산에서 CSV 과거 재분석을 허용하면 안 됩니다.")
        } catch (expected: IllegalArgumentException) {
            assertTrue(expected.message.orEmpty().contains("과거 재구성"))
        }
    }

    fun testFutureQuestionnaireIsExcludedWhileKnownRevisionIsAttached() {
        val record = record(8, 23, 7)
        val cutoff = instant(8, 20)
        val known = survey(savedAt = instant(8, 10).toString(), rested = 5, latency = 20)
        val future = survey(savedAt = instant(9, 10).toString(), rested = 1, latency = 80)

        val joined = ImportedSleepStore.withQuestionnairesBefore(listOf(record), listOf(known, future), cutoff).single()

        assertEquals(5, joined.rested)
        assertEquals(20, joined.sleepLatencyMinutes)
        assertEquals(known.savedAt, joined.subjectiveObservedAt)
        assertEquals(record.modifiedAt, joined.modifiedAt)
    }

    fun testSavedBulkCsvRunsEndToEndWithoutRewritingStoredFiles() = withModelState {
        val root = File(
            instrumentation.targetContext.cacheDir,
            "csv-reanalysis-e2e-${System.nanoTime()}"
        ).also { assertTrue(it.mkdirs()) }
        try {
            val importAt = instant(20, 12)
            val cutoff = instant(8, 20)
            val sessions = records(1..12).map { row ->
                val bed = Instant.parse(row.bedtime).toEpochMilli()
                val wake = Instant.parse(row.wakeTime).toEpochMilli()
                HcSleepSession(
                    id = row.id, startMs = bed, endMs = wake, offsetSeconds = 9 * 3_600,
                    stages = listOf(
                        HcSleepStage(bed, bed + 15 * 60_000L, HealthConnectCsv.AWAKE),
                        HcSleepStage(bed + 15 * 60_000L, wake - 20 * 60_000L, HealthConnectCsv.LIGHT),
                        HcSleepStage(wake - 20 * 60_000L, wake, HealthConnectCsv.AWAKE)
                    )
                )
            }
            val source = File(root, "bulk.csv").apply {
                writeText(HealthConnectCsv.toCsv(sessions), Charsets.UTF_8)
            }
            val dataDir = File(root, "sleep-data")
            SleepDataStore.saveArchive(dataDir, source, importAt.toEpochMilli())
            val (merged, info) = SleepDataStore.buildMerged(dataDir)!!
            assertEquals(12, info.total)
            val imports = ImportedSleepStore.loadReanalysisCandidates(dataDir, importAt.toEpochMilli())
            assertEquals(12, imports.size)
            assertTrue(imports.all { it.modifiedAt == importAt.toString() })
            NextNightModel.importedRecords = imports

            val protectedFiles = listOf(
                source,
                File(dataDir, SleepDataStore.ARCHIVE),
                File(dataDir, ImportedSleepStore.FILE_NAME),
                merged
            )
            val before = protectedFiles.associateWith { it.readBytes() }
            val reference = sessions[7].endMs
            val output = AnalysisRunner.runFileAt(
                merged, null, reference,
                recommendationTimeMs = cutoff.toEpochMilli(),
                historicalReconstruction = true,
                historicalCsvReanalysis = true,
                historicalImports = imports
            )

            assertTrue(output is HealthResult.Success)
            val result = (output as HealthResult.Success).data
            assertTrue(result.sleepRecords.isNotEmpty())
            assertEquals(PredictionOrigin.CSV_REANALYSIS, result.recommendation!!.predictionOrigin)
            assertEquals(8, result.recommendation!!.csvHistoryRecordCount)
            assertEquals(8, result.recommendation!!.historyRecordCount)
            assertEquals(imports, NextNightModel.importedRecords)
            protectedFiles.forEach { file ->
                assertTrue("재분석이 ${file.name} 내용을 바꾸면 안 됩니다.", before.getValue(file).contentEquals(file.readBytes()))
            }
        } finally {
            root.deleteRecursively()
        }
    }

    fun testMixedCsvAndHealthConnectKeepsCsvForPastReanalysisWithoutChangingLivePolicy() = withModelState {
        val root = File(
            instrumentation.targetContext.cacheDir,
            "csv-reanalysis-mixed-${System.nanoTime()}"
        ).also { assertTrue(it.mkdirs()) }
        try {
            val bed = instant(7, 23).toEpochMilli()
            val wake = instant(8, 7).toEpochMilli()
            val session = HcSleepSession(
                id = "same-sleep", startMs = bed, endMs = wake, offsetSeconds = 9 * 3_600,
                stages = listOf(HcSleepStage(bed, wake, HealthConnectCsv.LIGHT))
            )
            val archiveInput = File(root, "archive.csv").apply {
                writeText(HealthConnectCsv.toCsv(listOf(session)), Charsets.UTF_8)
            }
            val dataDir = File(root, "sleep-data")
            val csvReceipt = instant(20, 12)
            val healthConnectReceipt = instant(20, 13)
            SleepDataStore.saveArchive(dataDir, archiveInput, csvReceipt.toEpochMilli())
            SleepDataStore.saveHealthConnect(
                dataDir, HealthConnectCsv.toCsv(listOf(session)),
                observedAtMs = healthConnectReceipt.toEpochMilli()
            )

            val candidates = ImportedSleepStore.loadReanalysisCandidates(
                dataDir, healthConnectReceipt.toEpochMilli()
            )
            assertEquals(2, candidates.size)
            assertEquals(
                setOf("samsung_health_csv", "health_connect"),
                candidates.map { it.source }.toSet()
            )
            val cutoff = instant(8, 20)
            val historical = NextNightModel.nightlyRecords(
                emptyList(), observedAt = cutoff, includeImportedRecords = true,
                historicalCsvReanalysis = true, imports = candidates
            )
            assertEquals(1, historical.size)
            assertEquals("samsung_health_csv", historical.single().source)
            assertEquals(Instant.ofEpochMilli(wake).toString(), historical.single().modifiedAt)

            val (merged, info) = SleepDataStore.buildMerged(dataDir)!!
            assertEquals(1, info.archiveSessions)
            assertEquals(0, info.healthConnectAdded)
            val output = AnalysisRunner.runFileAt(
                merged, null, wake,
                recommendationTimeMs = cutoff.toEpochMilli(),
                historicalReconstruction = true,
                historicalCsvReanalysis = true,
                historicalImports = candidates
            )
            assertTrue(output is HealthResult.Success)
            val result = (output as HealthResult.Success).data
            assertTrue(result.sleepRecords.isNotEmpty())
            assertEquals(PredictionOrigin.CSV_REANALYSIS, result.recommendation!!.predictionOrigin)
            assertEquals(1, result.recommendation!!.csvHistoryRecordCount)
            assertEquals(1, result.recommendation!!.historyRecordCount)

            // Live behavior still collapses a wake date and respects the later provider receipt.
            val liveCollapsed = ImportedSleepStore.loadModelNights(
                dataDir, healthConnectReceipt.toEpochMilli()
            )
            assertEquals(1, liveCollapsed.size)
            assertEquals("health_connect", liveCollapsed.single().source)
            assertEquals(healthConnectReceipt.toString(), liveCollapsed.single().modifiedAt)
            val live = NextNightModel.nightlyRecords(
                emptyList(), observedAt = healthConnectReceipt,
                includeImportedRecords = true, imports = liveCollapsed
            )
            assertEquals(liveCollapsed, live)
        } finally {
            root.deleteRecursively()
        }
    }

    fun testUtcDisplayedDateBoundaryExcludesLaterCsvHealthConnectDiaryAndSurvey() = withModelState {
        val utc = ZoneId.of("UTC")
        val reference = Instant.parse("2026-11-05T22:00:00Z") // 11/5 UTC, but 11/6 07:00 KST.
        val modelCutoff = Instant.parse("2026-11-06T11:00:00Z") // 11/6 20:00 KST.
        val dataCutoff = HistoricalCsvReanalysis.dataCutoffMs(
            reference.toEpochMilli(), utc, modelCutoff.toEpochMilli()
        )
        assertEquals(Instant.parse("2026-11-06T00:00:00Z").toEpochMilli(), dataCutoff)

        val base = recordIso(
            "base", "2026-11-05T12:00:00Z", "2026-11-05T15:00:00Z",
            "samsung_health_csv", "2026-11-10T00:00:00Z"
        )
        val laterCsv = recordIso(
            "later-csv", "2026-11-05T21:00:00Z", "2026-11-06T05:00:00Z",
            "samsung_health_csv", "2026-11-10T00:00:00Z"
        )
        val laterHealthConnect = recordIso(
            "later-hc", "2026-11-05T21:30:00Z", "2026-11-06T05:30:00Z",
            "health_connect", "2026-11-06T06:00:00Z"
        )
        val exactlyAtDisplayedDayEnd = recordIso(
            "at-data-cutoff", "2026-11-05T16:00:00Z", "2026-11-06T00:00:00Z",
            "health_connect", "2026-11-06T00:00:00Z"
        )
        val laterDiary = recordIso(
            "later-diary", "2026-11-05T22:00:00Z", "2026-11-06T06:00:00Z",
            "diary", "2026-11-06T06:30:00Z"
        )
        val diaryExactlyAtDisplayedDayEnd = recordIso(
            "diary-at-data-cutoff", "2026-11-05T16:00:00Z", "2026-11-06T00:00:00Z",
            "diary", "2026-11-06T00:00:00Z"
        )

        NextNightModel.diaryRecords = emptyList()
        val baseline = reanalyze(listOf(base), modelCutoff, Instant.ofEpochMilli(dataCutoff)).recommendation!!
        val baselineFeatures = data33FromPolicy(listOf(base), Instant.ofEpochMilli(dataCutoff), modelCutoff)
        listOf(laterCsv, laterHealthConnect, exactlyAtDisplayedDayEnd).forEach { later ->
            val result = reanalyze(listOf(base, later), modelCutoff, Instant.ofEpochMilli(dataCutoff)).recommendation!!
            val features = data33FromPolicy(listOf(base, later), Instant.ofEpochMilli(dataCutoff), modelCutoff)
            assertSamePredictionAndHistory(baseline, result)
            assertEquals(baselineFeatures.toString(), features.toString())
        }
        NextNightModel.diaryRecords = listOf(laterDiary)
        val withDiary = reanalyze(listOf(base), modelCutoff, Instant.ofEpochMilli(dataCutoff)).recommendation!!
        val diaryFeatures = data33FromPolicy(listOf(base), Instant.ofEpochMilli(dataCutoff), modelCutoff)
        assertSamePredictionAndHistory(baseline, withDiary)
        assertEquals(baselineFeatures.toString(), diaryFeatures.toString())

        // Both rows map to the same KST wake date. Strict filtering must happen before diary
        // precedence so an ineligible boundary diary cannot replace and then erase the valid CSV.
        NextNightModel.diaryRecords = listOf(diaryExactlyAtDisplayedDayEnd)
        val withBoundaryDiary = reanalyze(
            listOf(base), modelCutoff, Instant.ofEpochMilli(dataCutoff)
        ).recommendation!!
        val boundaryDiaryFeatures = data33FromPolicy(
            listOf(base), Instant.ofEpochMilli(dataCutoff), modelCutoff
        )
        assertSamePredictionAndHistory(baseline, withBoundaryDiary)
        assertEquals(baselineFeatures.toString(), boundaryDiaryFeatures.toString())

        // An unsaved direct fallback is observed exactly at dataCutoff. Even though its wake is
        // earlier and it is longer than the CSV on the same KST wake date, it was not knowable yet.
        NextNightModel.diaryRecords = emptyList()
        val boundaryDirect = DirectInput(
            sleepStartMs = Instant.parse("2026-11-05T12:00:00Z").toEpochMilli(),
            sleepEndMs = Instant.parse("2026-11-05T20:00:00Z").toEpochMilli(),
            targetWakeMs = null,
            plannedBedMs = null
        )
        val withBoundaryDirect = reanalyze(
            listOf(base), modelCutoff, Instant.ofEpochMilli(dataCutoff), boundaryDirect
        ).recommendation!!
        val boundaryDirectFeatures = data33FromPolicy(
            listOf(base), Instant.ofEpochMilli(dataCutoff), modelCutoff, boundaryDirect
        )
        assertSamePredictionAndHistory(baseline, withBoundaryDirect)
        assertEquals(baselineFeatures.toString(), boundaryDirectFeatures.toString())

        val known = survey(
            savedAt = "2026-11-05T23:30:00Z", rested = 5, latency = 20,
            date = "2026-11-06", wakeDate = "2026-11-06"
        )
        val afterDisplayedDate = survey(
            savedAt = "2026-11-06T05:00:00Z", rested = 1, latency = 80,
            date = "2026-11-06", wakeDate = "2026-11-06"
        )
        val joined = ImportedSleepStore.withQuestionnairesBefore(
            listOf(base), listOf(known, afterDisplayedDate), Instant.ofEpochMilli(dataCutoff)
        ).single()
        assertEquals(5, joined.rested)
        assertEquals(20, joined.sleepLatencyMinutes)
        assertEquals(known.savedAt, joined.subjectiveObservedAt)
    }

    fun testDataCutoffKeepsKstModelCutoffAndUsesNewYorkDstDayEnd() {
        val kst = ZoneId.of("Asia/Seoul")
        val kstReference = ZonedDateTime.of(2026, 11, 5, 7, 0, 0, 0, kst).toInstant()
        val kstModelCutoff = ZonedDateTime.of(2026, 11, 5, 20, 0, 0, 0, kst).toInstant()
        assertEquals(
            kstModelCutoff.toEpochMilli(),
            HistoricalCsvReanalysis.dataCutoffMs(
                kstReference.toEpochMilli(), kst, kstModelCutoff.toEpochMilli()
            )
        )

        val newYork = ZoneId.of("America/New_York")
        val selectedStart = LocalDate.of(2026, 11, 1).atStartOfDay(newYork)
        val selectedReference = selectedStart.withHour(12).toInstant()
        val nextDateStart = LocalDate.of(2026, 11, 2).atStartOfDay(newYork)
        val laterModelCutoff = nextDateStart.plusDays(1).toInstant()
        assertEquals(25L, Duration.between(selectedStart, nextDateStart).toHours())
        assertEquals("-05:00", nextDateStart.offset.toString())
        assertEquals(
            nextDateStart.toInstant().toEpochMilli(),
            HistoricalCsvReanalysis.dataCutoffMs(
                selectedReference.toEpochMilli(), newYork, laterModelCutoff.toEpochMilli()
            )
        )
    }

    private fun reanalyze(imports: List<SleepRecord>, cutoff: Instant) =
        AnalysisRunner.runDiary(
            null, emptyMap(), cutoff.toEpochMilli(), historicalReconstruction = true,
            historicalCsvReanalysis = true, historicalImports = imports
        )

    private fun reanalyze(
        imports: List<SleepRecord>,
        modelCutoff: Instant,
        dataCutoff: Instant,
        direct: DirectInput? = null
    ) =
        AnalysisRunner.runDiary(
            direct, emptyMap(), modelCutoff.toEpochMilli(), historicalReconstruction = true,
            historicalCsvReanalysis = true, historicalImports = imports,
            historicalDataCutoffMs = dataCutoff.toEpochMilli()
        )

    private fun data33(imports: List<SleepRecord>, cutoff: Instant) = UserModelEngine({ name ->
        instrumentation.targetContext.assets.open(name).use { it.readBytes() }
    }).compute(
        UserProfile(
            ageBand = "30-44", sex = "any", activityMinutes = 60,
            usualBed = "23:30", usualWake = "07:30", desiredSleepMinutes = 480,
            createdAt = "2026-09-01T00:00:00Z"
        ),
        imports.mapNotNull { HistoricalCsvReanalysis.prepare(it, cutoff) }, cutoff,
        historicalEvaluation = true, inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
    ).outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")

    private fun data33FromPolicy(
        imports: List<SleepRecord>,
        dataCutoff: Instant,
        modelCutoff: Instant,
        direct: DirectInput? = null
    ): org.json.JSONObject {
        val nights = NextNightModel.nightlyRecords(
            emptyList(), direct = direct, observedAt = dataCutoff, includeImportedRecords = true,
            historicalCsvReanalysis = true, imports = imports
        )
        return UserModelEngine({ name ->
            instrumentation.targetContext.assets.open(name).use { it.readBytes() }
        }).compute(
            UserProfile(
                ageBand = "30-44", sex = "any", activityMinutes = 60,
                usualBed = "23:30", usualWake = "07:30", desiredSleepMinutes = 480,
                createdAt = "2026-09-01T00:00:00Z"
            ),
            nights, modelCutoff, historicalEvaluation = true,
            inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        ).outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")
    }

    private fun assertSamePredictionAndHistory(
        expected: app.sleepwell.health.Recommendation,
        actual: app.sleepwell.health.Recommendation
    ) {
        assertEquals(expected.predictedBedTimeMs, actual.predictedBedTimeMs)
        assertEquals(expected.predictedWakeTimeMs, actual.predictedWakeTimeMs)
        assertEquals(expected.historyRecordCount, actual.historyRecordCount)
        assertEquals(expected.csvHistoryRecordCount, actual.csvHistoryRecordCount)
    }

    private fun records(days: IntRange): List<SleepRecord> = days.map { day ->
        val wakeHour = when {
            day <= 4 -> 6
            day <= 8 -> 8
            else -> 10
        }
        val duration = when {
            day <= 4 -> 450
            day <= 8 -> 510
            else -> 420
        }
        val wake = instant(day, wakeHour)
        recordAt(wake, wake.minusSeconds(duration * 60L), "csv-$day")
    }

    private fun record(
        wakeDay: Int,
        bedHour: Int,
        wakeHour: Int,
        source: String = "samsung_health_csv",
        observedAt: Instant = instant(20, 12)
    ): SleepRecord {
        val wake = instant(wakeDay, wakeHour)
        val bedDay = if (bedHour > wakeHour) wakeDay - 1 else wakeDay
        return recordAt(wake, instant(bedDay, bedHour), "$source-$wakeDay", source, observedAt)
    }

    private fun recordAt(
        wake: Instant,
        bed: Instant,
        id: String,
        source: String = "samsung_health_csv",
        observedAt: Instant = instant(20, 12)
    ) = SleepRecord(
        id = id, day = wake.atZone(zone).toLocalDate().toString(),
        bedtime = bed.toString(), wakeTime = wake.toString(),
        durationMinutes = java.time.Duration.between(bed, wake).toMinutes().toInt(),
        rested = null, fatigue = null, source = source, modifiedAt = observedAt.toString()
    )

    private fun recordIso(
        id: String,
        bed: String,
        wake: String,
        source: String,
        observedAt: String
    ): SleepRecord {
        val bedInstant = Instant.parse(bed)
        val wakeInstant = Instant.parse(wake)
        return SleepRecord(
            id = id, day = wakeInstant.atZone(zone).toLocalDate().toString(),
            bedtime = bed, wakeTime = wake,
            durationMinutes = Duration.between(bedInstant, wakeInstant).toMinutes().toInt(),
            rested = null, fatigue = null, source = source, modifiedAt = observedAt
        )
    }

    private fun shift(record: SleepRecord, bedMinutes: Long, wakeMinutes: Long): SleepRecord {
        val bed = Instant.parse(record.bedtime).plusSeconds(bedMinutes * 60)
        val wake = Instant.parse(record.wakeTime).plusSeconds(wakeMinutes * 60)
        return record.copy(
            day = wake.atZone(zone).toLocalDate().toString(), bedtime = bed.toString(), wakeTime = wake.toString(),
            durationMinutes = java.time.Duration.between(bed, wake).toMinutes().toInt()
        )
    }

    private fun survey(
        savedAt: String,
        rested: Int,
        latency: Int,
        date: String = "2026-10-08",
        wakeDate: String = date
    ) = SurveyDay(
        participant = "me", date = date, caffeineUsed = null, caffeineMg = null,
        lastCaffeine = null, napMinutes = null, napEnd = null, exerciseMinutes = null,
        exerciseEnd = null, exerciseIntensity = null, fatigue = null, steps = null,
        heartRate = null, alarmTime = null, firstCommitment = null, examTomorrow = false,
        alcohol = false, illness = false, savedAt = savedAt, rested = rested,
        sleepWakeDate = wakeDate, sleepLatencyMinutes = latency
    )

    private fun instant(day: Int, hour: Int, minute: Int = 0): Instant =
        LocalDate.of(2026, 10, day).atTime(hour, minute).atZone(zone).toInstant()

    private inline fun withModelState(block: () -> Unit) {
        val profile = NextNightModel.profile
        val imported = NextNightModel.importedRecords
        val diaries = NextNightModel.diaryRecords
        val surveys = NextNightModel.surveyDays
        val wearable = NextNightModel.wearablePersonalizationEnabled
        val demo = NextNightModel.demoMode
        try {
            NextNightModel.install { name -> instrumentation.targetContext.assets.open(name).use { it.readBytes() } }
            NextNightModel.profile = NextNightModel.ModelProfile(ageBand = "30-44", onboardingCompleted = true)
            NextNightModel.importedRecords = emptyList()
            NextNightModel.diaryRecords = emptyList()
            NextNightModel.surveyDays = emptyList()
            NextNightModel.wearablePersonalizationEnabled = true
            NextNightModel.demoMode = false
            block()
        } finally {
            NextNightModel.profile = profile
            NextNightModel.importedRecords = imported
            NextNightModel.diaryRecords = diaries
            NextNightModel.surveyDays = surveys
            NextNightModel.wearablePersonalizationEnabled = wearable
            NextNightModel.demoMode = demo
        }
    }
}

package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import java.io.File
import java.time.Duration
import java.time.ZoneId
import java.time.ZonedDateTime
import java.util.concurrent.Callable
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

@Suppress("DEPRECATION")
class BackgroundStoreConcurrencyRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun testConcurrentForegroundAndBackgroundImportsRetainEveryDistinctNight() {
        val dir = isolatedDir("distinct-imports")
        try {
            val payloads = (1..12).map { day -> csvFor(day, "concurrent-$day") }
            runConcurrentImports(dir, payloads)

            val merged = SleepDataStore.buildMerged(dir)!!.second
            val normalized = ImportedSleepStore.loadModelNights(dir, atSeoul(2026, 11, 20, 12).toEpochMilli())
            assertEquals(payloads.size, merged.healthConnectSessions)
            assertEquals(payloads.size, merged.total)
            assertEquals(payloads.size, normalized.size)
            assertEquals(payloads.size, normalized.map { it.id }.distinct().size)
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testConcurrentModelLoadsNeverObservePartialNormalizedWrite() {
        val dir = isolatedDir("read-during-import")
        try {
            runConcurrentImportsAndLoads(dir, (1..12).map { day -> csvFor(day, "read-$day") })
            val normalized = ImportedSleepStore.loadModelNights(dir, atSeoul(2026, 11, 20, 12).toEpochMilli())
            assertEquals(12, normalized.size)
        } finally {
            dir.deleteRecursively()
        }
    }

    private fun runConcurrentImports(dir: File, payloads: List<String>) {
        val start = CountDownLatch(1)
        val pool = Executors.newFixedThreadPool(4)
        try {
            val futures = payloads.mapIndexed { index, csv ->
                pool.submit(Callable {
                    start.await()
                    SleepDataStore.saveHealthConnect(dir, csv, observedAtMs = atSeoul(2026, 11, 20, 8).toEpochMilli() + index)
                })
            }
            start.countDown()
            futures.forEach { it.get(15, TimeUnit.SECONDS) }
        } finally {
            pool.shutdownNow()
        }
    }

    private fun runConcurrentImportsAndLoads(dir: File, payloads: List<String>) {
        val start = CountDownLatch(1)
        val pool = Executors.newFixedThreadPool(5)
        try {
            val writers = payloads.mapIndexed { index, csv ->
                pool.submit(Callable {
                    start.await()
                    SleepDataStore.saveHealthConnect(dir, csv, observedAtMs = atSeoul(2026, 11, 20, 8).toEpochMilli() + index)
                })
            }
            val readers = (1..20).map {
                pool.submit(Callable {
                    start.await()
                    ImportedSleepStore.loadModelNights(dir, atSeoul(2026, 11, 20, 12).toEpochMilli())
                })
            }
            start.countDown()
            (writers + readers).forEach { it.get(15, TimeUnit.SECONDS) }
        } finally {
            pool.shutdownNow()
        }
    }

    private fun csvFor(day: Int, id: String): String {
        val start = atSeoul(2026, 11, day, 23)
        val end = start.plus(Duration.ofHours(8))
        return HealthConnectCsv.toCsv(listOf(HcSleepSession(
            id = id,
            startMs = start.toEpochMilli(),
            endMs = end.toEpochMilli(),
            offsetSeconds = 9 * 3_600,
            stages = emptyList()
        )))
    }

    private fun atSeoul(year: Int, month: Int, day: Int, hour: Int) =
        ZonedDateTime.of(year, month, day, hour, 0, 0, 0, seoul).toInstant()

    private fun isolatedDir(label: String): File =
        File(instrumentation.targetContext.cacheDir, "background-store-$label-${System.nanoTime()}")
            .also { assertTrue(it.mkdirs()) }
}

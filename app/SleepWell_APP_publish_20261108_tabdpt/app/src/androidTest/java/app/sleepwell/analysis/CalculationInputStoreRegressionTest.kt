package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import java.io.File
import java.io.IOException
import java.io.InputStream

@Suppress("DEPRECATION")
class CalculationInputStoreRegressionTest : InstrumentationTestCase() {
    fun testLegacyCacheMigratesOnlyWhenPersistentInputIsAbsent() {
        val root = tempDir("migrate")
        val legacy = File(root, "cache/user_sleep_stage.csv").apply {
            parentFile!!.mkdirs(); writeText("legacy")
        }
        try {
            val first = CalculationInputStore.prepare(root, legacy)!!
            assertEquals("legacy", first.readText())
            legacy.writeText("changed cache")
            val second = CalculationInputStore.prepare(root, legacy)!!
            assertEquals(first.canonicalPath, second.canonicalPath)
            assertEquals("legacy", second.readText())
        } finally { root.deleteRecursively() }
    }

    fun testNoInputDoesNotCreateSyntheticWorkingFile() {
        val root = tempDir("empty")
        try {
            assertNull(CalculationInputStore.prepare(root, File(root, "missing.csv")))
            assertFalse(CalculationInputStore.workingFile(root).exists())
        } finally { root.deleteRecursively() }
    }

    fun testFailedReplacementPreservesExistingWorkingFile() {
        val root = tempDir("atomic")
        try {
            CalculationInputStore.replaceFrom("original".byteInputStream(), root)
            val failing = object : InputStream() {
                private var count = 0
                override fun read(): Int {
                    if (count++ < 3) return 'x'.code
                    throw IOException("planned failure")
                }
            }
            try {
                CalculationInputStore.replaceFrom(failing, root)
                fail("expected failure")
            } catch (_: IOException) { }
            assertEquals("original", CalculationInputStore.workingFile(root).readText())
            assertTrue(CalculationInputStore.workingFile(root).parentFile!!.listFiles()!!.none { it.name.contains(".tmp-") })
        } finally { root.deleteRecursively() }
    }

    fun testNonAtomicMoveFailurePropagatesWithoutReplacingTarget() {
        val root = tempDir("move-failure")
        val target = CalculationInputStore.workingFile(root)
        try {
            assertTrue(target.mkdirs())
            val marker = File(target, "preserved.txt").apply { writeText("preserved") }
            try {
                CalculationInputStore.replaceFrom("replacement".byteInputStream(), root)
                fail("expected move failure")
            } catch (_: Exception) { }
            assertTrue(target.isDirectory)
            assertEquals("preserved", marker.readText())
            assertTrue(target.parentFile!!.listFiles()!!.none { it.name.contains(".tmp-") })
        } finally { root.deleteRecursively() }
    }

    fun testClearRemovesOnlyPersistentCalculationCopy() {
        val root = tempDir("clear")
        try {
            CalculationInputStore.replaceFrom("sleep csv".byteInputStream(), root)
            val legacy = File(root, "cache/user_sleep_stage.csv").apply {
                parentFile!!.mkdirs(); writeText("old cache")
            }
            val sibling = File(CalculationInputStore.workingFile(root).parentFile, "keep.txt").apply {
                writeText("keep")
            }
            CalculationInputStore.clear(root, legacy)
            assertFalse(CalculationInputStore.workingFile(root).exists())
            assertFalse(legacy.exists())
            assertEquals("keep", sibling.readText())
            assertNull(CalculationInputStore.prepare(root, legacy))
            CalculationInputStore.clear(root, legacy)
        } finally { root.deleteRecursively() }
    }

    private fun tempDir(label: String) = File(
        instrumentation.targetContext.cacheDir,
        "calculation-input-$label-${System.nanoTime()}"
    ).also { assertTrue(it.mkdirs()) }
}

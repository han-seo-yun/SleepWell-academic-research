package app.sleepwell.ui

import android.test.InstrumentationTestCase
import androidx.compose.runtime.saveable.SaverScope
import app.sleepwell.analysis.DirectInputForm
import app.sleepwell.analysis.DirectInputParser

/** A restored draft must never silently reinterpret a legacy response as a new five-point answer. */
@Suppress("DEPRECATION")
class FatigueDraftRegressionTest : InstrumentationTestCase() {
    fun testNewFivePointDraftKeepsItsScaleAcrossRestore() {
        val form = DirectInputForm(condition = 4, rested = 5)
        val restored = inputDraftSaver.restore(saved(form))!!
        assertEquals(form, restored)
        val parsed = DirectInputParser.parse(restored, nowMs = 1_794_000_000_000L)
        assertTrue(parsed.errors.joinToString(), parsed.ok)
        assertEquals(4, parsed.survey!!.fatigue)
        assertEquals(5, parsed.survey!!.fatigueScaleMax)
    }

    fun testDraftFromPreviousVersionRemainsSevenPointEvenForOverlappingValue() {
        val previousValues = saved(DirectInputForm(condition = 4, rested = 5)).take(15)
        val restored = inputDraftSaver.restore(previousValues)!!
        assertEquals(4, restored.condition)
        assertEquals(7, restored.fatigueScaleMax)
        val parsed = DirectInputParser.parse(restored, nowMs = 1_794_000_000_000L)
        assertTrue(parsed.errors.joinToString(), parsed.ok)
        assertEquals(4, parsed.survey!!.fatigue)
        assertEquals(7, parsed.survey!!.fatigueScaleMax)
    }

    fun testExplicitUnrecognizedDraftScaleIsRejectedRatherThanGuessed() {
        val values = saved(DirectInputForm(condition = 4)).toMutableList()
        values[15] = "unrecognized"
        val restored = inputDraftSaver.restore(values)!!
        assertEquals(0, restored.fatigueScaleMax)
        assertFalse(DirectInputParser.parse(restored, nowMs = 1_794_000_000_000L).ok)
    }

    private fun saved(form: DirectInputForm): List<String> {
        val scope = object : SaverScope {
            override fun canBeSaved(value: Any): Boolean = true
        }
        val value = with(inputDraftSaver) { scope.save(form) } as List<*>
        return value.map { it as String }
    }
}

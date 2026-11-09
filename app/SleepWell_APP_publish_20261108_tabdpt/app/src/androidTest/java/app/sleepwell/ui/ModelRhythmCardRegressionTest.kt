package app.sleepwell.ui

import android.test.InstrumentationTestCase
import app.sleepwell.analysis.AnalysisDisplay
import app.sleepwell.analysis.ChartPoint
import app.sleepwell.analysis.PlanBand
import app.sleepwell.analysis.PlanKind

@Suppress("DEPRECATION")
class ModelRhythmCardRegressionTest : InstrumentationTestCase() {
    fun testFutureOnlyProjectionRemainsVisible() {
        val future = listOf(
            ChartPoint(2_000L, 0.7, 0.2, 0.9),
            ChartPoint(3_000L, 0.8, 0.1, 0.9)
        )
        val display = display(history = emptyList(), future = future, start = 1_000L, end = 4_000L)

        val projection = modelChartProjection(display)

        assertNotNull(projection)
        assertEquals(1_000L, projection?.startMs)
        assertEquals(4_000L, projection?.endMs)
        assertEquals(future, projection?.points)
    }

    fun testNearestPointIgnoresPointsOutsideVisibleRange() {
        val outside = ChartPoint(500L, 0.1, 0.2, 0.3)
        val first = ChartPoint(2_000L, 0.4, 0.2, 0.6)
        val second = ChartPoint(3_000L, 0.8, 0.1, 0.9)

        val nearest = nearestModelPoint(listOf(outside, first, second), 2_800L, 1_000L, 4_000L)

        assertEquals(second, nearest)
    }

    fun testProjectionUsesAllThreeRelativeModelSeriesForScale() {
        val display = display(
            history = listOf(ChartPoint(2_000L, -0.4, 0.2, 1.7)),
            future = emptyList(),
            start = 1_000L,
            end = 4_000L
        )

        val projection = modelChartProjection(display)!!

        assertTrue(projection.low < -0.4)
        assertTrue(projection.high > 1.7)
    }

    fun testPointerMappingUsesDensityConvertedPlotBounds() {
        val start = ChartPoint(1_000L, 0.2, 0.1, 0.3)
        val middle = ChartPoint(2_000L, 0.4, 0.2, 0.6)
        val end = ChartPoint(3_000L, 0.6, 0.3, 0.9)
        val projection = modelChartProjection(
            display(listOf(start, middle, end), emptyList(), 1_000L, 3_000L)
        )!!
        val density = 3f
        val widthPx = 360f * density
        val leftPx = 40f * density
        val rightPaddingPx = 4f * density
        val rightPx = widthPx - rightPaddingPx
        val bounds = modelChartHorizontalBounds(widthPx, leftPx, rightPaddingPx)

        assertEquals(leftPx, bounds.first)
        assertEquals(rightPx, bounds.second)
        assertEquals(start, selectedPointAt(leftPx, widthPx, leftPx, rightPaddingPx, projection))
        assertEquals(middle, selectedPointAt((leftPx + rightPx) / 2f, widthPx, leftPx, rightPaddingPx, projection))
        assertEquals(end, selectedPointAt(rightPx, widthPx, leftPx, rightPaddingPx, projection))
    }

    fun testPredictedSleepScenarioIsContinuousAndPreservesCircadianValues() {
        val hour = 3_600_000L
        val future = (0L..4L).map { index ->
            ChartPoint(index * hour, 0.70 + index * 0.01, -0.10 + index * 0.02, 0.0)
        }
        val scenario = predictedSleepScenario(
            future,
            listOf(PlanBand(hour, 3 * hour, PlanKind.PREDICTED))
        )!!

        assertEquals(future.map { it.timeMs }, scenario.map { it.timeMs })
        assertEquals(future.map { it.c }, scenario.map { it.c })
        assertEquals(future.first().s, scenario.first().s)
        scenario.drop(2).forEach { assertEquals(it.s + it.c, it.propensity, 1e-12) }
    }

    fun testPredictedSleepDecreasesPressureThenWakeIncreasesIt() {
        val hour = 3_600_000L
        val future = (0L..4L).map { index -> ChartPoint(index * hour, 0.8, 0.0, 0.8) }
        val scenario = predictedSleepScenario(
            future,
            listOf(PlanBand(hour, 3 * hour, PlanKind.PREDICTED))
        )!!

        assertTrue(scenario[2].s < scenario[1].s)
        assertTrue(scenario[3].s < scenario[2].s)
        assertTrue(scenario[4].s > scenario[3].s)
    }

    fun testScenarioSplitsIntervalsAtPartialSleepBoundaries() {
        val hour = 3_600_000L
        val future = listOf(
            ChartPoint(0L, 0.8, 0.0, 0.8),
            ChartPoint(hour, 0.8, 0.0, 0.8),
            ChartPoint(2 * hour, 0.8, 0.0, 0.8)
        )
        val halfHour = hour / 2
        val partial = predictedSleepScenario(
            future,
            listOf(PlanBand(halfHour, hour + halfHour, PlanKind.PREDICTED))
        )!!
        val afterWakeHalf = app.sleepwell.health.TwoProcessModel.advanceProcessS(0.8, halfHour, false)
        val expectedAtHour = app.sleepwell.health.TwoProcessModel.advanceProcessS(afterWakeHalf, halfHour, true)
        val afterSleepHalf = app.sleepwell.health.TwoProcessModel.advanceProcessS(expectedAtHour, halfHour, true)
        val expectedAtTwoHours = app.sleepwell.health.TwoProcessModel.advanceProcessS(afterSleepHalf, halfHour, false)

        assertEquals(expectedAtHour, partial[1].s, 1e-12)
        assertEquals(expectedAtTwoHours, partial[2].s, 1e-12)
    }

    fun testScenarioRequiresOverlappingPredictedBandAndIgnoresRecommendation() {
        val future = listOf(
            ChartPoint(1_000L, 0.7, 0.1, 0.8),
            ChartPoint(2_000L, 0.8, 0.1, 0.9)
        )

        assertNull(predictedSleepScenario(future, emptyList()))
        assertNull(predictedSleepScenario(
            future,
            listOf(PlanBand(1_100L, 1_900L, PlanKind.RECOMMENDED))
        ))
        assertNull(predictedSleepScenario(
            future,
            listOf(PlanBand(3_000L, 4_000L, PlanKind.PREDICTED))
        ))
    }

    fun testScenarioDoesNotApplyPredictedSleepBeforeCalculationReference() {
        val hour = 3_600_000L
        val future = (0L..3L).map { index -> ChartPoint(index * hour, 0.8, 0.0, 0.8) }
        val scenario = predictedSleepScenario(
            future,
            listOf(PlanBand(0L, 3 * hour, PlanKind.PREDICTED)),
            referenceMs = 2 * hour
        )!!
        assertEquals(future[1], scenario[1])
        assertEquals(future[2], scenario[2])
        assertTrue(scenario[3].s < scenario[2].s)
    }

    private fun display(
        history: List<ChartPoint>,
        future: List<ChartPoint>,
        start: Long,
        end: Long
    ) = AnalysisDisplay(
        modelLabel = "test",
        isTemporaryModel = true,
        referenceMs = 2_500L,
        readings = emptyList(),
        history = history,
        future = future,
        futureIsTemporary = future.isNotEmpty(),
        sleepBands = emptyList(),
        easiestSleepMs = null,
        recentSleeps = emptyList(),
        recommendationText = "",
        graphStartMs = start,
        graphEndMs = end
    )
}

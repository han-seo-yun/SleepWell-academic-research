package app.sleepwell.ui

import app.sleepwell.analysis.ChartPoint
import app.sleepwell.analysis.PlanBand
import app.sleepwell.analysis.PlanKind
import app.sleepwell.health.TwoProcessModel

/**
 * Replays the already-produced future rhythm for display with the model's predicted sleep
 * interval applied to Process S as if the user slept throughout that interval. C is preserved
 * exactly from the source points.
 *
 * This is a conditional visualization only. It does not replace the prediction, recommendation,
 * DATA33 input, personalization output, or any saved analysis result.
 */
internal fun predictedSleepScenario(
    future: List<ChartPoint>,
    planBands: List<PlanBand>,
    referenceMs: Long = future.minOfOrNull { it.timeMs } ?: 0L
): List<ChartPoint>? {
    val source = future.sortedBy { it.timeMs }
    if (source.size < 2) return null
    val firstMs = source.first().timeMs
    val lastMs = source.last().timeMs
    val predicted = mergeIntervals(
        planBands.asSequence()
            .filter { it.kind == PlanKind.PREDICTED && it.endMs > it.startMs }
            .mapNotNull { band ->
                val start = maxOf(firstMs, referenceMs, band.startMs)
                val end = minOf(lastMs, band.endMs)
                if (end > start) start..end else null
            }
            .toList()
    )
    if (predicted.isEmpty()) return null

    val result = ArrayList<ChartPoint>(source.size)
    var s = source.first().s
    val firstSleepStart = predicted.first().first
    result += source.first()
    source.zipWithNext().forEach { (previous, current) ->
        if (current.timeMs <= firstSleepStart) {
            // Preserve the source forecast byte-for-byte until the conditional path diverges.
            s = current.s
            result += current
        } else {
            s = advanceAcrossInterval(s, previous.timeMs, current.timeMs, predicted)
            result += current.copy(s = s, propensity = s + current.c)
        }
    }
    return result
}

private fun advanceAcrossInterval(
    initialS: Double,
    startMs: Long,
    endMs: Long,
    sleepIntervals: List<LongRange>
): Double {
    if (endMs <= startMs) return initialS
    val boundaries = buildList {
        add(startMs)
        sleepIntervals.forEach { interval ->
            if (interval.first in (startMs + 1) until endMs) add(interval.first)
            if (interval.last in (startMs + 1) until endMs) add(interval.last)
        }
        add(endMs)
    }.distinct().sorted()
    var s = initialS
    boundaries.zipWithNext().forEach { (segmentStart, segmentEnd) ->
        val midpoint = segmentStart + (segmentEnd - segmentStart) / 2
        val sleeping = sleepIntervals.any { midpoint >= it.first && midpoint < it.last }
        s = TwoProcessModel.advanceProcessS(s, segmentEnd - segmentStart, sleeping)
    }
    return s
}

private fun mergeIntervals(intervals: List<LongRange>): List<LongRange> {
    if (intervals.isEmpty()) return emptyList()
    val sorted = intervals.sortedBy { it.first }
    val merged = mutableListOf<LongRange>()
    sorted.forEach { current ->
        val last = merged.lastOrNull()
        if (last == null || current.first > last.last) merged += current
        else merged[merged.lastIndex] = last.first..maxOf(last.last, current.last)
    }
    return merged
}

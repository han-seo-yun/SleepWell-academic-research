package app.sleepwell.health

import java.time.Instant
import java.util.TimeZone

/** Local clock ticks, including both occurrences of repeated hours and no nonexistent hours. */
fun localHourTicks(startMs: Long, endMs: Long, everyHours: Int, tz: TimeZone): List<Long> {
    require(everyHours in 1..24)
    if (endMs < startMs) return emptyList()
    val zone = tz.toZoneId()
    var date = Instant.ofEpochMilli(startMs).atZone(zone).toLocalDate()
    val lastDate = Instant.ofEpochMilli(endMs).atZone(zone).toLocalDate()
    val ticks = mutableListOf<Long>()
    while (!date.isAfter(lastDate)) {
        for (hour in 0..23 step everyHours) {
            val local = date.atTime(hour, 0)
            zone.rules.getValidOffsets(local).forEach { offset ->
                val ms = local.toInstant(offset).toEpochMilli()
                if (ms in startMs..endMs) ticks += ms
            }
        }
        date = date.plusDays(1)
    }
    return ticks.sorted()
}

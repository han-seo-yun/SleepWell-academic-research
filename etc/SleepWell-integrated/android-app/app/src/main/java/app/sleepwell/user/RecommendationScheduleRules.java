package app.sleepwell.user;

import java.time.LocalDate;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.Set;

/**
 * Display/update timing only. The frozen research-model cutoff remains 20:00
 * Asia/Seoul and model offsets are never reinterpreted at a later cutoff.
 */
public final class RecommendationScheduleRules {
    public static final ZoneId ZONE = ZoneId.of("Asia/Seoul");
    private static final int[] UPDATE_HOURS = {20, 21, 22, 23};

    private RecommendationScheduleRules() {}

    public static final class Timing {
        private final LocalDate nightId;
        private final ZonedDateTime modelCutoff;
        private final ZonedDateTime expectedOnset;
        private final ZonedDateTime initialDecisionAt;

        Timing(LocalDate nightId, ZonedDateTime modelCutoff, ZonedDateTime expectedOnset,
               ZonedDateTime initialDecisionAt) {
            this.nightId = nightId;
            this.modelCutoff = modelCutoff;
            this.expectedOnset = expectedOnset;
            this.initialDecisionAt = initialDecisionAt;
        }

        public LocalDate nightId() { return nightId; }
        public ZonedDateTime modelCutoff() { return modelCutoff; }
        public ZonedDateTime expectedOnset() { return expectedOnset; }
        public ZonedDateTime initialDecisionAt() { return initialDecisionAt; }
    }

    public static final class DueSlot {
        private final String slot;
        private final String kind;
        private final ZonedDateTime scheduledAt;

        DueSlot(String slot, String kind, ZonedDateTime scheduledAt) {
            this.slot = slot;
            this.kind = kind;
            this.scheduledAt = scheduledAt;
        }

        public String slot() { return slot; }
        public String kind() { return kind; }
        public ZonedDateTime scheduledAt() { return scheduledAt; }
    }

    public static Timing timing(LocalDate nightId, ZonedDateTime expectedOnset) {
        ZonedDateTime cutoff = nightId.atTime(20, 0).atZone(ZONE);
        ZonedDateTime onset = expectedOnset.withZoneSameInstant(ZONE);
        ZonedDateTime candidate = onset.minusHours(2);
        ZonedDateTime initial = candidate.isBefore(cutoff) ? candidate : cutoff;
        return new Timing(nightId, cutoff, onset, initial);
    }

    public static DueSlot dueSlot(
            ZonedDateTime now,
            Timing timing,
            Set<String> seenSlots,
            boolean awake) {
        if (!awake) return null;
        ZonedDateTime current = now.withZoneSameInstant(ZONE);
        ZonedDateTime cutoff = timing.modelCutoff();
        ZonedDateTime initial = timing.initialDecisionAt();
        if (initial.isBefore(cutoff)
                && !current.isBefore(initial)
                && current.isBefore(cutoff)
                && !seenSlots.contains("initial")) {
            return new DueSlot("initial", "personalized_initial", initial);
        }
        if (!current.toLocalDate().equals(cutoff.toLocalDate()) || !isUpdateHour(current.getHour())) {
            return null;
        }
        ZonedDateTime scheduled = current.withMinute(0).withSecond(0).withNano(0);
        boolean initialCollision = initial.equals(cutoff) && current.getHour() == 20;
        String slot = initialCollision ? "initial" : String.format("hourly_%02d", current.getHour());
        if (seenSlots.contains(slot)) return null;
        return new DueSlot(slot, initialCollision ? "personalized_initial" : "conditional_hourly", scheduled);
    }

    public static ZonedDateTime[] hourlyUpdates(LocalDate nightId) {
        ZonedDateTime[] values = new ZonedDateTime[UPDATE_HOURS.length];
        for (int i = 0; i < UPDATE_HOURS.length; i++) {
            values[i] = nightId.atTime(LocalTime.of(UPDATE_HOURS[i], 0)).atZone(ZONE);
        }
        return values;
    }

    private static boolean isUpdateHour(int hour) {
        for (int value : UPDATE_HOURS) if (value == hour) return true;
        return false;
    }
}

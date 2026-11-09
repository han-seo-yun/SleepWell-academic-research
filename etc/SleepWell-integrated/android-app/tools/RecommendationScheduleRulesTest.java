package app.sleepwell.user;

import java.time.LocalDate;
import java.time.ZonedDateTime;
import java.util.Set;

public final class RecommendationScheduleRulesTest {
    private static void check(boolean value, String message) {
        if (!value) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        LocalDate night = LocalDate.parse("2026-10-28");
        var early = RecommendationScheduleRules.timing(
                night, ZonedDateTime.parse("2026-10-28T21:00:00+09:00[Asia/Seoul]"));
        check(early.initialDecisionAt().toLocalTime().toString().equals("19:00"),
                "21:00 onset must schedule 19:00 initial recommendation");
        check(early.modelCutoff().toLocalTime().toString().equals("20:00"),
                "research cutoff must remain 20:00");

        var late = RecommendationScheduleRules.timing(
                night, ZonedDateTime.parse("2026-10-28T23:00:00+09:00[Asia/Seoul]"));
        check(late.initialDecisionAt().toLocalTime().toString().equals("20:00"),
                "23:00 onset must cap initial recommendation at 20:00");

        var collision = RecommendationScheduleRules.dueSlot(
                ZonedDateTime.parse("2026-10-28T20:05:00+09:00[Asia/Seoul]"), late,
                Set.of(), true);
        check(collision != null && collision.slot().equals("initial"),
                "20:00 initial must consume the hourly collision");

        var initial = RecommendationScheduleRules.dueSlot(
                ZonedDateTime.parse("2026-10-28T19:10:00+09:00[Asia/Seoul]"), early,
                Set.of(), true);
        check(initial != null && initial.slot().equals("initial"), "initial slot missing");
        check(RecommendationScheduleRules.dueSlot(
                ZonedDateTime.parse("2026-10-28T19:10:00+09:00[Asia/Seoul]"), early,
                Set.of("initial"), true) == null, "initial slot must deduplicate");
        check(RecommendationScheduleRules.dueSlot(
                ZonedDateTime.parse("2026-10-28T22:15:00+09:00[Asia/Seoul]"), late,
                Set.of(), false) == null, "hourly update requires awake evidence");
        var hourly = RecommendationScheduleRules.dueSlot(
                ZonedDateTime.parse("2026-10-28T22:15:00+09:00[Asia/Seoul]"), late,
                Set.of(), true);
        check(hourly != null && hourly.slot().equals("hourly_22"), "22:00 slot missing");
    }
}

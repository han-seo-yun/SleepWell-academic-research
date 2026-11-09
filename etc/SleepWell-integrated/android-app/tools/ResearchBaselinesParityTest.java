import app.sleepwell.user.DailyContext;
import app.sleepwell.user.ResearchBaselines;
import app.sleepwell.user.SleepRecord;
import app.sleepwell.user.UserProfile;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

public final class ResearchBaselinesParityTest {
    private static void close(Double actual, double expected, String name) {
        if (actual == null || Math.abs(actual - expected) > 1e-9) {
            throw new AssertionError(name + ": " + actual + " != " + expected);
        }
    }

    private static SleepRecord record(String id, String bed, String wake, String modified) {
        int duration = (int) java.time.Duration.between(Instant.parse(bed), Instant.parse(wake)).toMinutes();
        return new SleepRecord(id, wake.substring(0, 10), bed, wake, duration, 4, 2, "manual", modified);
    }

    private static ResearchBaselines.Prediction prediction(ResearchBaselines.Snapshot snapshot, String name) {
        var value = snapshot.getPredictions().get(name);
        if (value == null) throw new AssertionError("missing baseline: " + name);
        return value;
    }

    private static void contains(String source, String token, String name) {
        if (!source.contains(token)) throw new AssertionError(name + " contract missing: " + token);
    }

    public static void main(String[] args) throws Exception {
        Instant cutoff = Instant.parse("2026-10-25T11:00:00Z"); // 20:00 Asia/Seoul
        UserProfile profile = new UserProfile("30-39", "female", 60, "00:00", "08:00", 480,
            "2026-10-01T00:00:00Z", "researcher", null);
        var prior = List.of(
            record("n1", "2026-10-22T14:50:00Z", "2026-10-22T22:00:00Z", "2026-10-22T23:00:00Z"),
            record("n2", "2026-10-23T15:10:00Z", "2026-10-23T22:00:00Z", "2026-10-23T23:00:00Z")
        );

        var base = ResearchBaselines.INSTANCE.compute(profile, prior, cutoff, null);
        if (base.getHistoryCount() != 2) throw new AssertionError("strict history count mismatch");
        close(prediction(base, "circular_mean_7_calendar_days").getOnsetOffsetMin(), 240.0,
            "midnight circular onset");
        if (!ResearchBaselines.PROFILE_ASSUMPTION.equals(
            prediction(base, "profile_clock_assumption").getFallback())) {
            throw new AssertionError("profile assumption must be explicitly non-cohort/non-TRAIN");
        }

        var changedAfterCutoff = new ArrayList<>(prior);
        changedAfterCutoff.add(record("future-wake", "2026-10-25T14:00:00Z", "2026-10-25T22:00:00Z",
            "2026-10-25T23:00:00Z"));
        changedAfterCutoff.add(record("late-report", "2026-10-24T14:00:00Z", "2026-10-24T22:00:00Z",
            "2026-10-25T11:00:00Z"));
        var unchanged = ResearchBaselines.INSTANCE.compute(profile, changedAfterCutoff, cutoff, null);
        if (unchanged.getHistoryCount() != 2 || !unchanged.getPredictions().equals(base.getPredictions())) {
            throw new AssertionError("future/at-cutoff records changed a frozen prediction");
        }

        var noSchedule = prediction(base, "next_day_first_schedule_only_profile_duration");
        if (noSchedule.getAvailable() || !"no_schedule_context".equals(noSchedule.getReason())) {
            throw new AssertionError("missing schedule must be unavailable");
        }
        DailyContext late = new DailyContext("2026-10-25", "work", "2026-10-25T22:00:00Z", 60,
            null, null, null, null, "2026-10-25T11:00:01Z");
        var lateSchedule = prediction(ResearchBaselines.INSTANCE.compute(profile, prior, cutoff, late),
            "next_day_first_schedule_only_profile_duration");
        if (lateSchedule.getAvailable() || !"context_not_available_by_cutoff".equals(lateSchedule.getReason())) {
            throw new AssertionError("late schedule must be unavailable");
        }
        DailyContext tooEarly = new DailyContext("2026-10-25", "work", "2026-10-25T15:00:00Z", 60,
            null, null, null, null, "2026-10-25T10:59:59Z");
        var infeasibleSchedule = prediction(ResearchBaselines.INSTANCE.compute(profile, prior, cutoff, tooEarly),
            "next_day_first_schedule_only_profile_duration");
        if (infeasibleSchedule.getAvailable() ||
            !"fixed_profile_duration_outside_prospective_prediction_window".equals(infeasibleSchedule.getReason())) {
            throw new AssertionError("schedule-only baseline must not truncate fixed profile duration");
        }
        DailyContext valid = new DailyContext("2026-10-25", "work", "2026-10-25T22:00:00Z", 60,
            null, null, null, null, "2026-10-25T10:59:59Z");
        var schedule = prediction(ResearchBaselines.INSTANCE.compute(profile, prior, cutoff, valid),
            "next_day_first_schedule_only_profile_duration");
        if (!schedule.getAvailable() || schedule.getContributingRecords() != 0 ||
            !"fixed_user_profile_desired_duration_not_population_trained".equals(schedule.getFallback())) {
            throw new AssertionError("schedule-only profile contract mismatch");
        }
        close(schedule.getOnsetOffsetMin(), 120.0, "schedule-only onset");
        close(schedule.getWakeOffsetMin(), 600.0, "schedule-only wake");

        if (args.length == 1) {
            Path root = Path.of(args[0]);
            String repository = Files.readString(root.resolve("app/src/main/java/app/sleepwell/user/UserRepository.kt"));
            String backup = Files.readString(root.resolve("app/src/main/java/app/sleepwell/user/BackupManager.kt"));
            contains(repository, ".put(\"research_baselines\", output.outcomes.optJSONObject", "prospective snapshot export");
            contains(repository, "value.prospectiveForecasts.none", "first snapshot remains immutable");
            contains(repository, ".put(\"source_research_baselines\"", "matched outcome source preservation");
            contains(backup, ".put(\"prospective_forecasts\"", "daily backup snapshot preservation");
            contains(backup, ".put(\"research_baselines\", snapshot.optJSONObject", "realized baseline export");
        }
        System.out.println("ResearchBaselinesParityTest: leakage, midnight, schedule, immutability/export contracts passed");
    }
}

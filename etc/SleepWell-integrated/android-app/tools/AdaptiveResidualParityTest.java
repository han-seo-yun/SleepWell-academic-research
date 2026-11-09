import app.sleepwell.user.AdaptiveResidualPersonalization;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;

public final class AdaptiveResidualParityTest {
    private static void close(double actual, double expected, String name) {
        if (Math.abs(actual - expected) > 1e-12) {
            throw new AssertionError(name + ": " + actual + " != " + expected);
        }
    }

    public static void main(String[] args) {
        if (!AdaptiveResidualPersonalization.POLICY_VERSION.equals("online-residual-shrinkage-v2-daily")) {
            throw new AssertionError("policy version mismatch");
        }
        Instant cutoff = Instant.parse("2026-10-25T11:00:00Z");
        var adjustment = AdaptiveResidualPersonalization.INSTANCE.computeAdjustment(List.of(
            new AdaptiveResidualPersonalization.HistoryResidual("a", 10.0, 20.0,
                Instant.parse("2026-10-24T11:00:00Z"), Instant.parse("2026-10-23T11:00:00Z")),
            new AdaptiveResidualPersonalization.HistoryResidual("b", -5.0, 15.0,
                Instant.parse("2026-10-25T10:00:00Z"), Instant.parse("2026-10-24T11:00:00Z")),
            new AdaptiveResidualPersonalization.HistoryResidual("future", 999.0, 999.0, cutoff, cutoff)
        ), cutoff, 14.0, 14.0, 2);

        if (!adjustment.getActive() || adjustment.getEligibleCount() != 2) {
            throw new AssertionError("eligibility/activation mismatch");
        }
        close(adjustment.getDecayedMass(), 1.8574188172745263, "decayed_mass");
        close(adjustment.getKishEffectiveN(), 1.9987756051486254, "kish_effective_n");
        close(adjustment.getWeight(), 0.11713248156447241, "weight");
        close(adjustment.getRawOnsetResidualMeanMin(), 2.3143734938002476, "raw_onset");
        close(adjustment.getRawWakeResidualMeanMin(), 17.438124497933416, "raw_wake");
        close(adjustment.getOnsetCorrectionMin(), 0.2710883105958611, "onset_correction");
        close(adjustment.getWakeCorrectionMin(), 2.0425707962731603, "wake_correction");
        close(adjustment.getLastAvailableGapDays(), 1.0 / 24.0, "last_available_gap");
        close(adjustment.getLastOutcomeGapDays(), 1.0, "last_outcome_gap");

        var zero = AdaptiveResidualPersonalization.INSTANCE.computeAdjustment(
            List.of(), cutoff, 14.0, 14.0, 1);
        if (zero.getActive() || zero.getEligibleCount() != 0 || zero.getWeight() != 0.0) {
            throw new AssertionError("zero-history activation mismatch");
        }

        Instant oneDayAgo = cutoff.minus(1, ChronoUnit.DAYS);
        var one = AdaptiveResidualPersonalization.INSTANCE.computeAdjustment(List.of(
            new AdaptiveResidualPersonalization.HistoryResidual("one", 90.0, -90.0, oneDayAgo, oneDayAgo)
        ), cutoff, 14.0, 14.0, 1);
        if (!one.getActive() || one.getEligibleCount() != 1 || one.getWeight() <= 0.0) {
            throw new AssertionError("first eligible residual must activate");
        }
        close(one.getDecayedMass(), Math.pow(2.0, -1.0 / 14.0), "one_record_mass");

        Instant sixtyDaysAgo = cutoff.minus(60, ChronoUnit.DAYS);
        var stale = AdaptiveResidualPersonalization.INSTANCE.computeAdjustment(List.of(
            new AdaptiveResidualPersonalization.HistoryResidual("old", 90.0, -90.0, sixtyDaysAgo, sixtyDaysAgo)
        ), cutoff, 14.0, 14.0, 1);
        if (!(stale.getDecayedMass() < one.getDecayedMass() && stale.getWeight() < one.getWeight() &&
            Math.abs(stale.getOnsetCorrectionMin()) < Math.abs(one.getOnsetCorrectionMin()))) {
            throw new AssertionError("gap decay must shrink correction toward zero");
        }

        var eightRows = new ArrayList<AdaptiveResidualPersonalization.HistoryResidual>();
        for (int i = 1; i <= 8; i++) {
            Instant at = cutoff.minus(i, ChronoUnit.HOURS);
            eightRows.add(new AdaptiveResidualPersonalization.HistoryResidual(
                "night-" + i, 1000.0, -1000.0, at, at));
        }
        var eight = AdaptiveResidualPersonalization.INSTANCE.computeAdjustment(
            eightRows, cutoff, 14.0, 14.0, 1);
        if (!eight.getActive() || eight.getEligibleCount() != 8) {
            throw new AssertionError("eight-record activation mismatch");
        }
        close(eight.getRawOnsetResidualMeanMin(), 180.0, "clipped_raw_onset");
        close(eight.getRawWakeResidualMeanMin(), -180.0, "clipped_raw_wake");
        close(eight.getOnsetCorrectionMin(), 60.0, "capped_onset_correction");
        close(eight.getWakeCorrectionMin(), -60.0, "capped_wake_correction");

        var projected = AdaptiveResidualPersonalization.INSTANCE.project(-50.0, 2000.0);
        close(projected.getFirst(), 0.0, "projected_onset");
        close(projected.getSecond(), 960.0, "projected_wake");
        System.out.println("AdaptiveResidualParityTest: daily parity, 0/1/8 activation, gap and caps passed");
    }
}

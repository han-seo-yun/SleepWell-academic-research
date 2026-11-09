import app.sleepwell.user.UserContractRules;

public final class UserContractRulesTest {
    public static void main(String[] args) {
        check(UserContractRules.shouldFreezeForecast("prospective_20_window"), "20:05 prospective capture");
        check(!UserContractRules.shouldFreezeForecast("late_reconstruction_after_20"), "20:16 late run must not freeze");
        check(UserContractRules.isAcknowledged("2026-10-24", 3, "2026-10-24", 3), "exact ACK");
        check(!UserContractRules.isAcknowledged("2026-10-24", 4, "2026-10-24", 3), "new revision preserved");
        check(!UserContractRules.keepSamsungRecord(true, false), "missing/ineligible Samsung record reconciled");
        check(UserContractRules.keepSamsungRecord(false, false), "outside query window preserved");
        check(UserContractRules.isPrimaryActual("manual"), "diary primary");
        check(!UserContractRules.isPrimaryActual("samsung_health"), "wearable remains auxiliary");
        check(UserContractRules.isCompleteGoodNight(4, 3), "complete good-night response");
        check(!UserContractRules.isCompleteGoodNight(5, null), "missing fatigue excluded");
        check(!UserContractRules.isCompleteGoodNight(null, 1), "missing rested excluded");
        check(UserContractRules.boundAdultSleepTarget(360) == 420, "adult lower bound");
        check(UserContractRules.boundAdultSleepTarget(600) == 540, "adult upper bound");
        check(UserContractRules.shouldFreezeFirstManualReport(false), "first manual report frozen even if later excluded");
        check(!UserContractRules.shouldFreezeFirstManualReport(true), "later edit cannot substitute first report");
        System.out.println("UserContractRulesTest: 15 checks passed");
    }

    private static void check(boolean value, String label) {
        if (!value) throw new AssertionError(label);
    }
}

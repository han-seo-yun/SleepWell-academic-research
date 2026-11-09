package app.sleepwell.user;

/** Small deterministic protocol rules kept Java-only so they can be tested without an Android runtime. */
public final class UserContractRules {
    private UserContractRules() {}

    public static boolean shouldFreezeForecast(String status) {
        return "prospective_20_window".equals(status);
    }

    public static boolean isAcknowledged(String itemDay, int itemRevision, String ackDay, int ackRevision) {
        return itemDay.equals(ackDay) && itemRevision == ackRevision;
    }

    public static boolean keepSamsungRecord(boolean insideCompleteWindow, boolean stillEligible) {
        return !insideCompleteWindow || stillEligible;
    }

    public static boolean isPrimaryActual(String source) {
        return "manual".equals(source);
    }

    public static boolean isCompleteGoodNight(Integer rested, Integer fatigue) {
        return rested != null && fatigue != null && rested >= 4 && fatigue <= 3;
    }

    public static int boundAdultSleepTarget(int minutes) {
        return Math.max(420, Math.min(540, minutes));
    }

    public static boolean shouldFreezeFirstManualReport(boolean nightAlreadyFrozen) {
        return !nightAlreadyFrozen;
    }
}

package app.sleepwell.user

import org.json.JSONObject

internal const val HGB_MODEL_VERSION = "hgb-hybrid39-equal-budget-20261107-trial2"

internal class LocalHgbPredictionProvider(
    assetLoader: (String) -> ByteArray
) : TabDptPredictionProvider, PredictionProviderMetadata {
    override val backend: PredictionBackend = PredictionBackend.LOCAL_HGB
    private data class Models(val cmi: HgbModel, val mesa: HgbModel)
    private val models = runCatching {
        Models(
            HgbModel(assetLoader("models/mobile_hgb_cmi.bin")),
            HgbModel(assetLoader("models/mobile_hgb_mesa.bin"))
        )
    }

    init {
        validateFeatureOrder(PortableFusionModel.HYBRID_FEATURE_ORDER)
    }

    override fun predict(requestId: String, snapshots: JSONObject): TabDptResult = runCatching {
        require(requestId.isNotBlank() && requestId.length <= 128) { "request_id가 올바르지 않습니다." }
        val loaded = models.getOrThrow()
        val cmiOutput = HgbModel.constrained(loaded.cmi.predict(rawFeatures(snapshots, "cmi")))
        val mesaOutput = HgbModel.constrained(loaded.mesa.predict(rawFeatures(snapshots, "mesa")))
        TabDptPrediction(
            cmiOutput[0], cmiOutput[1], mesaOutput[0], mesaOutput[1],
            HGB_MODEL_VERSION,
            JSONObject()
                .put("runtime", "pure_java_swl_hgb01")
                .put("execution", "on_device_offline")
                .put("feature_contract", TABDPT_FEATURE_CONTRACT)
                .put("network_required", false),
            PredictionBackend.LOCAL_HGB
        )
    }.fold(
        onSuccess = { TabDptResult.Success(it) },
        onFailure = {
            TabDptResult.Unavailable(
                "local_hgb_failed", it.message ?: it::class.java.simpleName,
                PredictionBackend.LOCAL_HGB
            )
        }
    )

    private fun rawFeatures(snapshots: JSONObject, cohort: String): DoubleArray {
        val snapshot = snapshots.getJSONObject(cohort)
        require(snapshot.length() == HGB_FEATURE_ORDER.size) {
            "$cohort HGB 입력은 정확히 ${HGB_FEATURE_ORDER.size}개여야 합니다."
        }
        return DoubleArray(HGB_FEATURE_ORDER.size) { index ->
            val name = HGB_FEATURE_ORDER[index]
            require(snapshot.has(name)) { "$cohort HGB 입력에 $name 항목이 없습니다." }
            val value = snapshot.opt(name)
            require(value == JSONObject.NULL || value is Number) { "$cohort HGB의 $name 값 형식이 잘못됐습니다." }
            if (value is Number) {
                value.toDouble().also { require(it.isFinite()) { "$cohort HGB의 $name 값이 유한하지 않습니다." } }
            } else Double.NaN
        }
    }

    internal companion object {
        internal val HGB_FEATURE_ORDER = listOf(
            "population_bed", "population_wake", "habit_bed", "habit_wake",
            "recent_bed", "recent_wake", "previous_bed", "previous_wake",
            "recent_duration", "bed_variability", "wake_variability", "history_count_7",
            "history_age_days", "personal_weight", "weekend", "next_day_sin",
            "next_day_cos", "next_day_weekend", "median14_bed", "median14_wake",
            "ew_bed", "ew_wake", "weekday_bed", "weekday_wake", "weekday_count",
            "bed_mad", "wake_mad", "duration_mad", "previous_duration", "bed_trend",
            "wake_trend", "gap_days_mean", "recent_count14", "current_s", "current_c",
            "sleep_threshold", "wake_threshold", "physical_bed", "physical_wake"
        )

        internal fun validateFeatureOrder(actual: List<String>) {
            require(actual == HGB_FEATURE_ORDER) {
                "HGB feature order drift: expected the frozen hybrid39 contract"
            }
        }
    }
}

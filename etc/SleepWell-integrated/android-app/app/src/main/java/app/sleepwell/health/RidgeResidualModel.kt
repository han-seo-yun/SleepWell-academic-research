package app.sleepwell.health

/** Python에서 내보낸 전처리 기준과 Ridge 계수를 Kotlin에서 그대로 계산한다. */
data class RidgeResidualModel(
    val schemaVersion: Int,
    val modelVersion: String,
    val status: String,
    val featureOrder: List<String>,
    val imputationMedian: List<Double>,
    val mean: List<Double>,
    val scale: List<Double>,
    val bedtimeIntercept: Double,
    val bedtimeCoefficients: List<Double>,
    val wakeIntercept: Double,
    val wakeCoefficients: List<Double>
) {
    init {
        require(schemaVersion == 1) { "지원하지 않는 Ridge JSON schema_version=$schemaVersion" }
        require(featureOrder.isNotEmpty()) { "feature_order가 비어 있습니다" }
        require(featureOrder.distinct().size == featureOrder.size) { "feature_order에 중복이 있습니다" }
        val n = featureOrder.size
        require(imputationMedian.size == n) { "imputation_median 길이가 feature_order와 다릅니다" }
        require(mean.size == n) { "mean 길이가 feature_order와 다릅니다" }
        require(scale.size == n) { "scale 길이가 feature_order와 다릅니다" }
        require(bedtimeCoefficients.size == n) { "취침 계수 길이가 feature_order와 다릅니다" }
        require(wakeCoefficients.size == n) { "기상 계수 길이가 feature_order와 다릅니다" }
        require(scale.all { it.isFinite() && it >= 0.0 }) { "scale은 0 이상의 유한값이어야 합니다" }
    }

    fun predict(features: Map<String, Double?>): ResidualPrediction {
        require(status == "trained") { "학습 완료 모델만 잔차 계산에 사용할 수 있습니다: status=$status" }
        val z = featureOrder.indices.map { i ->
            val raw = features[featureOrder[i]]?.takeIf { it.isFinite() } ?: imputationMedian[i]
            val denominator = scale[i].takeIf { it > 0.0 } ?: 1.0
            (raw - mean[i]) / denominator
        }
        return ResidualPrediction(
            bedtimeResidualMin = bedtimeIntercept + dot(bedtimeCoefficients, z),
            wakeResidualMin = wakeIntercept + dot(wakeCoefficients, z)
        )
    }

    private fun dot(a: List<Double>, b: List<Double>): Double =
        a.indices.sumOf { i -> a[i] * b[i] }

    companion object {
        fun fromJson(json: String): RidgeResidualModel {
            val root = MiniJson.parse(json).asObject("root")
            val preprocessing = root.required("preprocessing").asObject("preprocessing")
            val targets = root.required("targets").asObject("targets")
            val bed = targets.required("bedtime_residual_min").asObject("bedtime target")
            val wake = targets.required("wake_residual_min").asObject("wake target")
            return RidgeResidualModel(
                schemaVersion = root.required("schema_version").asInt("schema_version"),
                modelVersion = root.required("model_version").asString("model_version"),
                status = root.required("status").asString("status"),
                featureOrder = root.required("feature_order").asStringList("feature_order"),
                imputationMedian = preprocessing.required("imputation_median").asDoubleList("imputation_median"),
                mean = preprocessing.required("mean").asDoubleList("mean"),
                scale = preprocessing.required("scale").asDoubleList("scale"),
                bedtimeIntercept = bed.required("intercept").asDouble("bedtime intercept"),
                bedtimeCoefficients = bed.required("coefficients").asDoubleList("bedtime coefficients"),
                wakeIntercept = wake.required("intercept").asDouble("wake intercept"),
                wakeCoefficients = wake.required("coefficients").asDoubleList("wake coefficients")
            )
        }
    }
}

data class ResidualPrediction(
    val bedtimeResidualMin: Double,
    val wakeResidualMin: Double
)

private fun Map<String, Any?>.required(key: String): Any? =
    if (containsKey(key)) get(key) else throw IllegalArgumentException("Ridge JSON 필드 누락: $key")

@Suppress("UNCHECKED_CAST")
private fun Any?.asObject(label: String): Map<String, Any?> =
    this as? Map<String, Any?> ?: throw IllegalArgumentException("$label 은 object여야 합니다")

private fun Any?.asString(label: String): String =
    this as? String ?: throw IllegalArgumentException("$label 은 string이어야 합니다")

private fun Any?.asDouble(label: String): Double =
    (this as? Number)?.toDouble()?.takeIf { it.isFinite() }
        ?: throw IllegalArgumentException("$label 은 유한한 number여야 합니다")

private fun Any?.asInt(label: String): Int {
    val d = asDouble(label)
    require(d % 1.0 == 0.0) { "$label 은 정수여야 합니다" }
    return d.toInt()
}

private fun Any?.asDoubleList(label: String): List<Double> =
    (this as? List<*>)?.mapIndexed { i, v -> v.asDouble("$label[$i]") }
        ?: throw IllegalArgumentException("$label 은 number array여야 합니다")

private fun Any?.asStringList(label: String): List<String> =
    (this as? List<*>)?.mapIndexed { i, v -> v.asString("$label[$i]") }
        ?: throw IllegalArgumentException("$label 은 string array여야 합니다")

/** 외부 JSON 라이브러리 없이 assets의 고정 모델 파일을 읽기 위한 작은 표준 JSON 파서. */
private class MiniJson private constructor(private val text: String) {
    private var i = 0

    private fun value(): Any? {
        space()
        require(i < text.length) { "JSON이 중간에 끝났습니다" }
        return when (text[i]) {
            '{' -> obj()
            '[' -> array()
            '"' -> string()
            't' -> literal("true", true)
            'f' -> literal("false", false)
            'n' -> literal("null", null)
            else -> number()
        }
    }

    private fun obj(): Map<String, Any?> {
        expect('{')
        val out = linkedMapOf<String, Any?>()
        space()
        if (take('}')) return out
        while (true) {
            space()
            val key = string()
            space(); expect(':')
            out[key] = value()
            space()
            if (take('}')) return out
            expect(',')
        }
    }

    private fun array(): List<Any?> {
        expect('[')
        val out = mutableListOf<Any?>()
        space()
        if (take(']')) return out
        while (true) {
            out += value()
            space()
            if (take(']')) return out
            expect(',')
        }
    }

    private fun string(): String {
        expect('"')
        val out = StringBuilder()
        while (i < text.length) {
            val c = text[i++]
            if (c == '"') return out.toString()
            if (c != '\\') {
                out.append(c)
                continue
            }
            require(i < text.length) { "잘못된 JSON escape" }
            when (val e = text[i++]) {
                '"', '\\', '/' -> out.append(e)
                'b' -> out.append('\b')
                'f' -> out.append('\u000C')
                'n' -> out.append('\n')
                'r' -> out.append('\r')
                't' -> out.append('\t')
                'u' -> {
                    require(i + 4 <= text.length) { "잘못된 unicode escape" }
                    out.append(text.substring(i, i + 4).toInt(16).toChar())
                    i += 4
                }
                else -> throw IllegalArgumentException("지원하지 않는 JSON escape: $e")
            }
        }
        throw IllegalArgumentException("닫히지 않은 JSON string")
    }

    private fun number(): Double {
        val start = i
        if (i < text.length && text[i] == '-') i++
        while (i < text.length && text[i].isDigit()) i++
        if (i < text.length && text[i] == '.') {
            i++
            while (i < text.length && text[i].isDigit()) i++
        }
        if (i < text.length && (text[i] == 'e' || text[i] == 'E')) {
            i++
            if (i < text.length && (text[i] == '+' || text[i] == '-')) i++
            while (i < text.length && text[i].isDigit()) i++
        }
        require(i > start) { "JSON number가 필요합니다: 위치 $i" }
        return text.substring(start, i).toDoubleOrNull()
            ?: throw IllegalArgumentException("잘못된 JSON number")
    }

    private fun literal(token: String, result: Any?): Any? {
        require(text.startsWith(token, i)) { "JSON token 오류: 위치 $i" }
        i += token.length
        return result
    }

    private fun space() { while (i < text.length && text[i].isWhitespace()) i++ }
    private fun expect(c: Char) { space(); require(i < text.length && text[i++] == c) { "JSON '$c' 필요" } }
    private fun take(c: Char): Boolean {
        if (i < text.length && text[i] == c) { i++; return true }
        return false
    }

    companion object {
        fun parse(text: String): Any? {
            val parser = MiniJson(text)
            val result = parser.value()
            parser.space()
            require(parser.i == text.length) { "JSON 뒤에 불필요한 문자가 있습니다" }
            return result
        }
    }
}

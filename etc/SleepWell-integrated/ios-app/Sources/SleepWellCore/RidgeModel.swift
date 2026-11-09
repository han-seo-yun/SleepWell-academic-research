import Foundation

public enum ModelError: LocalizedError {
    case invalid(String)
    public var errorDescription: String? { if case let .invalid(message) = self { return message }; return nil }
}

public struct RidgeModel: Decodable {
    public struct Preprocessing: Decodable {
        public let imputation_median: [Double]
        public let mean: [Double]
        public let scale: [Double]
    }
    public struct Target: Decodable {
        public let intercept: Double
        public let coefficients: [Double]
    }
    public let schema_version: Int
    public let model_version: String
    public let status: String
    public let feature_order: [String]
    public let preprocessing: Preprocessing
    public let targets: [String: Target]

    public static func load(_ data: Data) throws -> RidgeModel {
        let m = try JSONDecoder().decode(Self.self, from: data)
        let n = m.feature_order.count
        guard m.schema_version == 1, m.status == "trained", n > 0,
              Set(m.feature_order).count == n,
              Set(m.feature_order).isSubset(of: Set(FeatureBuilder.names)) else {
            throw ModelError.invalid("학습 완료된 schema 1 모델과 지원되는 feature_order가 필요합니다.")
        }
        let arrays = [m.preprocessing.imputation_median, m.preprocessing.mean, m.preprocessing.scale]
        guard arrays.allSatisfy({ $0.count == n && $0.allSatisfy(\.isFinite) }),
              m.preprocessing.scale.allSatisfy({ $0 >= 0 }) else {
            throw ModelError.invalid("모델 전처리 배열의 길이 또는 수치가 잘못되었습니다.")
        }
        for name in ["bedtime_residual_min", "wake_residual_min"] {
            guard let t = m.targets[name], t.intercept.isFinite, t.coefficients.count == n,
                  t.coefficients.allSatisfy(\.isFinite) else {
                throw ModelError.invalid("모델 계수가 누락되었거나 잘못되었습니다.")
            }
        }
        return m
    }

    public func predict(_ features: [String: Double]) throws -> (bed: Double, wake: Double) {
        let z = feature_order.enumerated().map { i, name -> Double in
            let raw = features[name].flatMap { $0.isFinite ? $0 : nil } ?? preprocessing.imputation_median[i]
            let scale = preprocessing.scale[i] > 0 ? preprocessing.scale[i] : 1
            return (raw - preprocessing.mean[i]) / scale
        }
        func value(_ name: String) -> Double {
            let t = targets[name]!
            return zip(z, t.coefficients).reduce(t.intercept) { $0 + $1.0 * $1.1 }
        }
        let b = value("bedtime_residual_min"), w = value("wake_residual_min")
        guard b.isFinite, w.isFinite else { throw ModelError.invalid("모델 예측값이 유한하지 않습니다.") }
        return (b, w)
    }
}

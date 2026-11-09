"""Read existing local artifacts; never upload personal records or alter source files."""
from pathlib import Path
from datetime import datetime, timedelta
import importlib.util
import json
import math

from engine import ZONE, parse_date, clock_min, clock_mean
from forecast_model import ForecastModelError, PortableForecastModel, completed_history_features


def load_data_readiness(path: Path | None) -> dict:
    unavailable = {"available": False, "status": "not_generated", "reason": "MESA 집계 점검 파일이 없습니다."}
    if path is None or not path.exists():
        return unavailable
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("schema_version=1이 필요합니다")
        scope, inventory = raw["scope"], raw["inventory"]
        headers, qc, interpretation = raw["headers"], raw["semantic_qc"], raw["interpretation"]
        if not all(isinstance(item, dict) for item in (scope, inventory, headers, qc, interpretation)):
            raise ValueError("집계 섹션 형식이 잘못되었습니다")

        def integer(value, label):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label}은 0 이상의 정수여야 합니다")
            return value

        def fraction(value, label):
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= value <= 1:
                raise ValueError(f"{label}은 0~1 숫자여야 합니다")
            return float(value)

        modalities = inventory.get("modalities")
        if not isinstance(modalities, dict):
            raise ValueError("inventory.modalities가 필요합니다")
        modality_counts = {}
        for name in ("actigraphy", "heart_rate", "steps", "psg_edf", "psg_annotation"):
            section = modalities.get(name, {})
            if not isinstance(section, dict):
                raise ValueError(f"modalities.{name} 형식이 잘못되었습니다")
            modality_counts[name] = integer(section.get("files", 0), f"modalities.{name}.files")

        variants = headers.get("variants")
        if not isinstance(variants, list) or any(not isinstance(item, dict) for item in variants):
            raise ValueError("headers.variants 배열이 필요합니다")
        allowed_columns = {"mesaid", "line", "linetime", "offwrist", "activity", "marker", "whitelight",
                           "redlight", "greenlight", "bluelight", "wake", "interval", "dayofweek",
                           "daybymidnight", "daybynoon", "date_shifted", "actigraphy_month_start"}
        header_variants = []
        observed_columns = set()
        unreadable_files = 0
        schema_mismatch_files = 0
        for item in variants[:20]:
            columns = item.get("columns")
            files = integer(item.get("files"), "headers.variants.files")
            if columns is None:
                unreadable_files += files
                header_variants.append({"columns": None, "files": files, "unknown_columns": 0})
                continue
            if not isinstance(columns, list) or any(not isinstance(column, str) for column in columns):
                raise ValueError("headers.variants.columns는 문자열 배열 또는 null이어야 합니다")
            known = [column for column in columns if column in allowed_columns]
            unknown_count = len(columns) - len(known)
            observed_columns.update(known)
            if set(known) != allowed_columns or unknown_count:
                schema_mismatch_files += files
            # Unknown names may contain source-specific or sensitive labels. Expose only their count.
            header_variants.append({"columns": known, "files": files, "unknown_columns": unknown_count})

        sampling = qc.get("sampling", {})
        categories = qc.get("categories", {})
        if not isinstance(sampling, dict) or not isinstance(categories, dict):
            raise ValueError("semantic_qc 집계 형식이 잘못되었습니다")
        wake_categories = categories.get("wake", {})
        wake_total = sum(integer(value, "categories.wake") for value in wake_categories.values()) if isinstance(wake_categories, dict) else 0
        wake_labeled = sum(integer(wake_categories.get(key, 0), "categories.wake.labeled")
                           for key in ("0", "1")) if isinstance(wake_categories, dict) else 0
        has_wake = "wake" in observed_columns and wake_labeled > 0
        psg_ready = interpretation.get("psg_supervised_stage_training_ready") is True
        active = scope.get("active_ingestion_detected") is True
        ending_file_count = integer(scope.get("ending_file_count"), "scope.ending_file_count")
        snapshot_file_count = integer(scope.get("snapshot_file_count", ending_file_count), "scope.snapshot_file_count")
        actigraphy_files = modality_counts["actigraphy"]
        usable = []
        if actigraphy_files and has_wake:
            usable = ["subject-held-out sleep/wake baseline", "sleep timing population prior"]
        blocked = []
        if modality_counts["heart_rate"] == 0:
            blocked.append("heart-rate feature model")
        if not psg_ready:
            blocked.append("PSG-supervised 5-stage sleep model")
        blocked.append("student app personalization without external validation")
        return {
            "available": True,
            "status": "ingestion_active" if active else "inventory_complete",
            "reason": "업로드 중 스냅샷입니다." if active else "집계 점검이 완료된 스냅샷입니다.",
            "generated_at": scope.get("completed_at_utc") or scope.get("discovered_at_utc"),
            "file_count": snapshot_file_count,
            "ending_file_count": ending_file_count,
            "active_ingestion": active,
            "integrity_claim": str(scope.get("integrity_claim", ""))[:300],
            "total_bytes": integer(inventory.get("total_bytes_at_discovery"), "inventory.total_bytes_at_discovery"),
            "modalities": modality_counts,
            "headers": {"eligible_csv_files": integer(headers.get("eligible_csv_files"), "headers.eligible_csv_files"),
                        "scanned_files": integer(headers.get("scanned_files"), "headers.scanned_files"),
                        "truncated": headers.get("truncated") is True, "unreadable_files": unreadable_files,
                        "schema_mismatch_files": schema_mismatch_files, "variants": header_variants},
            "quality": {"sampled_files": integer(sampling.get("stable_complete_files_in_aggregate", 0), "sampling.stable_complete_files_in_aggregate"),
                        "rows": integer(qc.get("rows", 0), "semantic_qc.rows"),
                        "labeled_wake_fraction": (wake_labeled / wake_total) if wake_total else None,
                        "offwrist_fraction": fraction(qc.get("offwrist_flagged_fraction"), "offwrist_flagged_fraction"),
                        "thirty_second_cadence_fraction": fraction(qc.get("thirty_second_cadence_fraction"), "thirty_second_cadence_fraction")},
            "usable_for": usable,
            "blocked_for": blocked,
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return {"available": False, "status": "invalid_inventory", "reason": f"MESA 집계 점검 파일을 사용할 수 없습니다: {error}"}


class ResearchBridge:
    def __init__(self, workspace: Path, forecast_model: Path | None = None, mesa_inventory: Path | None = None):
        self.summary = {"available": False, "warnings": []}
        self.records = []
        self.metrics = None
        self.models = {}
        self.portable_model = None
        self.portable_error = None
        self.data_readiness = load_data_readiness(mesa_inventory)
        if forecast_model is not None:
            try:
                self.portable_model = PortableForecastModel.load(forecast_model)
            except ForecastModelError as error:
                self.portable_error = str(error)
                self.summary["warnings"].append(f"휴대형 행동 예측 모델 사용 불가: {error}")
        try:
            import pandas as pd
            base = workspace / "260925" / "한서윤데이터_전처리" / "output"
            # macOS may store Korean filenames in decomposed Unicode.
            if not base.exists():
                import unicodedata
                base = next(p for p in workspace.glob("260925/*/output") if unicodedata.normalize("NFC", p.parent.name) == "한서윤데이터_전처리")
            episodes = pd.read_parquet(base / "sleep_episodes.parquet")
            valid = episodes[episodes.is_main_sleep & episodes.episode_qc_pass].copy()
            daily = pd.read_parquet(base / "longitudinal_daily.parquet")
            step_days = daily.loc[daily.day_steps.notna() & (daily.step_day_coverage >= 0.8), "day_steps"]
            def number(value):
                return float(value) if pd.notna(value) and math.isfinite(float(value)) else None
            for _, r in valid.iterrows():
                bed, wake = parse_date(str(r.sleep_start)), parse_date(str(r.sleep_end))
                actual = number(r.get("reported_sleep_min"))
                interval = (wake - bed).total_seconds() / 60
                if actual is None or actual <= 0 or actual > interval:
                    actual = max(0, interval - (number(r.get("awake_stage_min")) or 0))
                self.records.append({"date": wake.date().isoformat(), "bed": bed.isoformat(), "wake": wake.isoformat(),
                    "sleep_min": actual, "latency_min": number(r.get("sleep_latency_min")), "awake_min": number(r.get("awake_stage_min")),
                    "fatigue": None, "rested": None, "steps": None, "exercise_min": None, "source": "archive",
                    "note": "본인 Samsung Health 과거 자료 · 주 수면/QC 통과", "observed_at": None})
            # One principal episode per wake date, chosen by interval duration.
            unique = {}
            for record in self.records:
                if record["date"] not in unique or record["sleep_min"] > unique[record["date"]]["sleep_min"]:
                    unique[record["date"]] = record
            self.records = sorted(unique.values(), key=lambda r: r["wake"])
            recent = self.records[-30:]
            phase = clock_mean([clock_min(parse_date(r["bed"]) + timedelta(seconds=(parse_date(r["wake"]) - parse_date(r["bed"])).total_seconds()/2)) for r in recent])
            self.summary.update({"available": True, "sleep_nights": len(self.records), "recent_phase_min": phase,
                "average_sleep_h": float(sum(r["sleep_min"] for r in self.records) / len(self.records) / 60),
                "average_steps": float(step_days.mean()) if len(step_days) else None,
                "step_days": len(step_days), "first_date": min(r["date"] for r in self.records), "last_date": max(r["date"] for r in self.records),
                "population": "본인 1명 · 학생 집단 평균 아님"})
            model_dir = next(p for p in workspace.iterdir() if p.is_dir() and __import__('unicodedata').normalize("NFC", p.name) == "미래예측모델")
            self.metrics = json.loads((model_dir / "output/validation_report.json").read_text())
            spec = importlib.util.spec_from_file_location("existing_forecast", model_dir / "causal_sleep_timing_forecast.py")
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            frame = pd.read_parquet(model_dir / "output/causal_daily_features.parquet")
            development = frame[frame["split"].isin(["train", "validation"])]
            self.feature_names = self.metrics["features"]
            for target in ["bedtime", "wake"]:
                chosen = self.metrics[target]["selected_model_by_validation_mae"]
                fitted = module.candidate_models()[chosen]
                fitted.fit(development[self.feature_names], development[target + "_hours"])
                self.models[target] = fitted
            self.summary["forecast_loaded"] = True
        except Exception as error:
            self.summary["warnings"].append(f"일부 연구 자료 연결 실패: {error}")

    def model_status(self, profile=None, records=None, now=None):
        portable_reason = None
        if self.portable_model is not None:
            if profile is None:
                return {"available": False, "status": "waiting_for_profile", "reason": "프로필 적용 범위와 과거 기록을 확인해야 합니다.",
                        "model_version": self.portable_model.model_version, "source": "portable"}
            eligible, reason = self.portable_model.population.eligibility(profile)
            if eligible:
                current = now or datetime.now(ZONE)
                cutoff = current.replace(hour=20, minute=0, second=0, microsecond=0)
                if current < cutoff:
                    return {"available": False, "status": "waiting_for_decision",
                            "reason": "행동 예측 기준 시각인 20:00 이후에 계산합니다.",
                            "model_version": self.portable_model.model_version, "source": "portable"}
                portable = self.portable_model.predict(profile, records or [], cutoff)
                if portable["available"]:
                    return {"available": True, "status": "ready", "reason": reason,
                            "model_version": self.portable_model.model_version, "source": "portable"}
                portable_reason = portable["reason"]
                portable_status = "waiting_for_history" if "기록" in portable_reason else "prediction_refused"
            # A valid portable artifact must not be applied outside its population.
            # The existing legacy path may still be usable for the linked owner.
            else:
                portable_reason = reason
                portable_status = "ineligible_population"
        if len(self.models) == 2:
            if profile is not None and profile.get("link_history"):
                reason = "기존 로컬 개발구간 모델을 사용할 수 있습니다."
                if portable_reason:
                    reason = f"휴대형 모델은 적용 불가({portable_reason})하여 {reason}"
                elif self.portable_error:
                    reason = f"휴대형 모델 검증 실패({self.portable_error})로 {reason}"
                return {"available": True, "status": "legacy_ready", "reason": reason,
                        "model_version": "legacy-local-development", "source": "legacy"}
            if portable_reason:
                return {"available": False, "status": portable_status, "reason": portable_reason,
                        "model_version": self.portable_model.model_version, "source": "portable"}
            return {"available": False, "status": "waiting_for_linked_history",
                    "reason": "기존 본인 자료 연결 시 로컬 개발구간 모델을 사용할 수 있습니다.",
                    "model_version": "legacy-local-development", "source": "legacy"}
        if portable_reason:
            return {"available": False, "status": portable_status, "reason": portable_reason,
                    "model_version": self.portable_model.model_version, "source": "portable"}
        reason = f"휴대형 모델 검증 실패: {self.portable_error}" if self.portable_error else "학습 완료 행동 예측 모델이 없습니다."
        return {"available": False, "status": "unavailable", "reason": reason,
                "model_version": None, "source": None}

    def behavioral_forecast(self, profile, records, now):
        """Auxiliary 20:00 forecast from frozen development data; never schedule objective."""
        cutoff = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now < cutoff:
            return {"available": False, "reason": "기존 행동 예측 모델의 기준 시각은 20:00입니다. 수면 개선 추천은 지금도 제공됩니다."}
        if self.portable_model is not None:
            portable = self.portable_model.predict(profile, records, cutoff)
            if portable["available"]:
                return portable
        if not profile.get("link_history") or len(self.models) != 2:
            reason = self.model_status(profile, records, now)["reason"]
            return {"available": False, "reason": reason}
        import pandas as pd
        import numpy as np
        prior = [r for r in records if parse_date(r["wake"]) < cutoff and parse_date(r["bed"]) < cutoff]
        prior.sort(key=lambda r: r["wake"])
        prior = prior[-7:]
        f = {key: np.nan for key in self.feature_names}
        f.update({"weekday": cutoff.weekday(), "weekend": int(cutoff.weekday() >= 5),
                  "day_hr_n_pre20": 0, "late_hr_n_pre20": 0, "step_bins_pre20": 0})
        if prior:
            # Match existing training code: offsets relative to the evening cutoff, not clock minutes.
            bed = [(clock_min(parse_date(r["bed"])) - 1200) % 1440 / 60 for r in prior]
            wake = [b + (parse_date(r["wake"]) - parse_date(r["bed"])).total_seconds() / 3600 for b, r in zip(bed, prior)]
            f.update({"prior_bedtime_median_7": float(np.median(bed)), "prior_wake_median_7": float(np.median(wake)),
                      "prior_duration_median_7": float(np.median([(parse_date(r["wake"]) - parse_date(r["bed"])).total_seconds() / 60 for r in prior])),
                      "previous_sleep_duration_h": (parse_date(prior[-1]["wake"]) - parse_date(prior[-1]["bed"])).total_seconds() / 3600})
        row = pd.DataFrame([f], columns=self.feature_names)
        values = {target: float(model.predict(row)[0]) for target, model in self.models.items()}
        return {"available": True, "cutoff": cutoff.isoformat(),
                "bed": (cutoff + timedelta(hours=values["bedtime"])).isoformat(),
                "wake": (cutoff + timedelta(hours=values["wake"])).isoformat(),
                "role": "과거 행동패턴 참고 · 권장 시각 계산에는 미사용",
                "warning": "현재 웹 기록에는 시간별 센서 입력이 없어 해당 특징을 학습 중앙값으로 대치합니다. 기존 테스트 성능이 그대로 적용되지 않습니다."}

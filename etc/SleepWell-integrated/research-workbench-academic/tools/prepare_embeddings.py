"""Download one pinned multilingual embedding model, then verify local-only use."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from researchbench.evidence_library import EvidenceLibrary  # noqa: E402


MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
MODEL_LICENSE = "Apache-2.0"
DEFAULT_OUTPUT = ROOT / ".models" / "paraphrase-multilingual-MiniLM-L12-v2"
ACCEPTANCE = {"recall_at_3_min": 0.8, "mrr_min": 0.8, "no_evidence_accuracy_min": 1.0}


def directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        if item.name in {"sleepwell_model_manifest.json", "sleepwell_embedding_evaluation.json"}:
            continue
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def download_model(output: Path) -> dict:
    from huggingface_hub import snapshot_download

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".downloading")
    if temporary.exists():
        shutil.rmtree(temporary)
    snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=temporary,
        ignore_patterns=["onnx/*", "openvino/*", "*.h5", "pytorch_model.bin"],
    )
    from sentence_transformers import SentenceTransformer
    SentenceTransformer(str(temporary), local_files_only=True, trust_remote_code=False, device="cpu")
    model_hash = directory_sha256(temporary)
    manifest = {
        "model_id": MODEL_ID, "revision": MODEL_REVISION, "license": MODEL_LICENSE,
        "directory_sha256": model_hash, "local_files_only_verified": True,
        "excluded_optional_formats": ["onnx", "openvino", "tensorflow", "pytorch_model.bin"],
    }
    (temporary / "sleepwell_model_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if output.exists():
        shutil.rmtree(output)
    temporary.replace(output)
    return manifest


def verify_model(output: Path) -> dict:
    manifest_path = output / "sleepwell_model_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("embedding_model_manifest_not_found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model_id") != MODEL_ID or manifest.get("revision") != MODEL_REVISION:
        raise ValueError("embedding_model_identity_mismatch")
    actual = directory_sha256(output)
    if actual != manifest.get("directory_sha256"):
        raise ValueError("embedding_model_hash_mismatch")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(str(output), local_files_only=True, trust_remote_code=False, device="cpu")
    vectors = model.encode(["반복 측정 평가", "repeated-measures evaluation"], normalize_embeddings=True)
    return {**manifest, "directory_sha256": actual, "bilingual_vector_shape": list(vectors.shape)}


def evaluate_fixture(output: Path, fixture_path: Path, split: str = "test", top_k: int = 3) -> dict:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "embedding-evaluation.swa"
        project.mkdir()
        source_map = {}
        with EvidenceLibrary(project) as library:
            for document in fixture["documents"]:
                source = library.import_document(fixture_path.parent / document["file"], document.get("metadata"))
                source_map[document["key"]] = source["source_id"]
            questions = [question for question in fixture["questions"] if question["split"] == split]
            recalls, reciprocal = [], []
            no_evidence_total = no_evidence_correct = 0
            cases = []
            for question in questions:
                result = library.search(question["query"], lang=question.get("language"), top_k=top_k,
                                        embedding_model_path=output)
                returned = [(hit["source_id"], hit["locator"]) for hit in result["hits"]]
                gold = [(source_map[item["source"]], item["locator"])
                        for item in question.get("expected_anchors", [])]
                if gold:
                    found = [item for item in gold if item in returned]
                    ranks = [returned.index(item) + 1 for item in gold if item in returned]
                    recalls.append(len(found) / len(gold))
                    reciprocal.append(1 / min(ranks) if ranks else 0.0)
                else:
                    no_evidence_total += 1
                    no_evidence_correct += int(result["status"] == "no_evidence")
                cases.append({"id": question["id"], "status": result["status"],
                              "returned_anchors": returned, "gold_anchors": gold})
    metrics = {
        "split": split, "top_k": top_k, "denominator": len(questions),
        "answerable_denominator": len(recalls), "no_evidence_denominator": no_evidence_total,
        "recall_at_3": sum(recalls) / len(recalls) if recalls else None,
        "mrr": sum(reciprocal) / len(reciprocal) if reciprocal else None,
        "no_evidence_accuracy": no_evidence_correct / no_evidence_total if no_evidence_total else None,
        "acceptance_preregistered": ACCEPTANCE, "holdout_used_for_tuning": False,
        "semantic_support_review": "not_performed", "cases": cases,
    }
    metrics["accepted"] = (
        metrics["recall_at_3"] >= ACCEPTANCE["recall_at_3_min"]
        and metrics["mrr"] >= ACCEPTANCE["mrr_min"]
        and metrics["no_evidence_accuracy"] >= ACCEPTANCE["no_evidence_accuracy_min"]
    )
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--fixture", type=Path, default=ROOT / "evaluation" / "rag_fixture_v1.json")
    parser.add_argument("--skip-evaluation", action="store_true")
    arguments = parser.parse_args()
    output = arguments.output.expanduser().resolve()
    manifest = verify_model(output) if arguments.verify_only else download_model(output)
    result = {"model": manifest, "output": str(output)}
    if not arguments.skip_evaluation:
        result["evaluation"] = evaluate_fixture(output, arguments.fixture.resolve())
        (output / "sleepwell_embedding_evaluation.json").write_text(
            json.dumps(result["evaluation"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("evaluation", {"accepted": True})["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

# TRIPOD+AI and PROBAST+AI audit service

`GuidelineAuditService` creates an immutable, project-local assessment under
`research/guideline_audits/`. It keeps two different questions separate:

- TRIPOD+AI records whether the prediction-model study has been reported completely.
- PROBAST+AI records signalling answers, development quality, evaluation risk of bias,
  and applicability judgments.

The service does not calculate a compliance percentage and does not issue certification.
A checklist status is not evidence that a method has low risk of bias. Domain judgments
remain explicit human judgments with a reviewer, time, rationale, and immutable parent
revision.

## Quick start

```python
from researchbench.guideline_audit import GuidelineAuditService

service = GuidelineAuditService(project.path)

# A generic project starts with TRIPOD items pending and PROBAST answers NI.
audit = service.create(language="ko", title="Model evidence assessment")

# The SleepWell template is an explicit opt-in reviewed seed. It fails closed if the
# bundled 52-item/34-question evidence snapshot is absent or incomplete.
audit = service.create(
    evaluation_path="deliverables/gradual_v1_20261103/evaluation.json",
    language="ko",
    title="SleepWell gradual_v1 evidence assessment",
    template="sleepwell_fixed",
)

revised = service.revise(
    audit["audit_id"],
    {
        "tripod": {
            "23a": {
                "status": "partially_satisfied",
                "note": "Performance, uncertainty and denominators reviewed.",
                "evidence_ids": ["GRADUAL_V1_EVALUATION"],
            }
        },
        "probast": {
            "evaluation:4.5": {
                "answer": "PN",
                "rationale": "The reassessment reuses previously exposed test data.",
            }
        },
        "domains": {
            "evaluation:4": {
                "judgment": "High",
                "rationale": "Adaptive test reuse limits evaluation independence.",
            }
        },
        "reason": "Human evidence review",
    },
    assessed_by="Reviewer name",
)

paths = service.export(revised["audit_id"])
```

`list()`, `get(audit_id)`, and `check(audit_id)` support the GUI. `export()` creates an
immutable `<destination>/<audit_id>/` package and refuses to overwrite it. The package
contains full JSON, a concise human-readable Markdown/DOCX/PDF report, TRIPOD+AI and
PROBAST+AI matrices, metrics/diagnostics/paired-difference CSV files, and a SHA-256
manifest.

## Evaluation import contract

An attached evaluation must be an aggregate JSON object with:

- `schema: "sleepwell.gradual_evaluation.v1"`
- `policy: "gradual_v1"`
- `status: "retrospective_reused_holdout"`

The source bytes and canonical snapshot receive separate hashes. The immutable snapshot
remains usable if the original file later moves; `check()` warns when the source is missing
or changed. Keys shaped like raw records, rows, or participant/patient/subject identifiers
are rejected. Aggregate denominators such as `n_participants` remain allowed.

Importing the result adds its evidence ID to TRIPOD+AI 23a and 24 and PROBAST+AI
evaluation question 4.7. These items remain partial where new independent validation,
subgroup evidence, or other reporting is still missing. A reused holdout never becomes
external or prospective validation through import.

The human report summarizes participant-weighted reused-test onset, wake and joint MAE
with participant-cluster confidence intervals, paired policy differences, residual and
calibration diagnostics, exploratory interval results, cohort flow, failures, subgroup
coverage and limitations. It does not dump all nested machine records into the document;
the complete aggregate stays in JSON and dedicated CSV files.

## Evidence attachments

`attach_document()` copies an evidence file into the project evidence store by content
hash and creates a new audit revision. It can link the new evidence ID to selected items:

```python
revised = service.attach_document(
    audit_id,
    "methods_report.pdf",
    "Consolidated methods report",
    assessed_by="Reviewer name",
    item_links={"tripod": ["7", "12b", "18f"], "probast": ["development:2.2"]},
)
```

Original audit JSON files and evidence snapshots are not edited in place. Attachment
hashes are checked every time the revised audit is loaded.

## Judgment safeguards

TRIPOD+AI statuses are `satisfied`, `partially_satisfied`, `not_reported`, `unknown`,
`not_applicable`, and `pending`. PROBAST+AI answers are `Y`, `PY`, `PN`, `N`, `NI`, and
`NA`. `NA` is accepted only for development 4.4 and evaluation 4.4, 4.5, and 4.6.

Domain judgments are `Low`, `High`, or `Unclear`. A Low domain containing `NI`, `PN`, or
`N` answers generates a warning instead of silently passing. Overall development,
evaluation and new-population applicability judgments are derived by the official rule:
any High gives High; otherwise any Unclear gives Unclear; all Low gives Low. SleepWell's reused-test
evaluation domain 4 is seeded High with a rationale. Applicability to the same operational
endpoint remains separate from the high-concern scenario of new Korean app users, live
device/diary predictors, and diary or physiologic outcomes.

The report separates work possible with existing data and code from work requiring new
participants or data, and from author/governance declarations. Ethics approval, consent,
funding, conflicts, public involvement, and registration are never inferred from missing
records.

## Sources

- TRIPOD+AI statement: https://www.bmj.com/content/385/bmj-2023-078378
- TRIPOD+AI expanded checklist: https://www.tripod-statement.org/wp-content/uploads/2024/04/TRIPODAI-Supplement.pdf
- PROBAST+AI statement: https://www.bmj.com/content/388/bmj-2024-082505
- PROBAST+AI assessment tool: https://www.probast.org/probast_ai/downloads/
- Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026).
  *Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents* (v2).
  https://doi.org/10.48550/arXiv.2609.00065

The bundled short labels are navigation paraphrases. Use the official source documents
for the full wording and instructions.

## Official guidelines library and printable PDFs (2026-11-04)

Open **TRIPOD·PROBAST → 공식 지침 자료실 · PDF**. The library contains nine
unchanged PDFs: the TRIPOD+AI checklist, expanded checklist/writing guidance,
statement and abstract checklist; the PROBAST+AI tool, E&E Light and statement;
the DECIDE-AI BMJ statement; and DiMe's 2024 V3+ use-specification guide.
The V3+ 2025 journal article is linked separately, not confused with the 2024 guide.

- **원문 PDF 열기·인쇄** opens a verified original in the default viewer. Use Ctrl+P there.
- **원문 PDF 저장** saves the original bytes; **원문 9개 전체 저장** also writes a
  source/integrity manifest in a new output folder.
- **공식 사이트에서 다시 받기** attempts the official PDF URL. A failed download,
  changed SHA-256 or non-PDF response is reported; a verified stored copy is used
  when available. The library never calls an HTML error page a PDF download.
- **SleepWell 적용 안내 PDF 저장** generates a separate Korean or English application
  guide with scope, references, versions and hashes. It is not an official original.
- **기준별 점검·보고서 → 선택 점검 PDF 저장** exports the selected immutable assessment
  directly to a printable PDF. The existing full report package remains available.

Bundled sources and provenance are recorded in `resources/guideline_library.json`.
Originals retain their attribution and license notices. Noncommercial and other
source-specific terms continue to apply; inclusion does not grant a new license.
DECIDE-AI and V3+ are supplemental reference frameworks, not automatically scored
assessments or fulfilled criteria.

The current project constraint is **external existing data only**. No participant,
diary or wearable collection is being started. An independent external dataset can
support retrospective external validation if its population, inputs, outcomes and
access conditions fit the frozen model. Existing CMI/MESA reanalysis and chronological
replay do not become independent external or genuine prospective validation. Live
usability, clinical safety and unobserved sensor accuracy still require separate evidence.
Previously prepared collection protocols remain historical drafts, not active recruitment.

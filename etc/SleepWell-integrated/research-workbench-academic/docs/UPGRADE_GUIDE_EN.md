# Upgrade Feature Guide

## Install and launch

The supported baseline is 64-bit Windows 10/11, 64-bit Python 3.12, and CPU execution. Run `install.cmd` in this folder to create the project-local environment, then start `launch.cmd` or the **SleepWell Academic Workbench** desktop shortcut.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Recreate
.\.venv\Scripts\python.exe -m researchbench --project ".\deliverables\upgrade_examples\demo.swa"
```

Run `diagnose_windows.cmd` for installation diagnostics. GPU execution is not supported or validated.

## Workspaces

- **SleepWell research** runs the preserved mathematical, data, and fusion models and retrospective, personalization, and prospective workflows.
- **General tabular data** opens the existing native `Toplevel` classification/regression application. Its project can be linked read-only using **Link general project** in the evidence/report workspace.
- **Evidence and reports** consume completed runs from both engines without changing their original storage or semantics.

## Register and search evidence

1. In **Evidence**, select a PDF, DOCX, Markdown, or text file.
2. Confirm title, author/organization, version, domain, and rights. A URL/DOI-only record remains metadata-only.
3. Check extraction status and source hash. An image-only PDF is marked as requiring OCR; automatic OCR is not supplied.
4. Enter a query and language/domain filters. Default search is lexical, and its score is relevance—not a probability of truth.
5. Inspect the short excerpt and page/section/paragraph locator before attaching a result. Preserve the insufficient-evidence state when no result answers the question.

Local embeddings are an optional separate installation via `install_optional_ai.cmd` and `tools\prepare_embeddings.py`. The fixed evaluation failed its no-evidence acceptance criterion, so hybrid search remains experimental and is not the default.

## Create a report

1. Choose a research report, industrial validation report, model card, or experiment comparison memo.
2. Select one or more actual completed runs. Do not combine different tasks, targets, or evaluation populations into a single ranking.
3. Enter research domain, audience, intended use, and acceptance criteria. Explicitly record whether the criteria were predeclared.
4. Enable the medical-prediction option only for a medical prediction study. It opens a pending TRIPOD+AI applicability review; it does not establish certification or fabricate a completed checklist.
5. Select literature search hits and create the draft.
6. Review the section/claim tree, editable prose, exact experiment values, source locators, and check results.

Numeric claims retain exact run field paths, units, and denominators. An edit creates a new immutable report version and requires rechecking. A named human review record is separate from automatic checks.

## Check and export

- Numeric mismatch, unsupported extra numbers, changed run hashes, and fabricated, changed, or revoked citations are blocking errors.
- Semantic support requires human review. An automatic check is not expert validation, regulatory compliance, or certification.
- A draft may be exported with a visible warning. A checked export requires no blocking errors.
- Export formats include Markdown, DOCX, complete-report PDF, audit JSON, BibTeX, CSV, and figures when real predictions exist.
- Translation creates a new report version from the same evidence and never overwrites user-edited prose.

## Interpretation limits

- Everything under `deliverables\upgrade_examples` is synthetic or a licensed fixture and demonstrates software behavior only.
- A stored historical validation table is not a new training run or external reassessment.
- External evaluation requires a final frozen model/preprocessing/schema and a separate dataset that was not used for model selection.
- Repeated-measure rows and independent participant counts remain distinct. Uncertainty or subgroup estimates are not invented when raw predictions or an adequate independent unit are unavailable.
- Nested cross-validation, GPU execution, real external-LLM calls, local generative narration, and OCR are not claimed as completed features.


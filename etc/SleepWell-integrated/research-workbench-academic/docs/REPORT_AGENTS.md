# Report-agent harness

`ReportAgentHarness` adds an optional, evidence-linked interpretation to an
already verified `model_comparison` report. It does not replace or edit the
deterministic metrics, sections, claims, or TRIPOD+AI/PROBAST+AI checklist.

```python
analysis = ReportAgentHarness(project_path, provider_client).run(base_report)
derived_report["ai_analysis"] = analysis
errors = validate_ai_analysis(report_service, derived_report)
```

The provider interface is deliberately small:

```python
client.complete(messages, *, role, max_output_tokens=None, timeout_seconds=None)
# -> {content, provider, model, usage, request_id, actual_llm}
```

Only aggregate comparison claims, checklist items, and registered evidence
chunks are sent. Raw observations, predictions, participant identifiers,
credentials, and local paths are excluded. Agents can use only three local,
read-only tools: `read_metrics`, `search_guidelines`, and `get_evidence`.
There is no shell, filesystem, or general-web tool.

Numeric prose must use `{{claim:claim_id}}`; Python expands it from the
canonical report claim. Unknown claims/models/sources, unbound numbers,
fabricated uncertainty or significance, clinical-superiority language, and
claims of completed external validation are rejected. Every run is bounded by
call, tool, timeout, output, and optional cost limits and writes a hashed audit
trace under `research/agent_runs/`.

A client response with `actual_llm=False` is always marked
`test_only=True`. This supports fixtures and offline demonstrations without
presenting them as a real API success. Even a successful real run retains the
explicit warnings that semantic support and final PROBAST+AI judgments require
human review.

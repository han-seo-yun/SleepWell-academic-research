# Invalid export — do not use for model accuracy or deployment

The archived `tabdpt_ctx1024_*_fp32_preprocessed` graphs and their Validation
predictions are invalid as TabDPT-v1.3 candidates.  The original
`PreprocessedRegressionForward` omitted the checkpoint's 64 learned thinking
rows, the corresponding zero target embeddings, and the adjusted attention and
head indices.  This changed the network structure and caused the poor archived
Validation result.

No Test prediction completed under this invalid graph.  These files are kept
only for auditability and must not be reported as TabDPT mobile accuracy.

The corrected method identity is
`tabdpt13_mobile_fp32_external_preprocess_think64_v2`.  Its independent gate
calls the installed native `model.forward` with only preprocessing replaced by
identity and verifies exact equality before patching attention.

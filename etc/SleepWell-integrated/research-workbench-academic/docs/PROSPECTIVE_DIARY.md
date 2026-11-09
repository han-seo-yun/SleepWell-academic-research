# Wearable-free prospective diary study

Open **Prospective evaluation → Diary study**. This adds a study coordinator above the unchanged participant forecast journals; no vendor model, coefficient, cutoff or original dataset is edited.

- Main design: 28 nights, first 7 adaptation, next 21 primary. Two frozen data arms, participant-equal joint MAE, 97.5% model-specific intervals. The draft planning target is 500 enrolled; a 280-person wider-precision alternative must be fixed before recruitment.
- Feasibility: a separate 14-night study with separate people and files, never pooled into validation.
- Real enrollment requires recorded governance fields and actual consent. A populated field does not itself verify institutional approval. Missing facts remain a draft.
- The participant form is a self-reported sleep diary, not a physiological reference or treatment. It hides all predictions. The server records receipt time; clients cannot supply a receipt or forecast timestamp.
- Forecast capture uses real KST 20:00–20:15. The running server attempts every 30 seconds. It cannot recover forecasts missed while offline. Historical diary input is filtered by original receipt time.
- Late, missing, revised, out-of-contract and withdrawn records have explicit handling. The first eligible diary is not replaced by a more favorable revision. Transient assessment failures can be reconciled using the original receipt.
- Source/model/protocol hashes are frozen. Changed study software requires a new study version; existing studies fail closed rather than silently mixing versions.
- Aggregate exports include primary, initial, whole-period, completeness and late-receipt analyses. They carry counts, uncertainty limitations, source hashes, and synthetic/feasibility labels. They are evidence drafts, not TRIPOD/PROBAST certification.

Run the receiver from this app directory:

```powershell
.\.venv\Scripts\python.exe -m researchbench.diary_server --study '<study folder>'
```

Default `127.0.0.1:8767` is only accessible from this PC. Non-loopback listening requires `--certfile` and `--keyfile`; institutional hosting, certificate trust, firewall, continuous availability and access management remain deployment work. No messages, recruiting invitations or data uploads are sent by setup.

The protocol, sample-size calculator, 56-row remaining-item map, dictionary, screening template and consent draft are in `../../reports/prospective_validation_20261103/`. The participant ID/contact linkage stays outside the analytical database.

The local SHA256 manifest is a reproducibility check, not a cryptographic proof against a machine administrator changing the system clock or database. Institutional server time, access control, monitoring and backup are needed for the actual study.

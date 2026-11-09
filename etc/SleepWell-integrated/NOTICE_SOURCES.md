# Integrated source provenance

- Model-layer base: <https://github.com/iuniion3287-web/SleepWell>, commit `91797e3c26415f1513b81b73bb181f4dcaeeba09`.
- Android collection/UI base: <https://github.com/soin7563-ops/---SleepWell>, commit `b95c4d93daf42cc0e8ecab9e562f57331ecaff69`.

The Android UI repository provides the `HealthDataSource` contract, fake source, 30-second export, chart, and Compose screen. Its `SamsungHealthSource` implementation is still a TODO in the upstream code. The integrated recommendation path is implemented in this repository without representing the fake source as real Watch data.

## Target-validity methodological reference (2026-10-25)

- Reaper2403, passive-sensing-target-validity: <https://github.com/Reaper2403/passive-sensing-target-validity>, reviewed commit `627b1006a95b698a0f9f74ff19d499a8a0ef5a60` (MIT).
- SleepWell independently implements baseline controls, within-person diagnostics, period-specific skill, feasibility gates, label sensitivity and observation-process audits. No GLOBEM raw records, pretrained weights or psychological-target results were imported. The repository's accompanying research-project report is not treated as peer-reviewed clinical evidence.
- Statistical workflow assistance: Kassis et al. (2026), <https://doi.org/10.48550/arXiv.2609.00065>. This acknowledges workflow assistance, not physiological or clinical validation of SleepWell.

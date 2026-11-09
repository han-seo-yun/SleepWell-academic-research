# CMI v2 build addendum

- Build-time builder SHA-256: `7BEB83289EF6C62CF73726F0EFEA474019CB8FC9A1B1FA09A61DF69C430BA9C7`.
- Current future-run builder SHA-256: `1331A384B3AC0FD817ED0FA401D0600E403E10540C82EC5133DB23F7B8735FA6`. It is intentionally different from the build-time hash: after v2 completed, the builder was hardened to (1) require raw `step:uint32` schema before a run, (2) snapshot each source before processing and fail if its post-processing provenance differs, and (3) reject non-finite, fractional, or negative source steps before integer conversion.
- Post-build source schema sweep: all 277 raw subject partitions declared `step: uint32`; therefore fractional and negative step values are not representable in the source files used by this build.
- The builder captured per-subject input and output hashes in `subject_qc_private.json` after each subject was processed. An independent post-build check found 269/269 current source hashes and 269/269 output hashes equal to their captured values.
- These hashes matched at the post-build check. They cannot exclude an intermediate source change or prove immutability before v2 read each source file, because v2 recorded its input hash after reading. The current builder has subsequently been strengthened for future runs to snapshot inputs before processing and compare after processing.
- Result scope remains research-only: onset/wakeup-derived labels, not PSG stages, and unknown source regions were excluded rather than assigned wake.

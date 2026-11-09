# Galaxy A52s offline hybrid accuracy experiment

This directory is an isolated research experiment. It does not replace the
production Android app or the current PC TabDPT service.

The protocol freezes six reduced-context TabDPT candidates before Validation
is scored. One setting is selected by equal-cohort participant-macro corrected
MAE on the existing person-disjoint Validation split. Only that setting is then
evaluated on the historically reused Test split. Current full-context TabDPT
and the previously selected HGB are immutable reference predictions.

Desktop GPU timings are exploratory and must not be described as Galaxy A52s
measurements. Deployment has no automatic accuracy threshold; the user reviews
the accuracy intervals together with real-device latency and memory.

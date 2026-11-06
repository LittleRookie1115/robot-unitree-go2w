# Contributing

This package lives in this Go2-W deployment repository. Submit source changes
here. Do not copy generated bindings, SDK binaries, model weights, logs or
credentials.

Run `PYTHONPATH=. python3 -m unittest discover -s tests -q`,
`bash build.sh`, `bash runtime_service/build.sh`, and the parent robot's offline
validation for daemon changes. Until robonix#276 lands, the current rbnx validator
rejects the multi-Driver root manifest; deployment uses the per-action manifests.
Validate the separate runtime package with `rbnx validate runtime_service`.
Default execution must remain motion-disabled. Preserve runtime stop/cancel,
command timeout, fresh state and single-controller behavior. Report physical
evidence separately from offline tests.

Use the responsible human's Git author and committer identity. Disclose
material AI assistance with `Assisted-by: AGENT_NAME:MODEL_VERSION`; do not
fabricate sign-offs, human reviews or physical testing trailers.

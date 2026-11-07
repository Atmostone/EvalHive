# EvalHive SWE-bench classic connector

This package targets the unmodified SWE-bench 5.0.2 evaluator at source revision
`02e7a74ffd0b707aab73d203fe87bdc7c76afc8e` and the classic task repository at
`3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`. The worker, private evaluation
service and installation tools support both Pi and OpenCode through the common
coordinator. Catalog discovery does not claim that every instance image is
installed or runnable.

Native `published_tasks` determines subset and split membership. Verified is
the default and contains 500 test cases. Lite publishes 300 test and 23 dev
cases; Full publishes 2,294 test and 225 dev cases. Verified does not publish a
dev split. Multilingual, multimodal and other SWE benchmark families are outside
this connector's identity.

`native.lock.json`, `native-code.lock.json` and `requirements.lock` pin the
official wheel, evaluator files, task repository and dependency closure. Native
imports require disabled dotenv. The local task repository is explicit; catalog
discovery neither consults Hugging Face credentials nor downloads datasets.
Receipts freeze complete membership and private file inventories. Agent-visible
descriptors contain the public problem, repository identity and base commit;
gold patches, test patches, grading scripts and expected tests remain private.

Instance image tags in native metadata are provenance, not immutable execution
grants. Runtime installation must resolve explicit image digests and retain
unavailable cases in the catalog with honest execution availability. Evaluation
must use a fresh environment and unique native run identity for every patch.
Cached native reports must never replace a new evaluation or changed-patch
regrade.

The worker receives public catalog metadata. Protected tasks and native grading
live in a separate World service. The agent container shares only its captured
patch with that service; it never receives test scripts, gold patches or grader
logs. A fresh evaluator uses the same pinned instance image and reads private
evaluation inputs from a read-only stage. Its log storage is writable only by
the evaluator and readable only by the private World. Large evidence files do
not pass through JSONL command frames.

Native images may commit environment setup and permission changes after the
dataset's source base commit. Installation records both commits. Preparation
checks the frozen image baseline, source ancestry and clean tracked state. Patch
capture compares against the installed image commit, preserving upstream setup.
Checkpoint metadata includes both commits, the case/image fingerprint and patch
digest. Regrading needs no agent or orchestrator model and creates fresh native
test evidence and a unique grading run directory.

Image bindings are explicit administrator input. Each entry provides the image
digest, source `base_commit`, inspected `image_commit`, separate `task_limits`
and `evaluator_limits`, and `reserved_paths_checked=true`. Limits contain
`memory_mb`, `cpu_cores` and `disk_mb`. Inspect the actual image before registering
it; the installer neither pulls images nor executes them. Uninstalled cases
remain in the catalog with `NATIVE_IMAGE_NOT_INSTALLED`. Changes to installed
image identities change the frozen case fingerprint.

Build the runtime with the verified offline wheel context, then publish a new
installation revision:

```bash
docker build --target runtime \
  --build-context runtime_wheels=/private/swebench-runtime-wheels \
  -f benchmarks/swebench/Dockerfile -t evalhive-benchmark-swebench:0.1.0 .
PYTHON_DOTENV_DISABLED=1 HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python -m evalhive_swebench.installation /private/swe-bench-tasks /new/package \
  --revision classic-local-v1 --subset verified --split test \
  --worker-image sha256:<worker-image-id> --image-bindings /private/images.json
```

The default engineering allocation is 256 MiB for the worker and 512 MiB for
the World. Task and evaluator limits come from inspected image bindings; they
are admitted together before execution. Installed images retain their native
testbed environment. Resource shortages, missing images and incomplete evidence
remain explicit failures, without fabricated verdicts or reduced membership.

Engineering acceptance exercises the original offline Verified case
`pytest-dev__pytest-10051` through both real harnesses and a local fake model.
The fixture adds a comment, captures its real patch and runs the original
evaluator in a fresh instance container. Execution succeeds while native
correctness remains false, as expected. Pi acceptance also restores the retained
patch into a new evaluation context without an agent or additional model calls;
the grading run ID changes, cached reports are not used, and the source trial
journal and verdict digest remain unchanged. Database downgrade/upgrade is
checked after each harness acceptance. This verifies integration, not model
performance or complete dataset execution.

Patch stdout is bounded independently from Git's internal repository writes.
A process-wide file-size limit would also constrain rootless overlay copy-up of
existing Git pack files. Capture retains its byte budget, deadline and process
group cancellation without imposing that unrelated limit on repository files.

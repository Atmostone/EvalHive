# EvalHive Cases

The native package for existing EvalHive YAML/JSON case files. The portable
format reader has no backend, database or native harness dependency. It retains
input, gold, environment, reproduction and extension metadata without rewriting
the committed dataset.

Agent input contains only the task title and description. Gold answers, rubric,
capability expectations, canonical trajectories and executable evaluation
configuration remain private native data. Public case browsing uses the SDK's
whitelisted projection rather than serializing that native data.

Cases are indexed by suite and identity with a digest of their original bytes.
Frozen descriptors fail resolution when installed source changes. Dataset roots
are supplied by the administrator; suite selection cannot escape that root.
Duplicate keys, executable YAML tags, aliases, links, oversized documents and
values incompatible with the JSON protocol are rejected explicitly.

The worker speaks the public Benchmark SDK JSONL protocol. Execution asks the
host-owned Harness session to run, reads the committed final assistant text and
exports the owned workspace. Reasoning and nested session messages are excluded.
Bounded answer checkpoints support evaluation-only restoration without another
agent call. Missing or oversized answers remain unknown. Native evaluation is
explicit: `reference_exact` uses whitespace/case normalization, and
`reference_fuzzy` uses the legacy sequence similarity rule. The default `deferred`
mode records an unavailable verdict; it never substitutes string matching for
semantic, rubric or pointwise quality evaluation. Aggregates retain unscored
population counts and do not turn missing grades into failures.

Cases needing executable Toolathlon evaluation require `toolathlon-gym`.
External services and document context bindings require a suitable environment
adapter; missing MCP resources fail validation before allocation or model use.
Legacy reproduction metadata remains in the native source. A newly selected
Harness profile controls execution; its model/seed selection does not claim an
exact replay of historical conditions.

Build from the repository root with `docker build -f
benchmarks/evalhive-cases/Dockerfile -t evalhive-benchmark-cases:0.1.0 .` using the
EvalHive rootless engine. Runtime dependencies and the base image are pinned.
The checked-in manifest is a build declaration. Registration must replace its
image tag with the resulting immutable digest.

An administrator can build an offline installation with:

```bash
python -m evalhive_cases.install --source /path/to/corpus --suite simple-qa \
  --revision revision-1 --worker-image 'registry/worker@sha256:<digest>' \
  --output /path/to/new-installation
```

The output contains a read-only native snapshot, an installation receipt and
coordinator registration metadata. An existing output is rejected. Later source
edits do not modify the snapshot; resolution rejects changed installed bytes.
Mount only `native` at `/dataset` in the worker, never in the agent environment.
Registered rootless asset `evalhive-cases.task` supplies the agent environment;
neither a case nor the worker can select host paths or override its image.
Deployment/runtime registration is tracked in SPA-145; copying source code alone
does not install a dataset/image or enable benchmark runs.

The versioned JSON schema is packaged with the reader. Development tooling can
refresh it from the legacy data-model declarations without importing backend
runtime code. Corpus compatibility tests use `CASE_CORPUS_PATH`; the package's
runtime has no dependency on the backend case store.

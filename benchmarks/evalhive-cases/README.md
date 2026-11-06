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

Runtime, installation and evaluator integration are tracked in SPA-145. This
source package alone does not install a dataset/image or enable benchmark runs.

The versioned JSON schema is packaged with the reader. Development tooling can
refresh it from the legacy data-model declarations without importing backend
runtime code. Corpus compatibility tests use `CASE_CORPUS_PATH`; the package's
runtime has no dependency on the backend case store.

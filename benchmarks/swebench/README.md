# EvalHive SWE-bench classic connector

This package targets the unmodified SWE-bench 5.0.2 evaluator at source revision
`02e7a74ffd0b707aab73d203fe87bdc7c76afc8e` and the classic task repository at
`3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`. Execution wiring is in progress;
catalog discovery does not claim that instance images are installed or runnable.

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

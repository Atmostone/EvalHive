# Installed benchmark packages

Each administrator-installed package has its own directory containing a Benchmark
SDK `manifest.json`. The backend discovers JSON only; executable code runs in the
registered worker image. Profiles cannot install packages, change entrypoints or
grant resource access. Environment-provider declarations use the same contract
with a different manifest kind.

Dataset installation is a server operation, separate from selecting a dataset in
the UI. The installer verifies native source/data bytes, resolves the worker
image to a digest and writes a `dataset_installation` receipt under the package's
`datasets/` directory. The filename is the Benchmark SDK `dataset_key(dataset_id,
revision)` followed by `.json`; neither external identity becomes a path.

The receipt records exact connector/dataset identities, a source digest, immutable
worker image, native case descriptors and private provenance. It does not assert
that credentials, runtime resources or native live acceptance are available.
Registering it into a workspace freezes the manifest, receipt fingerprint and
ordered case descriptors in PostgreSQL. Repeated registration is idempotent;
changing an existing identity's content is a conflict and requires a new revision.
PostgreSQL rejects updates to registered revisions and cases.

Case descriptors retained for execution may contain private native locators and
metadata. Public case browsing returns only the case reference, title,
description, source digest and native requirements. Evaluator assets, answers and
user-simulator context must never be copied into agent resources by an installer.

The initial adapters and installation commands are tracked in Epic G. A package's
source being present does not imply that its native dataset/images are installed
or that the current server can run it.

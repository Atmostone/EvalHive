# Benchmark SDK VPS release runbook

Release acceptance is tracked in [SPA-154](https://linear.app/spawnhive/issue/SPA-154).
The Benchmark SDK release was activated on 2026-11-08. Its application source is
`c0a082ea54ff7784a6263b75026abdfb48d832e4`, schema `bg16`, following the previous
release `491292180da6c122aaeef4302114382945307fd9` / `ha09`. Operational test and
documentation follow-ups do not change that deployed application identity.

## Recorded release acceptance

- Backend image: `sha256:235e9313ab0f04e53321ec85ce83554107a9f7ef94292ee5291cd4aba6e35635`.
- Frontend image: `sha256:21ba491b8d8dcaef143e409a84ae6b208b0f7fa982bc2d761f48d0006c58d509`.
- The installed catalog contains seven connectors, twelve dataset revisions and
  1,454 case descriptors; its inventory retains seventeen immutable images.
  Installation does not imply live acceptance of every variant.
- The write-paused PostgreSQL/MinIO pair is
  `/home/evalhive/evalhive-backups/evalhive-backup-20261108T075036Z`; its manifest
  SHA256 is `e365a804b5f7d4bc791200ed965c534823dce92792952ea1e5ef360d9971ba83`.
  Scratch restoration and upgrade/downgrade with both release images passed.
- Before activation, an isolated copy passed four local smoke scenarios and a
  complete paired rollback to the previous backend. The deployed stand then
  passed the same four scenarios: direct work and native Benchmark SDK work
  through each of Pi and OpenCode, including native correctness, downloaded
  files and cleanup. No external models or scientific experiments were used.
- Existing profile settings and templates were preserved. Compatible Pi profile
  pins were upgraded to `0.2.1`; the OpenCode catalog version is `0.2.2`.
  Scheduler state was restored, health checks passed, and no fixture rows,
  objects or owned runtime resources remained.
- The source stand had no native evidence tables or stored task objects. Its
  backup checks explicitly report `not_installed` / `nothing_to_check`; positive
  native evidence and corruption detection were tested separately in B16.

Public image inventories, previous configuration, backup references and the
release report are retained under
`/home/evalhive/deployment/releases/c0a082ea54ff7784a6263b75026abdfb48d832e4`.
Detailed checks are retained in the stand
`results/benchmark-sdk/b17-*` reports.

## Prepare immutable installations

Use only the EvalHive user's rootless Docker daemon. Engineering trials are
serial. Task memory/CPU limits and native environment requirements remain
unchanged; admission includes the existing host reserves.

```sh
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/docker.sock"
python3 scripts/engineering-disk-guard.py
```

The disk guard cleans unused build cache only at 95% usage. Below that threshold,
insufficient build headroom defers a large build. It does not delete images,
volumes, retained installations or running containers. Recent unused intermediate
layers are eligible too: restricting cleanup to old cache can leave a single
large preparation above the threshold. Root-reserved blocks count as unavailable
to the rootless user. All `/data` runtime trees are excluded from Git.

Build connector workers, task/service images and both Harness runtime bundles
from their locked inputs. Keep the previous images addressable by immutable
retention tags. Follow each benchmark package's native installer; a source
checkout or a dataset receipt alone is not a runtime-ready installation.

`scripts/publish-benchmark-catalog.py` publishes runtime-ready packages without
importing the backend or native frameworks. Run it with the installed Benchmark
SDK interpreter, for example:

```sh
python scripts/publish-benchmark-catalog.py \
  --package /path/to/installed/appworld/dev \
  --package /path/to/installed/appworld/train \
  --destination /path/to/new-release/benchmarks
```

Each input has `manifest.json`, `datasets/`, `resources.json`, `files.json` and
its contained native files. All runtime container images must be installed and
digest-pinned. Multiple datasets/revisions for a connector share an identical
manifest; duplicate identities or different worker declarations are rejected.
The publisher copies the packages, writes a contained installation index,
retains the images and atomically publishes a new directory. Existing catalogs
are never overwritten. Native inputs remain server-private; public browsing uses
the backend's case projection.

Publish the Harness catalog with `scripts/install-harness-manifests.py`, after
building connector images, the supervisor/tooling images and the required bundles.
Image source tags are resolved to immutable local IDs. Preserve the previous
release's catalog and image inventory.

## Configure the prepared stand

Use the existing deployment helpers under `/home/evalhive/deployment`.
`deploy.sh` delegates to the installed `build-and-start` helper. The deployment
owns the private environment references and Compose definitions; repository
scripts do not copy credentials into manifests or release reports.

Enable `BENCHMARK_RUNTIME_ENABLED`, configure matching
`BENCHMARK_MANIFEST_DIR` / `HOST_BENCHMARK_MANIFEST_DIR` paths, mount the catalog
read-only, and provide the complete `DATA_DIR` / `HOST_DATA_DIR` mapping.
The host and backend must see the same persistent bundle/evidence-spool trees.
Keep previous catalog inputs in dedicated read-only mounts. Configure
`BENCHMARK_RETAINED_CATALOG_ROOTS` as a JSON map from each retained host root to
its corresponding backend path. This lets cold regrading authenticate recorded
old inputs after the current catalog changes. The default map is empty; a missing
or modified retained input remains an explicit failure. These roots do not grant
workers arbitrary file access: recorded registrations and content proofs still
apply.
Install the configured `BENCHMARK_EVIDENCE_BUCKET` in MinIO before admission.
Worker/scheduler dispatch remains paused until schema and storage checks pass.

The release migration `bg16` upgrades compatible built-in profile pins with an
audit trail. Settings and model/secret references remain unchanged. Unsupported
configurations fail explicitly; execution history keeps its original connector
versions. See [the architecture decision](benchmark-sdk.md).

## Pair the backup and prove restoration

1. Drain or cancel owned executions, confirm cleanup and pause application writes.
2. Run `scripts/backup.sh` against the deployed source SHA. Keep its database dump,
   complete MinIO volume archive and checksummed manifest together.
3. Run `scripts/restore.sh --backup /absolute/backup/path --scratch` and retain the
   report. This checks the restored schema, row counts and legacy object paths.
4. Verify native artifact references on an isolated restored backend using the
   same server encryption identity and restored MinIO. Keep its worker and
   scheduler stopped. Run `scripts/check-benchmark-evidence.py` inside that
   backend; it authenticates private locators and verifies stored sizes and SHA256
   through the production evidence reader. It reports counts and failures, never
   native payloads, credentials or object locators. `empty` and `not_installed`
   are explicit states; a fixture containing native evidence should use
   `--expect-min-artifacts 1`.
5. Upgrade the isolated copy, check compatible profile conversion and historical
   snapshots, then rehearse restoring the previous paired backup and previous
   image/catalog selection. Record exact schema and source identities.

Run the admin checker through stdin so Python resolves the installed backend
from its working directory. For a restored fixture known to contain artifacts:

```sh
docker exec -i "$ISOLATED_API_CONTAINER" python - --expect-min-artifacts 1 \
  < scripts/check-benchmark-evidence.py
```

The full MinIO archive includes native evidence even though its locators are not
ordinary SQL S3-path columns. Legacy path verification alone must not be reported
as proof that native evidence was checked.

## Switch and validate

Select the prepared release's exact backend/frontend images and installed
catalogs, apply migrations, then start API/orchestrator with the scheduler paused.
Verify health and schema, then run scoped engineering smoke against the deployed
release using a local fixture endpoint. Check both direct Harness execution and
the native benchmark path, retained files, journals, native verdicts and cleanup.
Temporary principals/providers belong only to the fixture workspace; owner
connections and settings remain unchanged.

Restore scheduler service state, check for orphan resources and record the final
release manifest. No external model calls or scientific experiments are required.
The [acceptance inventory](benchmark-sdk-validation.md) distinguishes native live
coverage, mocked external transports and unavailable variants.

Rollback selects the previous release and restores its matching database/MinIO
pair. Migrations deliberately refuse destructive loss of clone ancestry, retained
cleanup intents or edited upgraded profiles. A previous image with an unrelated
new schema is not a verified rollback.

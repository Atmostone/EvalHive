# Harness engineering acceptance

This suite drives the built Work UI in Chromium against the real backend,
runner, rootless Docker runtime, Pi 1.5.0 and OpenCode 1.19.5. One common
scenario tests both installed connectors. The only model endpoint is a local
OpenAI protocol fixture; no external model or scientific benchmark is called.

It uses a private network, disposable PostgreSQL/MinIO/Redis and freshly
registered workspace. No production environment file or owner provider is
loaded. Do not point this stack at a production database or data directory.

```bash
npm ci --prefix frontend
npm run build --prefix frontend
export DOCKER_HOST=unix:///run/user/11001/docker.sock
export E2E_DATA_DIR=/home/evalhive/deployment/harness-e2e-data
export E2E_RESULTS_DIR=/home/evalhive/deployment/results/harness-e2e
bash tests/e2e/run-rootless.sh
```

The script refuses a non-rootless daemon, runs native tests serially, then
checks profile selection, direct execution without an orchestrator model,
native file writes, environment MCP, authenticated downloads, feedback receipts,
cancellation, contiguous terminal journals and unknown usage/cost semantics.
Artifacts are checked after success, a provider error and cancellation. Local
vocabulary data is pinned into the backend image, so trace tokenization does not
download data during acceptance runs. Backend API tests use deterministic fake
embeddings instead of loading an embedding model.
It removes only its own services and runner containers on its private network.
Screenshots, acceptance JSON and service logs remain in the results directory.
`E2E_SKIP_BUILD=1` reuses already built images for a focused diagnostic rerun.

Native test images use a network-disabled container and a loopback fixture.
The browser acceptance stack allows container-to-container traffic on its own
network. Each native execution has a single CPU and at most 2 GiB of memory;
the backend runner also enforces a global execution concurrency of one.

For a deployed release, use the separate scoped smoke helper after pausing its
scheduler (the browser Compose stack above remains disposable):

```bash
/home/evalhive/deployment/compose stop scheduler
bash tests/e2e/run-release-smoke.sh
/home/evalhive/deployment/compose start scheduler
```

It provisions a temporary engineering principal directly, so closed registration
stays closed and owner providers are never cloned. The tasks explicitly disable
memory without modifying global settings. Pi and OpenCode run serially against
an isolated local fixture, write a file, archive their results and download the
file through the authenticated API. Cleanup removes only that principal's rows,
objects and task directories; the report contains no authentication tokens.
The caller verifies successful cleanup before resuming scheduled evaluation.

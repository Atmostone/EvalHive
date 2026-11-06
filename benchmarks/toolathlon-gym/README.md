# Toolathlon-GYM connector

This package adapts the GYM derivative pinned in
[`sources.lock.json`](../../benchmark-sdk/sources.lock.json), independently of the
official Toolathlon identity. Native task, environment and evaluation details
belong here; the backend must not import this package.

The CLI adapter binds the selected native task/phase, public workspace, private
groundtruth and report location as argv. One canonical UTC launch time is frozen;
the legacy native parser fallback removes the weekday from that same instant.
Neither retries nor evaluation take a fresh wall-clock value. Conflicting or
unsupported CLI arguments fail explicitly.

Native stdio MCP declarations retain executable/arguments, empty arguments,
working directories, timeouts and canonical aliases. Template variables resolve
inside the Task container; private native dataset directories never become MCP
working directories. The package uses the isolated native fixture PostgreSQL
coordinates, never a server owner's account. UV processes use already installed
environments with synchronization/network package installation disabled. Missing,
ambiguous or unsupported native settings are rejected before agent use.

Portal readiness will probe the actual public loopback HTTP resources in the
owned native network namespace. The URL adapter preserves prompt paths rather
than treating elapsed time or a running process as readiness.

The native dataset exporter creates a tar archive directly from the pinned Git
revision/tree, verifies its byte digest and indexes task/configuration metadata
without host extraction. All 503 native tasks can be represented as frozen SDK
case descriptors. Generated case metadata binds the complete native archive
digest, so changes to native scripts, data or MCP definitions require a new
dataset installation. Private groundtruth stays inside that archive; case prompts
contain only native public task documentation. Installed archive edits fail
validation before case resolution. Native writable task copies belong in the
owned private environment, not the administrator's source checkout.

Native corpus checks accept `TOOLATHLON_SOURCE_PATH` for the pinned public source
and `CASE_CORPUS_PATH` for existing EvalHive case templates. The current support
checks also accept `TOOLATHLON_SOURCE_ARCHIVE` and `TOOLATHLON_ARCHIVE_DIGEST` for
complete native archive verification. The support library and metadata installer
are not an installed/runnable lifecycle connector yet. Worker, private
reset/checkpoint handling, image build and coordinator launch migration remain
tracked in SPA-145.

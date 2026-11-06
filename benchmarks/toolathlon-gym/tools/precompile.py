"""Precompile native Python dependencies without importing benchmark modules."""

import compileall
import hashlib
import json
import py_compile
import re
import sys
from pathlib import Path

project = Path("/opt/local_servers/Office-Word-MCP-Server")
legacy = project / ".venv/lib/python3.12/site-packages/olefile/olefile2.py"
legacy_digest = "44655509298f5fd9a69bc54636f95dcd35720757c5576fe7e6ac972618b60a1f"
if hashlib.sha256(legacy.read_bytes()).hexdigest() != legacy_digest:
    raise RuntimeError("Legacy Python 2 bytecode exclusion requires review")
for directory in (project / ".venv", project / "word_document_server"):
    if not directory.is_dir() or not compileall.compile_dir(
        directory,
        quiet=1,
        force=True,
        rx=re.compile(r"/olefile/olefile2\.py$"),
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    ):
        raise RuntimeError("Native Word bytecode preparation failed")
Path("/opt/build-info/python-bytecode.json").write_text(
    json.dumps(
        {
            "format_version": "1.0",
            "python": sys.version.split()[0],
            "projects": [project.name],
            "invalidation_mode": "checked-hash",
            "excluded_legacy_files": [{"path": str(legacy), "sha256": legacy_digest}],
        }
    )
    + "\n"
)

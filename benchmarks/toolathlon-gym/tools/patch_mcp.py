"""Apply reviewed build repairs against exact pinned native source bytes."""

import hashlib
import json
from pathlib import Path

root = Path("/opt/local_servers")
patches = Path("/opt/native-build-patches")
records = {}
for relative, patch in json.loads((patches / "package-dependencies.json").read_text()).items():
    path = root / relative
    if hashlib.sha256(path.read_bytes()).hexdigest() != patch["source_sha256"]:
        raise RuntimeError("Native package repair no longer matches pinned source: " + relative)
    document = json.loads(path.read_text())
    for section, values in patch["merge"].items():
        document.setdefault(section, {}).update(values)
    path.write_text(json.dumps(document, indent=2) + "\n")
    records[relative] = {"source_sha256": patch["source_sha256"], "merge": patch["merge"]}
replacement = patches / "filesystem-tsconfig.json"
(root / "filesystem/tsconfig.json").write_bytes(replacement.read_bytes())
records["filesystem/tsconfig.json"] = {
    "replacement_sha256": hashlib.sha256(replacement.read_bytes()).hexdigest()
}
metadata = Path("/opt/build-info")
metadata.mkdir(parents=True, exist_ok=True)
(metadata / "native-build-patches.json").write_text(json.dumps(records, indent=2) + "\n")

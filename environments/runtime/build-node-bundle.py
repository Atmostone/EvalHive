"""Build a relocatable Node connector bundle inside its pinned build image.

This is a build helper, not a runtime plugin loader. Connector Dockerfiles choose
their package and extra files; the backend consumes only the resulting receipt.
ELF interpreters/RPATH are private to the bundle. Task PATH and library variables
are unchanged, including for shell tools launched by the native harness.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
from pathlib import Path

DESTINATION = "/evalhive-runtime"


def run(*args, optional=False):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode and not optional:
        raise RuntimeError("Bundle native dependency inspection failed: " + str(args[0]))
    return result


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def native_elf(path):
    with path.open("rb") as stream:
        header = stream.read(20)
    return (
        len(header) == 20
        and header[:6] == b"\x7fELF\x02\x01"
        and struct.unpack("<H", header[18:20])[0] == 62
    )


def build(package, output, extras, probes, excludes, wrappers):
    declaration = json.loads((package / "manifest.json").read_text())
    output.mkdir(parents=True, exist_ok=False)
    connector = output / "connector"
    connector.mkdir()
    for name in ["dist", "node_modules", "package.json", "manifest.json", *extras]:
        source, target = package / name, connector / name
        if source.is_dir():
            # Local SDK package links must become real files inside the bundle.
            shutil.copytree(source, target, symlinks=False)
        else:
            shutil.copy2(source, target)
    for name in excludes:
        target = connector / name
        if not target.resolve().is_relative_to(connector) or not target.exists():
            raise RuntimeError("Invalid connector-owned bundle exclusion")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    shutil.copy2("/usr/local/bin/node", output / "node")
    shutil.copy2("/usr/local/bin/evalhive-supervisor", output / "supervisor")
    library = output / "lib"
    library.mkdir()
    native = [path for path in output.rglob("*") if path.is_file() and native_elf(path)]
    dependencies = {}
    dynamic = []
    for path in native:
        needed = run("patchelf", "--print-needed", str(path), optional=True)
        if needed.returncode:
            continue  # A statically linked executable has no dynamic section.
        dynamic.append(path)
        linked = run("ldd", str(path), optional=True)
        if "not found" in linked.stdout:
            raise RuntimeError(
                "Bundle contains an unresolved native dependency: " + str(path.relative_to(output))
            )
        # ldd includes the transitive closure and the actual dynamic loader.
        for value in re.findall(r"(?:=>\s+|^\s*)(/[^\s]+)\s+\(", linked.stdout, re.MULTILINE):
            dependency = Path(value)
            if dependency.name in dependencies and digest(dependency) != digest(
                dependencies[dependency.name]
            ):
                raise RuntimeError("Bundle dependencies have conflicting library identities")
            dependencies[dependency.name] = dependency
    for name, source in dependencies.items():
        shutil.copy2(source.resolve(), library / name)
    loader = library / "ld-linux-x86-64.so.2"
    if not loader.is_file():
        raise RuntimeError("The bundle has no pinned glibc loader")
    # Keep PT_INTERP shorter than the upstream /lib64/ld-linux-x86-64.so.2.
    # Growing both PT_INTERP and RPATH can corrupt executable load mappings.
    shutil.copy2(loader, output / "ld.so")
    wrapped = set()
    for path in dynamic:
        if path.is_relative_to(connector) and str(path.relative_to(connector)) in wrappers:
            # Some compiled runtimes cannot tolerate dynamic-section rewriting.
            # Keep their bytes intact and select libraries for this process only.
            raw = path.with_name(path.name + ".elf")
            if raw.exists():
                raise RuntimeError("Native loader wrapper collides with a package file")
            path.rename(raw)
            executable = DESTINATION + "/" + str(raw.relative_to(output))
            path.write_text(
                "#!/bin/sh\nexec "
                + DESTINATION
                + "/ld.so --library-path "
                + DESTINATION
                + "/lib "
                + shlex.quote(executable)
                + ' "$@"\n'
            )
            path.chmod(0o755)
            wrapped.add(str(path.relative_to(connector)))
            continue
        interpreter = run("patchelf", "--print-interpreter", str(path), optional=True)
        original = run("patchelf", "--print-rpath", str(path)).stdout.strip()
        values = [DESTINATION + "/lib", "$ORIGIN", *filter(None, original.split(":"))]
        options = (
            ["--set-interpreter", DESTINATION + "/ld.so"] if interpreter.returncode == 0 else []
        )
        run(
            "patchelf",
            "--no-clobber-old-sections",
            *options,
            "--force-rpath",
            "--set-rpath",
            ":".join(dict.fromkeys(values)),
            str(path),
        )
    if wrapped != set(wrappers):
        raise RuntimeError("Connector declared an unavailable native loader wrapper")
    for name in ["libc6", "libstdc++6", "libgcc-s1"]:
        source = Path("/usr/share/doc") / name / "copyright"
        if source.is_file():
            target = output / "licenses" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(source, target)
    # Validate patched programs at their final interpreter path during the build.
    Path(DESTINATION).symlink_to(output)
    checks = [
        {
            "argv": [DESTINATION + "/node", "--version"],
            "stdout": run("/usr/local/bin/node", "--version").stdout,
        },
        *probes,
    ]
    for check in checks:
        result = subprocess.run(
            check["argv"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "OPENCODE_DISABLE_MODELS_FETCH": "1"},
        )
        if result.returncode or result.stdout != check["stdout"]:
            raise RuntimeError(
                "Patched runtime bundle probe failed: "
                + check["argv"][0]
                + " exit="
                + str(result.returncode)
                + " stdout="
                + repr(result.stdout[:1024])
                + " stderr="
                + repr(result.stderr[:1024])
            )
    inventory = {
        str(path.relative_to(output)): {
            "sha256": digest(path),
            "size": path.stat().st_size,
            "executable": bool(path.stat().st_mode & 0o111),
        }
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    receipt = {
        "bundle_version": "1.0",
        "connector_id": declaration["id"],
        "connector_version": declaration["version"],
        "platform": {"os": "linux", "architecture": "amd64", "libc": "glibc"},
        "mount_path": DESTINATION,
        "entrypoint": [DESTINATION + "/node", DESTINATION + "/connector/dist/index.js"],
        "probes": checks,
        "excluded_paths": excludes,
        "loader_wrappers": wrappers,
        "files": inventory,
    }
    (output / "bundle.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    )
    print("Built bundle", declaration["id"], declaration["version"], "files", len(inventory))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--probe", action="append", type=json.loads, default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--loader-wrapper", action="append", default=[])
    options = parser.parse_args()
    build(
        options.package,
        options.output,
        options.extra,
        options.probe,
        options.exclude,
        options.loader_wrapper,
    )

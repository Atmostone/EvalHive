"""Translate native Docker CLI effects into operations on one registered site."""

from __future__ import annotations

from evalhive_benchmark import ProtocolError


class SiteTransport:
    def __init__(self, site, broker, *, remaining, handles):
        self.site, self.broker = site, broker
        self.remaining, self.handles = remaining, handles
        self.released, self.stopped = False, False

    @staticmethod
    def result(exit_code=0, stdout="", stderr=""):
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}

    async def command(self, argv, *, broker=None):
        broker = broker or self.broker
        if not isinstance(argv, list) or any(
            not isinstance(arg, str) or not arg or "\0" in arg for arg in argv
        ):
            raise ProtocolError(
                "NATIVE_EFFECT_UNAVAILABLE", "Native site arguments are invalid", "unsupported"
            )
        name, image, port = (self.site[key] for key in ("container_alias", "image_alias", "port"))
        if argv == ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"]:
            return self.result(stdout=image + ("" if ":" in image else ":latest") + "\n")
        if argv == ["docker", "run", "--name", name, "-p", f"{port}:{port}", "-d", image]:
            if self.released or "site" in self.handles:
                raise ProtocolError(
                    "NATIVE_SITE_ALREADY_STARTED",
                    "A native site cannot be started twice",
                    "uncertain",
                )
            value = await broker("allocate", {"resource_id": "site"})
            self.handles["site"] = value["handle"]
            return self.result(stdout=value["handle"] + "\n")
        if argv == ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"]:
            if "site" not in self.handles or self.released:
                return self.result()
            value = await broker("inspect", {}, resource="site")
            return self.result(
                stdout=name + "\n" if value.get("native", {}).get("status") == "running" else ""
            )
        if argv in (["docker", "stop", name], ["docker", "rm", name]):
            if "site" not in self.handles:
                return self.result(1, stderr="The owned native site is absent")
            release = argv[1] == "rm"
            if not self.released and (release or not self.stopped):
                await broker("release" if release else "stop", {}, resource="site")
                self.released, self.stopped = release, True
            return self.result(stdout=name + "\n")
        if len(argv) > 3 and argv[:3] == ["docker", "exec", name]:
            if "site" not in self.handles or self.released or self.stopped:
                raise ProtocolError(
                    "NATIVE_SITE_UNAVAILABLE",
                    "Native site execution requires an owned running site",
                    "preparation",
                )
            value = await broker(
                "exec",
                {
                    "argv": argv[3:],
                    "timeout_seconds": self.remaining(),
                    "deadline_policy": "inherit",
                    "process_policy": "stop_after_command",
                },
                resource="site",
            )
            return {key: value[key] for key in ("exit_code", "stdout", "stderr")}
        raise ProtocolError(
            "NATIVE_EFFECT_UNAVAILABLE",
            "Native Docker command is outside the registered site binding",
            "unsupported",
        )

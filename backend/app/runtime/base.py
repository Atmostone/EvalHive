from abc import ABC, abstractmethod

from app.runtime.spec import ProcessHandle, ProcessSpec


class ProcessRuntime(ABC):
    @abstractmethod
    def resolve_image(self, connector_id: str, connector_version: str) -> str: ...

    @abstractmethod
    def spawn(self, spec: ProcessSpec) -> ProcessHandle: ...

    @abstractmethod
    def stop(
        self, container_id: str, workspace_id: str, *, grace_seconds: int = 10, remove: bool = False
    ) -> bool: ...

    @abstractmethod
    def inspect(self, container_id: str, workspace_id: str) -> dict | None: ...

    @abstractmethod
    def list_active(self, workspace_id: str | None = None) -> list[dict]: ...

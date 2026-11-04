"""Attempt HTTP transport; network failures are retryable, scope errors are final."""

import httpx


class RetryableDeliveryError(Exception):
    pass


class AttemptRejected(Exception):
    def __init__(self, status: int):
        super().__init__(f"Attempt API rejected the request ({status})")
        self.status = status


class HTTPAttemptAPI:
    def __init__(self, base_url: str, attempt_id: str, token: str, client: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self.attempt_id, self._token, self.client = attempt_id, token, client

    async def post(self, suffix: str, body: dict | None = None) -> dict:
        try:
            response = await self.client.post(
                f"{self.base_url}/api/v1/attempts/{self.attempt_id}/{suffix}",
                headers={"Authorization": "Bearer " + self._token},
                json=body or {},
                timeout=10,
            )
        except httpx.TransportError:
            raise RetryableDeliveryError("Attempt API is unreachable") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableDeliveryError("Attempt API is temporarily unavailable")
        if response.status_code >= 400:
            raise AttemptRejected(response.status_code)
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise AttemptRejected(502) from None

    async def events(self, batch: list[dict]) -> dict:
        return await self.post("events", {"events": batch})

    async def prepare(self, config: dict) -> dict:
        return await self.post("configuration", {"config": config})

    async def heartbeat(self) -> dict:
        return await self.post("heartbeat")

    async def acknowledge(self, command_id: str, response: dict) -> dict:
        return await self.post(f"commands/{command_id}/ack", response)

"""Redact known private values without changing public configuration semantics."""

import json
from urllib.parse import quote


class Redactor:
    def __init__(self, values=()):
        variants = set()
        candidates = list(values)
        for value in list(candidates):
            # Some native launchers consume a structured credential (for
            # example a private MCP configuration). Its leaf values must also
            # be removed when the native process echoes only part of that input.
            if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                try:
                    pending = [json.loads(value)]
                    while pending:
                        item = pending.pop()
                        if isinstance(item, dict):
                            pending.extend(item.values())
                        elif isinstance(item, list):
                            pending.extend(item)
                        elif isinstance(item, str):
                            candidates.append(item)
                except (ValueError, RecursionError):
                    pass
        for value in candidates:
            if isinstance(value, str) and value:
                variants.update(
                    (value, quote(value, safe=""), json.dumps(value, ensure_ascii=True)[1:-1])
                )
        self.values = sorted(variants, key=len, reverse=True)

    def text(self, value: str) -> str:
        for private in self.values:
            value = value.replace(private, "[REDACTED]")
        return value

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.metadata(key): self.value(item) for key, item in value.items()}
        return value

    def metadata(self, value):
        if not isinstance(value, str):
            return value
        for private in self.values:
            if len(private) >= 8:
                value = value.replace(private, "[REDACTED]")
        return value

    def error(self, error):
        clean = {
            **error,
            "code": self.metadata(error["code"]),
            "message": self.text(error["message"]),
        }
        if "details" in error:
            clean["details"] = self.value(error["details"])
        return clean

    def event(self, event):
        # Envelope identities and normalized enum fields are protocol metadata,
        # not credential-bearing content (a short password can equal "success").
        data = {key: self.value(value) for key, value in event["data"].items()}
        for key in (
            "status",
            "role",
            "channel",
            "mode",
            "message_id",
            "call_id",
            "parent_call_id",
            "usage_id",
            "request_id",
        ):
            if key in event["data"]:
                data[key] = event["data"][key]
        for key in ("name", "model", "purpose", "path", "media_type"):
            if key in event["data"]:
                data[key] = self.metadata(event["data"][key])
        if "error" in event["data"]:
            data["error"] = self.error(event["data"]["error"])
        if event["event"] == "usage" and "cost" in event["data"]:
            data["cost"] = event["data"]["cost"]
        return {**event, "data": data}

    def response(self, response):
        if response["ok"]:
            return {**response, "result": self.value(response["result"])}
        return {**response, "error": self.error(response["error"])}

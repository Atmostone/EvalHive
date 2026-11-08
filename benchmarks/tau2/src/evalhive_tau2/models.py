"""Keep native model clients while delegating HTTP to a scoped host role."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy

import httpx
from evalhive_benchmark import ProtocolError

PARAMETERS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "reasoning_effort",
    "stop",
    "response_format",
}


class ModelTransport(httpx.BaseTransport):
    def __init__(self, context, scope, role, model, loop, deadline):
        self.context, self.scope, self.role, self.model = context, deepcopy(scope), role, model
        self.loop, self.deadline = loop, deadline

    def handle_request(self, request):
        operations = {"/v1/chat/completions": "chat/completions", "/v1/responses": "responses"}
        if request.method != "POST" or request.url.path not in operations or request.url.query:
            raise ProtocolError(
                "NATIVE_MODEL_OPERATION",
                "Native model client requested an unsupported operation",
                "unsupported",
            )
        body = json.loads(request.content)
        if body.get("model") != self.model or body.get("stream", False) is not False:
            raise ProtocolError(
                "NATIVE_MODEL_BINDING",
                "Native request differs from its bound auxiliary model",
                "configuration",
            )
        remaining = self.deadline - time.monotonic()
        if self.context.cancelled.is_set() or remaining <= 0:
            raise ProtocolError(
                "NATIVE_MODEL_STOPPED", "Native model role was cancelled or expired", "execution"
            )
        native_timeout = request.extensions.get("timeout", {}).get("read")
        if native_timeout is not None:
            remaining = min(remaining, native_timeout)
        future = asyncio.run_coroutine_threadsafe(
            self.context.model(
                self.scope,
                self.role,
                body,
                operation=operations[request.url.path],
                request_id="tau2.model." + uuid.uuid4().hex,
                timeout=min(remaining, 86400),
            ),
            self.loop,
        )
        try:
            result = future.result(timeout=remaining)
        except BaseException:
            future.cancel()
            raise
        return httpx.Response(result["status_code"], json=result["body"], request=request)


class BoundModel:
    def __init__(self, context, spec, role, loop, deadline):
        from openai import OpenAI

        model = spec["models"].get(role)
        if role == "agent" or model is None:
            raise ProtocolError(
                "NATIVE_MODEL_REQUIRED",
                "Native auxiliary role requires an explicit model binding",
                "configuration",
            )
        config = deepcopy(model.get("config", {}))
        if set(config) - PARAMETERS:
            raise ProtocolError(
                "NATIVE_MODEL_PARAMETER",
                "Auxiliary model parameters require a supported native adapter",
                "unsupported",
            )
        self.name = "openai/" + model["name"]
        self.transport = ModelTransport(context, spec["scope"], role, model["name"], loop, deadline)
        self.http = httpx.Client(transport=self.transport, trust_env=False)
        self.client = OpenAI(
            api_key="host-bound-role",
            base_url="http://auxiliary.invalid/v1",
            http_client=self.http,
            max_retries=0,
        )
        self.arguments = {
            "temperature": 0.0,
            **config,
            "api_key": "host-bound-role",
            "api_base": "http://auxiliary.invalid/v1",
            "num_retries": 0,
        }

    @contextmanager
    def native_transport(self, root, domain):
        from .native import load_domain

        load_domain(root, domain)
        from tau2.utils import llm_utils

        previous = llm_utils.completion

        def completion(*args, **kwargs):
            if kwargs.get("model") != self.name:
                raise ProtocolError(
                    "NATIVE_MODEL_BINDING",
                    "Native model differs from the active role",
                    "configuration",
                )
            return previous(*args, **{**kwargs, "client": self.client, "num_retries": 0})

        llm_utils.completion = completion
        try:
            yield
        finally:
            llm_utils.completion = previous

    @contextmanager
    def judge(self, root, domain):
        from .native import load_domain

        load_domain(root, domain)
        from tau2.evaluator import evaluator_nl_assertions as module

        previous = module.DEFAULT_LLM_NL_ASSERTIONS, module.DEFAULT_LLM_NL_ASSERTIONS_ARGS
        module.DEFAULT_LLM_NL_ASSERTIONS = self.name
        module.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {**previous[1], **self.arguments}
        try:
            with self.native_transport(root, domain):
                yield
        finally:
            module.DEFAULT_LLM_NL_ASSERTIONS, module.DEFAULT_LLM_NL_ASSERTIONS_ARGS = previous

    def close(self):
        self.client.close()

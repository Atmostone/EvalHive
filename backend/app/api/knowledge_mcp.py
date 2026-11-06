"""Read-only, attempt-scoped MCP access to the EvalHive knowledge base."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.api.attempts import bounded_json
from app.auth.tokens import verify_knowledge_token
from app.database import get_db

router = APIRouter(tags=["knowledge"])
SEARCH = {
    "name": "search_knowledge_base",
    "description": "Search the EvalHive workspace knowledge base",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 8000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


@router.post("/api/v1/attempts/{attempt_id}/knowledge/mcp")
async def knowledge_mcp(attempt_id: uuid.UUID, request: Request, db=Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    token = await verify_knowledge_token(
        db, plain=auth.removeprefix("Bearer "), attempt_id=attempt_id
    )
    if not auth.startswith("Bearer ") or token is None:
        raise HTTPException(401, "Invalid or expired knowledge token")
    body = await bounded_json(request)
    if (
        not isinstance(body, dict)
        or body.get("jsonrpc") != "2.0"
        or not isinstance(body.get("method"), str)
    ):
        raise HTTPException(422, "Invalid MCP request")
    method, identity = body["method"], body.get("id")
    if not isinstance(body.get("params", {}), dict):
        raise HTTPException(422, "Invalid MCP parameters")
    if identity is not None and (
        not isinstance(identity, (str, int)) or isinstance(identity, bool)
    ):
        raise HTTPException(422, "Invalid MCP request identity")
    if identity is None:
        return Response(status_code=202)

    def reply(result=None, error=None):
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": identity,
                **({"error": error} if error else {"result": result}),
            }
        )

    if method == "initialize":
        version = (body.get("params") or {}).get("protocolVersion")
        supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
        return reply(
            {
                "protocolVersion": version if version in supported else "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "evalhive-knowledge", "version": "1"},
            }
        )
    if method == "ping":
        return reply({})
    if method == "tools/list":
        return reply({"tools": [SEARCH]})
    if method != "tools/call":
        return reply(error={"code": -32601, "message": "Method not found"})
    from jsonschema import Draft202012Validator

    params = body.get("params") or {}
    arguments = params.get("arguments", {}) if isinstance(params, dict) else None
    if (
        not isinstance(params, dict)
        or params.get("name") != SEARCH["name"]
        or not Draft202012Validator(SEARCH["inputSchema"]).is_valid(arguments)
    ):
        return reply(error={"code": -32602, "message": "Invalid tool arguments"})
    from app.knowledge.rag import search_documents

    try:
        results = await search_documents(
            arguments["query"], workspace_id=token.workspace_id, limit=arguments.get("limit", 5)
        )
    except Exception:
        return reply(
            {
                "isError": True,
                "content": [{"type": "text", "text": "Knowledge search is unavailable"}],
            }
        )
    import json

    return reply(
        {
            "content": [
                {"type": "text", "text": json.dumps({"results": results}, ensure_ascii=False)}
            ]
        }
    )

# Structured Memory (P0)

> Implementation: `backend/app/memory/{store,extractor}.py`, `app/api/memory.py`. Toggled by the `memory_mode = "structured"` setting. Default is `flat` (legacy `/data/memory.md`).

## Why

`flat` mode: `/data/memory.md` is a single markdown file the agent reads end-to-end. It doesn't scale, has no dedup, and no relations.

`structured` mode: an entity-relation graph, auto-extracted from completed tasks, with a relevant sub-graph injected into the agent before spawn.

## Model

- `memory_entities`: `(type, name, attributes JSONB, embedding_id, created_by, …)`. Type is extensible (person/project/decision/file/...).
- `memory_relations`: `(from_id, to_id, relation_type, attributes)`.
- Qdrant collection `memory_entities` — one vector per entity. Embedding text: `"type:name k1=v1 k2=v2"`.

## Extraction pipeline

When a task receives `event=completed` and the auto-review approves it (and `memory_mode=structured`):

1. `asyncio.create_task(extract_memory(task_id))` — runs in the background.
2. LLM call via `litellm.acompletion` with the `extract_memory_facts` tool. Inputs: `task_title`, `task_description`, `result_summary`. The tool returns `{entities: [{type, name, attributes}], relations: [{from, to, type, attributes}]}`.
3. For each entity:
   - Embed the text.
   - Query top-1 in the Qdrant `memory_entities` collection.
   - If score ≥ 0.92 and the type matches — merge attributes into the existing entity instead of creating a duplicate.
   - Otherwise — INSERT + Qdrant upsert.
4. For each relation: resolve `from`/`to` by name (preferring entities from this extraction, falling back to a DB lookup). The relation is created only if both ends resolve.
5. `log_event memory_extracted {new_entities, merged_entities, new_relations}`.

The threshold `0.92` lives in `store.py` as `DEDUP_THRESHOLD`.

## Delivery pipeline

In `engine.py:process_ready_task`, just before `spawn_agent`:

1. `build_memory_context(query_text=task.title + description)`.
2. `find_relevant_entities` — embedding search across `memory_entities`, top-K=10, threshold=0.7.
3. `expand_with_relations` — one-hop graph traversal: neighbours of seed nodes via relations are pulled in.
4. `serialize_context_md` — a compact markdown blob ≤ 8000 chars (~2000 tokens):
   ```
   # Relevant context
   ## person:Ivan
   role: lead developer
   ## project:Alpha
   status: active
   ### Relations
   - person:Ivan works_on project:Alpha
   ```
5. The text goes to the agent's env as `AGENT_MEMORY_CONTEXT`.
6. In `agent.py:build_system_prompt`, if `AGENT_MEMORY_CONTEXT` is non-empty it is injected **instead of** `/data/memory.md`; otherwise the flat file is used.

## Manual trigger

`POST /api/memory/extract?task_id=<uuid>` — runs extraction on any existing task (not necessarily one that just completed).

## Reset

`POST /api/knowledge/reset` drops both Qdrant collections (`evalhive_docs` + `memory_entities`) and deletes documents/entities/relations. Used when switching embedding providers (different dim).

## Known limitations

- Dedup only matches on cosine ≥ 0.92 + same type. If the LLM emits the same object under a different type, a duplicate slips through.
- Relations are not dedup'd. You can end up with multiple `(A, works_on, B)` rows with different attributes.

## Toggle

`PATCH /api/settings {"memory_mode": "structured"}` — turn it on.
`PATCH /api/settings {"memory_mode": "flat"}` — turn it off (new extractions are skipped; existing entities remain).

"""One bounded event delivery implementation for execution and evaluator journals."""

import uuid

from evalhive_benchmark import encode_line, validate
from evalhive_benchmark.peer import identity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.benchmark_trial import BenchmarkEventStream


class StreamLedger:
    async def register_stream(self, owner, actor_id):
        from app.benchmarks.trials import database_fence, failure

        async with database_fence(self.engine, owner.trial_id) as db:
            trial, _ = await self.current(db, owner)
            if trial.phase == "finished":
                raise failure("TRIAL_FINISHED", "Finished journals cannot register new streams")
            stream = await db.scalar(
                select(BenchmarkEventStream).where(
                    BenchmarkEventStream.trial_id == owner.trial_id,
                    BenchmarkEventStream.control_id == owner.control_id,
                    BenchmarkEventStream.generation == owner.generation,
                    BenchmarkEventStream.actor_id == actor_id,
                )
            )
            if stream is None:
                stream = BenchmarkEventStream(
                    trial_id=owner.trial_id,
                    control_id=owner.control_id,
                    generation=owner.generation,
                    actor_id=actor_id,
                    next_seq=0,
                )
                db.add(stream)
                await db.commit()
            return stream.id

    async def ingest(self, owner, actor_id, events):
        """Persist 1..128 contiguous records before acknowledging their delivery.

        Raw worker data is private by default. Consumers must opt into an explicit
        redacted projection; an event name does not grant public visibility.
        """
        from app.benchmarks.trials import failure

        if not 1 <= len(events) <= 128:
            raise failure("INVALID_BATCH", "Event delivery requires 1 to 128 records")
        for event in events:
            validate("event", event)
            encode_line(event)
            if event["scope"] != {
                "workspace_id": str(owner.workspace_id),
                "trial_id": str(owner.trial_id),
                "generation": owner.generation,
            }:
                raise failure("STALE_GENERATION", "Event scope is not current")
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            trial, _ = await self.current(db, owner, row_lock=True)
            if trial.phase == "finished":
                raise failure("TRIAL_FINISHED", "Finished journal is sealed")
            stream = await db.scalar(
                select(BenchmarkEventStream).where(
                    BenchmarkEventStream.trial_id == owner.trial_id,
                    BenchmarkEventStream.control_id == owner.control_id,
                    BenchmarkEventStream.generation == owner.generation,
                    BenchmarkEventStream.actor_id == actor_id,
                )
            )
            if stream is None:
                raise failure("UNKNOWN_ACTOR", "Event stream is not registered")
            for event in events:
                previous = await db.scalar(
                    select(self.journal_model).where(
                        self.journal_model.stream_id == stream.id,
                        self.journal_model.event_id == event["id"],
                    )
                )
                digest = identity(event)
                if previous is not None:
                    if previous.content_hash != digest:
                        raise failure(
                            "EVENT_CONFLICT", "Event identity was reused with different content"
                        )
                    continue
                if stream.final_seq is not None:
                    raise failure("STREAM_SEALED", "Event stream is sealed")
                if event["seq"] != stream.next_seq:
                    raise failure("EVENT_GAP", "Event sequence must be contiguous", "recording")
                entry_id = uuid.uuid4()
                await self.append(
                    db,
                    trial,
                    event["event"],
                    {"source": event["source"]},
                    id=entry_id,
                    stream_id=stream.id,
                    source_seq=event["seq"],
                    event_id=event["id"],
                    content_hash=digest,
                    private_data=self.private.seal("journal:" + str(entry_id), event),
                )
                stream.next_seq += 1
            await db.commit()
            return {"accepted_through": stream.next_seq - 1}

    async def seal_stream(self, owner, actor_id, final_seq):
        from app.benchmarks.trials import database_fence, failure

        async with database_fence(self.engine, owner.trial_id) as db:
            trial, _ = await self.current(db, owner, row_lock=True)
            stream = await db.scalar(
                select(BenchmarkEventStream).where(
                    BenchmarkEventStream.trial_id == owner.trial_id,
                    BenchmarkEventStream.control_id == owner.control_id,
                    BenchmarkEventStream.generation == owner.generation,
                    BenchmarkEventStream.actor_id == actor_id,
                )
            )
            if stream is None or type(final_seq) is not int or final_seq != stream.next_seq - 1:
                raise failure("EVENT_GAP", "Completion requires all preceding events", "recording")
            if stream.final_seq is None:
                stream.final_seq = final_seq
                stream.checkpoint_seq = final_seq
                await self.append(
                    db, trial, "stream.sealed", {"actor_id": str(actor_id), "final_seq": final_seq}
                )
                await db.commit()
            return {"accepted_through": final_seq}

    async def checkpoint_stream(self, owner, actor_id, through_seq):
        """Persist a phase delivery barrier without ending the worker's stream.

        The host invokes this after the command response and durable draining of
        all earlier ordered JSONL frames. Capture/evaluation may emit later events
        from the same process. Only final shutdown permanently seals a stream.
        """
        from app.benchmarks.trials import database_fence, failure

        async with database_fence(self.engine, owner.trial_id) as db:
            trial, _ = await self.current(db, owner, row_lock=True)
            stream = await db.scalar(
                select(BenchmarkEventStream).where(
                    BenchmarkEventStream.trial_id == owner.trial_id,
                    BenchmarkEventStream.control_id == owner.control_id,
                    BenchmarkEventStream.generation == owner.generation,
                    BenchmarkEventStream.actor_id == actor_id,
                )
            )
            if stream is None or type(through_seq) is not int or through_seq != stream.next_seq - 1:
                raise failure("EVENT_GAP", "Checkpoint requires all preceding events", "recording")
            if stream.checkpoint_seq != through_seq:
                stream.checkpoint_seq = through_seq
                await self.append(
                    db,
                    trial,
                    "stream.checkpoint",
                    {"actor_id": str(actor_id), "through_seq": through_seq},
                )
                await db.commit()
            return {"accepted_through": through_seq}

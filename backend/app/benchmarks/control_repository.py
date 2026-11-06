"""SQL owner fencing and atomic journals shared by execution and evaluator contexts."""

from evalhive_benchmark import ProtocolError
from sqlalchemy import func, select, update
from sqlalchemy.orm.attributes import set_committed_value

from app.models.benchmark_trial import BenchmarkJournalEntry, BenchmarkTrial


class SqlControlRepository:
    def __init__(self, model, journal, *, owner_key, journal_key, source_key=None):
        self.model, self.journal = model, journal
        self.owner_key, self.journal_key, self.source_key = owner_key, journal_key, source_key

    async def current(self, db, owner, *, allow_expired=False, row_lock=False):
        key = getattr(owner, self.owner_key)
        if key is None or (self.owner_key == "trial_id" and owner.control_id is not None):
            raise ProtocolError(
                "FOREIGN_CONTROL", "Controller context is unavailable", "configuration"
            )
        statement = (
            select(self.model).where(self.model.id == key).execution_options(populate_existing=True)
        )
        if row_lock:
            statement = statement.with_for_update()
        row = await db.scalar(statement)
        now = await db.scalar(select(func.clock_timestamp()))
        if (
            row is None
            or row.workspace_id != owner.workspace_id
            or (self.source_key and getattr(row, self.source_key) != owner.trial_id)
        ):
            raise ProtocolError(
                "TRIAL_NOT_FOUND", "Trial is unavailable in this workspace", "configuration"
            )
        if row.generation != owner.generation or row.controller_id != owner.controller_id:
            raise ProtocolError(
                "STALE_GENERATION", "Controller no longer owns this trial", "configuration"
            )
        if not allow_expired and (
            row.controller_expires_at is None or row.controller_expires_at <= now
        ):
            raise ProtocolError("OWNER_EXPIRED", "Controller lease has expired", "configuration")
        return row, now

    async def append(self, db, row, event, public_data=None, *, private_data=None, **source):
        # A native resource call can commit several intents while holding its
        # session fence. Allocate atomically rather than from cached ORM state.
        next_seq = await db.scalar(
            update(self.model)
            .where(self.model.id == row.id)
            .values(next_seq=self.model.next_seq + 1)
            .returning(self.model.next_seq)
            .execution_options(synchronize_session=False)
        )
        set_committed_value(row, "next_seq", next_seq)
        entry = self.journal(
            **{self.journal_key: row.id},
            seq=next_seq - 1,
            generation=row.generation,
            event=event,
            public_data=public_data or {},
            private_data=private_data,
            **source,
        )
        db.add(entry)
        return entry


EXECUTION_CONTROLS = SqlControlRepository(
    BenchmarkTrial, BenchmarkJournalEntry, owner_key="trial_id", journal_key="trial_id"
)

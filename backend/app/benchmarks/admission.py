"""One host admission transaction for execution and independent grading contexts."""

from dataclasses import asdict

from sqlalchemy import func, select

from app.models.benchmark_trial import BenchmarkHostReservation
from app.runtime.admission import check_group


class HostAdmission:
    async def reserve(
        self,
        owner,
        *,
        demands,
        native_resources,
        measure,
        runtime_policy,
        group_policy,
        stage="full",
        auxiliary_disk_mb=0,
    ):
        """Admit an isolated planner, then freeze the complete group exactly once.

        A planning reservation only authorizes one registered worker. Finalizing
        retains its exact demand; no later resize or lowering native minima is
        allowed. Measurements remain conservative while that worker is alive.
        """
        from app.benchmarks.trials import database_fence, failure

        if stage not in {"planning", "full"}:
            raise failure("INVALID_ADMISSION", "Unknown host admission stage")
        if type(auxiliary_disk_mb) is not int or auxiliary_disk_mb < 0:
            raise failure("INVALID_ADMISSION", "Auxiliary storage requires a nonnegative budget")
        if stage == "planning" and (len(demands) != 1 or native_resources or auxiliary_disk_mb):
            raise failure("INVALID_ADMISSION", "Planning admission requires one worker demand")
        resources = [asdict(item) for item in demands]
        async with database_fence(self.engine, owner.trial_id, host_id=self.host_id) as db:
            trial, now = await self.current(db, owner)
            if (
                trial.phase != "planning"
                or trial.cancel_requested_at is not None
                or trial.deadline_at <= now
            ):
                raise failure("INVALID_PHASE", "Host admission requires an active planning phase")
            reservation = await db.get(BenchmarkHostReservation, self.host_id)
            if reservation is not None:
                if (reservation.trial_id, reservation.generation, reservation.controller_id) != (
                    owner.trial_id,
                    owner.generation,
                    owner.controller_id,
                ):
                    raise failure("HOST_BUSY", "Execution host remains reserved", "preparation")
                previous_stage = reservation.budget.get("stage", "full")
                if previous_stage == stage:
                    if (
                        reservation.budget["resources"] != resources
                        or reservation.budget["native_resources"] != native_resources
                        or reservation.budget.get("auxiliary_disk_mb", 0) != auxiliary_disk_mb
                    ):
                        raise failure(
                            "ADMISSION_CONFLICT",
                            "Host reservation has different resource requirements",
                        )
                    return reservation.budget
                selected = {item["resource_id"]: item for item in resources}
                if (
                    previous_stage != "planning"
                    or stage != "full"
                    or any(
                        selected.get(item["resource_id"]) != item
                        for item in reservation.budget["resources"]
                    )
                ):
                    raise failure(
                        "ADMISSION_CONFLICT", "Final admission must preserve the planning worker"
                    )
            measured = await measure()
            # Measurement may yield while a cancellation request arrives.
            await db.refresh(trial)
            now = await db.scalar(select(func.clock_timestamp()))
            if trial.cancel_requested_at is not None or trial.deadline_at <= now:
                raise failure("TRIAL_STOPPING", "Trial stopped during host admission", "execution")
            decision = check_group(
                demands,
                native_resources,
                measured,
                runtime_policy,
                group_policy,
                active_trials=0,
                auxiliary_disk_mb=auxiliary_disk_mb,
            )
            if not decision["eligible"]:
                raise failure(
                    "CAPACITY_UNAVAILABLE",
                    "Native group does not fit the execution host",
                    "preparation",
                )
            budget = {
                **decision,
                "stage": stage,
                "runtime_admission": "reserved",
                "can_run": False,
                "resources": resources,
                "native_resources": native_resources,
                "auxiliary_disk_mb": auxiliary_disk_mb,
            }
            finalizing = reservation is not None
            if finalizing:
                reservation.budget = budget
            else:
                db.add(
                    BenchmarkHostReservation(
                        host_id=self.host_id,
                        trial_id=owner.trial_id,
                        generation=owner.generation,
                        controller_id=owner.controller_id,
                        budget=budget,
                    )
                )
            await self.append(
                db,
                trial,
                "host.reservation_finalized" if finalizing else "host.reserved",
                {"stage": stage, "group_resources": decision["group_resources"]},
            )
            await db.commit()
            return budget

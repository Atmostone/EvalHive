"""Provider-neutral resource dependency ordering for preparation and recovery."""

from app.benchmarks.trials import failure


def dependency_order(dependencies):
    parents = {handle: set(values) & dependencies.keys() for handle, values in dependencies.items()}
    ordered, active, seen = [], set(), set()

    def visit(handle):
        if handle in active:
            raise failure("RECOVERY_CYCLE", "Resource dependencies cannot be reconciled", "cleanup")
        if handle in seen:
            return
        active.add(handle)
        for parent in sorted(parents[handle]):
            visit(parent)
        active.remove(handle)
        seen.add(handle)
        ordered.append(handle)

    for handle in sorted(dependencies):
        visit(handle)
    return ordered, parents

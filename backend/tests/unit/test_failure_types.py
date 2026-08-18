"""Typing a run's death, and deciding what it costs the aggregate (SPA-87)."""

from app.utils.failures import (
    CONTAMINATING_FAILURES,
    FAILURE_AGENT,
    FAILURE_CAP_HIT,
    FAILURE_INFRA,
    FAILURE_LLM_AUTH,
    FAILURE_LLM_ERROR,
    FAILURE_LLM_RATE_LIMIT,
    FAILURE_LLM_TRANSIENT,
    FAILURE_TIMEOUT,
    classify_agent_failure,
    classify_llm_error,
    infrastructure_decided,
    is_contaminated,
    is_transient_llm_error,
    measures_the_model,
)


class TestClassifyLlmError:
    def test_quota_is_a_rate_limit(self):
        assert classify_llm_error("APIError", 429) == FAILURE_LLM_RATE_LIMIT

    def test_dead_key_and_spent_credit_are_auth(self):
        assert classify_llm_error(None, 401) == FAILURE_LLM_AUTH
        assert classify_llm_error(None, 402) == FAILURE_LLM_AUTH
        assert classify_llm_error(None, 403) == FAILURE_LLM_AUTH

    def test_server_side_and_timeouts_are_transient(self):
        for status in (408, 500, 502, 503, 504):
            assert classify_llm_error(None, status) == FAILURE_LLM_TRANSIENT

    def test_exception_class_when_no_status(self):
        assert classify_llm_error("RateLimitError", None) == FAILURE_LLM_RATE_LIMIT
        assert classify_llm_error("APIConnectionError", None) == FAILURE_LLM_TRANSIENT
        assert classify_llm_error("AuthenticationError", None) == FAILURE_LLM_AUTH

    def test_status_wins_over_class_name(self):
        """A provider that wraps everything in one APIError still tells the truth
        in the status line."""
        assert classify_llm_error("APIError", 429) == FAILURE_LLM_RATE_LIMIT

    def test_a_plain_bad_request_is_not_infrastructure(self):
        # A 400 (context too long, malformed tool schema) is something the run
        # did. Typing it is useful; excluding it would forgive the model.
        assert classify_llm_error("BadRequestError", 400) == FAILURE_LLM_ERROR
        assert is_contaminated(FAILURE_LLM_ERROR) is False

    def test_unknown_shapes_do_not_crash(self):
        assert classify_llm_error(None, None) == FAILURE_LLM_ERROR
        assert classify_llm_error("", 0) == FAILURE_LLM_ERROR


class TestClassifyAgentFailure:
    def test_llm_facts_are_typed_by_the_same_table(self):
        facts = {"kind": "llm_error", "exception": "RateLimitError", "status_code": 429}
        assert classify_agent_failure(facts) == FAILURE_LLM_RATE_LIMIT

    def test_cap_hit_and_leaks_are_the_run_own_doing(self):
        assert classify_agent_failure({"kind": "cap_hit"}) == FAILURE_CAP_HIT
        assert classify_agent_failure({"kind": "tool_leak"}) == FAILURE_AGENT
        assert classify_agent_failure({"kind": "crash"}) == FAILURE_AGENT

    def test_an_older_agent_reports_nothing_and_stays_unclassified(self):
        """NULL is «not classified». It must never be filled in with a guess: a
        default of `agent` would launder infrastructure into model quality, and a
        default of `infra` would delete real failures from the numbers."""
        assert classify_agent_failure(None) is None
        assert classify_agent_failure({}) is None
        assert classify_agent_failure("LLM call failed: boom") is None
        assert classify_agent_failure({"kind": "something_new"}) is None

    def test_a_non_numeric_status_falls_back_to_the_class_name(self):
        facts = {"kind": "llm_error", "exception": "Timeout", "status_code": "n/a"}
        assert classify_agent_failure(facts) == FAILURE_LLM_TRANSIENT


class TestContamination:
    def test_only_infrastructure_is_excluded(self):
        for t in (
            FAILURE_LLM_RATE_LIMIT,
            FAILURE_LLM_AUTH,
            FAILURE_LLM_TRANSIENT,
            FAILURE_INFRA,
        ):
            assert is_contaminated(t) is True

    def test_what_the_run_did_stays_in_the_numbers(self):
        for t in (FAILURE_AGENT, FAILURE_CAP_HIT, FAILURE_TIMEOUT, FAILURE_LLM_ERROR, None):
            assert is_contaminated(t) is False


class TestRetryPredicate:
    """One table serves both questions — «retry this HTTP call?» and «did
    infrastructure decide this run?» — so they cannot drift apart."""

    def test_transient_statuses_retry(self):
        for status in (408, 429, 500, 502, 503, 504):
            exc = Exception("boom")
            exc.status_code = status
            assert is_transient_llm_error(exc) is True

    def test_auth_does_not_retry(self):
        # Retrying a dead key just burns the clock — but the run is still
        # contaminated, which is the distinction between the two questions.
        exc = Exception("no credit")
        exc.status_code = 402
        assert is_transient_llm_error(exc) is False
        assert is_contaminated(classify_llm_error(None, 402)) is True

    def test_class_names_without_a_status(self):
        class RateLimitError(Exception):
            pass

        class BadRequestError(Exception):
            pass

        assert is_transient_llm_error(RateLimitError()) is True
        assert is_transient_llm_error(BadRequestError()) is False


class TestSqlPredicates:
    """The review's second finding: the report excluded a quota-killed run while
    /analytics, the live matrix and the global leaderboard went on averaging it.
    One definition, compiled here so its SQL is checked rather than assumed."""

    def _sql(self, expr):
        return str(expr.compile(compile_kwargs={"literal_binds": True}))

    def test_the_population_keeps_unclassified_rows(self):
        """`NULL NOT IN (...)` is NULL in SQL, which drops the row — that would
        have silently deleted every run predating the classifier."""
        from sqlalchemy import Column, String

        col = Column("failure_type", String)
        sql = self._sql(measures_the_model(col))
        assert "IS NULL" in sql
        assert "NOT IN" in sql

    def test_the_two_predicates_are_built_from_one_set(self):
        from sqlalchemy import Column, String

        col = Column("failure_type", String)
        keep = self._sql(measures_the_model(col))
        drop = self._sql(infrastructure_decided(col))
        for t in CONTAMINATING_FAILURES:
            assert t in keep and t in drop
        for t in (FAILURE_AGENT, FAILURE_CAP_HIT, FAILURE_TIMEOUT, FAILURE_LLM_ERROR):
            assert t not in drop

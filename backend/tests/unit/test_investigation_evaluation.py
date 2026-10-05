"""The investigation evaluation suite: the judge, the built-in cases, and the runner."""

import json
from typing import Any

import pytest

from incident_intel.investigation.llm import LLMError
from incident_intel.investigation.schemas import Hypothesis, Report
from incident_intel.learning.evaluation import (
    EvalCase,
    builtin_cases,
    format_results,
    judge,
    run_cases,
)
from tests.support import FakeLLM


def report(*hypotheses: tuple[str, str]) -> Report:
    return Report(
        summary="s",
        observed_facts=[],
        hypotheses=[
            Hypothesis(
                statement=statement,
                assessment=assessment,
                supporting_evidence=["E2"],
                contradicting_evidence=[],
                reasoning="because",
            )
            for assessment, statement in hypotheses
        ],
        unknowns=[],
        next_steps=[],
    )


CASE = EvalCase(
    "c",
    "d",
    evidence=[{"ref": "E1", "kind": "incident"}, {"ref": "E2", "kind": "anomaly"}],
    expect_supported=True,
    must_mention=("2.43.0", "deployment"),
    must_not_mention=("inventory-api",),
)


def test_a_report_pointing_the_right_way_passes() -> None:
    assert judge(CASE, report(("supported", "The Deployment of 2.43.0 slowed the database."))) == []


def test_missing_supported_hypothesis_fails() -> None:
    [failure] = judge(CASE, report(("weak", "Maybe the deployment of 2.43.0.")))

    assert "expected a supported hypothesis" in failure


def test_leading_hypothesis_must_mention_a_keyword() -> None:
    failures = judge(CASE, report(("supported", "Cosmic rays."), ("weak", "The deployment.")))

    assert any("mentions none of" in failure for failure in failures)


def test_blaming_the_wrong_thing_fails_unless_it_was_ruled_out() -> None:
    blamed = judge(
        CASE, report(("supported", "Deployment 2.43.0."), ("weak", "inventory-api broke it."))
    )
    ruled_out = judge(
        CASE,
        report(("supported", "Deployment 2.43.0."), ("contradicted", "inventory-api broke it.")),
    )

    assert any("blames 'inventory-api'" in failure for failure in blamed)
    assert ruled_out == []


def test_a_case_can_require_that_nothing_is_called_supported() -> None:
    case = EvalCase("c", "d", evidence=[], expect_supported=False)

    assert judge(case, report(("untested", "Could be load."))) == []
    assert "cannot justify" in judge(case, report(("supported", "It was load.")))[0]


def test_builtin_cases_are_well_formed() -> None:
    cases = builtin_cases()

    assert len({case.name for case in cases}) == len(cases) == 6
    for case in cases:
        refs = [item["ref"] for item in case.evidence]
        assert refs == [f"E{i}" for i in range(1, len(refs) + 1)], case.name
        assert all({"kind", "title"} <= item.keys() for item in case.evidence)
        assert case.expect_supported is not None or case.must_mention or case.must_not_mention
    # The injection case really does carry instructions in its evidence.
    injected = next(case for case in cases if case.name == "instructions_inside_logs")
    assert "ignore the evidence" in json.dumps(injected.evidence)


def _reply(statement: str, assessment: str = "supported", evidence: list[str] | None = None) -> str:
    body: dict[str, Any] = {
        "summary": "s",
        "observed_facts": [],
        "hypotheses": [
            {
                "statement": statement,
                "assessment": assessment,
                "supporting_evidence": evidence if evidence is not None else ["E2"],
                "reasoning": "r",
            }
        ],
    }
    return json.dumps(body)


HOLDS = json.dumps({"reviews": [{"hypothesis_index": 0, "verdict": "holds", "explanation": "ok"}]})


async def test_runner_judges_each_case_through_the_real_checks() -> None:
    cases = [CASE, EvalCase("second", "d", CASE.evidence, expect_supported=True)]
    # First case: a good answer. Second: "supported" but citing only invented evidence, so
    # the citation checks downgrade it to untested and the case fails.
    llm = FakeLLM(
        replies=[_reply("Deployment 2.43.0."), HOLDS, _reply("Anything.", evidence=["E9"]), HOLDS]
    )

    results = await run_cases(llm, cases, concurrency=1)

    assert [(r.case, r.passed) for r in results] == [("c", True), ("second", False)]
    assert results[1].corrections == 2
    text = format_results(results, model="fake-model", prompt_version="2")
    assert "1 passed, 1 failed, 0 could not run (of 2 cases)" in text
    assert "FAIL   second" in text


async def test_a_failed_run_is_a_failed_case() -> None:
    llm = FakeLLM(replies=[LLMError("Gemini answered 400", transient=False)])

    [result] = await run_cases(llm, [CASE])

    assert (result.passed, result.error, result.failures) == (False, "Gemini answered 400", [])
    text = format_results([result], model="m", prompt_version="2")
    assert "0 passed, 0 failed, 1 could not run" in text
    assert "ERROR  c" in text


@pytest.mark.parametrize("case", builtin_cases(), ids=lambda case: case.name)
def test_each_builtin_case_can_be_failed(case: EvalCase) -> None:
    """A case nothing can fail measures nothing: a deliberately wrong report must fail."""
    wrong = report(("supported", "inventory-api deployment release broke everything."))

    assert judge(case, wrong) != []

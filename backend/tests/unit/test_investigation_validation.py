"""The checks code applies to a model-written report."""

from typing import Any

from incident_intel.investigation.schemas import RawReport, RawVerification
from incident_intel.investigation.validation import apply_verification, validate_report

REFS = {"E1", "E2", "E3"}


def report(**overrides: Any) -> RawReport:
    base: dict[str, Any] = {
        "summary": "Latency rose after a deployment.",
        "observed_facts": [{"statement": "Latency rose.", "evidence": ["E1"]}],
        "hypotheses": [
            {
                "statement": "The deployment caused it.",
                "assessment": "supported",
                "supporting_evidence": ["E1", "E2"],
                "contradicting_evidence": [],
                "reasoning": "Timing.",
            }
        ],
        "unknowns": ["Whether traffic changed."],
        "next_steps": [{"action": "Diff the release.", "expected_evidence": "A new query."}],
    }
    return RawReport.model_validate({**base, **overrides})


def test_a_well_cited_report_passes_unchanged() -> None:
    checked, notes = validate_report(report(), REFS)

    assert notes == []
    assert checked.observed_facts[0].evidence == ["E1"]
    assert checked.hypotheses[0].assessment == "supported"


def test_references_to_evidence_that_does_not_exist_are_removed() -> None:
    checked, notes = validate_report(
        report(observed_facts=[{"statement": "Latency rose.", "evidence": ["E1", "E99", "e2"]}]),
        REFS,
    )

    assert checked.observed_facts[0].evidence == ["E1", "E2"]  # normalized, invented one gone
    assert any("'E99'" in note for note in notes)


def test_a_fact_without_evidence_is_removed() -> None:
    checked, notes = validate_report(
        report(
            observed_facts=[
                {"statement": "Cited.", "evidence": ["E1"]},
                {"statement": "Uncited claim.", "evidence": []},
                {"statement": "Cites only invented evidence.", "evidence": ["E42"]},
            ]
        ),
        REFS,
    )

    assert [fact.statement for fact in checked.observed_facts] == ["Cited."]
    assert sum("cited no evidence" in note for note in notes) == 2


def test_a_hypothesis_cannot_be_supported_without_evidence() -> None:
    checked, notes = validate_report(
        report(
            hypotheses=[
                {
                    "statement": "Cosmic rays.",
                    "assessment": "supported",
                    "supporting_evidence": ["E77"],
                    "reasoning": "Trust me.",
                }
            ]
        ),
        REFS,
    )

    assert checked.hypotheses[0].assessment == "untested"
    assert any("shown as untested" in note for note in notes)


def _two_hypotheses() -> RawReport:
    return report(
        hypotheses=[
            {
                "statement": "A",
                "assessment": "supported",
                "supporting_evidence": ["E1"],
                "reasoning": "r",
            },
            {
                "statement": "B",
                "assessment": "supported",
                "supporting_evidence": ["E2"],
                "reasoning": "r",
            },
        ]
    )


def test_verification_can_contradict_a_hypothesis_only_with_real_evidence() -> None:
    checked, _ = validate_report(_two_hypotheses(), REFS)
    verification = RawVerification.model_validate(
        {
            "reviews": [
                {
                    "hypothesis_index": 0,
                    "verdict": "contradicted",
                    "contradicting_evidence": ["E3"],
                    "explanation": "E3 predates it.",
                },
                {
                    "hypothesis_index": 1,
                    "verdict": "contradicted",
                    "contradicting_evidence": ["E50"],
                    "explanation": "No real citation.",
                },
                {"hypothesis_index": 9, "verdict": "holds", "explanation": "No such hypothesis."},
            ]
        }
    )

    verified, notes = apply_verification(checked, verification, REFS)

    by_statement = {h.statement: h for h in verified.hypotheses}
    assert by_statement["A"].assessment == "contradicted"
    assert by_statement["A"].contradicting_evidence == ["E3"]
    assert by_statement["A"].review == "E3 predates it."
    assert by_statement["B"].assessment == "supported"  # the uncited verdict was not applied
    # Supported first, contradicted last.
    assert [h.statement for h in verified.hypotheses] == ["B", "A"]
    assert any("without citing evidence" in note for note in notes)
    assert any("does not exist" in note for note in notes)


def test_verification_can_weaken_but_never_strengthen() -> None:
    raw = report(
        hypotheses=[
            {
                "statement": "A",
                "assessment": "supported",
                "supporting_evidence": ["E1"],
                "reasoning": "r",
            },
            {
                "statement": "B",
                "assessment": "weak",
                "supporting_evidence": ["E2"],
                "reasoning": "r",
            },
        ]
    )
    checked, _ = validate_report(raw, REFS)
    verification = RawVerification.model_validate(
        {
            "reviews": [
                {
                    "hypothesis_index": 0,
                    "verdict": "weakened",
                    "explanation": "Another cause fits.",
                },
                {"hypothesis_index": 1, "verdict": "holds", "explanation": "Nothing against it."},
            ]
        }
    )

    verified, _ = apply_verification(checked, verification, REFS)

    assert {h.statement: h.assessment for h in verified.hypotheses} == {"A": "weak", "B": "weak"}

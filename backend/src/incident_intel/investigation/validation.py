"""Checks applied by code to what the model wrote. Pure.

The model is not trusted to cite correctly. After these checks, every reference in the
report points at evidence collected for this investigation, every "observed fact" cites
something, and no hypothesis is called supported without evidence. Whatever was changed is
listed in the notes, which are stored and shown with the report.
"""

from incident_intel.investigation.schemas import (
    Assessment,
    Hypothesis,
    RawFact,
    RawReport,
    RawVerification,
    Report,
)

_ORDER: dict[Assessment, int] = {"supported": 0, "weak": 1, "untested": 2, "contradicted": 3}


def _known(refs: list[str], valid: set[str], notes: list[str], where: str) -> list[str]:
    kept: list[str] = []
    for ref in refs:
        normalized = ref.strip().upper()
        if normalized in valid:
            if normalized not in kept:
                kept.append(normalized)
        else:
            notes.append(
                f"Removed a reference to evidence that does not exist ({ref!r}) from {where}."
            )
    return kept


def validate_report(raw: RawReport, valid_refs: set[str]) -> tuple[Report, list[str]]:
    notes: list[str] = []

    facts: list[RawFact] = []
    for index, fact in enumerate(raw.observed_facts, start=1):
        evidence = _known(fact.evidence, valid_refs, notes, f"observed fact {index}")
        if not evidence:
            notes.append(f"Removed observed fact {index}: it cited no evidence.")
            continue
        facts.append(RawFact(statement=fact.statement, evidence=evidence))

    hypotheses: list[Hypothesis] = []
    for index, hypothesis in enumerate(raw.hypotheses, start=1):
        where = f"hypothesis {index}"
        supporting = _known(hypothesis.supporting_evidence, valid_refs, notes, where)
        contradicting = _known(hypothesis.contradicting_evidence, valid_refs, notes, where)
        assessment: Assessment = hypothesis.assessment
        if not supporting and assessment != "untested":
            notes.append(
                f"Hypothesis {index} was marked '{assessment}' without citing supporting "
                "evidence; it is shown as untested."
            )
            assessment = "untested"
        hypotheses.append(
            Hypothesis(
                statement=hypothesis.statement,
                assessment=assessment,
                supporting_evidence=supporting,
                contradicting_evidence=contradicting,
                reasoning=hypothesis.reasoning,
            )
        )

    return (
        Report(
            summary=raw.summary,
            observed_facts=facts,
            hypotheses=hypotheses,
            unknowns=list(raw.unknowns),
            next_steps=list(raw.next_steps),
        ),
        notes,
    )


def apply_verification(
    report: Report, verification: RawVerification, valid_refs: set[str]
) -> tuple[Report, list[str]]:
    """Fold the verification step's reviews into the report. It can only lower an
    assessment, and a "contradicted" verdict counts only if it cites real evidence."""
    notes: list[str] = []
    hypotheses = [hypothesis.model_copy() for hypothesis in report.hypotheses]
    for review in verification.reviews:
        if review.hypothesis_index >= len(hypotheses):
            notes.append("Ignored a verification review of a hypothesis that does not exist.")
            continue
        number = review.hypothesis_index + 1
        hypothesis = hypotheses[review.hypothesis_index]
        cited = _known(
            review.contradicting_evidence, valid_refs, notes, f"the review of hypothesis {number}"
        )
        hypothesis.review = review.explanation
        if review.verdict == "contradicted":
            if not cited:
                notes.append(
                    f"Verification called hypothesis {number} contradicted without citing "
                    "evidence; its assessment was left unchanged."
                )
                continue
            hypothesis.assessment = "contradicted"
        elif review.verdict == "weakened" and hypothesis.assessment == "supported":
            hypothesis.assessment = "weak"
        for ref in cited:
            if ref not in hypothesis.contradicting_evidence:
                hypothesis.contradicting_evidence.append(ref)

    hypotheses.sort(key=lambda hypothesis: _ORDER[hypothesis.assessment])
    return report.model_copy(update={"hypotheses": hypotheses}), notes


def order_hypotheses(report: Report) -> Report:
    return report.model_copy(
        update={"hypotheses": sorted(report.hypotheses, key=lambda h: _ORDER[h.assessment])}
    )

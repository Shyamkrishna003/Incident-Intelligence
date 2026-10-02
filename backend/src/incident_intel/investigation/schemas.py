"""The report format. ``Raw*`` models are what the model must produce; ``Report`` is what
is stored and shown after code has checked it."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# How well the evidence supports a hypothesis. Ordinal words, never probabilities.
Assessment = Literal["supported", "weak", "untested", "contradicted"]
RawAssessment = Literal["supported", "weak", "untested"]

Text = Annotated[str, StringConstraints(min_length=1, max_length=600)]
Refs = Annotated[list[str], Field(max_length=12)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class RawFact(_Strict):
    statement: Text
    evidence: Refs = Field(default_factory=list)


class RawHypothesis(_Strict):
    statement: Text
    assessment: RawAssessment
    supporting_evidence: Refs = Field(default_factory=list)
    contradicting_evidence: Refs = Field(default_factory=list)
    reasoning: str = Field(min_length=1, max_length=1000)


class RawNextStep(_Strict):
    action: Text
    expected_evidence: Text


class RawReport(_Strict):
    summary: str = Field(min_length=1, max_length=800)
    observed_facts: list[RawFact] = Field(default_factory=list, max_length=12)
    hypotheses: list[RawHypothesis] = Field(default_factory=list, max_length=5)
    unknowns: list[str] = Field(default_factory=list, max_length=8)
    next_steps: list[RawNextStep] = Field(default_factory=list, max_length=6)


class RawReview(_Strict):
    hypothesis_index: int = Field(ge=0)
    verdict: Literal["holds", "weakened", "contradicted"]
    contradicting_evidence: Refs = Field(default_factory=list)
    explanation: str = Field(min_length=1, max_length=600)


class RawVerification(_Strict):
    reviews: list[RawReview] = Field(default_factory=list, max_length=5)


class Hypothesis(BaseModel):
    statement: str
    assessment: Assessment
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    reasoning: str
    # What the verification step said about it, if anything.
    review: str | None = None


class Report(BaseModel):
    """A checked report: every evidence reference in it exists in this investigation."""

    summary: str
    observed_facts: list[RawFact]
    # Ordered: supported, weak, untested, contradicted.
    hypotheses: list[Hypothesis]
    unknowns: list[str]
    next_steps: list[RawNextStep]

"""Detectors: pure functions that judge one value against a window of recent normal values.

A detector returns a ``score``: how far the value is from the baseline's center, measured in
units of the baseline's typical spread. The score is NOT a probability and is not
calibrated; it is only comparable to that detector's own threshold.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Direction = Literal["above", "below"]

# 1.4826 * MAD estimates the standard deviation for normally distributed data.
_MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class Verdict:
    anomalous: bool
    # Signed distance from the baseline center in units of its spread.
    score: float
    # What the baseline said "normal" is, recorded so the judgment can be explained later.
    center: float
    spread: float

    @property
    def direction(self) -> Direction:
        return "above" if self.score >= 0 else "below"


class Detector(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def threshold(self) -> float:
        """The score at or beyond which a value is anomalous (also used to scale severity)."""
        ...

    def evaluate(self, baseline: Sequence[float], value: float) -> Verdict | None:
        """Judge ``value``. None means there is not enough history to judge yet."""
        ...


def _floor_spread(spread: float, center: float, relative_floor: float) -> float:
    """Never let the spread reach zero.

    A perfectly flat series has zero spread, which would make the tiniest change infinitely
    anomalous. The floor says: a change smaller than ``relative_floor`` of the usual level
    (or a tiny absolute amount, for series near zero) is never anomalous by itself.
    """
    return max(spread, abs(center) * relative_floor, 1e-9)


@dataclass(frozen=True)
class RobustZScoreDetector:
    """Distance from the median, in units of the median absolute deviation (MAD).

    Median and MAD barely move when a few extreme values are in the window, so one earlier
    spike does not blind the detector the way a mean and standard deviation would.
    """

    threshold: float = 6.0
    min_history: int = 30
    relative_floor: float = 0.05
    name: str = "robust_zscore"
    version: str = "1"

    def evaluate(self, baseline: Sequence[float], value: float) -> Verdict | None:
        if len(baseline) < self.min_history:
            return None
        center = statistics.median(baseline)
        mad = statistics.median(abs(item - center) for item in baseline)
        spread = _floor_spread(mad * _MAD_TO_SIGMA, center, self.relative_floor)
        score = (value - center) / spread
        return Verdict(abs(score) >= self.threshold, score, center, spread)


@dataclass(frozen=True)
class EwmaDetector:
    """Distance from an exponentially weighted moving average, in units of the exponentially
    weighted standard deviation. Recent values count more, so it follows slow drift, at the
    price of being pulled along by the very change it should flag."""

    threshold: float = 6.0
    min_history: int = 30
    alpha: float = 0.1
    relative_floor: float = 0.05
    name: str = "ewma"
    version: str = "1"

    def evaluate(self, baseline: Sequence[float], value: float) -> Verdict | None:
        if len(baseline) < self.min_history:
            return None
        mean = baseline[0]
        variance = 0.0
        for item in baseline[1:]:
            delta = item - mean
            mean += self.alpha * delta
            variance = (1 - self.alpha) * (variance + self.alpha * delta * delta)
        spread = _floor_spread(math.sqrt(variance), mean, self.relative_floor)
        score = (value - mean) / spread
        return Verdict(abs(score) >= self.threshold, score, mean, spread)


@dataclass(frozen=True)
class StaticThresholdDetector:
    """A fixed upper limit: the simple rule every statistical detector must beat.

    The score is how many times over the limit the value is, scaled so that the limit
    itself scores exactly ``threshold``.
    """

    upper: float
    threshold: float = 1.0
    name: str = "static_threshold"
    version: str = "1"

    def evaluate(self, baseline: Sequence[float], value: float) -> Verdict | None:
        spread = max(abs(self.upper), 1e-9)
        score = value / spread
        return Verdict(value > self.upper, score, 0.0, spread)


def build_detector(name: str, *, threshold: float, min_history: int) -> Detector:
    """The detectors that can run in production, by name."""
    if name == "robust_zscore":
        return RobustZScoreDetector(threshold=threshold, min_history=min_history)
    if name == "ewma":
        return EwmaDetector(threshold=threshold, min_history=min_history)
    raise ValueError(f"unknown detector: {name}")

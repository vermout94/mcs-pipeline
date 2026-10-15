"""Tests, effect size and bootstrap after Arcuri and Briand (2014).

- Mann-Whitney U test, two-sided. The method is set explicitly: exact if both
  samples have fewer than 50 values and the pooled sample has no ties,
  otherwise the normal approximation with tie correction and continuity
  correction. The method of every test is part of the result.
- Vargha and Delaney A12 from the rank sum, equation (1) of Arcuri and Briand:
  A12 = (R1/m - (m + 1)/2)/n, midranks for ties.
- Percentile bootstrap intervals (Efron and Tibshirani, An Introduction to
  the Bootstrap, Sec. 13.3). Both
  samples are resampled independently. The random stream of a comparison
  comes from SHA-256 of the seed and the name of the comparison, so it does
  not depend on the order of the computation or on PYTHONHASHSEED.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
from scipy import stats as sps

EXACT_LIMIT = 50


@dataclass(frozen=True)
class MannWhitney:
    u: float
    p: float
    method: str


def has_ties(*samples) -> bool:
    pooled = np.concatenate([np.asarray(s, dtype=float) for s in samples])
    return np.unique(pooled).size < pooled.size


def mann_whitney(x, y) -> MannWhitney:
    """Two-sided Mann-Whitney U test of x against y. U is the statistic of x."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.size == 0 or y.size == 0:
        raise ValueError("Mann-Whitney U test needs two non-empty samples")
    if np.unique(np.concatenate([x, y])).size == 1:
        # Every value is the same: U = m n / 2 and the p-value is undefined.
        return MannWhitney(x.size * y.size / 2, float("nan"), "none (all values equal)")
    if x.size < EXACT_LIMIT and y.size < EXACT_LIMIT and not has_ties(x, y):
        method = "exact"
        result = sps.mannwhitneyu(x, y, alternative="two-sided", method="exact")
    else:
        method = "asymptotic"
        result = sps.mannwhitneyu(
            x, y, use_continuity=True, alternative="two-sided", method="asymptotic"
        )
    return MannWhitney(float(result.statistic), float(result.pvalue), method)


def a12(x, y) -> float:
    """Vargha and Delaney A12 of x against y: P(X > Y) + P(X = Y)/2."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m, n = x.size, y.size
    ranks = sps.rankdata(np.concatenate([x, y]))
    r1 = ranks[:m].sum()
    return float((r1 / m - (m + 1) / 2) / n)


def a12_rows(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """A12 for every row of two resampled matrices (B x m and B x n)."""
    m, n = xs.shape[1], ys.shape[1]
    ranks = sps.rankdata(np.concatenate([xs, ys], axis=1), axis=1)
    r1 = ranks[:, :m].sum(axis=1)
    return (r1 / m - (m + 1) / 2) / n


@dataclass(frozen=True)
class Bootstrap:
    seed: int
    resamples: int
    confidence: float

    def __post_init__(self) -> None:
        self.order_statistics()

    def order_statistics(self) -> tuple[int, int]:
        """Ranks (1-based) of the interval limits in the ordered replicates.

        Efron and Tibshirani take the B alpha-th and the B (1 - alpha)-th value
        of the ordered replicates. B alpha has to be an integer here.
        """
        alpha = (1 - Fraction(str(self.confidence))) / 2
        lower = self.resamples * alpha
        upper = self.resamples * (1 - alpha)
        if lower.denominator != 1 or upper.denominator != 1 or lower < 1:
            raise ValueError(
                f"resamples * alpha must be a positive integer, got "
                f"{self.resamples} * {alpha}"
            )
        return int(lower), int(upper)

    def generator(self, name: str) -> np.random.Generator:
        digest = hashlib.sha256(f"{self.seed}:{name}".encode("utf-8")).digest()
        sequence = np.random.SeedSequence(int.from_bytes(digest, "big"))
        return np.random.Generator(np.random.PCG64(sequence))

    def resample(self, name: str, x, y) -> tuple[np.ndarray, np.ndarray]:
        """B resamples of x and of y, drawn independently, x first."""
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        rng = self.generator(name)
        ix = rng.integers(0, x.size, size=(self.resamples, x.size))
        iy = rng.integers(0, y.size, size=(self.resamples, y.size))
        return x[ix], y[iy]

    def interval(self, replicates: np.ndarray) -> tuple[float, float]:
        """Percentile interval from the replicates."""
        replicates = np.sort(np.asarray(replicates, dtype=float))
        if replicates.size != self.resamples:
            raise ValueError("number of replicates differs from resamples")
        lower, upper = self.order_statistics()
        return float(replicates[lower - 1]), float(replicates[upper - 1])


@dataclass(frozen=True)
class Comparison:
    """Difference of medians of x and y with bootstrap intervals, and A12."""

    median_x: float
    median_y: float
    difference: float
    difference_ci: tuple[float, float]
    relative: float
    relative_ci: tuple[float, float]
    a12: float
    a12_ci: tuple[float, float]


def compare(bootstrap: Bootstrap, name: str, x, y) -> Comparison:
    """Compare x against y.

    difference = median(x) - median(y), relative = difference / median(y),
    A12 of x against y. All three intervals come from the same resamples.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    xs, ys = bootstrap.resample(name, x, y)
    med_x = float(np.median(x))
    med_y = float(np.median(y))
    med_xs = np.median(xs, axis=1)
    med_ys = np.median(ys, axis=1)
    diff = med_x - med_y
    return Comparison(
        median_x=med_x,
        median_y=med_y,
        difference=diff,
        difference_ci=bootstrap.interval(med_xs - med_ys),
        relative=diff / med_y,
        relative_ci=bootstrap.interval((med_xs - med_ys) / med_ys),
        a12=a12(x, y),
        a12_ci=bootstrap.interval(a12_rows(xs, ys)),
    )


@dataclass(frozen=True)
class StageCost:
    """Median of a stage that only one configuration runs, and its share."""

    median: float
    median_ci: tuple[float, float]
    reference_median: float
    share: float
    share_ci: tuple[float, float]


def stage_cost(bootstrap: Bootstrap, name: str, x, reference) -> StageCost:
    """median(x) and median(x) / median(reference) with bootstrap intervals.

    x are the durations of a stage that the reference configuration does not
    run (0 s there), reference the sum of the stages of the reference.
    """
    x = np.asarray(x, dtype=float)
    r = np.asarray(reference, dtype=float)
    xs, rs = bootstrap.resample(name, x, r)
    med_xs = np.median(xs, axis=1)
    med_rs = np.median(rs, axis=1)
    med_x = float(np.median(x))
    med_r = float(np.median(r))
    return StageCost(
        median=med_x,
        median_ci=bootstrap.interval(med_xs),
        reference_median=med_r,
        share=med_x / med_r,
        share_ci=bootstrap.interval(med_xs / med_rs),
    )

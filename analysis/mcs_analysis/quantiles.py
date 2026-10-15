"""Sample quantiles after definition 8 of Hyndman and Fan (1996).

Hyndman and Fan write every sample quantile as Q(p) = (1 - g) X(j) + g X(j+1)
with j = floor(p n + m) and g = p n + m - j (their equation (1)). Definition 8
sets the plotting position p_k = (k - 1/3)/(n + 1/3), which gives m = (p + 1)/3.
numpy implements it as method "median_unbiased" and clips the order statistics
to X(1) and X(n).
"""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np

NUMPY_METHOD = "median_unbiased"
DEFINITION = (
    "Hyndman and Fan (1996), definition 8: p_k = (k - 1/3)/(n + 1/3), "
    "numpy.quantile method 'median_unbiased'"
)


def quantile(values, p: float) -> float:
    """The p-quantile of the values after definition 8."""
    data = np.asarray(values, dtype=float)
    if data.size == 0:
        raise ValueError("quantile of an empty sample")
    return float(np.quantile(data, p, method=NUMPY_METHOD))


def quantile_formula(values, p: float) -> float:
    """Equation (1) of Hyndman and Fan with m = (p + 1)/3, written out.

    Order statistics outside 1..n are clipped to X(1) and X(n), as numpy does.
    Used by the tests to check the numpy method.
    """
    x = sorted(float(v) for v in values)
    n = len(x)
    if n == 0:
        raise ValueError("quantile of an empty sample")
    m = (p + 1) / 3
    j = math.floor(p * n + m)
    g = p * n + m - j

    def order_statistic(k: int) -> float:
        return x[min(max(k, 1), n) - 1]

    return (1 - g) * order_statistic(j) + g * order_statistic(j + 1)


def equals_maximum(n: int, p: float) -> bool:
    """True if the p-quantile of any n values is the sample maximum X(n).

    That holds when j = floor(p n + (p + 1)/3) >= n. Exact rational arithmetic,
    so that n = 13 for p = 0.95 (p n + m = 13 exactly) is not lost to rounding.
    """
    if n < 1:
        raise ValueError("n must be positive")
    q = Fraction(str(p))
    return math.floor(q * n + (q + 1) / 3) >= n


def largest_n_at_maximum(p: float) -> int:
    """The largest n for which the p-quantile equals the sample maximum."""
    n = 1
    while equals_maximum(n + 1, p):
        n += 1
    return n

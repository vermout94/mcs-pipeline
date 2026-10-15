import numpy as np
import pytest

from mcs_analysis.quantiles import equals_maximum, largest_n_at_maximum, quantile, quantile_formula


@pytest.mark.parametrize("n", [1, 2, 3, 5, 13, 14, 29, 30, 31, 66, 67, 100])
@pytest.mark.parametrize("p", [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
def test_numpy_method_is_definition_8(n, p):
    values = np.random.default_rng(n).lognormal(3.0, 0.4, size=n)
    assert quantile(values, p) == pytest.approx(quantile_formula(values, p), rel=1e-12)


def test_known_values():
    x = list(range(1, 11))
    assert quantile(x, 0.5) == pytest.approx(5.5)
    # p n + m = 2.5 + 1.25/3 = 2.91667: X(2) + 0.91667 (X(3) - X(2))
    assert quantile(x, 0.25) == pytest.approx(2 + 0.25 * 10 + 1.25 / 3 - 2)
    assert quantile(x, 0.95) == 10
    # n = 30: p95 lies between X(29) and X(30), p99 is X(30)
    y = list(range(1, 31))
    assert quantile(y, 0.95) == pytest.approx(29.15)
    assert quantile(y, 0.99) == 30


def test_maximum_rule():
    assert equals_maximum(30, 0.99)
    assert equals_maximum(66, 0.99)
    assert not equals_maximum(67, 0.99)
    assert equals_maximum(13, 0.95)
    assert not equals_maximum(14, 0.95)
    assert not equals_maximum(30, 0.95)
    assert largest_n_at_maximum(0.99) == 66
    assert largest_n_at_maximum(0.95) == 13


@pytest.mark.parametrize("n", range(1, 80))
def test_maximum_rule_matches_quantile(n):
    values = np.arange(1.0, n + 1.0)
    for p in (0.95, 0.99):
        assert (quantile(values, p) == n) == equals_maximum(n, p)

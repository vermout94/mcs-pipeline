import math

import numpy as np
import pytest

from mcs_analysis.stats import Bootstrap, a12, a12_rows, compare, mann_whitney, stage_cost


def test_a12_example_of_arcuri_and_briand():
    # Arcuri and Briand, p. 235: X = {42, 11, 7}, Y = {1, 20, 5}, R1 = 13
    assert a12([42, 11, 7], [1, 20, 5]) == pytest.approx((13 / 3 - 2) / 3)
    assert a12([42, 11, 7], [1, 20, 5]) == pytest.approx(7 / 9)


def test_a12_limits():
    assert a12([5, 6, 7], [1, 2, 3]) == 1.0
    assert a12([1, 2, 3], [5, 6, 7]) == 0.0
    assert a12([1, 2, 3], [1, 2, 3]) == 0.5


def test_a12_equals_u_over_mn():
    rng = np.random.default_rng(1)
    x, y = rng.normal(size=17), rng.normal(size=23)
    assert a12(x, y) == pytest.approx(mann_whitney(x, y).u / (17 * 23))


def test_a12_rows():
    rng = np.random.default_rng(2)
    xs, ys = rng.normal(size=(5, 7)), rng.normal(size=(5, 9))
    expected = [a12(x, y) for x, y in zip(xs, ys)]
    assert a12_rows(xs, ys) == pytest.approx(expected)


def test_mann_whitney_exact_for_separated_samples():
    test = mann_whitney([6, 7, 8, 9, 10], [1, 2, 3, 4, 5])
    assert test.method == "exact"
    assert test.u == 25
    assert test.p == pytest.approx(2 / math.comb(10, 5), rel=1e-12)


def test_mann_whitney_asymptotic_with_ties_or_large_samples():
    assert mann_whitney([1, 2, 2, 3], [2, 4, 5, 6]).method == "asymptotic"
    rng = np.random.default_rng(3)
    assert mann_whitney(rng.normal(size=50), rng.normal(size=10)).method == "asymptotic"
    assert mann_whitney(rng.normal(size=49), rng.normal(size=49)).method == "exact"


def test_mann_whitney_identical_samples():
    test = mann_whitney([1, 2, 3, 4], [4, 3, 2, 1])
    assert test.method == "asymptotic"
    assert test.u == 8
    assert test.p == 1.0


def test_mann_whitney_all_values_equal():
    test = mann_whitney([60.0] * 4, [60.0] * 5)
    assert test.method == "none (all values equal)"
    assert test.u == 10 and math.isnan(test.p)


def test_bootstrap_order_statistics():
    assert Bootstrap(1, 10_000, 0.95).order_statistics() == (250, 9750)
    assert Bootstrap(1, 1_000, 0.95).order_statistics() == (25, 975)
    assert Bootstrap(1, 200, 0.95).order_statistics() == (5, 195)
    with pytest.raises(ValueError):
        Bootstrap(1, 10, 0.95)


def test_bootstrap_streams():
    b = Bootstrap(7, 1_000, 0.95)
    x, y = np.arange(10.0), np.arange(12.0)
    first = b.resample("a", x, y)
    again = b.resample("a", x, y)
    other = b.resample("b", x, y)
    assert np.array_equal(first[0], again[0]) and np.array_equal(first[1], again[1])
    assert not np.array_equal(first[0], other[0])
    assert not np.array_equal(first[0], Bootstrap(8, 1_000, 0.95).resample("a", x, y)[0])


def test_compare_constant_samples():
    c = compare(Bootstrap(7, 1_000, 0.95), "constant", [5.0] * 10, [3.0] * 10)
    assert c.difference == 2.0 and c.difference_ci == (2.0, 2.0)
    assert c.relative == pytest.approx(2 / 3) and c.relative_ci == pytest.approx((2 / 3, 2 / 3))
    assert c.a12 == 1.0 and c.a12_ci == (1.0, 1.0)


def test_compare_known_shift():
    y = np.random.default_rng(4).lognormal(3.0, 0.1, size=31)
    x = y[::-1] + 5.0
    c = compare(Bootstrap(7, 10_000, 0.95), "shift", x, y)
    assert c.difference == pytest.approx(5.0)
    assert c.difference_ci[0] < 5.0 < c.difference_ci[1]
    assert c.relative == pytest.approx(5.0 / np.median(y))


def test_stage_cost():
    reference = np.linspace(40.0, 44.0, 9)
    s = stage_cost(Bootstrap(7, 1_000, 0.95), "stage", [4.0] * 9, reference)
    assert s.median == 4.0 and s.median_ci == (4.0, 4.0)
    assert s.share == pytest.approx(4.0 / 42.0)
    assert s.share_ci[0] <= 4.0 / 42.0 <= s.share_ci[1]

import math

from scoring import base_score, streak_multiplier


def test_base_score_midpoint():
    assert base_score(1000, 1000) == 250


def test_base_score_harder_is_higher():
    score = base_score(1000, 1200)
    assert score > 250


def test_base_score_easier_is_lower():
    score = base_score(1000, 800)
    assert score < 250


def test_streak_multiplier_caps():
    assert math.isclose(streak_multiplier(0), 1.0)
    assert math.isclose(streak_multiplier(7), 1.35)
    assert math.isclose(streak_multiplier(10), 1.35)

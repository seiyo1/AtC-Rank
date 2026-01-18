import math


def base_score(rating: int, difficulty: int) -> int:
    exponent = (rating - difficulty) / 400.0
    score = 500.0 / (1.0 + math.exp(exponent))
    return round(score)


def streak_multiplier(streak: int) -> float:
    return 1.0 + min(streak, 7) * 0.05

import random

_SYLLABLES = ["ba", "ke", "lo", "mi", "nu", "ra", "so", "ti", "vu", "xe", "za", "qui"]


def _vocabulary():
    rng = random.Random(20261017)
    words = set()
    while len(words) < 400:
        words.add("".join(rng.choice(_SYLLABLES) for _ in range(rng.randrange(1, 4))))
    return sorted(words)


VOCABULARY = _vocabulary()


def _decorate(rng, word):
    roll = rng.randrange(20)
    if roll == 0:
        return word.capitalize()
    if roll == 1:
        return word.upper()
    if roll == 2:
        return word + rng.choice([",", ".", "!", ";", "?"])
    if roll == 3:
        return word + str(rng.randrange(1000))
    if roll == 4:
        return "(" + word + ")"
    if roll == 5:
        return str(rng.randrange(100000))
    return word


def generate_input(seed, size):
    """size words spread over lines of 5-15 tokens; skewed so some words are far commoner."""
    rng = random.Random(seed)
    lines, remaining = [], size
    while remaining > 0:
        count = min(remaining, rng.randrange(5, 16))
        tokens = [_decorate(rng, VOCABULARY[int(len(VOCABULARY) * rng.random() ** 2)])
                  for _ in range(count)]
        lines.append(rng.choice([" ", " ", "  ", " - "]).join(tokens))
        remaining -= count
    return {"lines": lines, "k": 10}

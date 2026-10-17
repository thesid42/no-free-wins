import re

_WORD = re.compile(r"[a-z]+")


def top_words(data):
    """Return the k most frequent words as [[word, count], ...], ties broken alphabetically."""
    words = []
    for line in data["lines"]:
        words.extend(_WORD.findall(line.lower()))
    unique = sorted(set(words))
    counts = []
    for word in unique:
        counts.append([word, sum(1 for other in words if other == word)])
    counts.sort(key=lambda pair: (-pair[1], pair[0]))
    return counts[:data["k"]]

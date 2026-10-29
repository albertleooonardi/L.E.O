"""Representative coding tasks with hidden acceptance tests (spec §9).

Prompts state the full contract; tests are never shown to any model. Each test file imports the
candidate solution from ``solution.py`` and exits non-zero on the first failed assertion.
"""

TASKS = [
    {
        "id": "roman",
        "prompt": "Write a Python function roman_to_int(s: str) -> int that converts a Roman numeral (I, V, X, L, C, D, M) "
                  "to an integer in the range 1-3999. Only canonical subtractive forms are valid (IV, IX, XL, XC, CD, CM). "
                  "Raise ValueError for an empty string, unknown characters, lowercase input, or non-canonical numerals such as "
                  "IIII, VV, IC, or IIV.",
        "tests": """
from solution import roman_to_int
assert roman_to_int("III") == 3
assert roman_to_int("MCMXCIV") == 1994
assert roman_to_int("MMMCMXCIX") == 3999
assert roman_to_int("XLII") == 42
for bad in ["", "IIII", "VV", "IC", "IIV", "ABC", "mcm", "MMMM", "XM", "LL"]:
    try:
        roman_to_int(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted invalid numeral {bad!r}")
""",
    },
    {
        "id": "intervals",
        "prompt": "Write a Python function merge_intervals(intervals: list[list[int]]) -> list[list[int]] that merges "
                  "overlapping or touching closed intervals ([1, 2] and [2, 3] merge into [1, 3]). The input may be unsorted "
                  "or empty. Return a new list sorted by start and do not modify the input list or its inner lists.",
        "tests": """
from solution import merge_intervals
assert merge_intervals([]) == []
assert merge_intervals([[1, 3], [2, 6], [8, 10], [15, 18]]) == [[1, 6], [8, 10], [15, 18]]
assert merge_intervals([[1, 2], [2, 3]]) == [[1, 3]]
assert merge_intervals([[5, 7], [1, 2], [3, 4]]) == [[1, 2], [3, 4], [5, 7]]
assert merge_intervals([[1, 10], [2, 3], [4, 5]]) == [[1, 10]]
original = [[3, 4], [1, 2], [2, 3]]
snapshot = [list(i) for i in original]
assert merge_intervals(original) == [[1, 4]]
assert original == snapshot, "input was modified"
""",
    },
    {
        "id": "duration",
        "prompt": "Write a Python function parse_duration(text: str) -> int that converts a duration such as '1h30m', '45s', "
                  "'2h 5m 10s', or '1d' into total seconds. Units are d, h, m, s; each unit may appear at most once and units "
                  "must appear in that descending order; amounts are non-negative integers; spaces between parts are allowed. "
                  "Raise ValueError for empty or blank input, unknown units, a unit without a number, a number without a unit, "
                  "repeated units, or units out of order.",
        "tests": """
from solution import parse_duration
assert parse_duration("45s") == 45
assert parse_duration("1h30m") == 5400
assert parse_duration("2h 5m 10s") == 7510
assert parse_duration("1d") == 86400
assert parse_duration("0s") == 0
assert parse_duration(" 1m ") == 60
for bad in ["", "   ", "5x", "h", "10", "1h1h", "5m1h", "1.5h", "-1s"]:
    try:
        parse_duration(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted invalid duration {bad!r}")
""",
    },
    {
        "id": "topk",
        "prompt": "Write a Python function top_k_words(text: str, k: int) -> list[tuple[str, int]] that returns the k most "
                  "frequent words with their counts. Words are case-insensitive maximal runs of ASCII letters and apostrophes "
                  "(so \"don't\" is one word) and are returned in lowercase. Sort by count descending, then alphabetically. "
                  "If k exceeds the number of distinct words, return all of them; if k <= 0, return an empty list.",
        "tests": """
from solution import top_k_words
assert top_k_words("b a c a b a", 2) == [("a", 3), ("b", 2)]
assert top_k_words("Apple apple BANANA banana cherry", 3) == [("apple", 2), ("banana", 2), ("cherry", 1)]
assert top_k_words("Don't stop. DON'T stop!", 1) == [("don't", 2)]
assert top_k_words("one two", 10) == [("one", 1), ("two", 1)]
assert top_k_words("one two", 0) == []
assert top_k_words("one two", -1) == []
assert top_k_words("", 3) == []
assert top_k_words("x1y x-y", 5) == [("x", 2), ("y", 2)]
""",
    },
    {
        "id": "lru",
        "prompt": "Write a Python class LRUCache with __init__(self, capacity: int), get(self, key) and put(self, key, value). "
                  "get returns the value or -1 when missing and marks the key as most recently used. put inserts or updates a "
                  "key, marks it most recently used, and evicts the least recently used key when over capacity. Raise "
                  "ValueError if capacity is not a positive int (including bool values).",
        "tests": """
from solution import LRUCache
cache = LRUCache(2)
cache.put(1, 1); cache.put(2, 2)
assert cache.get(1) == 1
cache.put(3, 3)
assert cache.get(2) == -1
cache.put(1, 10)
cache.put(4, 4)
assert cache.get(3) == -1 and cache.get(1) == 10 and cache.get(4) == 4
single = LRUCache(1)
single.put("a", 1); single.put("b", 2)
assert single.get("a") == -1 and single.get("b") == 2
for bad in [0, -1, 1.5, True, "2"]:
    try:
        LRUCache(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted capacity {bad!r}")
""",
    },
    {
        "id": "ipv4",
        "prompt": "Write a Python function is_valid_ipv4(s: str) -> bool that returns True only for dotted-decimal IPv4 "
                  "addresses: exactly four parts, each made only of ASCII digits, value 0-255, and no leading zeros (\"0\" is "
                  "fine, \"01\" is not). No surrounding whitespace, signs, or empty parts are allowed.",
        "tests": """
from solution import is_valid_ipv4
for good in ["0.0.0.0", "255.255.255.255", "192.168.1.1", "10.0.0.10"]:
    assert is_valid_ipv4(good), good
for bad in ["", "1.1.1", "1.1.1.1.1", "256.1.1.1", "01.1.1.1", "1.1.1.+1", " 1.1.1.1", "1..1.1", "a.b.c.d", "1.1.1.1 ", "١.1.1.1", "1.1.1.-0"]:
    assert not is_valid_ipv4(bad), bad
""",
    },
]

"""Google Business Profile categories, for the Local Offline Business search.

The list (app/data/gmb_categories.json, ~4,000 names) is Google's own category
vocabulary, which is what the Local Business Data API's `subtypes` filter
matches against. Offering exactly these names in the UI means whatever the
user picks is a value the search can actually filter on.

Two lookups are served from it:

* `search()`   -- type-ahead for the category dropdown.
* `related()`  -- categories close to a keyword and/or the ones already
  picked ("Dentist" -> "Dental clinic", "Cosmetic dentist", ...), offered as
  one-click suggestions. Selecting none is always valid: the keyword alone is
  a complete search.
"""

import json
import math
import re
from functools import lru_cache
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "gmb_categories.json"

# Longest first, so "dentistry" loses "istry" rather than just "y".
_SUFFIXES = (
    "istry", "ists", "ings", "ants", "ist", "ing", "ers", "ant", "als", "ies",
    "er", "al", "es", "ry", "s", "y",
)  # fmt: skip
_MIN_STEM = 4

# A stem carried by more categories than this says nothing about what a
# business is ("store", "service", "supplier", "restaurant"). Such stems still
# add a little to a score, but cannot on their own make two categories related.
_COMMON_STEM_DF = 60


def _stem(token: str) -> str:
    """Crude suffix stripping: enough to meet "dentist" with "dental" and
    "plumber" with "plumbing", which is all the matching here needs."""
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
            return token[: -len(suffix)]

    return token


def _stems(text: str) -> set[str]:
    return {
        _stem(token) for token in re.findall(r"\w+", text.casefold()) if len(token) > 2
    }


class _Index:
    def __init__(self, names: list[str]) -> None:
        self.names: tuple[str, ...] = tuple(names)
        self.by_key: dict[str, str] = {name.casefold(): name for name in names}
        self.stems: dict[str, set[str]] = {name: _stems(name) for name in names}

        frequency: dict[str, int] = {}
        for stems in self.stems.values():
            for stem in stems:
                frequency[stem] = frequency.get(stem, 0) + 1
        self.frequency = frequency

    def weight(self, stem: str) -> float:
        """Rare stems count for more, the way IDF does."""
        return 1.0 / (1.0 + math.log(self.frequency.get(stem, 1)))


@lru_cache(maxsize=1)
def _index() -> _Index:
    with _DATA_FILE.open(encoding="utf-8") as handle:
        names = json.load(handle)

    return _Index([str(name) for name in names])


def total() -> int:
    return len(_index().names)


def canonical(name: str) -> str | None:
    """The category exactly as Google spells it, or None if it is not one."""
    return _index().by_key.get(" ".join(name.split()).casefold())


def search(query: str, *, limit: int = 20) -> list[str]:
    """Type-ahead. Best first: exact, then "starts with", then a word that
    starts with it, then anywhere. Blank returns the head of the list so the
    dropdown is not empty before the first keystroke."""
    index = _index()
    needle = " ".join(query.split()).casefold()

    if not needle:
        return list(index.names[:limit])

    ranked: list[tuple[int, int, str]] = []
    for name in index.names:
        key = name.casefold()
        position = key.find(needle)
        if position < 0:
            continue

        if key == needle:
            rank = 0
        elif position == 0:
            rank = 1
        elif key[position - 1] in " -/(&":
            rank = 2
        else:
            rank = 3

        ranked.append((rank, len(name), name))

    ranked.sort()

    return [name for _, _, name in ranked[:limit]]


def related(
    keyword: str | None = None,
    categories: list[str] | None = None,
    *,
    limit: int = 12,
) -> list[str]:
    """Categories close to the keyword and/or the ones already selected.

    Closeness is shared word stems, weighted so that rare stems ("dent",
    "plumb") decide and ubiquitous ones ("store", "service") barely register.
    A candidate needs at least one uncommon stem in common, otherwise every
    "... store" would be related to every other. Already-selected categories
    are never suggested back.
    """
    index = _index()
    selected = {c for c in (canonical(name) for name in categories or []) if c}

    keyword_stems = _stems(keyword) if keyword else set()
    category_stems: set[str] = set()
    for name in selected:
        category_stems |= index.stems[name]

    def uncommon(stems: set[str]) -> set[str]:
        return {s for s in stems if 0 < index.frequency.get(s, 0) <= _COMMON_STEM_DF}

    # The keyword says what the user is after, so it anchors the suggestions:
    # with "dentist" typed and "Cosmetic dentist" picked, "Cosmetics store" is
    # noise even though it shares a word with the pick. The picked categories
    # only anchor when there is no keyword the list knows anything about.
    anchors = uncommon(keyword_stems) or uncommon(category_stems)
    if not anchors:
        return []

    wanted = keyword_stems | category_stems

    scored: list[tuple[float, int, str]] = []
    for name in index.names:
        if name in selected:
            continue

        stems = index.stems[name]
        if not (anchors & stems):
            continue

        score = sum(index.weight(stem) for stem in wanted & stems)
        scored.append((-score, len(name), name))

    scored.sort()

    return [name for _, _, name in scored[:limit]]


__all__ = ["canonical", "related", "search", "total"]

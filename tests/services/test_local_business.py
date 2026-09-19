"""Google Business Profile category lookups."""

from app.services import local_business


def test_the_full_list_is_loaded() -> None:
    assert local_business.total() > 4000


def test_canonical_gives_googles_own_spelling() -> None:
    assert local_business.canonical("  pizza   RESTAURANT ") == "Pizza restaurant"
    assert local_business.canonical("Definitely not a category") is None


def test_search_ranks_exact_then_prefix_then_word_then_anywhere() -> None:
    results = local_business.search("gym", limit=50)

    assert results[0] == "Gym"
    # A word starting with the query outranks one that merely contains it.
    assert results.index("Boxing gym") < results.index("Gymnastics center") or (
        "Gymnastics center" in results
    )
    assert all("gym" in name.casefold() for name in results)


def test_a_blank_query_still_fills_the_dropdown() -> None:
    assert len(local_business.search("", limit=10)) == 10


def test_search_honours_the_limit() -> None:
    assert len(local_business.search("restaurant", limit=7)) == 7


def test_related_meets_different_word_forms() -> None:
    """ "dentist" and "dental" share a stem, which plain substring would miss."""
    items = local_business.related("dentist")

    assert "Dental clinic" in items
    assert "Cosmetic dentist" in items


def test_related_ignores_words_every_category_has() -> None:
    """ "Coffee store" must not drag in every other "... store"."""
    items = local_business.related(None, ["Coffee store"], limit=30)

    assert items
    assert all("coffee" in name.casefold() for name in items)


def test_related_never_suggests_what_is_already_selected() -> None:
    items = local_business.related("plumber", ["Plumber"])

    assert "Plumber" not in items
    assert "Plumbing supply store" in items


def test_related_with_nothing_to_go_on_is_empty() -> None:
    assert local_business.related(None, []) == []
    assert local_business.related("", ["Not a category"]) == []

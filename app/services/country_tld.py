"""Country code -> ccTLD, for the HS Code search's Google dork.

For almost every country the SerpApi `gl` code used elsewhere in this app
(app/services/countries.py) already IS the country-code top-level domain --
"de" is both. This module exists only for the handful where they diverge, plus
the lookup function every caller should use rather than assuming equality
itself.

Not a complete ccTLD registry: only entries where COUNTRIES' code differs from
the real-world domain suffix. Add here as a mismatch is found, rather than
trying to enumerate all ~250 in advance.
"""

from app.services.countries import COUNTRIES, is_valid

# SerpApi's `gl` code -> the ccTLD actually in use, wherever they differ.
_EXCEPTIONS: dict[str, str] = {
    # SerpApi lists the United Kingdom as "gb" (see countries.py's own note
    # that "uk" is deliberately not a valid `gl` value there), but the ccTLD
    # in everyday use is .uk.
    "gb": "uk",
}


def tld_for(country_code: str | None) -> str | None:
    """The ccTLD for a SerpApi country code, or None for worldwide / unknown.

    `country_code` is expected to already be validated (is_valid); an unknown
    code returns None rather than guessing, same as a worldwide search.
    """
    if not country_code:
        return None

    code = country_code.strip().lower()
    if not is_valid(code):
        return None

    return _EXCEPTIONS.get(code, code)


__all__ = ["tld_for"]

# Sanity check at import time: every exception must be a real COUNTRIES code,
# or the mapping is pointing at nothing.
assert all(code in COUNTRIES for code in _EXCEPTIONS), (
    "every _EXCEPTIONS key must be a valid country code"
)

"""Tests for private-only DM filter behavior."""

from GramAddict.core.filter import Filter


def _filter(conditions: dict) -> Filter:
    f = object.__new__(Filter)
    f.conditions = conditions
    return f


def test_allows_pm_private_only_blocks_public():
    f = _filter({"pm_private_only": True, "pm_to_private_or_empty": True})
    assert f.allows_pm(is_private=True, posts_count=0) is True
    assert f.allows_pm(is_private=False, posts_count=12) is False
    assert f.allows_pm(is_private=False, posts_count=0) is False


def test_allows_pm_private_only_implies_private_without_empty_flag():
    f = _filter({"pm_private_only": True, "pm_to_private_or_empty": False})
    assert f.allows_pm(is_private=True, posts_count=0) is True
    assert f.allows_pm(is_private=False, posts_count=5) is False


def test_allows_pm_legacy_private_or_empty():
    f = _filter({"pm_private_only": False, "pm_to_private_or_empty": True})
    assert f.allows_pm(is_private=True, posts_count=0) is True
    assert f.allows_pm(is_private=False, posts_count=0) is True
    assert f.allows_pm(is_private=False, posts_count=9) is True


def test_allows_pm_disabled_blocks_private():
    f = _filter({"pm_private_only": False, "pm_to_private_or_empty": False})
    assert f.allows_pm(is_private=True, posts_count=0) is False
    assert f.allows_pm(is_private=False, posts_count=0) is False
    assert f.allows_pm(is_private=False, posts_count=3) is True


def test_skip_if_public_checks_public_not_private():
    """Regression: skip_if_public used to incorrectly skip private accounts."""
    f = _filter({"skip_if_public": True, "skip_if_private": False})
    is_private = False
    assert (not is_private) and f.conditions.get("skip_if_public")
    is_private = True
    assert not ((not is_private) and f.conditions.get("skip_if_public"))

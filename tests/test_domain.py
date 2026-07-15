"""Unit tests for pure domain logic (no DB, no network)."""
import pytest

from domain import (
    DRAFT_TITLE_MAX_CHARS,
    DraftValidationError,
    is_active_draft_status,
    normalize_draft_title,
)


def test_is_active_draft_status_defaults_to_draft():
    assert is_active_draft_status(None) is True
    assert is_active_draft_status("draft") is True
    assert is_active_draft_status("published") is False


def test_normalize_draft_title_trims_and_collapses_whitespace():
    assert normalize_draft_title("  My   Site ") == "My Site"


def test_normalize_draft_title_rejects_empty():
    with pytest.raises(DraftValidationError):
        normalize_draft_title("   ")


def test_normalize_draft_title_rejects_non_string():
    with pytest.raises(DraftValidationError):
        normalize_draft_title(None)


def test_normalize_draft_title_rejects_too_long():
    with pytest.raises(DraftValidationError):
        normalize_draft_title("x" * (DRAFT_TITLE_MAX_CHARS + 1))


@pytest.mark.parametrize("bad", ["hi<script>", "a/b", 'q"uote', "back`tick"])
def test_normalize_draft_title_rejects_forbidden_chars(bad):
    with pytest.raises(DraftValidationError):
        normalize_draft_title(bad)

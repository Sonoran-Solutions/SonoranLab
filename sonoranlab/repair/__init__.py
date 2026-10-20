"""Opt-in repair interchange; importing the deterministic core never loads this package."""


class Refused(ValueError):
    """An unsafe workspace or a failed repair check (CLI exit 1)."""


class EditorUnavailable(Refused):
    """The pinned runtime or its OS containment is unavailable."""

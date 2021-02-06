"""Exceptions."""
from __future__ import annotations

from enum import Enum


class AnkiError(Enum):
    """AnkiConnect error."""

    #: Duplicate note
    DUPLICATE_NOTE = 'cannot create note because it is a duplicate'
    #: Note not found
    NOTE_NOT_FOUND = 'note was not found'
    #: Model exists
    MODEL_EXISTS = 'Model name already exists'
    #: Unknown error
    UNKNOWN = 'unknown'

    @classmethod
    def _missing_(cls, value: object) -> AnkiError:
        """Return unknown error."""
        if isinstance(value, str):
            if 'note was not found' in value:
                return cls.NOTE_NOT_FOUND
        return cls.UNKNOWN

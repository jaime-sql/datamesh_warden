"""ID generation helpers.

ULIDs are used (rather than UUID4) because they are lexicographically
sortable by creation time, which keeps Firestore document listings and local
debugging output naturally ordered.
"""

from __future__ import annotations

from ulid import ULID


def new_id() -> str:
    return str(ULID())

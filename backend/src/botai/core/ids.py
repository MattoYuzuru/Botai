from ulid import ULID


def new_id() -> str:
    """26-char Crockford ULID, sortable by creation time."""
    return str(ULID())

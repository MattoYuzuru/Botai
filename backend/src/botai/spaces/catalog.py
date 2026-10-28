"""Name-only suggestions. No origins, login presets, source recipes or network access."""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=1, max_length=120)
    short_title: str = Field(min_length=1, max_length=16)
    aliases: tuple[str, ...]


def name_key(value: str) -> str:
    """Ignore punctuation, spacing and Cyrillic case, including е/ё."""
    return "".join(ch for ch in value.casefold().replace("ё", "е") if ch.isalnum())


def names(entry: CatalogEntry) -> tuple[str, ...]:
    return (entry.title, entry.short_title, entry.id, *entry.aliases)


@lru_cache(maxsize=1)
def entries() -> tuple[CatalogEntry, ...]:
    path = Path(__file__).with_name("university-names.json")
    return TypeAdapter(tuple[CatalogEntry, ...]).validate_json(path.read_bytes())


def identify(*values: str) -> CatalogEntry | None:
    keys = {name_key(value) for value in values if value}
    return next((row for row in entries() if keys.intersection(map(name_key, names(row)))), None)

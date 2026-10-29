"""Bounded rendered replay contract. Targets are compiled by the executor from observed DOM."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_DATE_TEMPLATE = re.compile(
    r"^\{(?:today|weekStart|(?:plusDays|minusDays):(?:[1-9]|[1-9][0-9]|[12][0-9]{2}|3[0-5][0-9]|36[0-6]))\}$"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


class ReadTarget(_Model):
    selector: str = Field(min_length=1, max_length=300)
    tag: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    role: str = Field(default="", max_length=40)
    label: str = Field(default="", max_length=120)
    controls: str = Field(default="", max_length=120)


class ReadFrame(_Model):
    selector: str = Field(min_length=1, max_length=300)
    title: str = Field(default="", max_length=120)
    name: str = Field(default="", max_length=120)


class BrowserStep(_Model):
    op: Literal["activate_tab", "set_date", "select_option", "next_link", "wait_ready"]
    target: ReadTarget
    frame_path: list[ReadFrame] = Field(default_factory=list, max_length=3, alias="framePath")
    region: ReadTarget | None = None
    value: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def shape(self) -> Self:
        expected = {"set_date": "input", "select_option": "select", "next_link": "a"}
        if self.op in expected and self.target.tag != expected[self.op]:
            raise ValueError("browser step target has the wrong native type")
        if self.op in {"set_date", "select_option"}:
            if self.value is None or self.region is None:
                raise ValueError("a filter requires a value and an observed owning region")
            if self.op == "set_date" and _DATE_TEMPLATE.fullmatch(self.value) is None:
                raise ValueError("date values must use supported ISO date templates")
        elif self.value is not None:
            raise ValueError("this browser step does not accept a value")
        if self.op == "activate_tab" and (self.target.role != "tab" or self.region is None):
            raise ValueError("tab replay requires an observed tab and its panel")
        return self


class BrowserPagination(_Model):
    next: BrowserStep
    max_pages: int = Field(default=3, ge=1, le=3, alias="maxPages")

    @model_validator(mode="after")
    def next_link_only(self) -> Self:
        if self.next.op != "next_link":
            raise ValueError("browser pagination accepts only a verified next link")
        return self


class BrowserPlan(_Model):
    version: Literal[1] = 1
    steps: list[BrowserStep] = Field(default_factory=list, max_length=8)
    frame_path: list[ReadFrame] = Field(default_factory=list, max_length=3, alias="framePath")
    empty_target: ReadTarget | None = Field(default=None, alias="emptyTarget")
    pagination: BrowserPagination | None = None

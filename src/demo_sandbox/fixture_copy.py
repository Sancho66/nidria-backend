"""Authored labels for the fictional data populated in a demonstration."""

import json
from functools import cache
from pathlib import Path
from typing import TypedDict

from src.admin.demo_schema import PacketLanguage


class FixtureCopy(TypedDict):
    journeys: dict[str, str]
    documents: dict[str, list[str]]
    step_titles: dict[str, list[str]]
    labels: dict[str, str]


@cache
def fixture_copy(language: PacketLanguage) -> FixtureCopy:
    path = Path(__file__).with_name("locales") / f"{language}.json"
    result: FixtureCopy = json.loads(path.read_text(encoding="utf-8"))
    return result

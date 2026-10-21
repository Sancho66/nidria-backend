"""Immutable demo inputs. The production application never imports this module."""

import hashlib
import json
from collections import Counter
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_PATH = Path(__file__).resolve().parents[2] / "demo-sandbox" / "catalog.json"


class DemoRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=200)
    language: Literal["fr", "en"]
    public_code: str = Field(pattern=r"^[a-f0-9]{32}$")
    promised_journey: str
    state: dict[str, Any]
    journey_import: dict[str, Any]

    @property
    def agency_slug(self) -> str:
        return f"demo-sandbox-{self.public_code}"

    @model_validator(mode="after")
    def check_fixture(self) -> "DemoRecord":
        state = self.state
        if (
            state.get("id") != self.id
            or state.get("language") != self.language
            or state.get("fictional_data") is not True
            or state.get("read_only") is not True
            or state.get("progress") != {"done": 2, "total": 7}
        ):
            raise ValueError("Demo identity, language or fictional state mismatch")
        steps = state["steps"]
        source = self.journey_import["parcours"]
        if len(steps) != 7 or len(source["etapes"]) != 7:
            raise ValueError("Each letter must retain its seven original steps")
        if source["langue_par_defaut"] != self.language:
            raise ValueError("Import language differs from the letter")
        for step, original in zip(steps, source["etapes"], strict=True):
            if step["ref"] != original["ref"] or step["label"] != original["nom"][self.language]:
                raise ValueError("State differs from the approved journey import")
        if [step["status"] for step in steps] != ["done", "done", "in_progress"] + ["todo"] * 4:
            raise ValueError("Unexpected demo progression")
        if any(doc.get("fictional") is not True for doc in state["documents"]):
            raise ValueError("Only fictional documents are permitted")
        if any(reminder.get("delivery_enabled") is not False for reminder in state["reminders"]):
            raise ValueError("Demo reminders must not be delivered")
        if state["signature"].get("provider_call_enabled") is not False:
            raise ValueError("Demo signatures must not call a provider")
        return self


class DemoCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    source_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    records: list[DemoRecord]

    @model_validator(mode="after")
    def check_batch(self) -> "DemoCatalog":
        if len(self.records) != 40 or Counter(r.language for r in self.records) != {
            "fr": 25,
            "en": 15,
        }:
            raise ValueError("Expected exactly forty letters: 25 FR and 15 EN")
        if (
            len({r.public_code for r in self.records}) != 40
            or len({r.id for r in self.records}) != 40
        ):
            raise ValueError("Duplicate public code or cabinet identifier")
        return self

    @cached_property
    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(payload).hexdigest()

    def by_code(self, code: str) -> DemoRecord | None:
        return next((record for record in self.records if record.public_code == code), None)

    def by_id(self, record_id: str) -> DemoRecord | None:
        return next((record for record in self.records if record.id == record_id), None)


def load_catalog() -> DemoCatalog:
    return DemoCatalog.model_validate_json(CATALOG_PATH.read_bytes())

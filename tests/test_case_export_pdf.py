"""Case PDF export — Unicode font, translated chrome, readable values.

Every assertion reads the TEXT OF THE PDF itself, decoded from its content
streams through each embedded font's ToUnicode map (`_pdf_text`, no PDF
dependency): with an embedded TTF the glyphs are 2-byte font codes, so a
bytes `in` check on the raw output can never see a name — and could never
fail either. Reintroducing a latin-1 squash (`_latin1`) makes the Cyrillic
test fail on « ? »."""

import logging
import re
import uuid
import zlib
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.cases import case_export
from src.cases.case_export import build_case_pdf
from src.core.enums import ActorType, CaseStatus, MaritalStatus, Sex
from src.core.i18n import SUPPORTED_LANGUAGES
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase

# --- PDF text extraction (test-only, no dependency) ---------------------------------------

_OBJ_HEAD = re.compile(rb"(\d+) 0 obj\s")
_CONTENT_TOKEN = re.compile(rb"/(F\d+)\s+[\d.]+\s+Tf|\((?:\\.|[^\\)])*\)|\bBT\b|\bET\b", re.S)
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _pdf_objects(data: bytes) -> dict[int, tuple[bytes, bytes]]:
    """{object number: (dictionary, decoded stream)}. Streams are sliced by
    their /Length, so the binary font programs never derail the walk."""
    objects: dict[int, tuple[bytes, bytes]] = {}
    pos = 0
    while (head := _OBJ_HEAD.search(data, pos)) is not None:
        end = data.index(b"endobj", head.end())
        keyword = data.find(b"stream", head.end(), end)
        if keyword == -1:
            objects[int(head.group(1))] = (data[head.end() : end], b"")
            pos = end + len(b"endobj")
            continue
        header = data[head.end() : keyword]
        length_match = re.search(rb"/Length (\d+)", header)
        assert length_match is not None
        start = keyword + len(b"stream")
        start += 2 if data[start : start + 2] == b"\r\n" else 1
        raw = data[start : start + int(length_match.group(1))]
        body = zlib.decompress(raw) if b"/FlateDecode" in header else raw
        objects[int(head.group(1))] = (header, body)
        pos = data.index(b"endobj", start + len(raw)) + len(b"endobj")
    return objects


def _parse_cmap(cmap: bytes) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[int(src, 16)] = bytes.fromhex(dst.decode()).decode("utf-16-be")
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", cmap, re.S):
        triples = re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block)
        for low, high, dst in triples:
            for offset, code in enumerate(range(int(low, 16), int(high, 16) + 1)):
                mapping[code] = chr(int(dst, 16) + offset)
    return mapping


def _unescape(literal: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(literal):
        byte = literal[i : i + 1]
        if byte != b"\\":
            out += byte
            i += 1
            continue
        nxt = literal[i + 1 : i + 2]
        if nxt.isdigit():
            digits = re.match(rb"[0-7]{1,3}", literal[i + 1 : i + 4])
            assert digits is not None
            out.append(int(digits.group(0), 8))
            i += 1 + len(digits.group(0))
        else:
            out += _ESCAPES.get(nxt, nxt)
            i += 2
    return bytes(out)


def _pdf_text(data: bytes) -> str:
    """The visible text, one line per text object (BT…ET), pages in order.
    Fails if any font lacks a ToUnicode map (i.e. a latin-1 core font)."""
    objects = _pdf_objects(data)
    cmaps: dict[bytes, dict[int, str]] = {}
    for header, _ in objects.values():
        for name, ref in re.findall(rb"/(F\d+) (\d+) 0 R", header):
            to_unicode = re.search(rb"/ToUnicode (\d+) 0 R", objects[int(ref)][0])
            assert to_unicode is not None, f"font {name!r} is not an embedded Unicode font"
            cmaps[name] = _parse_cmap(objects[int(to_unicode.group(1))][1])
    assert cmaps, "no font resource found"
    lines: list[str] = []
    for header, _ in objects.values():
        if not re.search(rb"/Type /Page\b(?!s)", header):
            continue
        contents = re.search(rb"/Contents (\d+) 0 R", header)
        assert contents is not None
        font = b""
        chunk: list[str] = []
        for token in _CONTENT_TOKEN.finditer(objects[int(contents.group(1))][1]):
            text = token.group(0)
            if token.group(1) is not None:
                font = token.group(1)
            elif text == b"BT":
                chunk = []
            elif text == b"ET":
                if chunk:
                    lines.append("".join(chunk))
            else:
                codes = _unescape(text[1:-1])
                chunk.extend(
                    cmaps[font].get(int.from_bytes(codes[k : k + 2], "big"), "�")
                    for k in range(0, len(codes), 2)
                )
    return "\n".join(lines)


# --- builders (transient ORM objects — the builder never touches the DB) --------------------

EXPORTED_AT = datetime(2026, 9, 30, 15, 52, tzinfo=UTC)


def _row(action_type: str, actor: str = "agent", **details: Any) -> ActivityLog:
    return ActivityLog(
        actor_type=actor,
        action_type=action_type,
        details=details,
        created_at=datetime(2026, 9, 3, 18, 42, tzinfo=UTC),
    )


def _render(
    lang: str,
    *,
    first_name: str = "Marie",
    last_name: str = "Curie",
    status: str = CaseStatus.SUBMITTED.value,
    rows: list[ActivityLog] | None = None,
    principal_person: dict[str, Any] | None = None,
    family: list[CasePerson] | None = None,
    definitions: list[CustomFieldDefinition] | None = None,
    created_at: datetime = datetime(2026, 3, 4, 10, 0, tzinfo=UTC),
) -> str:
    case = ClientCase(
        id=uuid.uuid4(),
        status=status,
        origin_country="FR",
        dest_country="PY",
        dest_city="Asunción",
        tags=[],
        created_at=created_at,
    )
    principal = ExpatUser(first_name=first_name, last_name=last_name, email="c@example.com")
    persons = [
        CasePerson(kind="principal", **{"custom_fields": {}, **(principal_person or {})}),
        *(family or []),
    ]
    pdf = build_case_pdf(
        case=case,
        principal=principal,
        owner=Agent(first_name="Eloïse", last_name="Martin"),
        persons=persons,
        custom_field_definitions=definitions or [],
        activity_rows=rows or [],
        lang=lang,
        agency_default="fr",
        exported_at=EXPORTED_AT,
    )
    assert pdf.startswith(b"%PDF")
    return _pdf_text(pdf)


# --- 1. Unicode: Cyrillic, Hungarian ő/ű and the em dash survive ----------------------------


def test_cyrillic_hungarian_and_dash_render_without_question_marks() -> None:
    text = _render("ru", first_name="Иван", last_name="Петров")
    assert "Дело — Иван Петров" in text  # title: Cyrillic label + em dash + name
    assert "Основное лицо — Иван Петров" in text

    hungarian = _render("hu", first_name="Őrs", last_name="Szűcs")
    assert "Ügy — Őrs Szűcs" in hungarian
    assert "Fő személy — Őrs Szűcs" in hungarian

    # The French title carries the em dash too — the defect hit every agency.
    french = _render("fr")
    assert "Dossier — Marie Curie" in french

    for rendered in (text, hungarian, french):
        assert "?" not in rendered, rendered
        assert "�" not in rendered, rendered


def test_the_embedded_font_is_dejavu_and_no_core_font_remains() -> None:
    pdf = build_case_pdf(
        case=ClientCase(id=uuid.uuid4(), status="prospect", tags=[], created_at=EXPORTED_AT),
        principal=ExpatUser(first_name="A", last_name="B", email="a@example.com"),
        owner=None,
        persons=[],
        custom_field_definitions=[],
        activity_rows=[],
        exported_at=EXPORTED_AT,
    )
    assert b"/FontFile2" in pdf and b"DejaVuSans" in pdf
    assert b"/Helvetica" not in pdf
    assert b"/Lang (fr)" in pdf


def test_font_subsetting_does_not_flood_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    # src.main runs the root logger at INFO: fontTools' per-table INFO
    # narration would print ~75 lines per export.
    caplog.set_level(logging.INFO)
    _render("fr")
    assert not [r for r in caplog.records if r.name.startswith("fontTools")]


# --- 2. Chrome labels in the request language -----------------------------------------------
#
# French writes « Statut : … » with a NON-BREAKING space before the colon;
# DejaVu draws U+00A0 with the space glyph, so the ToUnicode map (one code
# per glyph) reads it back as a plain space — hence « : » below.


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("fr", ["Dossier — Marie Curie", "Statut : Soumis", "Personnes", "Activité"]),
        ("en", ["Case file — Marie Curie", "Status: Submitted", "People", "Activity"]),
        ("es", ["Expediente — Marie Curie", "Estado: Enviado", "Personas", "Actividad"]),
        ("ru", ["Дело — Marie Curie", "Статус: Подано", "Лица", "Активность"]),
        ("hu", ["Ügy — Marie Curie", "Státusz: Benyújtva", "Személyek", "Tevékenység"]),
        ("pt", ["Processo — Marie Curie", "Estado: Submetido", "Pessoas", "Atividade"]),
        ("it", ["Pratica — Marie Curie", "Stato: Inviata", "Persone", "Attività"]),
    ],
)
def test_labels_follow_the_request_language(lang: str, expected: list[str]) -> None:
    text = _render(lang)
    for fragment in expected:
        assert fragment in text, (fragment, text)
    # Never the former hard-coded English chrome, never the raw status code.
    assert "submitted" not in text
    if lang != "en":
        assert "Case file" not in text and "Activity journal" not in text


def test_unsupported_language_falls_back_to_french() -> None:
    assert "Dossier — Marie Curie" in _render("xx")


# --- 3. Status, enums and dates: readable, never codes -------------------------------------


def test_status_enums_and_dates_are_translated() -> None:
    text = _render(
        "fr",
        status=CaseStatus.AWAITING_DOCUMENTS.value,
        principal_person={
            "sex": Sex.FEMALE.value,
            "marital_status": MaritalStatus.PARTNERSHIP.value,
            "nationality": "HU",
            "date_of_birth": date(1985, 9, 5),
        },
        family=[
            CasePerson(kind="family", full_name="Léa", relationship="spouse", custom_fields={})
        ],
    )
    assert "Statut : Attente documents" in text
    assert "Sexe : Femme" in text
    assert "Situation familiale : Pacsé(e)" in text
    assert "Nationalité : Hongrie" in text
    assert "Date de naissance : 5 septembre 1985" in text
    assert "Pays d'origine : France" in text
    assert "Pays de destination : Paraguay" in text
    assert "Léa (Conjoint(e))" in text
    assert "Créé le : 4 mars 2026" in text
    for raw in ("awaiting_documents", "partnership", "1985-09-05", "2026-03-04", "spouse"):
        assert raw not in text, raw


def test_custom_field_values_are_rendered_for_a_reader() -> None:
    definitions = [
        CustomFieldDefinition(
            key="resident", label="Résident", label_i18n={}, field_type="boolean"
        ),
        CustomFieldDefinition(
            key="arrival", label="Arrivée", label_i18n={"en": "Arrival"}, field_type="date"
        ),
        CustomFieldDefinition(
            key="prev", label="Pays précédent", label_i18n={}, field_type="country"
        ),
        CustomFieldDefinition(key="home", label="Domicile", label_i18n={}, field_type="address"),
    ]
    text = _render(
        "en",
        definitions=definitions,
        principal_person={
            "custom_fields": {
                "resident": True,
                "arrival": "2026-11-15",
                "prev": "DE",
                "home": {
                    "street": "1 Main St",
                    "city": "Oslo",
                    "postal_code": "0150",
                    "country": "NO",
                },
            }
        },
    )
    assert "Résident: Yes" in text
    assert "Arrival: November 15, 2026" in text
    assert "Pays précédent: Germany" in text
    assert "Domicile: 1 Main St, 0150 Oslo, Norway" in text
    assert "True" not in text and "2026-11-15" not in text and "{" not in text


def test_instants_are_rendered_in_utc_and_say_so() -> None:
    # 01:30 at UTC+2 on March 5th is still March 4th in UTC.
    paris_summer = timezone(timedelta(hours=2))
    row = ActivityLog(
        actor_type="agent",
        action_type="case.created",
        details={},
        created_at=datetime(2026, 3, 5, 1, 30, tzinfo=paris_summer),
    )
    text = _render("fr", created_at=datetime(2026, 3, 5, 1, 30, tzinfo=paris_summer), rows=[row])
    assert "Créé le : 4 mars 2026" in text
    assert "4 mars 2026, 23:30 UTC" in text
    assert "Exporté le 30 septembre 2026, 15:52 UTC" in text
    assert "2026. szeptember 30. 15:52 UTC" in _render("hu")


# --- 4. Activity journal: known events translated, unknown ones still readable ----------------


def test_activity_entries_are_readable_labels() -> None:
    rows = [
        _row("document.uploaded", actor=ActorType.EXPAT.value),
        _row(
            "case.status_changed", actor=ActorType.SYSTEM.value, old="prospect", new="in_progress"
        ),
        _row("signature.request_completed", actor=ActorType.SYSTEM.value),
        _row("case.archived_for_audit"),  # an event no catalog knows (yet)
    ]
    fr = _render("fr", rows=rows)
    assert "Document déposé · Le client" in fr
    assert "Statut du dossier modifié : Prospect → En cours · Système" in fr
    assert "Demande de signature finalisée · Système" in fr
    assert "Autre événement (case archived for audit) · L'agence" in fr
    assert "3 septembre 2026, 18:42 UTC" in fr

    ru = _render("ru", rows=rows)
    assert "Документ загружен · Клиент" in ru
    assert "Статус дела изменён: Потенциальный → В работе · Система" in ru
    assert "Другое событие (case archived for audit) · Агентство" in ru

    for text in (fr, ru):
        for raw in ("document.uploaded", "case.status_changed", "case.archived_for_audit"):
            assert raw not in text, raw
        assert "[agent]" not in text and "[expat]" not in text


def test_empty_journal_says_so() -> None:
    assert "Aucune activité pour l'instant." in _render("fr")
    assert "Egyelőre nincs tevékenység." in _render("hu")


# --- 5. Catalog integrity (also guarded at import) -----------------------------------------


def test_every_catalog_covers_every_language_and_code() -> None:
    catalogs = {
        "labels": case_export._LABELS,
        "status": case_export._STATUS,
        "sex": case_export._SEX,
        "marital": case_export._MARITAL,
        "relationship": case_export._RELATIONSHIP,
        "activity": case_export._ACTIVITY,
    }
    for name, table in catalogs.items():
        assert set(table) == set(SUPPORTED_LANGUAGES), name
        reference = set(table["fr"])
        for lang, entries in table.items():
            assert set(entries) == reference, (name, lang)
            assert all(v.strip() for v in entries.values()), (name, lang)
    assert set(case_export._STATUS["fr"]) == {s.value for s in CaseStatus}
    for actor in ActorType:
        assert f"actor_{actor.value}" in case_export._LABELS["fr"]


# --- 6. Wiring: the route renders in the request language ----------------------------------


@pytest_asyncio.fixture
async def member(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["member"])


@pytest.mark.usefixtures("rbac_baseline")
async def test_export_route_renders_in_the_request_language(
    client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    case = await make_client_case(agency_id=member.agency_id, status="in_progress")
    principal = await db_session.get(ExpatUser, case.principal_expat_user_id)
    assert principal is not None
    principal.first_name, principal.last_name = "Őrs", "Szűcs"
    await db_session.commit()

    by_query = await client.get(f"/cases/{case.id}/export?lang=hu", headers=headers)
    assert by_query.status_code == 200
    hu = _pdf_text(by_query.content)
    assert "Ügy — Őrs Szűcs" in hu and "Státusz: Folyamatban" in hu

    by_header = await client.get(
        f"/cases/{case.id}/export", headers={**headers, "Accept-Language": "ru-RU,ru;q=0.9"}
    )
    ru = _pdf_text(by_header.content)
    assert "Дело — Őrs Szűcs" in ru and "Статус: В работе" in ru
    assert "?" not in hu and "?" not in ru

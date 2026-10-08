"""Trial-nurture daily job (nurture bloc 3).

Covers: (a) S0 at J+7 → s0_j7 sent ONCE (re-run no-op); (b) S0→S1
between mails → s1_j21; (c) catch-up — J+9 sends the J+7 mail, two due
slots the same day send only the most recent (older burned skipped);
(d) empty booking URL holds the J+28 as pending_config, then sends once
configured; (e) guards — no trial / platform slug / long-past trial get
NOTHING; (f) the sent content matches Eric's draft verbatim (and no
em-dash anywhere); plus the dry-run and the /jobs trigger wiring."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.nurture import NurtureSend
from shared.models.rbac import Role
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.core import email
from src.core.config import get_settings
from src.jobs.jobs_baseline import seed_job_configs
from src.nurture.nurture_job import send_trial_nurture
from src.nurture.nurture_texts import NURTURE_MAILS
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

TrialAgency = Callable[..., Awaitable[tuple[Agency, Agent]]]


@pytest.fixture
def trial_agency(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> TrialAgency:
    """An agency activated N days ago (trial running), with its first
    admin and the given usage milestones — the wizard's real shape."""

    async def _make(
        *,
        days_ago: float,
        milestones: tuple[str, ...] = (),
        slug: str | None = None,
        trial: bool = True,
        trial_days: int | None = None,
    ) -> tuple[Agency, Agent]:
        activated_at = datetime.now(UTC) - timedelta(days=days_ago, hours=1)
        agency = await make_agency(
            slug=slug or f"trial-{uuid.uuid4().hex[:8]}",
            trial_ends_at=(activated_at + timedelta(days=trial_days or 30)) if trial else None,
        )
        admin = await make_agent(
            role=system_roles["admin"], agency_id=agency.id, first_name="Sidney"
        )
        for key in ("agence_activee", *milestones):
            db_session.add(
                AgencyUsageMilestone(agency_id=agency.id, key=key, first_at=activated_at, count=1)
            )
        if trial_days is not None:
            db_session.add(
                UsageEvent(
                    agency_id=agency.id,
                    event_type="agency.activated",
                    actor_type="agent",
                    actor_id=admin.id,
                    details={"trial_days": trial_days},
                    created_at=activated_at,
                )
            )
        await db_session.commit()
        return agency, admin

    return _make


def _run(
    sync_session_local: sessionmaker[Session], *, dry_run: bool = False
) -> tuple[dict, list[str]]:
    lines: list[str] = []
    with sync_session_local() as db:
        stats = send_trial_nurture(db, log=lines.append, dry_run=dry_run)
    return stats, lines


async def _rows(db: AsyncSession, agency_id: uuid.UUID) -> dict[str, NurtureSend]:
    stmt = select(NurtureSend).where(NurtureSend.agency_id == agency_id)
    return {row.day_key: row for row in (await db.execute(stmt)).scalars()}


# --- (a) S0 at J+7: sent once ---------------------------------------------------------------


async def test_s0_j7_sent_once(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
) -> None:
    agency, admin = await trial_agency(days_ago=7)

    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 1
    assert len(email.outbox) == 1
    sent = email.outbox[0]
    assert sent.to == admin.email
    assert sent.sender == "eric@nidria.com"
    assert sent.reply_to == "eric@nidria.com"

    rows = await _rows(db_session, agency.id)
    assert set(rows) == {"j7"}
    assert rows["j7"].mail_key == "s0_j7"
    assert rows["j7"].status == "sent"
    assert rows["j7"].sent_at is not None
    assert rows["j7"].recipient == admin.email
    assert rows["j7"].lang == "fr"

    # Strict dedup: the re-run moves nothing.
    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 0
    assert len(email.outbox) == 1


# --- (b) S0 -> S1 between J+7 and J+21 -------------------------------------------------------


async def test_state_change_between_mails_sends_s1_j21(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
) -> None:
    agency, admin = await trial_agency(days_ago=21, milestones=("premier_dossier_cree",))
    db_session.add(
        NurtureSend(
            agency_id=agency.id,
            day_key="j7",
            mail_key="s0_j7",
            status="sent",
            sent_at=datetime.now(UTC) - timedelta(days=14),
            recipient=admin.email,
        )
    )
    await db_session.commit()

    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 1
    assert email.outbox[0].subject == "teste-le sans embêter un client"  # the S1 J+21 text
    rows = await _rows(db_session, agency.id)
    assert rows["j21"].mail_key == "s1_j21"
    assert rows["j7"].mail_key == "s0_j7"  # untouched


# --- (c) catch-up ----------------------------------------------------------------------------


async def test_catchup_missed_day_still_sends(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
) -> None:
    agency, _ = await trial_agency(days_ago=9)  # tick missed at J+7 and J+8
    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 1
    rows = await _rows(db_session, agency.id)
    assert rows["j7"].status == "sent"


async def test_two_due_slots_send_only_the_most_recent(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
) -> None:
    agency, _ = await trial_agency(days_ago=22)  # J+7 AND J+21 due, nothing sent yet
    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 1
    assert stats["skipped"] == 1
    assert len(email.outbox) == 1  # never two mails the same day
    rows = await _rows(db_session, agency.id)
    assert rows["j21"].status == "sent"
    assert rows["j7"].status == "skipped"


# --- (d) booking URL gate on J+28 ------------------------------------------------------------


async def test_j28_held_until_booking_url_is_set(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agency, admin = await trial_agency(days_ago=28)
    agency_id = agency.id
    now = datetime.now(UTC)
    for day_key in ("j7", "j21"):
        db_session.add(
            NurtureSend(
                agency_id=agency.id,
                day_key=day_key,
                mail_key=f"s0_{day_key}",
                status="sent",
                sent_at=now - timedelta(days=10),
                recipient=admin.email,
            )
        )
    await db_session.commit()

    # NURTURE_BOOKING_URL is empty by default: held, nothing sent.
    stats, _ = _run(sync_session_local)
    assert stats == {"in_scope": 1, "sent": 0, "skipped": 0, "pending_config": 1}
    assert email.outbox == []
    rows = await _rows(db_session, agency_id)
    assert rows["j28"].status == "pending_config"

    # Config lands: the next run releases the mail (row updated in place).
    monkeypatch.setattr(get_settings(), "nurture_booking_url", "https://cal.com/eric-nidria")
    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 1
    assert "https://cal.com/eric-nidria" in email.outbox[0].body
    db_session.expire_all()
    rows = await _rows(db_session, agency_id)
    assert rows["j28"].status == "sent"
    assert rows["j28"].mail_key == "s0_j28"


# --- (e) guards ------------------------------------------------------------------------------


async def test_guards_never_mail_out_of_scope_agencies(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
) -> None:
    no_trial, _ = await trial_agency(days_ago=7, trial=False)
    platform, _ = await trial_agency(days_ago=7, slug="nidria-demo")  # excluded by config
    long_gone, _ = await trial_agency(days_ago=60)  # way past the calendar

    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 0
    assert email.outbox == []
    for agency in (no_trial, platform, long_gone):
        assert await _rows(db_session, agency.id) == {}


# --- (f) verbatim ----------------------------------------------------------------------------


async def test_sent_content_is_erics_verbatim(
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
) -> None:
    await trial_agency(days_ago=7)
    _run(sync_session_local)
    sent = email.outbox[0]
    assert sent.subject == "un dossier d'exemple t'attend dans Nidria"
    assert "Salut Sidney," in sent.body
    assert "tu en as déjà un d'exemple dans ton espace Nidria, complet et déjà rempli" in sent.body
    assert sent.body.rstrip().endswith("Éric, cofondateur de Nidria")
    assert "{Prénom}" not in sent.body

    # Eric's anti-AI rule holds over the whole catalogue: no em-dash.
    for (state, day_key), mail in NURTURE_MAILS.items():
        assert "—" not in mail.subject and "—" not in mail.body, (state, day_key)
        assert "mois" not in mail.subject and "mois" not in mail.body, (state, day_key)
    assert len(NURTURE_MAILS) == 9


# --- dry-run + scheduler wiring --------------------------------------------------------------


async def test_dry_run_lists_without_sending(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
) -> None:
    agency, _ = await trial_agency(days_ago=7)
    stats, lines = _run(sync_session_local, dry_run=True)
    assert stats["dry_run"] is True
    assert stats["sent"] == 1  # what WOULD leave
    assert any("would send s0_j7" in line for line in lines)
    assert email.outbox == []
    assert await _rows(db_session, agency.id) == {}  # zero writes


async def test_trigger_endpoint_runs_the_job(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    trial_agency: TrialAgency,
) -> None:
    await seed_job_configs(db_session)
    await trial_agency(days_ago=7)
    # /jobs is superadmin-only (job.manage is a platform permission).
    superadmin = await make_agent(role=system_roles["superadmin"])

    response = await client.post(
        "/jobs/trial_nurture/trigger",
        headers=agent_headers(superadmin),
        json={"dry_run": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert body["stats"]["dry_run"] is True
    assert body["stats"]["sent"] == 1
    assert email.outbox == []


@pytest.mark.parametrize(
    ("days_ago", "expected_key", "skipped"),
    [
        (2, None, 0),
        (3, "j7", 0),
        (9, "j7", 0),
        (10, "j21", 1),
        (12, "j21", 1),
        (13, "j28", 2),
        (14, "j28", 2),
        (15, None, 0),
        (21, None, 0),
    ],
)
async def test_fifteen_day_calendar_and_catchup_stop_at_expiry(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
    monkeypatch: pytest.MonkeyPatch,
    days_ago: int,
    expected_key: str | None,
    skipped: int,
) -> None:
    monkeypatch.setattr(get_settings(), "nurture_booking_url", "https://booking.example.test")
    agency, _ = await trial_agency(days_ago=days_ago, trial_days=15)
    stats, _ = _run(sync_session_local)
    assert stats["sent"] == int(expected_key is not None)
    assert stats["skipped"] == skipped
    assert len(email.outbox) == int(expected_key is not None)
    rows = await _rows(db_session, agency.id)
    if expected_key is None:
        assert rows == {}
    else:
        assert rows[expected_key].status == "sent"
        assert sum(row.status == "skipped" for row in rows.values()) == skipped
    assert _run(sync_session_local)[0]["sent"] == 0


@pytest.mark.parametrize(
    ("state", "milestones"),
    [
        ("S0", ()),
        ("S1", ("premier_dossier_cree",)),
        ("S2", ("premier_dossier_cree", "premier_client_compte_active")),
    ],
)
async def test_final_fifteen_day_mail_uses_live_state_and_trial_wording(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    milestones: tuple[str, ...],
) -> None:
    agency, _ = await trial_agency(days_ago=13, trial_days=15, milestones=milestones)
    monkeypatch.setattr(get_settings(), "nurture_booking_url", "https://booking.example.test")
    assert _run(sync_session_local)[0]["sent"] == 1
    sent = email.outbox[0]
    assert "ton essai" in sent.body
    assert "mois" not in sent.subject + sent.body
    assert "se termine" not in sent.subject + sent.body
    assert "dans quelques jours" not in sent.body
    assert "https://booking.example.test" in sent.body
    assert "{booking_url}" not in sent.body
    assert (await _rows(db_session, agency.id))["j28"].mail_key == f"{state.lower()}_j28"


@pytest.mark.parametrize("trial_days", [None, 30])
async def test_existing_trials_keep_their_original_calendar(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
    trial_days: int | None,
) -> None:
    agency, _ = await trial_agency(days_ago=3, trial_days=trial_days)
    assert get_settings().trial_days == 15
    assert _run(sync_session_local)[0]["sent"] == 0
    assert email.outbox == []
    assert await _rows(db_session, agency.id) == {}


async def test_extension_and_config_change_do_not_rearm_or_shift_slots(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agency, _ = await trial_agency(days_ago=13, trial_days=15)
    agency.trial_ends_at = datetime.now(UTC) + timedelta(days=30)
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "trial_days", 30)
    monkeypatch.setattr(get_settings(), "nurture_booking_url", "https://booking.example.test")
    assert _run(sync_session_local)[0]["sent"] == 1
    rows = await _rows(db_session, agency.id)
    assert {key: row.status for key, row in rows.items()} == {
        "j7": "skipped",
        "j21": "skipped",
        "j28": "sent",
    }
    assert _run(sync_session_local)[0]["sent"] == 0
    assert len(email.outbox) == 1


async def test_pending_final_mail_is_not_released_at_exact_expiry(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agency, _ = await trial_agency(days_ago=13, trial_days=15)
    assert _run(sync_session_local)[0]["pending_config"] == 1
    assert email.outbox == []
    agency_id = agency.id
    expiry = datetime.now(UTC)
    agency.trial_ends_at = expiry
    await db_session.commit()

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return expiry

    monkeypatch.setattr("src.nurture.nurture_job.datetime", FrozenDateTime)
    monkeypatch.setattr(get_settings(), "nurture_booking_url", "https://booking.example.test")
    assert _run(sync_session_local)[0]["in_scope"] == 0
    assert email.outbox == []
    db_session.expire_all()
    assert (await _rows(db_session, agency_id))["j28"].status == "pending_config"


async def test_new_calendar_dry_run_does_not_consume_slots(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    trial_agency: TrialAgency,
) -> None:
    agency, _ = await trial_agency(days_ago=10, trial_days=15)
    stats, _ = _run(sync_session_local, dry_run=True)
    assert stats["sent"] == 1 and stats["skipped"] == 1
    assert email.outbox == []
    assert await _rows(db_session, agency.id) == {}
    assert _run(sync_session_local)[0]["sent"] == 1

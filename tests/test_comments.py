"""Per-step comment thread (VAGUE 5) — agent <-> client ping-pong. Like
wave 2, the authorization PERIMETER is the priority: cross-tenant 404
with no write, "your own message only" in BOTH directions, thread scoped
to its step. Plus the anti-burst notification (table-backed, so a FAILED
first mail does not suppress the next) and soft-delete coherence."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def c_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Marie", last_name="Conseil")


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Jean", last_name="Martin")


async def _thread(
    client: AsyncClient,
    headers: dict[str, str],
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    *,
    owner: bool = True,
) -> tuple[ClientCase, str]:
    """Template + 1 step, case (principal=expat, owner=admin), journey
    assigned → returns (case, progress_id of the single step)."""
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Étape"})
    case = await make_client_case(
        agency_id=admin.agency_id,
        principal_expat_user_id=expat.id,
        owner_agent_id=admin.id if owner else None,
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    return case, steps[0]["id"]


async def test_notif_routes_recipient_language_and_resolves_step_name(
    c_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """BLOC NOTIF-1: a comment to a CLIENT whose preferred_lang='en' carries
    lang='en' to the builder AND resolves the step name in EN — even though the
    body text is still FR at this stage. The step name is the proof the right
    language reached the rendering point."""
    ah = agent_headers(admin)
    en_client = await make_expat_user(email="en@example.com", preferred_lang="en")
    tid = (await c_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    # The step carries both FR and EN names.
    await c_client.post(
        f"/journeys/{tid}/steps",
        headers=ah,
        json={"name": "Étape FR", "name_i18n": {"fr": "Étape FR", "en": "Step EN"}},
    )
    case = await make_client_case(
        agency_id=admin.agency_id,
        principal_expat_user_id=en_client.id,
        owner_agent_id=admin.id,
    )
    pid = (
        await c_client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()[0]["id"]

    email.outbox.clear()
    # Agent posts → the EN client is notified.
    r = await c_client.post(
        f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "hello"}
    )
    assert r.status_code == 201, r.text

    sent = next(m for m in email.outbox if m.to == "en@example.com")
    # The step name was resolved in the recipient's language (EN), not the FR scalar.
    assert "Step EN" in sent.body and "Étape FR" not in sent.body
    # NOTIF-2: the subject + body are now in EN (recipient language).
    assert sent.subject == "Nidria — New message from your advisor"
    assert 'html lang="en"' in sent.html


def _to(fragment: str) -> list[email.OutboxEmail]:
    return [m for m in email.outbox if fragment in m.subject]


# --- ping-pong + author resolution ---------------------------------------------------


async def test_agent_and_client_ping_pong(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)

    a_msg = await c_client.post(
        f"/cases/{case.id}/steps/{pid}/comments",
        headers=ah,
        json={"body": "Bonjour, où en êtes-vous ?"},
    )
    assert a_msg.status_code == 201, a_msg.text
    e_msg = await c_client.post(
        f"/expat/cases/{case.id}/steps/{pid}/comments",
        headers=eh,
        json={"body": "Je cherche le doc."},
    )
    assert e_msg.status_code == 201, e_msg.text

    # Both sides see the full thread, ordered, with resolved author labels.
    agent_view = (await c_client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=ah)).json()
    assert [c["author_label"] for c in agent_view] == ["Marie", "Jean Martin"]
    assert [c["author_type"] for c in agent_view] == ["agent", "expat"]
    assert [c["is_mine"] for c in agent_view] == [True, False]  # agent authored the first

    client_view = (
        await c_client.get(f"/expat/cases/{case.id}/steps/{pid}/comments", headers=eh)
    ).json()
    assert [c["is_mine"] for c in client_view] == [False, True]  # client authored the second
    assert "author_id" not in client_view[0]  # no internal UUID leaks to the client


# --- perimeter: cross-tenant ---------------------------------------------------------


async def test_cross_tenant_agent_404(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    other = await make_agent(role=system_roles["admin"])  # different agency
    oh = agent_headers(other)
    assert (
        await c_client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=oh)
    ).status_code == 404
    posted = await c_client.post(
        f"/cases/{case.id}/steps/{pid}/comments", headers=oh, json={"body": "intrus"}
    )
    assert posted.status_code == 404
    # Nothing written.
    assert (await c_client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=ah)).json() == []


async def test_cross_client_404_no_write(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    stranger = await make_expat_user(email="stranger@example.com")
    assert stranger.id != expat.id
    denied = await c_client.post(
        f"/expat/cases/{case.id}/steps/{pid}/comments",
        headers=expat_headers(stranger),
        json={"body": "INTRUSION"},
    )
    assert denied.status_code == 404  # never reveals the case
    assert (await c_client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=ah)).json() == []


# --- perimeter: own message only, BOTH directions ------------------------------------


async def test_only_own_message_both_directions(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    agent_c = (
        await c_client.post(
            f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "A"}
        )
    ).json()
    client_c = (
        await c_client.post(
            f"/expat/cases/{case.id}/steps/{pid}/comments", headers=eh, json={"body": "C"}
        )
    ).json()

    # Agent cannot touch the client's message.
    assert (
        await c_client.patch(
            f"/cases/{case.id}/steps/{pid}/comments/{client_c['id']}",
            headers=ah,
            json={"body": "x"},
        )
    ).status_code == 403
    assert (
        await c_client.delete(f"/cases/{case.id}/steps/{pid}/comments/{client_c['id']}", headers=ah)
    ).status_code == 403
    # Client cannot touch the agent's message.
    assert (
        await c_client.patch(
            f"/expat/cases/{case.id}/steps/{pid}/comments/{agent_c['id']}",
            headers=eh,
            json={"body": "x"},
        )
    ).status_code == 403
    assert (
        await c_client.delete(
            f"/expat/cases/{case.id}/steps/{pid}/comments/{agent_c['id']}", headers=eh
        )
    ).status_code == 403
    # But each edits its own.
    assert (
        await c_client.patch(
            f"/cases/{case.id}/steps/{pid}/comments/{agent_c['id']}",
            headers=ah,
            json={"body": "A2"},
        )
    ).status_code == 200


async def test_viewer_reads_but_cannot_post(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    viewer = await make_agent(agency_id=admin.agency_id, role=system_roles["viewer"])
    vh = agent_headers(viewer)
    assert (
        await c_client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=vh)
    ).status_code == 200
    # case.view but not case.comment → 403 on write.
    assert (
        await c_client.post(
            f"/cases/{case.id}/steps/{pid}/comments", headers=vh, json={"body": "x"}
        )
    ).status_code == 403


# --- thread scoped to its step -------------------------------------------------------


async def test_thread_scoped_to_step(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = (await c_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    for name in ("S1", "S2"):
        await c_client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": name})
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    steps = (
        await c_client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    p1, p2 = steps[0]["id"], steps[1]["id"]
    await c_client.post(
        f"/cases/{case.id}/steps/{p1}/comments", headers=ah, json={"body": "sur S1"}
    )
    assert (
        len((await c_client.get(f"/cases/{case.id}/steps/{p1}/comments", headers=ah)).json()) == 1
    )
    assert (await c_client.get(f"/cases/{case.id}/steps/{p2}/comments", headers=ah)).json() == []


# --- soft delete ---------------------------------------------------------------------


async def test_soft_delete_keeps_thread(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    c1 = (
        await c_client.post(
            f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "premier"}
        )
    ).json()
    await c_client.post(
        f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "second"}
    )

    assert (
        await c_client.delete(f"/cases/{case.id}/steps/{pid}/comments/{c1['id']}", headers=ah)
    ).status_code == 200
    thread = (await c_client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=ah)).json()
    assert len(thread) == 2  # thread intact, ordering preserved
    deleted = next(c for c in thread if c["id"] == c1["id"])
    assert deleted["deleted"] is True
    assert deleted["body"] is None  # content not leaked

    # Editing / re-deleting a deleted comment → 404 (gone from the actionable thread).
    assert (
        await c_client.patch(
            f"/cases/{case.id}/steps/{pid}/comments/{c1['id']}", headers=ah, json={"body": "x"}
        )
    ).status_code == 404
    assert (
        await c_client.delete(f"/cases/{case.id}/steps/{pid}/comments/{c1['id']}", headers=ah)
    ).status_code == 404


async def test_edited_flag(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    c1 = (
        await c_client.post(
            f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "v1"}
        )
    ).json()
    assert c1["edited"] is False
    edited = await c_client.patch(
        f"/cases/{case.id}/steps/{pid}/comments/{c1['id']}", headers=ah, json={"body": "v2"}
    )
    assert edited.json()["edited"] is True
    assert edited.json()["body"] == "v2"


# --- notifications: anti-burst, best-effort, the table-justifying retry ---------------


async def test_anti_burst_groups_to_one_mail(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    email.outbox.clear()
    await c_client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "1"})
    await c_client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "2"})
    # Two rapid agent messages → the client is notified once (grouped).
    assert len(_to("conseiller")) == 1
    assert _to("conseiller")[0].to == expat.email


async def test_failed_first_mail_does_not_suppress_next(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason it's a TABLE, not a derivation: last_notified_at is set
    only on a SUCCESSFUL send. If the first mail throws, the second
    message must still attempt one (a derivation from comment timestamps
    would wrongly suppress it — the client would never be told)."""
    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)

    calls: list[str] = []

    def flaky(to: str, subject: str, body: str, html: str | None = None) -> None:
        calls.append(to)
        if len(calls) == 1:
            raise RuntimeError("SMTP down")  # first send fails

    monkeypatch.setattr("src.comments.comments_manager.send_email", flaky)
    c1 = await c_client.post(
        f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "1"}
    )
    c2 = await c_client.post(
        f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "2"}
    )
    assert c1.status_code == 201 and c2.status_code == 201  # writes never blocked
    assert len(calls) == 2  # the failed first did NOT suppress the second attempt


async def test_mail_failure_never_blocks_comment(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr("src.comments.comments_manager.send_email", boom)
    posted = await c_client.post(
        f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "hello"}
    )
    assert posted.status_code == 201
    assert (
        len((await c_client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=ah)).json()) == 1
    )


async def test_no_owner_no_mail_graceful(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case, owner=False)
    email.outbox.clear()
    posted = await c_client.post(
        f"/expat/cases/{case.id}/steps/{pid}/comments", headers=eh, json={"body": "hi"}
    )
    assert posted.status_code == 201  # client posts, no owner to notify
    assert email.outbox == []  # graceful: no recipient → no mail


async def test_flag_disables_comment_notifications(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    db_session: object,
    agent_headers: AuthHeaders,
) -> None:
    from shared.models.agency import Agency

    ah = agent_headers(admin)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    agency = await db_session.get(Agency, admin.agency_id)  # type: ignore[attr-defined]
    agency.settings = {**(agency.settings or {}), "step_notifications_enabled": False}
    await db_session.commit()  # type: ignore[attr-defined]
    email.outbox.clear()
    await c_client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "x"})
    assert email.outbox == []  # same single switch as wave 2


# --- timeline integration: progress_id + comment_count (vague 5 front) ---------------


async def test_expat_timeline_exposes_progress_id(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    detail = (await c_client.get(f"/expat/cases/{case.id}", headers=eh)).json()
    step = detail["timeline"][0]
    assert step["progress_id"] == pid  # the id the comment route needs
    # …and it is directly usable to address the client's own thread.
    posted = await c_client.post(
        f"/expat/cases/{case.id}/steps/{step['progress_id']}/comments",
        headers=eh,
        json={"body": "via progress_id"},
    )
    assert posted.status_code == 201


async def test_comment_count_excludes_deleted_both_faces(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    case, pid = await _thread(c_client, ah, admin, expat, make_client_case)
    ids = []
    for n in ("1", "2", "3"):
        r = await c_client.post(
            f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": n}
        )
        ids.append(r.json()["id"])
    await c_client.delete(f"/cases/{case.id}/steps/{pid}/comments/{ids[0]}", headers=ah)

    # Agent face.
    agent_steps = (await c_client.get(f"/cases/{case.id}/steps", headers=ah)).json()
    assert agent_steps[0]["comment_count"] == 2  # 3 posted − 1 deleted
    # Expat face.
    detail = (await c_client.get(f"/expat/cases/{case.id}", headers=eh)).json()
    assert detail["timeline"][0]["comment_count"] == 2


async def test_comment_count_is_per_step_not_cross_contaminated(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Two steps, distinct counts — proves the grouped/batched COUNT keys
    on the right case_step_progress (no bleed, no N+1 fallback)."""
    ah = agent_headers(admin)
    tid = (await c_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    for name in ("S1", "S2"):
        await c_client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": name})
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    steps = (
        await c_client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    p1, p2 = steps[0]["id"], steps[1]["id"]
    await c_client.post(f"/cases/{case.id}/steps/{p1}/comments", headers=ah, json={"body": "a"})
    await c_client.post(f"/cases/{case.id}/steps/{p1}/comments", headers=ah, json={"body": "b"})

    by_id = {s["id"]: s for s in (await c_client.get(f"/cases/{case.id}/steps", headers=ah)).json()}
    assert by_id[p1]["comment_count"] == 2
    assert by_id[p2]["comment_count"] == 0  # untouched step → zero, not N+1-defaulted

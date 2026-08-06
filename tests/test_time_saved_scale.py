"""LE TEMPS GAGNÉ COMPTE TOUT CE QUI EST GAGNÉ — le barème élargi.

Un chiffre de valeur ne vaut que s'il tient devant un dirigeant. Trois
choses le rendent défendable, et ce fichier les tient :

1. LES SEPT GESTES SONT COMPTÉS, chacun à sa valeur de config. Un barème
   qui oublie les étapes et les clôtures sous-estime l'agence ; il ne
   « rassure » pas, il se fait corriger en réunion.
2. AUCUN GESTE N'EST COMPTÉ DEUX FOIS. La signature aboutie produit bien
   un document en base, mais posé par le SYSTÈME — la collecte, elle, ne
   compte que ce que le CLIENT a déposé. Sans cette frontière, chaque
   signature vaudrait deux fois.
3. LE MOIS EST UNE MAILLE À PART, pas une somme de semaines : il chevauche
   la période courante et la précédente, et se calcule indépendamment.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.document import Document
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.rbac import Role
from shared.models.signature import SignatureRequest
from shared.models.usage import UsageEvent
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest.fixture
def kpi_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KPI_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _kinds(block: dict) -> dict[str, dict]:
    return {i["kind"]: i for i in block["items"]}


async def test_the_seven_gestures_are_all_counted_at_their_scale(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    kpi_enabled,
) -> None:
    """UN de chaque geste, aujourd'hui : le total doit être la somme du
    barème entier. C'est le témoin qui échouerait si un geste cessait
    d'être compté (ou se comptait deux fois)."""
    now = datetime.now(UTC)
    principal = await make_expat_user(activated=True, email="scale@example.com")
    journey = JourneyTemplate(agency_id=admin.agency_id, name="Barème")
    db_session.add(journey)
    await db_session.flush()
    template_step = JourneyTemplateStep(template_id=journey.id, name="S0", position=0)
    db_session.add(template_step)
    await db_session.flush()
    case = await make_client_case(
        agency_id=admin.agency_id,
        principal_expat_user_id=principal.id,
        owner_agent_id=admin.id,
        journey_template_id=journey.id,  # dossier créé depuis un parcours : 20
    )
    progress = CaseStepProgress(  # étape franchie : 5
        case_id=case.id,
        template_step_id=template_step.id,
        status="done",
        completed_at=now,
        completed_by_agent_id=admin.id,
    )
    db_session.add(progress)
    await db_session.flush()
    db_session.add_all(
        [
            Document(  # pièce collectée (déposée par le CLIENT) : 8
                case_id=case.id,
                filename="passeport.pdf",
                storage_path="p/passeport.pdf",
                uploaded_by_type="expat",
                uploaded_by_id=principal.id,
            ),
            SignatureRequest(  # signature aboutie : 25
                case_id=case.id,
                case_step_progress_id=progress.id,
                reference="TS",
                level="ses",
                status="completed",
                completed_at=now,
                provider="docuseal",
            ),
            ActivityLog(  # dossier clôturé : 10
                case_id=case.id,
                actor_type="agent",
                actor_id=admin.id,
                action_type="case.status_changed",
                details={"old": "in_progress", "new": "closed"},
            ),
            UsageEvent(  # 3 fiches importées : 3 × 2 = 6
                agency_id=admin.agency_id,
                actor_type="agent",
                actor_id=admin.id,
                event_type="agency.profiles_imported",
                details={"created": 3},
            ),
        ]
    )
    await db_session.commit()

    r = await client.get("/agencies/me/activity-stats?period=today", headers=agent_headers(admin))
    assert r.status_code == 200, r.text
    period = r.json()["time_saved"]["period"]
    by_kind = _kinds(period)
    assert by_kind["case_created_from_template"]["minutes_total"] == 20
    assert by_kind["step_completed"]["minutes_total"] == 5
    assert by_kind["client_document_collected"]["minutes_total"] == 8
    assert by_kind["signature_completed"]["minutes_total"] == 25
    assert by_kind["case_closed"]["minutes_total"] == 10
    assert by_kind["profile_imported"]["count"] == 3
    assert by_kind["profile_imported"]["minutes_total"] == 6
    # La relance auto n'est pas dans ce scénario : elle vaut 0, et la
    # ligne EXISTE quand même (le barème se lit en entier).
    assert by_kind["auto_reminder_sent"]["minutes_total"] == 0
    assert period["total_minutes"] == 20 + 5 + 8 + 25 + 10 + 6


async def test_a_completed_signature_is_never_also_a_collected_document(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    kpi_enabled,
) -> None:
    """LA FRONTIÈRE QUI TIENT LE CHIFFRE. Une signature aboutie DÉPOSE un
    document signé en base — mais au nom du système. Si la collecte le
    comptait, chaque signature vaudrait 25 + 8 au lieu de 25, et le
    tableau de bord mentirait de 30 %."""
    now = datetime.now(UTC)
    principal = await make_expat_user(activated=True, email="sig@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id,
        principal_expat_user_id=principal.id,
        owner_agent_id=admin.id,
    )
    journey = JourneyTemplate(agency_id=admin.agency_id, name="Sig")
    db_session.add(journey)
    await db_session.flush()
    template_step = JourneyTemplateStep(template_id=journey.id, name="S0", position=0)
    db_session.add(template_step)
    await db_session.flush()
    # L'étape porte la signature mais reste OUVERTE : sinon elle compterait
    # ses propres 5 minutes et brouillerait ce que ce test isole.
    progress = CaseStepProgress(
        case_id=case.id, template_step_id=template_step.id, status="in_progress"
    )
    db_session.add(progress)
    await db_session.flush()
    db_session.add_all(
        [
            SignatureRequest(
                case_id=case.id,
                case_step_progress_id=progress.id,
                reference="TS",
                level="ses",
                status="completed",
                completed_at=now,
                provider="docuseal",
            ),
            Document(  # le PDF signé, tel que le pose `signatures_manager`
                case_id=case.id,
                filename="contrat-signe.pdf",
                storage_path="p/contrat-signe.pdf",
                uploaded_by_type="system",
                uploaded_by_id=uuid.uuid4(),  # la demande de signature, comme en prod
            ),
        ]
    )
    await db_session.commit()

    r = await client.get("/agencies/me/activity-stats?period=today", headers=agent_headers(admin))
    by_kind = _kinds(r.json()["time_saved"]["period"])
    assert by_kind["signature_completed"]["count"] == 1
    assert by_kind["client_document_collected"]["count"] == 0
    assert r.json()["time_saved"]["period"]["total_minutes"] == 25


async def test_the_month_is_its_own_grain_not_a_sum_of_weeks(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    kpi_enabled,
) -> None:
    """Le mois CIVIL chevauche la semaine courante et la précédente. On
    pose une pièce aujourd'hui et une autre au 1er du mois : le mois voit
    les deux, la semaine n'en voit qu'une (sauf si le mois vient de
    commencer — le test le dit alors sans tricher)."""
    now = datetime.now(UTC)
    first_of_month = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    principal = await make_expat_user(activated=True, email="month@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id,
        principal_expat_user_id=principal.id,
        owner_agent_id=admin.id,
    )
    for at, name in ((now, "aujourdhui.pdf"), (first_of_month, "debut-mois.pdf")):
        doc = Document(
            case_id=case.id,
            filename=name,
            storage_path=f"p/{name}",
            uploaded_by_type="expat",
            uploaded_by_id=principal.id,
        )
        db_session.add(doc)
        await db_session.flush()
        doc.created_at = at
    await db_session.commit()

    r = await client.get("/agencies/me/activity-stats?period=today", headers=agent_headers(admin))
    ts = r.json()["time_saved"]
    assert _kinds(ts["month"])["client_document_collected"]["count"] == 2
    assert ts["month"]["total_minutes"] == 16
    # La journée ne voit que la sienne — sauf le 1er du mois, où les deux
    # tombent le même jour.
    expected_today = 2 if now.day == 1 else 1
    assert _kinds(ts["period"])["client_document_collected"]["count"] == expected_today
    # Le mois n'est jamais plus grand que le cumul, jamais plus petit que
    # la journée : l'invariant qui attrape une borne inversée.
    assert ts["period"]["total_minutes"] <= ts["month"]["total_minutes"]
    assert ts["month"]["total_minutes"] <= ts["all_time"]["total_minutes"]


async def test_an_old_gesture_leaves_the_month_but_stays_in_the_cumul(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    kpi_enabled,
) -> None:
    """La borne du mois mord vraiment : un geste du mois dernier ne
    gonfle pas le cumul mensuel, mais il reste dans « depuis vos
    débuts »."""
    principal = await make_expat_user(activated=True, email="old@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id,
        principal_expat_user_id=principal.id,
        owner_agent_id=admin.id,
    )
    doc = Document(
        case_id=case.id,
        filename="mois-dernier.pdf",
        storage_path="p/mois-dernier.pdf",
        uploaded_by_type="expat",
        uploaded_by_id=principal.id,
    )
    db_session.add(doc)
    await db_session.flush()
    # La veille du 1er : dans le mois PRÉCÉDENT, quel que soit le jour.
    doc.created_at = datetime.now(UTC).replace(day=1, hour=12) - timedelta(days=1)
    await db_session.commit()

    ts = (
        await client.get("/agencies/me/activity-stats?period=today", headers=agent_headers(admin))
    ).json()["time_saved"]
    assert ts["month"]["total_minutes"] == 0
    assert ts["all_time"]["total_minutes"] == 8


async def test_an_import_dates_its_own_gesture(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    kpi_enabled,
) -> None:
    """Une fiche importée ne se distingue d'aucune autre en base : c'est
    l'ÉVÉNEMENT d'import qui la date. Deux imports s'additionnent, et
    l'événement d'une AUTRE agence ne franchit pas la frontière."""
    other = await make_other_agency_event(db_session)
    db_session.add_all(
        [
            UsageEvent(
                agency_id=admin.agency_id,
                actor_type="agent",
                actor_id=admin.id,
                event_type="agency.profiles_imported",
                details={"created": 1844},
            ),
            UsageEvent(
                agency_id=admin.agency_id,
                actor_type="agent",
                actor_id=admin.id,
                event_type="agency.profiles_imported",
                details={"created": 439},
            ),
        ]
    )
    await db_session.commit()

    ts = (
        await client.get("/agencies/me/activity-stats?period=today", headers=agent_headers(admin))
    ).json()["time_saved"]
    imported = _kinds(ts["period"])["profile_imported"]
    assert imported["count"] == 1844 + 439
    assert imported["minutes_total"] == (1844 + 439) * 2
    assert other not in {None}  # l'autre agence existe bien, et ne compte pas


async def make_other_agency_event(db: AsyncSession) -> uuid.UUID:
    """Un import chez le voisin — il ne doit JAMAIS entrer dans le chiffre
    de l'agence courante."""
    from shared.models.agency import Agency

    agency = Agency(name="Voisine", slug=f"voisine-{uuid.uuid4().hex[:8]}")
    db.add(agency)
    await db.flush()
    db.add(
        UsageEvent(
            agency_id=agency.id,
            actor_type="agent",
            actor_id=None,
            event_type="agency.profiles_imported",
            details={"created": 99999},
        )
    )
    await db.flush()
    return agency.id

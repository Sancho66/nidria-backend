"""« Partir avec ses données » — the agency data export (lot 10/08).

A READ that crosses the billing wall by construction (a GET is never
blocked by the billing lock, enforcement.py) — the agency leaves with its
data even past cancellation. This is ALSO an everyday feature (backup,
migration, reporting), so it lives in Settings, not in a cancellation flow.

v1 scope = the symmetry of what the import ingests: person + company
profiles + their custom fields, plus the cases and their history (activity
log + internal notes) as separate CSVs in one ZIP. Deposited DOCUMENTS are
out of v1 (a ZIP of files is another chantier: storage, size, signed URLs)
— the README says they stay downloadable case by case. Synchronous: custom
field values live in the JSONB column ON the row (no per-row query), so
1600 rows is a couple of bulk SELECTs.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_note import CaseNote
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.company_profile import CompanyFieldDefinition, CompanyProfile
from shared.models.custom_field import CustomFieldDefinition
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import RolePermission
from src.core.enums import ActorType
from src.core.exceptions import NotFoundError
from src.core.i18n import DEFAULT_LANG, resolve_i18n
from src.core.rbac.permissions import Permission
from src.export.export_builder import build_zip, render_csv
from src.usage.usage_manager import UsageManager

# Native person columns (attr → French header), in the fiche taxonomy order.
# Mirrors what profile_import_manager accepts (identity + civil + CRM), so a
# file exported here re-imports after editing.
_PERSON_NATIVE: list[tuple[str, str]] = [
    ("id", "id"),
    ("first_name", "Prénom"),
    ("last_name", "Nom"),
    ("email", "Email"),
    ("preferred_lang", "Langue préférée"),
    ("status_override", "Statut (forcé)"),
    ("passport_number", "N° passeport"),
    ("date_of_birth", "Date de naissance"),
    ("nationality", "Nationalité"),
    ("place_of_birth", "Lieu de naissance"),
    ("sex", "Sexe"),
    ("marital_status", "État civil"),
    ("phone", "Téléphone"),
    ("birth_name", "Nom de naissance"),
    ("profession", "Profession"),
    ("employer", "Employeur"),
    ("tags", "Tags"),
    ("source", "Source"),
]

_COMPANY_NATIVE: list[tuple[str, str]] = [
    ("id", "id"),
    ("name", "Dénomination"),
    ("tags", "Tags"),
    ("source", "Source"),
]


class ExportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_agency_export(self, agent: Agent) -> tuple[bytes, str]:
        """Assemble the ZIP and its download filename. Reads only; emits one
        usage event (agency.data_exported) and commits, exactly like the
        per-case PDF export."""
        agency = await self.db.get(Agency, agent.agency_id)
        if agency is None:
            raise NotFoundError("Agency not found.")
        lang = agency.default_language or DEFAULT_LANG
        perms = await self._permission_keys(agent.role_id)
        include_cost = Permission.COST_VIEW.value in perms
        include_confidential = Permission.NOTE_VIEW_CONFIDENTIAL.value in perms

        persons_csv, person_count = await self._persons_csv(agency.id, lang)
        companies_csv, company_count = await self._companies_csv(agency.id, lang)
        cases_csv, case_ids, case_count = await self._cases_csv(agency.id, include_cost)
        activity_csv = await self._activity_csv(case_ids)
        notes_csv, confidential_hidden = await self._notes_csv(case_ids, include_confidential)

        files = {
            "fiches-personnes.csv": persons_csv,
            "fiches-societes.csv": companies_csv,
            "dossiers.csv": cases_csv,
            "dossiers-activite.csv": activity_csv,
            "dossiers-notes.csv": notes_csv,
            "LISEZ-MOI.txt": self._readme(
                agency.name,
                include_cost=include_cost,
                confidential_hidden=confidential_hidden,
            ),
        }
        content = build_zip(files)

        await UsageManager(self.db).emit(
            agency_id=agency.id,
            event_type="agency.data_exported",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"persons": person_count, "companies": company_count, "cases": case_count},
        )
        await self.db.commit()
        stamp = datetime.now(UTC).date().isoformat()
        return content, f"nidria-export-{agency.slug}-{stamp}.zip"

    async def _permission_keys(self, role_id: uuid.UUID) -> set[str]:
        """The actor's effective permission keys — the export is admin-only
        (agency.manage), but the cost columns and confidential notes stay
        gated on their own permissions (a custom role may hold one, not
        the other)."""
        rows = await self.db.execute(
            select(PermissionRow.key)
            .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
            .where(RolePermission.role_id == role_id)
        )
        return set(rows.scalars())

    async def _persons_csv(self, agency_id: uuid.UUID, lang: str) -> tuple[str, int]:
        definitions = (
            await self.db.execute(
                select(CustomFieldDefinition)
                .where(
                    CustomFieldDefinition.agency_id == agency_id,
                    CustomFieldDefinition.scope == "person",
                    CustomFieldDefinition.archived_at.is_(None),
                )
                .order_by(CustomFieldDefinition.position, CustomFieldDefinition.key)
            )
        ).scalars().all()
        agency_default = lang
        header = [label for _, label in _PERSON_NATIVE] + [
            resolve_i18n(d.label_i18n, lang, agency_default, d.label) or d.key for d in definitions
        ]
        profiles = (
            await self.db.execute(
                select(ClientProfile).where(ClientProfile.agency_id == agency_id)
            )
        ).scalars().all()
        rows: list[list[object]] = []
        for profile in profiles:
            native = [getattr(profile, attr) for attr, _ in _PERSON_NATIVE]
            custom = [(profile.custom_fields or {}).get(d.key) for d in definitions]
            rows.append(native + custom)
        return render_csv(header, rows), len(profiles)

    async def _companies_csv(self, agency_id: uuid.UUID, lang: str) -> tuple[str, int]:
        definitions = (
            await self.db.execute(
                select(CompanyFieldDefinition)
                .where(
                    CompanyFieldDefinition.agency_id == agency_id,
                    CompanyFieldDefinition.archived_at.is_(None),
                )
                .order_by(CompanyFieldDefinition.position, CompanyFieldDefinition.key)
            )
        ).scalars().all()
        header = [label for _, label in _COMPANY_NATIVE] + [
            resolve_i18n(d.label_i18n, lang, lang, d.label) or d.key for d in definitions
        ]
        companies = (
            await self.db.execute(
                select(CompanyProfile).where(CompanyProfile.agency_id == agency_id)
            )
        ).scalars().all()
        rows: list[list[object]] = []
        for company in companies:
            native = [getattr(company, attr) for attr, _ in _COMPANY_NATIVE]
            custom = [(company.custom_fields or {}).get(d.key) for d in definitions]
            rows.append(native + custom)
        return render_csv(header, rows), len(companies)

    async def _cases_csv(
        self, agency_id: uuid.UUID, include_cost: bool
    ) -> tuple[str, list[uuid.UUID], int]:
        cases = (
            await self.db.execute(
                select(ClientCase).where(
                    ClientCase.agency_id == agency_id,
                    ClientCase.deleted_at.is_(None),
                    ClientCase.is_demo.is_(False),
                )
            )
        ).scalars().all()
        # Bulk lookups — no per-case query (owner name, journey name, client email).
        agents = {
            a.id: f"{a.first_name} {a.last_name}".strip()
            for a in (
                await self.db.execute(select(Agent).where(Agent.agency_id == agency_id))
            ).scalars()
        }
        templates = {
            t.id: t.name
            for t in (
                await self.db.execute(
                    select(JourneyTemplate).where(JourneyTemplate.agency_id == agency_id)
                )
            ).scalars()
        }
        principal_ids = {c.principal_expat_user_id for c in cases}
        emails = {}
        if principal_ids:
            emails = {
                e.id: e.email
                for e in (
                    await self.db.execute(
                        select(ExpatUser).where(ExpatUser.id.in_(principal_ids))
                    )
                ).scalars()
            }
        header = [
            "id",
            "Référence",
            "Statut",
            "Client (email)",
            "Responsable",
            "Parcours",
            "Pays origine",
            "Ville origine",
            "Pays destination",
            "Ville destination",
            "Source",
            "Tags",
            "Créé le",
        ]
        if include_cost:
            header += ["Montant facturé", "Devise"]
        rows: list[list[object]] = []
        for case in cases:
            row: list[object] = [
                str(case.id),
                case.reference,
                case.status,
                emails.get(case.principal_expat_user_id),
                agents.get(case.owner_agent_id) if case.owner_agent_id else None,
                templates.get(case.journey_template_id) if case.journey_template_id else None,
                case.origin_country,
                case.origin_city,
                case.dest_country,
                case.dest_city,
                case.source,
                case.tags,
                case.created_at.isoformat() if case.created_at else None,
            ]
            if include_cost:
                row += [case.billed_amount, case.billed_currency]
            rows.append(row)
        return render_csv(header, rows), [c.id for c in cases], len(cases)

    async def _activity_csv(self, case_ids: list[uuid.UUID]) -> str:
        header = ["Dossier", "Date", "Acteur", "Action", "Détails"]
        rows: list[list[object]] = []
        if case_ids:
            refs = await self._case_refs(case_ids)
            entries = (
                await self.db.execute(
                    select(ActivityLog)
                    .where(ActivityLog.case_id.in_(case_ids))
                    .order_by(ActivityLog.case_id, ActivityLog.created_at)
                )
            ).scalars().all()
            rows = [
                [
                    refs.get(a.case_id, str(a.case_id)),
                    a.created_at.isoformat() if a.created_at else None,
                    a.actor_type,
                    a.action_type,
                    a.details,
                ]
                for a in entries
            ]
        return render_csv(header, rows)

    async def _notes_csv(
        self, case_ids: list[uuid.UUID], include_confidential: bool
    ) -> tuple[str, bool]:
        header = ["Dossier", "Date", "Auteur", "Confidentielle", "Note"]
        rows: list[list[object]] = []
        confidential_hidden = False
        if case_ids:
            refs = await self._case_refs(case_ids)
            query = select(CaseNote).where(CaseNote.case_id.in_(case_ids))
            if not include_confidential:
                query = query.where(CaseNote.is_confidential.is_(False))
            notes = (
                await self.db.execute(query.order_by(CaseNote.case_id, CaseNote.created_at))
            ).scalars().all()
            author_ids = {n.author_agent_id for n in notes if n.author_agent_id}
            authors = {}
            if author_ids:
                authors = {
                    a.id: f"{a.first_name} {a.last_name}".strip()
                    for a in (
                        await self.db.execute(select(Agent).where(Agent.id.in_(author_ids)))
                    ).scalars()
                }
            rows = [
                [
                    refs.get(n.case_id, str(n.case_id)),
                    n.created_at.isoformat() if n.created_at else None,
                    authors.get(n.author_agent_id) if n.author_agent_id else None,
                    n.is_confidential,
                    n.body,
                ]
                for n in notes
            ]
            if not include_confidential:
                confidential_hidden = bool(
                    (
                        await self.db.execute(
                            select(CaseNote.id).where(
                                CaseNote.case_id.in_(case_ids),
                                CaseNote.is_confidential.is_(True),
                            )
                        )
                    ).first()
                )
        return render_csv(header, rows), confidential_hidden

    async def _case_refs(self, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """case_id → a human label (its reference, else the id) for the
        history CSVs' join column."""
        rows = (
            await self.db.execute(
                select(ClientCase.id, ClientCase.reference).where(ClientCase.id.in_(case_ids))
            )
        ).all()
        return {cid: (ref or str(cid)) for cid, ref in rows}

    @staticmethod
    def _readme(agency_name: str, *, include_cost: bool, confidential_hidden: bool) -> str:
        stamp = datetime.now(UTC).date().isoformat()
        lines = [
            f"Export des données — {agency_name}",
            f"Généré le {stamp}.",
            "",
            "Contenu de cette archive :",
            "- fiches-personnes.csv : vos fiches clients (personnes) et leurs champs personnalisés.",
            "- fiches-societes.csv : vos fiches sociétés et leurs champs personnalisés.",
            "- dossiers.csv : vos dossiers, une ligne par dossier.",
            "- dossiers-activite.csv : l'historique (journal d'activité) de vos dossiers.",
            "- dossiers-notes.csv : les notes internes de vos dossiers.",
            "",
            "Les colonnes des fiches correspondent à ce que l'import accepte :",
            "un fichier exporté ici peut être réimporté après édition.",
            "",
            "Les PIÈCES DÉPOSÉES (documents) ne sont PAS incluses dans cet export.",
            "Elles restent téléchargeables dossier par dossier depuis l'espace agence.",
        ]
        if not include_cost:
            lines.append("")
            lines.append(
                "Les montants facturés ne sont pas inclus (permission « voir les coûts » requise)."
            )
        if confidential_hidden:
            lines.append("")
            lines.append(
                "Des notes confidentielles existent mais ne sont pas incluses "
                "(permission dédiée requise)."
            )
        return "\n".join(lines) + "\n"

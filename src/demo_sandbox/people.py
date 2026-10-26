"""Deterministic fictional identities, never researched or imported people."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from src.admin.demo_schema import AgencyDemoInput

Name = tuple[str, str]

CLIENTS: dict[str, tuple[Name, ...]] = {
    "GB": (
        ("Jamie", "Hawthorne"),
        ("Amelia", "Bennett"),
        ("Oliver", "Reed"),
        ("Sophie", "Whitfield"),
        ("Daniel", "Brooks"),
        ("Grace", "Ellis"),
        ("Noah", "Hartley"),
        ("Chloe", "Warren"),
        ("Adam", "Foster"),
        ("Emma", "Sinclair"),
        ("Priya", "Shah"),
        ("Hassan", "Malik"),
        ("Freya", "Collins"),
        ("Tariq", "Hussain"),
        ("Isla", "Campbell"),
    ),
    "US": (
        ("Maria", "Gonzalez"),
        ("Wei", "Chen"),
        ("Oluwaseun", "Adeyemi"),
        ("Sofia", "Ramirez"),
        ("Arjun", "Patel"),
        ("Linh", "Nguyen"),
        ("Mateo", "Rivera"),
        ("Fatima", "Hassan"),
        ("Daniel", "Kim"),
        ("Olena", "Kovalenko"),
        ("Luis", "Mendoza"),
        ("Amina", "Diallo"),
        ("Carlos", "Alvarez"),
        ("Elena", "Petrova"),
        ("Jin", "Park"),
    ),
    "AU": (
        ("Lachlan", "Wilson"),
        ("Charlotte", "Nguyen"),
        ("Matilda", "Brooks"),
        ("Cooper", "Taylor"),
        ("Harper", "Singh"),
        ("Jack", "Anderson"),
        ("Isla", "Campbell"),
        ("Oliver", "Wright"),
        ("Amelia", "Chen"),
        ("Noah", "Williams"),
        ("Sophie", "Mitchell"),
        ("Ethan", "Tran"),
        ("Chloe", "Martin"),
        ("Harrison", "Walker"),
        ("Mia", "Thompson"),
    ),
    "IE": (
        ("Aoife", "Murphy"),
        ("Cian", "Walsh"),
        ("Saoirse", "Kelly"),
        ("Sean", "Byrne"),
        ("Niamh", "Ryan"),
        ("Conor", "Doyle"),
        ("Ciara", "McCarthy"),
        ("Darragh", "O'Brien"),
        ("Orla", "Fitzgerald"),
        ("Eoin", "Gallagher"),
        ("Aisling", "Nolan"),
        ("Ronan", "Kennedy"),
    ),
    "CA": (
        ("Emma", "Tremblay"),
        ("Liam", "Wilson"),
        ("Sophie", "Gagnon"),
        ("Noah", "Chen"),
        ("Olivia", "Roy"),
        ("Lucas", "Martin"),
        ("Maya", "Singh"),
        ("Ethan", "Brown"),
        ("Chloe", "Bouchard"),
        ("Nathan", "Campbell"),
        ("Isabelle", "Nguyen"),
        ("Owen", "MacDonald"),
    ),
    "NZ": (
        ("Charlotte", "Wilson"),
        ("Oliver", "Thompson"),
        ("Maia", "Taylor"),
        ("Liam", "Anderson"),
        ("Sophie", "Patel"),
        ("Jack", "Walker"),
        ("Amelia", "Chen"),
        ("Noah", "Williams"),
        ("Isla", "Campbell"),
        ("Lucas", "Martin"),
        ("Aria", "King"),
        ("Theo", "Scott"),
    ),
    "FR": (
        ("Camille", "Morel"),
        ("Hugo", "Perrin"),
        ("Léa", "Roussel"),
        ("Thomas", "Mercier"),
        ("Alice", "Garnier"),
        ("Louis", "Chevalier"),
        ("Jade", "Lefèvre"),
        ("Paul", "Fontaine"),
        ("Manon", "Colin"),
        ("Gabriel", "Roux"),
        ("Inès", "Laurent"),
        ("Arthur", "Dubois"),
    ),
    "BE": (
        ("Camille", "Dubois"),
        ("Louis", "Lambert"),
        ("Emma", "Peeters"),
        ("Arthur", "Laurent"),
        ("Louise", "Simon"),
        ("Jules", "Martin"),
        ("Alice", "Leclercq"),
        ("Hugo", "Dumont"),
        ("Elise", "Jacobs"),
        ("Noah", "Dupont"),
        ("Mila", "De Smet"),
        ("Lucas", "Willems"),
    ),
}
TEAMS: dict[str, tuple[Name, ...]] = {
    "GB": (
        ("Sarah", "Mitchell"),
        ("James", "Cooper"),
        ("Olivia", "Turner"),
        ("Nora", "Reed"),
        ("Harry", "Bennett"),
    ),
    "US": (
        ("Jessica", "Carter"),
        ("Michael", "Davis"),
        ("Ashley", "Johnson"),
        ("David", "Miller"),
        ("Emily", "Garcia"),
    ),
    "AU": (
        ("Emily", "Thompson"),
        ("Lachlan", "Harris"),
        ("Sophie", "Clarke"),
        ("Jack", "Robinson"),
        ("Grace", "Taylor"),
    ),
    "IE": (
        ("Niamh", "Doyle"),
        ("Conor", "Murphy"),
        ("Ciara", "Walsh"),
        ("Eoin", "Kelly"),
        ("Aoife", "Byrne"),
    ),
    "CA": (
        ("Sarah", "Campbell"),
        ("Alexandre", "Roy"),
        ("Emily", "Gagnon"),
        ("Daniel", "Wong"),
        ("Maya", "Wilson"),
    ),
    "NZ": (
        ("Sophie", "Harris"),
        ("Jack", "Williams"),
        ("Olivia", "King"),
        ("Theo", "Clarke"),
        ("Charlotte", "Taylor"),
    ),
    "FR": (
        ("Élodie", "Martin"),
        ("Lucas", "Bernard"),
        ("Clara", "Petit"),
        ("Nora", "Laurent"),
        ("Julien", "Robert"),
    ),
    "BE": (
        ("Sophie", "Lambert"),
        ("Thomas", "Dumont"),
        ("Julie", "Peeters"),
        ("Nora", "Dubois"),
        ("Louis", "Jacobs"),
    ),
}


@dataclass(frozen=True)
class FictionalPeople:
    clients: tuple[Name, ...]
    team: tuple[Name, ...]


def fictional_people(payload: AgencyDemoInput) -> FictionalPeople:
    seed = hashlib.sha256(f"{payload.country}:{payload.company_name}".encode()).digest()
    clients, team = CLIENTS[payload.country], TEAMS[payload.country]
    # Rotate stable, country-specific pools. The full US immigration pool has
    # diverse origins; no nationality is inferred from a person's name.
    offset = int.from_bytes(seed[:2], "big") % len(clients)
    team_offset = int.from_bytes(seed[2:4], "big") % len(team)
    return FictionalPeople(
        clients=tuple(clients[(offset + i) % len(clients)] for i in range(10)),
        team=tuple(team[(team_offset + i) % len(team)] for i in range(4)),
    )


def slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-") or "cabinet"


def fictional_email(name: Name, domain: str) -> str:
    return f"{slug(name[0])}.{slug(name[1])}@{domain}"


async def email_domain(db: AsyncSession, company: str) -> str:
    """Reserve a readable .example domain without reusing another demo's clients."""
    base = slug(company)[:48].rstrip("-")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"fictional-mail:{base}"},
    )
    suffix = 1
    while True:
        domain = f"{base}{'' if suffix == 1 else f'-{suffix}'}.example"
        if not await db.scalar(select(Agency.id).where(Agency.contact_email == f"office@{domain}")):
            return domain
        suffix += 1

"""Sémantique du flag signatures, VERROUILLÉE (LOT 6, point 2).

`SIGNATURES_ENABLED` (env) est l'interrupteur MAÎTRE : env off = off pour
toute agence, quoi que dise son réglage — le AND est structurel, aucun
PATCH ne peut le contourner. `agency.settings["signatures_enabled"]` est
le SOUS-interrupteur de rollout : absent = suit l'env (défaut True), False
= cette agence n'envoie pas (rollout sélectif / coupure ciblée).

Périmètre du sous-interrupteur : les ENVOIS (matérialisation de demandes)
et l'exposition espace client. Le webhook reste env-only : une demande en
vol converge toujours (signatures enregistrées, crédits jamais coincés),
couper le sous-interrupteur n'abandonne pas ce qui est parti."""

from shared.models.agency import Agency
from src.core.config import get_settings


def signatures_effectively_enabled(agency: Agency | None) -> bool:
    if not get_settings().signatures_enabled:
        return False  # l'interrupteur MAÎTRE — rien ne le contourne
    raw = ((agency.settings if agency else None) or {}).get("signatures_enabled")
    if isinstance(raw, bool):
        return raw
    return True  # absent (ou invalide) = suit l'env

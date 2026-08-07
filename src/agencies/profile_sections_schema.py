"""Contrat des sections de fiche configurables."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,49}$"


class ProfileSectionResponse(BaseModel):
    id: uuid.UUID
    # La clé STABLE : c'est elle que portent les définitions de champs
    # (`profile_section`). Elle ne change jamais après création — renommer
    # touche le libellé.
    key: str
    surface: str
    # Le libellé résolu pour la langue du visiteur.
    name: str
    # Le blob ×7, même forme que `label_i18n` sur les champs : celui de
    # l'agence si elle a renommé, celui du CATALOGUE sinon (le repli, qui
    # suit les corrections de traduction du produit).
    name_i18n: dict[str, str]
    # D'où vient le libellé servi. Sans lui, l'écran ne pourrait pas
    # proposer « rétablir le nom d'origine » sans deviner.
    customized: bool
    position: int
    # « Divers » ne se supprime jamais — servi plutôt que déduit d'une
    # comparaison de clé côté écran.
    deletable: bool


class ProfileSectionCreateRequest(BaseModel):
    surface: Literal["person", "company"]
    key: str = Field(pattern=_KEY_PATTERN)
    # Une section créée PORTE son libellé : il n'y a pas de catalogue où
    # retomber pour une clé que le produit ne connaît pas.
    label_i18n: dict[str, str] = Field(min_length=1)
    position: int | None = None


class ProfileSectionUpdateRequest(BaseModel):
    """`key` et `surface` sont ABSENTS, délibérément : la clé est portée
    par les définitions de champs (la changer les ferait toutes tomber en
    « Divers »), et déménager une section d'une face à l'autre laisserait
    ses champs derrière elle."""

    # `{}` REND le libellé d'origine — c'est l'annulation d'un renommage,
    # et elle ne touche pas aux champs de la section.
    label_i18n: dict[str, str] | None = None
    position: int | None = None

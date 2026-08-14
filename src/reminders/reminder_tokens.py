"""Les variables d'une relance, DÉCOUVRABLES (lot 14/08).

Trois jetons étaient déjà résolus ({client_name}, {step_name}, {days_left})
et RIEN ne les nommait : l'agence qui écrit un message dans la modale ne
pouvait pas savoir qu'ils existent. Le catalogue ci-dessous est la source
unique — servi au contrat avec un libellé humain et un EXEMPLE, exactement
comme le catalogue des conditions (`consents/agency_tokens`).

LA LOGIQUE EST L'INVERSE DE CELLE DES CONDITIONS, et ce n'est pas un
accident : une relance est un message daté envoyé UNE FOIS, donc son texte
est INTERPOLÉ AU FIGEAGE (à la création/édition du rappel) et ne bouge plus ;
un contrat est vivant, donc il résout à la LECTURE. Ne pas unifier les deux
systèmes — c'est la même mécanique de surface sur deux besoins opposés.

Trois conséquences de ce sens-là :

1. L'EXEMPLE, PAS LA VALEUR COURANTE. Au moment où l'agence écrit, il n'y a
   pas forcément un client en face — et il y en aura un DIFFÉRENT à chaque
   envoi. `{client_name}` s'annonce donc « Marie Dupont », un spécimen. Les
   jetons d'AGENCE font exception : ils ne varient pas d'un envoi à l'autre,
   donc leur exemple EST leur valeur réelle (l'agence lit son propre nom),
   avec un spécimen en repli quand le champ est vide.
2. UN JETON INCONNU EST GELÉ VERBATIM. `{tva}` ne lève rien : il traverse et
   se fige dans le texte envoyé. C'est la même règle que les conditions, mais
   la conséquence est plus dure ici — un contrat mal rendu se corrige, un
   message parti ne se rattrape pas. D'où `unknown_tokens`, à signaler À
   L'ÉDITION, avant le figeage.
3. UN JETON CONNU MAIS NON RÉSOLUBLE LÈVE UN 422 NOMMÉ, au figeage. Cette
   règle est bonne et reste ; elle ne doit simplement jamais être atteinte
   par un geste normal, ce que l'aperçu garantit en amont.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from shared.models.agency import Agency

# Ce à quoi ressemble un jeton quand on en CHERCHE un à signaler : n'importe
# quoi entre accolades sur une ligne. Volontairement PLUS LARGE que les noms
# du catalogue, pour attraper aussi bien la coquille ({client_nam}) que
# l'invention pleine d'espoir ({numéro de dossier}) — même idiome que le
# catalogue des conditions.
_TOKEN_PATTERN = re.compile(r"\{([^{}\n]{1,64})\}")


@dataclass(frozen=True)
class ReminderTokenSpec:
    """`name` sert de clé i18n au front ; `label` est le repli FR servi, pour
    qu'un jeton que ce front ne connaît pas affiche une phrase et non un
    identifiant. `sample` est le spécimen montré à l'écriture.

    `read_agency` n'existe que pour les jetons d'AGENCE : leur valeur ne
    dépend pas de l'envoi, donc l'exemple servi est la valeur réelle."""

    name: str
    label: str
    sample: str
    read_agency: Callable[[Agency], str | None] | None = None

    def example(self, agency: Agency | None) -> str:
        """Ce que le front montre à côté du jeton. Valeur réelle pour un
        jeton d'agence renseigné, spécimen sinon — jamais un vide, qui ne
        dirait rien à l'agence qui écrit."""
        if self.read_agency is not None and agency is not None:
            value = self.read_agency(agency)
            if value and value.strip():
                return value.strip()
        return self.sample


CATALOGUE: tuple[ReminderTokenSpec, ...] = (
    # --- le dossier : un client différent à chaque envoi, donc un spécimen.
    ReminderTokenSpec("client_name", "le nom de votre client", "Marie Dupont"),
    ReminderTokenSpec("step_name", "l'étape concernée", "Dépôt du dossier"),
    ReminderTokenSpec("days_left", "les jours restants sur l'étape", "5"),
    # --- l'agence : constante d'un envoi à l'autre, donc l'exemple est le
    # vrai. Une relance signée par l'agence doit pouvoir la nommer — le
    # canal MAIL porte déjà son nom dans l'enveloppe, mais WHATSAPP (copié-
    # collé par l'agent) et IN_APP n'ont que le corps du message.
    ReminderTokenSpec("agency_name", "le nom de votre agence", "Votre agence", lambda a: a.name),
    ReminderTokenSpec(
        "contact_email",
        "votre email de contact",
        "contact@votre-agence.fr",
        lambda a: a.contact_email,
    ),
    ReminderTokenSpec(
        "contact_phone",
        "votre téléphone de contact",
        "+33 1 23 45 67 89",
        lambda a: a.contact_phone,
    ),
)

_BY_NAME: dict[str, ReminderTokenSpec] = {spec.name: spec for spec in CATALOGUE}

# Les noms que le moteur d'interpolation sait résoudre — dérivés du
# catalogue, JAMAIS réécrits à la main : ajouter un jeton au catalogue suffit
# à ce que `_render` le reconnaisse (le motif d'AVANT ce lot listait les trois
# noms en dur, ce qui rendait toute addition silencieusement inopérante).
VARIABLE_PATTERN = re.compile(r"\{(" + "|".join(spec.name for spec in CATALOGUE) + r")\}")

# Les jetons d'AGENCE : résolus depuis l'agence du dossier, pas du dossier.
AGENCY_TOKENS: tuple[str, ...] = tuple(
    spec.name for spec in CATALOGUE if spec.read_agency is not None
)


def agency_value(name: str, agency: Agency) -> str | None:
    """La valeur RÉELLE d'un jeton d'agence, ou None si le champ est vide —
    auquel cas le figeage refuse plutôt que de sceller un trou dans un
    message qui ne partira qu'une fois."""
    spec = _BY_NAME[name]
    assert spec.read_agency is not None, f"{name} n'est pas un jeton d'agence"
    value = spec.read_agency(agency)
    return value.strip() if value and value.strip() else None


def token_values(agency: Agency | None) -> list[tuple[str, str, str]]:
    """Le catalogue comme (name, label, example) — ce que le contrat sert
    pour que le front ne devine aucune liste."""
    return [(spec.name, spec.label, spec.example(agency)) for spec in CATALOGUE]


def unknown_tokens(content: str | None) -> list[str]:
    """Les jetons que personne ne résout, dans l'ordre de lecture, sans
    doublon. Ils seront GELÉS TELS QUELS dans le message envoyé : c'est à
    l'édition qu'il faut le dire. Rendus entre accolades, comme écrits."""
    if not content:
        return []
    seen: list[str] = []
    for name in _TOKEN_PATTERN.findall(content):
        if name not in _BY_NAME and name not in seen:
            seen.append(name)
    return ["{" + name + "}" for name in seen]


def used_tokens(content: str | None) -> list[str]:
    """Les jetons CONNUS employés par un texte, dans l'ordre de lecture."""
    if not content:
        return []
    seen: list[str] = []
    for name in VARIABLE_PATTERN.findall(content):
        if name not in seen:
            seen.append(name)
    return seen


def render_with_examples(content: str, agency: Agency | None) -> str:
    """Le texte rendu avec les SPÉCIMENS — l'aperçu quand aucun dossier réel
    n'est en face (un modèle de message, une modale encore vide de client).
    Les jetons inconnus restent verbatim : c'est ce que le client lirait."""
    rendered = content
    for spec in CATALOGUE:
        placeholder = "{" + spec.name + "}"
        if placeholder in rendered:
            rendered = rendered.replace(placeholder, spec.example(agency))
    return rendered

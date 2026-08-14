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

Et une conséquence du sens INVERSE — le figeage sait QUI lira, une lecture ne
le saurait pas. Deux jetons en vivent (lot 14/08, second passage) :
{step_due_date} s'écrit dans la langue du destinataire, et {client_space_link}
ne se fige QUE si cet espace est activé. Le second est le seul jeton dont la
condition ne porte pas sur une donnée manquante mais sur un état : proposer le
lien à qui n'a pas activé son espace, c'est envoyer un client sur un mur.
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
    # La FAMILLE du jeton — une clé i18n, jamais un libellé : le front traduit
    # le titre, le back nomme la famille. Requise, donc aucun jeton ne peut
    # entrer au catalogue sans dire à quel groupe il appartient.
    #
    # C'est le titre du groupe qui porte la distinction que l'exemple ne peut
    # pas porter : dans le cas courant (aucun membre ciblé), {recipient_name}
    # et {case_client_name} rendent LA MÊME valeur. Seul « la personne qui
    # reçoit » / « le titulaire du dossier » les sépare à l'écriture.
    group: str
    read_agency: Callable[[Agency], str | None] | None = None
    # Ce jeton EXIGE une étape liée au rappel. Déclaré ici plutôt que dans une
    # liste à côté : c'est le catalogue qui décide, et son ORDRE décide lequel
    # est nommé le premier quand le rappel n'a pas d'étape (verdict stable).
    needs_step: bool = False
    # Ce jeton dépend du DESTINATAIRE — son nom, sa langue, son espace — et pas
    # du seul dossier. Ce drapeau évite les requêtes de résolution du
    # destinataire quand aucun jeton ne les réclame.
    needs_recipient: bool = False
    # Jeton DÉPRÉCIÉ : il continue de se résoudre (les textes déjà écrits par
    # les agences ne cassent pas) mais il n'est PLUS SERVI au catalogue, donc
    # la liste d'insertion ne le propose plus. Voir DEPRECATED_ALIASES.
    deprecated: bool = False

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
    # --- QUI LIT ce message. Le jeton des formules d'adresse (« Bonjour
    # Marie, »), et le seul correct pour ça : une relance ne part pas toujours
    # au titulaire du dossier. Elle peut partir au membre que l'étape désigne
    # (routage du 18/07), à un prestataire, ou au gestionnaire par escalade.
    ReminderTokenSpec(
        "recipient_name",
        "le nom de la personne qui reçoit ce message",
        "Marie Dupont",
        group="recipient",
        needs_recipient=True,
    ),
    ReminderTokenSpec(
        "recipient_first_name",
        "le prénom de la personne qui reçoit ce message",
        "Marie",
        group="recipient",
        needs_recipient=True,
    ),
    # --- LE TITULAIRE du dossier, quel que soit le destinataire. Ce n'est pas
    # un doublon du précédent : « le dossier de Jean Dupont attend votre
    # retour » écrit à un notaire est le cas d'usage NORMAL — le prestataire a
    # besoin de savoir de quel dossier on lui parle.
    #
    # Pourquoi pas « case_owner » : dans ce code, `owner` désigne déjà l'AGENT
    # propriétaire du dossier (`client_case.owner_agent_id`). Réutiliser le mot
    # pour l'expatrié aurait été une collision qui finit par mordre.
    ReminderTokenSpec(
        "case_client_name", "le nom du titulaire du dossier", "Jean Dupont", group="case_client"
    ),
    ReminderTokenSpec(
        "case_client_first_name",
        "le prénom du titulaire du dossier",
        "Jean",
        group="case_client",
    ),
    # --- LES DEUX DÉPRÉCIÉS. « client » ne disait pas DE QUEL humain il
    # s'agissait, et c'est exactement le bug : une relance adressée à Marie
    # commençait par « Bonjour Jean ». Ils gardent leur sens historique (le
    # titulaire) pour ne casser aucun texte déjà écrit, mais ils ne sont plus
    # proposés à l'écriture. Une agence qui veut saluer son lecteur prend
    # {recipient_first_name} ; une qui veut nommer le dossier prend
    # {case_client_name}.
    ReminderTokenSpec(
        "client_name",
        "le nom de votre client (déprécié)",
        "Jean Dupont",
        group="case_client",
        deprecated=True,
    ),
    ReminderTokenSpec(
        "client_first_name",
        "le prénom de votre client (déprécié)",
        "Jean",
        group="case_client",
        deprecated=True,
    ),
    # Le nom de l'étape est TRADUIT en base (`journey_template_step.name_i18n`)
    # depuis le bloc i18n : il se résout donc dans la langue du destinataire,
    # comme la date d'échéance juste en dessous.
    ReminderTokenSpec(
        "step_name",
        "l'étape concernée",
        "Dépôt du dossier",
        group="step",
        needs_step=True,
        needs_recipient=True,
    ),
    ReminderTokenSpec(
        "days_left", "les jours restants sur l'étape", "5", group="step", needs_step=True
    ),
    # L'ÉCHÉANCE FERME de l'étape (`case_step_progress.due_at`), pas le
    # compteur estimé : {days_left} dit « il reste 5 jours », celui-ci dit
    # « avant le 5 septembre 2026 ». La colonne est NULLABLE, donc même
    # discipline que {days_left} — un 422 NOMMÉ au figeage plutôt qu'un trou
    # scellé dans un message qui ne part qu'une fois. Le spécimen est en
    # français comme les libellés ; la vraie valeur suit la langue du
    # DESTINATAIRE (cf. `_resolve_values`).
    ReminderTokenSpec(
        "step_due_date",
        "l'échéance de l'étape",
        "5 septembre 2026",
        group="step",
        needs_step=True,
        needs_recipient=True,
    ),
    # --- l'agence : constante d'un envoi à l'autre, donc l'exemple est le
    # vrai. Une relance signée par l'agence doit pouvoir la nommer — le
    # canal MAIL porte déjà son nom dans l'enveloppe, mais WHATSAPP (copié-
    # collé par l'agent) et IN_APP n'ont que le corps du message.
    ReminderTokenSpec(
        "agency_name",
        "le nom de votre agence",
        "Votre agence",
        group="agency",
        read_agency=lambda a: a.name,
    ),
    ReminderTokenSpec(
        "contact_email",
        "votre email de contact",
        "contact@votre-agence.fr",
        group="agency",
        read_agency=lambda a: a.contact_email,
    ),
    ReminderTokenSpec(
        "contact_phone",
        "votre téléphone de contact",
        "+33 1 23 45 67 89",
        group="agency",
        read_agency=lambda a: a.contact_phone,
    ),
    # --- le lien vers l'espace client — SOUS CONDITION. Sa valeur ne dépend que
    # de l'agence (l'URL blanche-marque), mais sa RÉSOLUBILITÉ dépend du
    # destinataire : proposé à quelqu'un qui n'a pas activé son espace, il mène
    # à un mur. D'où le refus nommé au figeage, jamais un lien mort.
    ReminderTokenSpec(
        "client_space_link",
        "le lien vers l'espace de votre client",
        "https://app.nidria.com/space?agency=votre-agence",
        group="link",
        needs_recipient=True,
    ),
)

_BY_NAME: dict[str, ReminderTokenSpec] = {spec.name: spec for spec in CATALOGUE}

# Les familles, DANS L'ORDRE DU CATALOGUE et sans doublon — ce sont elles que le
# front traduit en titres de groupe. Dérivées, jamais réécrites à la main : une
# famille nouvelle apparaît par la seule addition d'un jeton qui la porte.
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(spec.group for spec in CATALOGUE))

# Les noms que le moteur d'interpolation sait résoudre — dérivés du
# catalogue, JAMAIS réécrits à la main : ajouter un jeton au catalogue suffit
# à ce que `_render` le reconnaisse (le motif d'AVANT ce lot listait les trois
# noms en dur, ce qui rendait toute addition silencieusement inopérante).
VARIABLE_PATTERN = re.compile(r"\{(" + "|".join(spec.name for spec in CATALOGUE) + r")\}")

# Les jetons d'AGENCE : résolus depuis l'agence du dossier, pas du dossier.
AGENCY_TOKENS: tuple[str, ...] = tuple(
    spec.name for spec in CATALOGUE if spec.read_agency is not None
)

# Les jetons qui exigent une étape liée, DANS L'ORDRE DU CATALOGUE — c'est lui
# qui nomme le premier refus quand le rappel n'a pas d'étape.
STEP_TOKENS: tuple[str, ...] = tuple(spec.name for spec in CATALOGUE if spec.needs_step)

# Les jetons qui exigent de savoir QUI LIT. Leur présence seule déclenche la
# résolution du destinataire (nom, langue, espace) — sinon on ne la paie pas.
RECIPIENT_TOKENS: tuple[str, ...] = tuple(spec.name for spec in CATALOGUE if spec.needs_recipient)

# Un jeton déprécié → le jeton canonique qui porte sa valeur. Le figeage résout
# le canonique et RECOPIE : une seule logique, deux orthographes. C'est ce qui
# permet de retirer un nom ambigu du catalogue sans réécrire les textes que les
# agences ont déjà enregistrés.
DEPRECATED_ALIASES: dict[str, str] = {
    "client_name": "case_client_name",
    "client_first_name": "case_client_first_name",
}
assert set(DEPRECATED_ALIASES) == {spec.name for spec in CATALOGUE if spec.deprecated}, (
    "tout jeton déprécié doit désigner son canonique, et réciproquement"
)
assert all(target in {spec.name for spec in CATALOGUE} for target in DEPRECATED_ALIASES.values()), (
    "un alias déprécié pointe un jeton qui n'existe pas au catalogue"
)


# Ce qu'une agence VOULAIT probablement dire en écrivant un jeton déprécié :
# saluer celui qui lit. Volontairement DISTINCT de DEPRECATED_ALIASES, qui dit
# ce que le jeton VAUT (le titulaire du dossier). Les deux ne coïncident pas —
# c'est exactement le malentendu qu'on répare, et c'est pourquoi le
# remplacement ne peut pas être automatique : changer {client_name} en
# {recipient_name} CHANGE la valeur rendue. L'agence tranche, en connaissance
# de cause.
DEPRECATED_SUGGESTIONS: dict[str, str] = {
    "client_name": "recipient_name",
    "client_first_name": "recipient_first_name",
}
assert set(DEPRECATED_SUGGESTIONS) == set(DEPRECATED_ALIASES), (
    "tout jeton déprécié doit proposer une alternative, et réciproquement"
)


def canonical_names(names: set[str]) -> set[str]:
    """Les noms à RÉSOUDRE pour satisfaire `names` : un alias déprécié est
    remplacé par son canonique."""
    return {DEPRECATED_ALIASES.get(name, name) for name in names}


def catalogue_index(name: str) -> int:
    """Le rang du jeton dans le catalogue. Sert à TRIER les refus : le 422 du
    figeage n'en nomme qu'un, et lequel ne doit pas dépendre de l'ordre dans
    lequel le code se trouve avoir résolu les valeurs."""
    return next(i for i, spec in enumerate(CATALOGUE) if spec.name == name)


def agency_value(name: str, agency: Agency) -> str | None:
    """La valeur RÉELLE d'un jeton d'agence, ou None si le champ est vide —
    auquel cas le figeage refuse plutôt que de sceller un trou dans un
    message qui ne partira qu'une fois."""
    spec = _BY_NAME[name]
    assert spec.read_agency is not None, f"{name} n'est pas un jeton d'agence"
    value = spec.read_agency(agency)
    return value.strip() if value and value.strip() else None


def token_values(agency: Agency | None) -> list[tuple[str, str, str, str]]:
    """Le catalogue comme (name, label, example) — ce que le contrat sert
    pour que le front ne devine aucune liste.

    Les jetons DÉPRÉCIÉS en sont absents : ils se résolvent encore (aucun texte
    d'agence ne casse) mais on cesse de les proposer. C'est toute la différence
    entre retirer un jeton et le déprécier."""
    return [
        (spec.name, spec.label, spec.example(agency), spec.group)
        for spec in CATALOGUE
        if not spec.deprecated
    ]


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


def deprecated_tokens(content: str | None) -> list[tuple[str, str, str, str]]:
    """Les jetons DÉPRÉCIÉS employés par un texte, comme
    (token, name, resolves_to, suggested) — le TROISIÈME signal d'édition,
    à côté des inconnus et des non-résolubles.

    Il existe parce que ces jetons sont devenus INVISIBLES : plus servis au
    catalogue (donc absents de la liste d'insertion) et pas signalés comme
    inconnus (puisqu'ils sont résolus). Sans ce signal, une agence garderait
    « Bonjour {client_name} » sans jamais apprendre qu'il nomme le titulaire
    du dossier et pas forcément la personne qui recevra le message.

    On ne réécrit rien à sa place : `resolves_to` dit ce que le jeton VAUT
    aujourd'hui (renommage sans changement de valeur), `suggested` ce qu'elle
    voulait probablement dire (saluer son lecteur). Le second CHANGE la valeur
    rendue — c'est un choix, pas une correction."""
    if not content:
        return []
    seen: list[str] = []
    for name in VARIABLE_PATTERN.findall(content):
        if name in DEPRECATED_ALIASES and name not in seen:
            seen.append(name)
    return [
        (
            "{" + name + "}",
            name,
            "{" + DEPRECATED_ALIASES[name] + "}",
            "{" + DEPRECATED_SUGGESTIONS[name] + "}",
        )
        for name in seen
    ]


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

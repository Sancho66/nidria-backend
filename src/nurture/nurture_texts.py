"""The 9 trial-nurture mails — Eric's draft v4 (2026-07-03), VERBATIM.

Source: "Mails cron essai (nurture) - draft.md". The texts follow Eric's
hardened rules (no em-dash, no AI tics, a different angle per mail) and
are NOT to be rewritten here — only the {Prénom} and booking placeholders
are interpolated at send time. ONE deliberate, ticket-approved deviation:
the J+28 "{créneau 1} ou {créneau 2}" slots become a booking-link
sentence ("réserve le créneau qui t'arrange : {booking_url}"), the
minimal rewording that lets the mail carry a link instead of dated slots.

FR only for now (Eric writes in FR, his current prospects are FR); the
send ledger records lang so translations can pick up later."""

from dataclasses import dataclass

FIRST_NAME_PLACEHOLDER = "{Prénom}"
BOOKING_PLACEHOLDER = "{booking_url}"


@dataclass(frozen=True)
class NurtureMail:
    subject: str
    body: str


# Keyed (state, day_key) — state evaluated AT SEND TIME, never at signup.
NURTURE_MAILS: dict[tuple[str, str], NurtureMail] = {
    # --- S0: no case created (the example is seeded, zero effort) -------------------
    ("S0", "j7"): NurtureMail(
        subject="un dossier d'exemple t'attend dans Nidria",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "Pour t'éviter le plus lourd, le premier dossier, tu en as déjà un d'exemple dans "
            "ton espace Nidria, complet et déjà rempli. Ouvre-le et regarde le côté client, la "
            "personne que tu accompagnes y voit chaque étape, ce qu'on attend d'elle, et où en "
            "est son dossier en temps réel.\n"
            "\n"
            "S'il tombe à côté de ce que tu fais, dis-moi ton type de dossier le plus courant "
            "et je t'en prépare un sur mesure.\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
    ("S0", "j21"): NurtureMail(
        subject="c'est le bon moment ou pas du tout ?",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "Une question simple, pour savoir où je me situe. Nidria, c'est le bon moment pour "
            "toi, ou pas du tout ?\n"
            "\n"
            "Si la semaine est juste chargée, je peux te préparer le terrain pour que tu n'aies "
            "presque rien à faire. Et si tu n'es pas convaincu, dis-le, ça ne me vexe pas et je "
            "te laisse tranquille.\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
    ("S0", "j28"): NurtureMail(
        subject="on continue ou on arrête ?",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "Ton mois d'essai se termine. Si tu veux, je te réserve vingt minutes pour te "
            "montrer Nidria sur un cas comme les tiens, réserve le créneau qui t'arrange : "
            "{booking_url}, et je te rouvre le temps de tester pour de vrai.\n"
            "\n"
            "Sinon, dis-moi juste que ce n'est pas le moment, et je te laisse tranquille.\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
    # --- S1: case created, no activated client (one action: put a client on it) -----
    ("S1", "j7"): NurtureMail(
        subject="qui suit ton dossier, à part toi ?",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "La partie qui te soulage commence quand ton client est sur le dossier : il suit "
            "l'avancement lui-même et arrête de t'écrire pour savoir où ça en est.\n"
            "\n"
            "Tu veux inviter un client sur un de tes dossiers ? Deux minutes, je te montre où.\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
    ("S1", "j21"): NurtureMail(
        subject="teste-le sans embêter un client",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "Si tu veux voir ce que ça donne sans exposer un vrai client, invite-toi toi-même "
            "avec une deuxième adresse à toi. Tu verras l'espace comme ton client le voit. Et "
            "tu comprendras pourquoi il arrête de te relancer.\n"
            "\n"
            "Deux minutes, je te guide si tu veux.\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
    ("S1", "j28"): NurtureMail(
        subject="ton mois se termine",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "Ton mois d'essai arrive au bout. Si tu veux, on prend quinze minutes ensemble sur "
            "un de tes dossiers, j'y mets un client avec toi, et tu vois en direct ce que ça "
            "change sur tes relances. Tu décides après.\n"
            "\n"
            "Réserve le créneau qui t'arrange : {booking_url}. Si tu ne le sens pas, "
            "dis-le-moi, on arrête sans souci.\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
    # --- S2: at least one activated client (ask, don't push) ------------------------
    ("S2", "j7"): NurtureMail(
        subject="il s'en sert vraiment ?",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "Ton client a activé son accès sur ton dossier. La vraie question, c'est de ton "
            "côté : est-ce qu'il suit vraiment, est-ce que ça t'enlève des relances, ou est-ce "
            "qu'un truc t'a fait tiquer ?\n"
            "\n"
            "Dis-moi, même ce qui cloche.\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
    ("S2", "j21"): NurtureMail(
        subject="ce premier dossier, il te sert ?",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "Tu tournes encore sur un dossier, et c'est le bon rythme pour juger "
            "tranquillement.\n"
            "\n"
            "Dis-moi juste : ce premier dossier partagé avec ton client, il te fait gagner du "
            "temps, ou pas encore ?\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
    ("S2", "j28"): NurtureMail(
        subject="ton mois se termine, on en parle ?",
        body=(
            "Salut {Prénom},\n"
            "\n"
            "Ton mois d'essai se termine dans quelques jours. Avant de parler de la suite, je "
            "veux ton avis vrai : ce dossier partagé avec ton client, ça t'a fait gagner du "
            "temps, ou tu attendais mieux ?\n"
            "\n"
            "Si c'est oui, on regarde ensemble la formule qui colle à ton agence, sans que tu "
            "paies pour des places vides. Si ce n'est pas encore concluant, dis-le-moi, je "
            "préfère ça à un abonnement que tu regrettes.\n"
            "\n"
            "Réserve le créneau qui t'arrange : {booking_url}.\n"
            "\n"
            "Éric, cofondateur de Nidria"
        ),
    ),
}


def render_mail(state: str, day_key: str, *, first_name: str, booking_url: str) -> NurtureMail:
    """Interpolate the two placeholders — nothing else is touched."""
    mail = NURTURE_MAILS[(state, day_key)]
    body = mail.body.replace(FIRST_NAME_PLACEHOLDER, first_name)
    body = body.replace(BOOKING_PLACEHOLDER, booking_url)
    return NurtureMail(subject=mail.subject, body=body)


def needs_booking_url(state: str, day_key: str) -> bool:
    return BOOKING_PLACEHOLDER in NURTURE_MAILS[(state, day_key)].body

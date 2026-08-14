import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MessageTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Agency-scoped reminder message template. `body` carries the
    variables of `reminders.reminder_tokens` ({client_name}, {agency_name},
    …) NON RESOLVED: a template is a text to reuse, never a rendered one.
    The resolution happens once, when a reminder freezes its own copy.

    APPLYING A TEMPLATE COPIES ITS TEXT. `reminder.message_body` is an
    independent string from that moment on, so editing this row NEVER
    rewrites a reminder already created — an approved reminder is a text a
    human validated, it must not change under them. The FK kept on the
    reminder is provenance, not a link that propagates.

    PAS D'OBJET ICI, et c'est délibéré (décision 14/08): le sujet du mail
    est produit par la chrome (`reminder_email`), localisé et au nom de
    l'agence. Une colonne `subject` sur ce modèle serait stockée puis
    IGNORÉE à l'envoi. Rendre l'objet personnalisable est un lot qui touche
    le chemin d'envoi (colonne sur `reminder` + override), pas les modèles —
    et il devra trancher WhatsApp/IN_APP (aucun objet) et le digest groupé
    (plusieurs relances, un seul mail)."""

    __tablename__ = "message_template"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # ÉTIQUETTES, pas des règles. Elles servent à RETROUVER un modèle dans
    # une liste qui grandit ; elles ne filtrent ni ne contraignent quoi que
    # ce soit à la création d'un rappel (une agence garde le droit
    # d'appliquer son modèle « email » à un WhatsApp).
    #
    # `language` : le texte lui-même, pas une traduction — une agence qui
    # relance en trois langues tient trois modèles et doit les distinguer.
    # NULL = non étiqueté. Pas de CHECK SQL : la liste des langues bouge
    # (i18n.SUPPORTED_LANGUAGES), la validation vit au schéma, comme pour
    # les autres champs de langue écrits par l'agence.
    language: Mapped[str | None] = mapped_column(String(5))
    # `channel` : on ne réutilise pas un texte d'email en WhatsApp (l'un a
    # une chrome autour de lui, l'autre est tout le message). NULL = tous
    # canaux. Valeurs : ReminderChannel (mail | whatsapp | in_app).
    channel: Mapped[str | None] = mapped_column(String(20))

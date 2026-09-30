"""Case PDF: key info, people and activity journal — in the requesting
agent's language. fpdf2 — pure Python, no system dependency (vs
WeasyPrint's pango/cairo).

FONT. The 14 core PDF fonts (Helvetica…) are latin-1 only: the former
`_latin1()` guard turned every Cyrillic letter, every Hungarian ő/ű and
even the « — » of the title into « ? » — for EVERY agency, French ones
included. DejaVu Sans 2.37 (TTF, `fonts/`, its licence alongside) is
embedded instead; fpdf2 subsets it, so a PDF only carries the glyphs it
actually uses.

LANGUAGE. `lang` is the request language (`RequestLang`: ?lang= →
Accept-Language → fr), i.e. the agent's UI language — the same "content
language = UI language" rule as every agency screen. The chrome comes from
the local catalogs below, worded like the front's locales (cases.json,
status.json) so the PDF and the screen say the same thing; `agency_default`
only feeds the fallback chain of the agency's own custom-field labels.
Stored codes (status, sex, marital status, countries, activity events) are
never printed raw.

TIME. The agency has no timezone in the model (same verdict as
`activity_stats`): instants are rendered in UTC, and the PDF says so.
"""

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.expat_user import ExpatUser
from src.core.countries import country_name
from src.core.enums import (
    ActorType,
    CasePersonKind,
    CaseStatus,
    CustomFieldType,
    MaritalStatus,
    Sex,
)
from src.core.i18n import DEFAULT_LANG, SUPPORTED_LANGUAGES, format_date_for_lang, resolve_i18n

_FONT_DIR = Path(__file__).parent / "fonts"
_FONT = "DejaVuSans"
_DASH = "—"
_MUTED = 110  # grey level of secondary text (export stamp, journal dates)

# fpdf2 subsets the embedded TTF through fontTools, which narrates every
# table it prunes at INFO — ~75 lines per export under the root INFO
# handler of `src.main`. Its warnings still come through.
logging.getLogger("fontTools.subset").setLevel(logging.WARNING)

# --- catalogs ------------------------------------------------------------------------------
#
# One block per language (a translator reads a language top to bottom). The
# drift guard at the end of this section fails at import when a language or
# a key goes missing — same doctrine as `src.core.i18n`.

_LABELS: dict[str, dict[str, str]] = {
    "fr": {
        "title": "Dossier",
        "exported": "Exporté le",
        "client": "Client",
        "origin_country": "Pays d'origine",
        "dest_country": "Pays de destination",
        "origin_address": "Adresse d'origine",
        "dest_address": "Adresse de destination",
        "status": "Statut",
        "owner": "Responsable du dossier",
        "tags": "Tags",
        "source": "Source",
        "created": "Créé le",
        "people": "Personnes",
        "principal": "Personne principale",
        "no_details": "Aucune information renseignée.",
        "passport_number": "N° de passeport",
        "date_of_birth": "Date de naissance",
        "nationality": "Nationalité",
        "place_of_birth": "Lieu de naissance",
        "sex": "Sexe",
        "marital_status": "Situation familiale",
        "phone": "Téléphone",
        "yes": "Oui",
        "no": "Non",
        "activity": "Activité",
        "no_activity": "Aucune activité pour l'instant.",
        "other_event": "Autre événement",
        "actor_agent": "L'agence",
        "actor_expat": "Le client",
        "actor_system": "Système",
        "actor_external": "Contact externe",
    },
    "en": {
        "title": "Case file",
        "exported": "Exported on",
        "client": "Client",
        "origin_country": "Origin country",
        "dest_country": "Destination country",
        "origin_address": "Origin address",
        "dest_address": "Destination address",
        "status": "Status",
        "owner": "Case owner",
        "tags": "Tags",
        "source": "Source",
        "created": "Created on",
        "people": "People",
        "principal": "Main person",
        "no_details": "No information provided.",
        "passport_number": "Passport no.",
        "date_of_birth": "Date of birth",
        "nationality": "Nationality",
        "place_of_birth": "Place of birth",
        "sex": "Sex",
        "marital_status": "Marital status",
        "phone": "Phone",
        "yes": "Yes",
        "no": "No",
        "activity": "Activity",
        "no_activity": "No activity yet.",
        "other_event": "Other event",
        "actor_agent": "The agency",
        "actor_expat": "The client",
        "actor_system": "System",
        "actor_external": "External contact",
    },
    "es": {
        "title": "Expediente",
        "exported": "Exportado el",
        "client": "Cliente",
        "origin_country": "País de origen",
        "dest_country": "País de destino",
        "origin_address": "Dirección de origen",
        "dest_address": "Dirección de destino",
        "status": "Estado",
        "owner": "Responsable del expediente",
        "tags": "Etiquetas",
        "source": "Origen",
        "created": "Creado el",
        "people": "Personas",
        "principal": "Persona principal",
        "no_details": "Ninguna información rellenada.",
        "passport_number": "N.º de pasaporte",
        "date_of_birth": "Fecha de nacimiento",
        "nationality": "Nacionalidad",
        "place_of_birth": "Lugar de nacimiento",
        "sex": "Sexo",
        "marital_status": "Estado civil",
        "phone": "Teléfono",
        "yes": "Sí",
        "no": "No",
        "activity": "Actividad",
        "no_activity": "Ninguna actividad por ahora.",
        "other_event": "Otro evento",
        "actor_agent": "La agencia",
        "actor_expat": "El cliente",
        "actor_system": "Sistema",
        "actor_external": "Contacto externo",
    },
    "ru": {
        "title": "Дело",
        "exported": "Экспортировано",
        "client": "Клиент",
        "origin_country": "Страна происхождения",
        "dest_country": "Страна назначения",
        "origin_address": "Адрес происхождения",
        "dest_address": "Адрес назначения",
        "status": "Статус",
        "owner": "Ответственный по делу",
        "tags": "Теги",
        "source": "Источник",
        "created": "Создано",
        "people": "Лица",
        "principal": "Основное лицо",
        "no_details": "Информация не заполнена.",
        "passport_number": "№ паспорта",
        "date_of_birth": "Дата рождения",
        "nationality": "Гражданство",
        "place_of_birth": "Место рождения",
        "sex": "Пол",
        "marital_status": "Семейное положение",
        "phone": "Телефон",
        "yes": "Да",
        "no": "Нет",
        "activity": "Активность",
        "no_activity": "Пока нет активности.",
        "other_event": "Другое событие",
        "actor_agent": "Агентство",
        "actor_expat": "Клиент",
        "actor_system": "Система",
        "actor_external": "Внешний контакт",
    },
    "pt": {
        "title": "Processo",
        "exported": "Exportado a",
        "client": "Cliente",
        "origin_country": "País de origem",
        "dest_country": "País de destino",
        "origin_address": "Morada de origem",
        "dest_address": "Morada de destino",
        "status": "Estado",
        "owner": "Responsável do processo",
        "tags": "Etiquetas",
        "source": "Fonte",
        "created": "Criado a",
        "people": "Pessoas",
        "principal": "Pessoa principal",
        "no_details": "Nenhuma informação preenchida.",
        "passport_number": "N.º de passaporte",
        "date_of_birth": "Data de nascimento",
        "nationality": "Nacionalidade",
        "place_of_birth": "Local de nascimento",
        "sex": "Sexo",
        "marital_status": "Estado civil",
        "phone": "Telefone",
        "yes": "Sim",
        "no": "Não",
        "activity": "Atividade",
        "no_activity": "Ainda não há atividade.",
        "other_event": "Outro evento",
        "actor_agent": "A agência",
        "actor_expat": "O cliente",
        "actor_system": "Sistema",
        "actor_external": "Contacto externo",
    },
    "it": {
        "title": "Pratica",
        "exported": "Esportato il",
        "client": "Cliente",
        "origin_country": "Paese di origine",
        "dest_country": "Paese di destinazione",
        "origin_address": "Indirizzo di origine",
        "dest_address": "Indirizzo di destinazione",
        "status": "Stato",
        "owner": "Responsabile della pratica",
        "tags": "Tag",
        "source": "Origine",
        "created": "Creata il",
        "people": "Persone",
        "principal": "Persona principale",
        "no_details": "Nessuna informazione inserita.",
        "passport_number": "N. di passaporto",
        "date_of_birth": "Data di nascita",
        "nationality": "Nazionalità",
        "place_of_birth": "Luogo di nascita",
        "sex": "Sesso",
        "marital_status": "Stato civile",
        "phone": "Telefono",
        "yes": "Sì",
        "no": "No",
        "activity": "Attività",
        "no_activity": "Nessuna attività per ora.",
        "other_event": "Altro evento",
        "actor_agent": "L'agenzia",
        "actor_expat": "Il cliente",
        "actor_system": "Sistema",
        "actor_external": "Contatto esterno",
    },
    "hu": {
        "title": "Ügy",
        "exported": "Exportálva",
        "client": "Ügyfél",
        "origin_country": "Származási ország",
        "dest_country": "Célország",
        "origin_address": "Származási cím",
        "dest_address": "Célországi cím",
        "status": "Státusz",
        "owner": "Az ügy gazdája",
        "tags": "Címkék",
        "source": "Forrás",
        "created": "Létrehozva",
        "people": "Személyek",
        "principal": "Fő személy",
        "no_details": "Nincs megadott adat.",
        "passport_number": "Útlevélszám",
        "date_of_birth": "Születési dátum",
        "nationality": "Állampolgárság",
        "place_of_birth": "Születési hely",
        "sex": "Nem",
        "marital_status": "Családi állapot",
        "phone": "Telefon",
        "yes": "Igen",
        "no": "Nem",
        "activity": "Tevékenység",
        "no_activity": "Egyelőre nincs tevékenység.",
        "other_event": "Egyéb esemény",
        "actor_agent": "Az iroda",
        "actor_expat": "Az ügyfél",
        "actor_system": "Rendszer",
        "actor_external": "Külső kapcsolat",
    },
}

# Agency-side case status labels (front: status.json › caseStatus).
_STATUS: dict[str, dict[str, str]] = {
    "fr": {
        "prospect": "Prospect",
        "in_progress": "En cours",
        "awaiting_documents": "Attente documents",
        "submitted": "Soumis",
        "validated": "Validé",
        "closed": "Clôturé",
    },
    "en": {
        "prospect": "Prospect",
        "in_progress": "In progress",
        "awaiting_documents": "Awaiting documents",
        "submitted": "Submitted",
        "validated": "Validated",
        "closed": "Closed",
    },
    "es": {
        "prospect": "Prospecto",
        "in_progress": "En curso",
        "awaiting_documents": "Documentos pendientes",
        "submitted": "Enviado",
        "validated": "Validado",
        "closed": "Cerrado",
    },
    "ru": {
        "prospect": "Потенциальный",
        "in_progress": "В работе",
        "awaiting_documents": "Ожидание документов",
        "submitted": "Подано",
        "validated": "Подтверждено",
        "closed": "Закрыто",
    },
    "pt": {
        "prospect": "Potencial cliente",
        "in_progress": "Em curso",
        "awaiting_documents": "A aguardar documentos",
        "submitted": "Submetido",
        "validated": "Validado",
        "closed": "Encerrado",
    },
    "it": {
        "prospect": "Prospect",
        "in_progress": "In corso",
        "awaiting_documents": "In attesa di documenti",
        "submitted": "Inviata",
        "validated": "Validata",
        "closed": "Chiusa",
    },
    "hu": {
        "prospect": "Érdeklődő",
        "in_progress": "Folyamatban",
        "awaiting_documents": "Dokumentumokra vár",
        "submitted": "Benyújtva",
        "validated": "Jóváhagyva",
        "closed": "Lezárva",
    },
}

# Civil status (front: cases.json › civil.sex / civil.marital).
_SEX: dict[str, dict[str, str]] = {
    "fr": {"M": "Homme", "F": "Femme", "X": "Autre"},
    "en": {"M": "Male", "F": "Female", "X": "Other"},
    "es": {"M": "Hombre", "F": "Mujer", "X": "Otro"},
    "ru": {"M": "Мужчина", "F": "Женщина", "X": "Другое"},
    "pt": {"M": "Homem", "F": "Mulher", "X": "Outro"},
    "it": {"M": "Uomo", "F": "Donna", "X": "Altro"},
    "hu": {"M": "Férfi", "F": "Nő", "X": "Egyéb"},
}

_MARITAL: dict[str, dict[str, str]] = {
    "fr": {
        "single": "Célibataire",
        "married": "Marié(e)",
        "divorced": "Divorcé(e)",
        "widowed": "Veuf(ve)",
        "partnership": "Pacsé(e)",
    },
    "en": {
        "single": "Single",
        "married": "Married",
        "divorced": "Divorced",
        "widowed": "Widowed",
        "partnership": "Civil partnership",
    },
    "es": {
        "single": "Soltero/a",
        "married": "Casado/a",
        "divorced": "Divorciado/a",
        "widowed": "Viudo/a",
        "partnership": "Pareja de hecho",
    },
    "ru": {
        "single": "Холост / не замужем",
        "married": "В браке",
        "divorced": "В разводе",
        "widowed": "Вдовец / вдова",
        "partnership": "Гражданское партнёрство",
    },
    "pt": {
        "single": "Solteiro(a)",
        "married": "Casado(a)",
        "divorced": "Divorciado(a)",
        "widowed": "Viúvo(a)",
        "partnership": "União de facto",
    },
    "it": {
        "single": "Celibe/Nubile",
        "married": "Sposato/a",
        "divorced": "Divorziato/a",
        "widowed": "Vedovo/a",
        "partnership": "Unito/a civilmente",
    },
    "hu": {
        "single": "Egyedülálló",
        "married": "Házas",
        "divorced": "Elvált",
        "widowed": "Özvegy",
        "partnership": "Bejegyzett élettársi kapcsolat",
    },
}

# `case_person.relationship` is FREE TEXT: imports and the wizard write
# English tokens (spouse, child…) that translate here (front: cases.json ›
# person.rel); anything else is the agency's own wording, printed verbatim.
_RELATIONSHIP: dict[str, dict[str, str]] = {
    "fr": {
        "associate": "Associé(e)",
        "brother": "Frère",
        "child": "Enfant",
        "daughter": "Fille",
        "father": "Père",
        "grandchild": "Petit-enfant",
        "grandparent": "Grand-parent",
        "husband": "Époux",
        "mother": "Mère",
        "other": "Autre",
        "parent": "Parent",
        "partner": "Partenaire",
        "sibling": "Frère/Sœur",
        "sister": "Sœur",
        "son": "Fils",
        "spouse": "Conjoint(e)",
        "wife": "Épouse",
    },
    "en": {
        "associate": "Associate",
        "brother": "Brother",
        "child": "Child",
        "daughter": "Daughter",
        "father": "Father",
        "grandchild": "Grandchild",
        "grandparent": "Grandparent",
        "husband": "Husband",
        "mother": "Mother",
        "other": "Other",
        "parent": "Parent",
        "partner": "Partner",
        "sibling": "Sibling",
        "sister": "Sister",
        "son": "Son",
        "spouse": "Spouse",
        "wife": "Wife",
    },
    "es": {
        "associate": "Socio/a",
        "brother": "Hermano",
        "child": "Hijo/a",
        "daughter": "Hija",
        "father": "Padre",
        "grandchild": "Nieto/a",
        "grandparent": "Abuelo/a",
        "husband": "Esposo",
        "mother": "Madre",
        "other": "Otro",
        "parent": "Padre/Madre",
        "partner": "Pareja",
        "sibling": "Hermano/a",
        "sister": "Hermana",
        "son": "Hijo",
        "spouse": "Cónyuge",
        "wife": "Esposa",
    },
    "ru": {
        "associate": "Партнёр по бизнесу",
        "brother": "Брат",
        "child": "Ребёнок",
        "daughter": "Дочь",
        "father": "Отец",
        "grandchild": "Внук/внучка",
        "grandparent": "Дедушка/бабушка",
        "husband": "Муж",
        "mother": "Мать",
        "other": "Другое",
        "parent": "Родитель",
        "partner": "Партнёр",
        "sibling": "Брат/сестра",
        "sister": "Сестра",
        "son": "Сын",
        "spouse": "Супруг(а)",
        "wife": "Жена",
    },
    "pt": {
        "associate": "Sócio/a",
        "brother": "Irmão",
        "child": "Filho/a",
        "daughter": "Filha",
        "father": "Pai",
        "grandchild": "Neto/a",
        "grandparent": "Avô/Avó",
        "husband": "Marido",
        "mother": "Mãe",
        "other": "Outro",
        "parent": "Pai/Mãe",
        "partner": "Parceiro/a",
        "sibling": "Irmão/ã",
        "sister": "Irmã",
        "son": "Filho",
        "spouse": "Cônjuge",
        "wife": "Esposa",
    },
    "it": {
        "associate": "Socio/a",
        "brother": "Fratello",
        "child": "Figlio/a",
        "daughter": "Figlia",
        "father": "Padre",
        "grandchild": "Nipote",
        "grandparent": "Nonno/a",
        "husband": "Marito",
        "mother": "Madre",
        "other": "Altro",
        "parent": "Genitore",
        "partner": "Partner",
        "sibling": "Fratello/sorella",
        "sister": "Sorella",
        "son": "Figlio",
        "spouse": "Coniuge",
        "wife": "Moglie",
    },
    "hu": {
        "associate": "Üzlettárs",
        "brother": "Fiútestvér",
        "child": "Gyermek",
        "daughter": "Lánygyermek",
        "father": "Apa",
        "grandchild": "Unoka",
        "grandparent": "Nagyszülő",
        "husband": "Férj",
        "mother": "Anya",
        "other": "Egyéb",
        "parent": "Szülő",
        "partner": "Élettárs",
        "sibling": "Testvér",
        "sister": "Lánytestvér",
        "son": "Fiúgyermek",
        "spouse": "Házastárs",
        "wife": "Feleség",
    },
}

# Activity events (front: cases.json › activity.action, same wording). The
# front has no label yet for the events after `step.validator_changed`
# (company link, invitation resends, client-sheet sync, automatic and
# forwarded reminders, e-signature): they are worded here. An event missing
# from this table renders as « <other event> (<humanized key>) », never raw.
_ACTIVITY: dict[str, dict[str, str]] = {
    "fr": {
        "case.created": "Dossier créé",
        "case.deleted": "Dossier supprimé",
        "case.invitation_sent": "Invitation envoyée au client",
        "case.journey_assigned": "Parcours assigné",
        "case.member_invited": "Personne invitée au dossier",
        "case.owner_changed": "Owner modifié",
        "case.status_changed": "Statut du dossier modifié",
        "case.updated": "Dossier modifié",
        "cost.added": "Coût ajouté",
        "cost.deleted": "Coût supprimé",
        "cost.edited": "Coût modifié",
        "document.deleted": "Document supprimé",
        "document.uploaded": "Document déposé",
        "document.validated": "Document validé",
        "external_contact.added": "Contact externe ajouté",
        "external_contact.removed": "Contact externe retiré",
        "external_contact.updated": "Contact externe modifié",
        "family_member.added": "Personne ajoutée au dossier",
        "family_member.removed": "Personne retirée du dossier",
        "family_member.updated": "Personne du dossier modifiée",
        "note.added": "Note ajoutée",
        "note.removed": "Note supprimée",
        "note.updated": "Note modifiée",
        "person.added": "Personne ajoutée",
        "person.removed": "Personne retirée",
        "person.updated": "Personne modifiée",
        "profile.updated": "Fiche client mise à jour",
        "reminder.approved": "Rappel approuvé",
        "reminder.cancelled": "Rappel annulé",
        "reminder.created": "Rappel créé",
        "reminder.edited": "Rappel modifié",
        "reminder.sent": "Rappel envoyé",
        "step.added": "Étape ajoutée",
        "step.completed": "Étape terminée",
        "step.deadline_changed": "Échéance de l'étape modifiée",
        "step.reopened": "Étape rouverte",
        "step.requirement_added": "Exigence ajoutée à l'étape",
        "step.responsible_changed": "Responsable d'étape modifié",
        "step.started": "Étape démarrée",
        "step.validator_changed": "Valideur de l'étape modifié",
        "case.company_changed": "Société liée modifiée",
        "case.invitation_resent": "Invitation renvoyée au client",
        "case.invitation_resent_by_client": "Nouveau lien d'invitation demandé par le client",
        "profile.field_promoted": "Donnée reportée sur la fiche client",
        "profile.field_pulled": "Donnée reprise de la fiche client",
        "profile.merged": "Fiches client fusionnées",
        "reminder.auto_created": "Rappel créé automatiquement",
        "reminder.escalated": "Rappel transmis au responsable du dossier",
        "signature.request_sent": "Demande de signature envoyée",
        "signature.request_completed": "Demande de signature finalisée",
        "signature.request_cancelled": "Demande de signature annulée",
        "signature.request_expired": "Demande de signature expirée",
        "signature.request_archived": "Demande de signature archivée",
    },
    "en": {
        "case.created": "Case created",
        "case.deleted": "Case deleted",
        "case.invitation_sent": "Invitation sent to the client",
        "case.journey_assigned": "Journey assigned",
        "case.member_invited": "Person invited to the case",
        "case.owner_changed": "Owner changed",
        "case.status_changed": "Case status changed",
        "case.updated": "Case updated",
        "cost.added": "Cost added",
        "cost.deleted": "Cost deleted",
        "cost.edited": "Cost edited",
        "document.deleted": "Document deleted",
        "document.uploaded": "Document uploaded",
        "document.validated": "Document validated",
        "external_contact.added": "External contact added",
        "external_contact.removed": "External contact removed",
        "external_contact.updated": "External contact updated",
        "family_member.added": "Person added to the case",
        "family_member.removed": "Person removed from the case",
        "family_member.updated": "Case person updated",
        "note.added": "Note added",
        "note.removed": "Note removed",
        "note.updated": "Note updated",
        "person.added": "Person added",
        "person.removed": "Person removed",
        "person.updated": "Person updated",
        "profile.updated": "Client sheet updated",
        "reminder.approved": "Reminder approved",
        "reminder.cancelled": "Reminder cancelled",
        "reminder.created": "Reminder created",
        "reminder.edited": "Reminder edited",
        "reminder.sent": "Reminder sent",
        "step.added": "Step added",
        "step.completed": "Step completed",
        "step.deadline_changed": "Step deadline changed",
        "step.reopened": "Step reopened",
        "step.requirement_added": "Requirement added to the step",
        "step.responsible_changed": "Step assignee changed",
        "step.started": "Step started",
        "step.validator_changed": "Step validator changed",
        "case.company_changed": "Linked company changed",
        "case.invitation_resent": "Invitation resent to the client",
        "case.invitation_resent_by_client": "New invitation link requested by the client",
        "profile.field_promoted": "Value copied to the client sheet",
        "profile.field_pulled": "Value taken from the client sheet",
        "profile.merged": "Client sheets merged",
        "reminder.auto_created": "Reminder created automatically",
        "reminder.escalated": "Reminder forwarded to the case owner",
        "signature.request_sent": "Signature request sent",
        "signature.request_completed": "Signature request completed",
        "signature.request_cancelled": "Signature request cancelled",
        "signature.request_expired": "Signature request expired",
        "signature.request_archived": "Signature request archived",
    },
    "es": {
        "case.created": "Expediente creado",
        "case.deleted": "Expediente eliminado",
        "case.invitation_sent": "Invitación enviada al cliente",
        "case.journey_assigned": "Trayecto asignado",
        "case.member_invited": "Persona invitada al expediente",
        "case.owner_changed": "Responsable modificado",
        "case.status_changed": "Estado del expediente modificado",
        "case.updated": "Expediente modificado",
        "cost.added": "Coste añadido",
        "cost.deleted": "Coste eliminado",
        "cost.edited": "Coste modificado",
        "document.deleted": "Documento eliminado",
        "document.uploaded": "Documento subido",
        "document.validated": "Documento validado",
        "external_contact.added": "Contacto externo añadido",
        "external_contact.removed": "Contacto externo retirado",
        "external_contact.updated": "Contacto externo modificado",
        "family_member.added": "Persona añadida al expediente",
        "family_member.removed": "Persona retirada del expediente",
        "family_member.updated": "Persona del expediente modificada",
        "note.added": "Nota añadida",
        "note.removed": "Nota eliminada",
        "note.updated": "Nota modificada",
        "person.added": "Persona añadida",
        "person.removed": "Persona retirada",
        "person.updated": "Persona modificada",
        "profile.updated": "Ficha de cliente actualizada",
        "reminder.approved": "Recordatorio aprobado",
        "reminder.cancelled": "Recordatorio cancelado",
        "reminder.created": "Recordatorio creado",
        "reminder.edited": "Recordatorio modificado",
        "reminder.sent": "Recordatorio enviado",
        "step.added": "Etapa añadida",
        "step.completed": "Etapa completada",
        "step.deadline_changed": "Plazo de la etapa modificado",
        "step.reopened": "Etapa reabierta",
        "step.requirement_added": "Requisito añadido a la etapa",
        "step.responsible_changed": "Responsable de etapa modificado",
        "step.started": "Etapa iniciada",
        "step.validator_changed": "Validador de la etapa modificado",
        "case.company_changed": "Empresa vinculada modificada",
        "case.invitation_resent": "Invitación reenviada al cliente",
        "case.invitation_resent_by_client": "Nuevo enlace de invitación solicitado por el cliente",
        "profile.field_promoted": "Dato trasladado a la ficha de cliente",
        "profile.field_pulled": "Dato recuperado de la ficha de cliente",
        "profile.merged": "Fichas de cliente fusionadas",
        "reminder.auto_created": "Recordatorio creado automáticamente",
        "reminder.escalated": "Recordatorio remitido al responsable del expediente",
        "signature.request_sent": "Solicitud de firma enviada",
        "signature.request_completed": "Solicitud de firma completada",
        "signature.request_cancelled": "Solicitud de firma cancelada",
        "signature.request_expired": "Solicitud de firma caducada",
        "signature.request_archived": "Solicitud de firma archivada",
    },
    "ru": {
        "case.created": "Дело создано",
        "case.deleted": "Дело удалено",
        "case.invitation_sent": "Приглашение отправлено клиенту",
        "case.journey_assigned": "Маршрут назначен",
        "case.member_invited": "Человек приглашён в досье",
        "case.owner_changed": "Ответственный изменён",
        "case.status_changed": "Статус дела изменён",
        "case.updated": "Дело изменено",
        "cost.added": "Расход добавлен",
        "cost.deleted": "Расход удалён",
        "cost.edited": "Расход изменён",
        "document.deleted": "Документ удалён",
        "document.uploaded": "Документ загружен",
        "document.validated": "Документ подтверждён",
        "external_contact.added": "Внешний контакт добавлен",
        "external_contact.removed": "Внешний контакт удалён",
        "external_contact.updated": "Внешний контакт изменён",
        "family_member.added": "Человек добавлен в досье",
        "family_member.removed": "Человек убран из досье",
        "family_member.updated": "Участник досье изменён",
        "note.added": "Заметка добавлена",
        "note.removed": "Заметка удалена",
        "note.updated": "Заметка изменена",
        "person.added": "Человек добавлен",
        "person.removed": "Человек удалён",
        "person.updated": "Данные человека изменены",
        "profile.updated": "Карточка клиента обновлена",
        "reminder.approved": "Напоминание утверждено",
        "reminder.cancelled": "Напоминание отменено",
        "reminder.created": "Напоминание создано",
        "reminder.edited": "Напоминание изменено",
        "reminder.sent": "Напоминание отправлено",
        "step.added": "Этап добавлен",
        "step.completed": "Этап завершён",
        "step.deadline_changed": "Срок этапа изменён",
        "step.reopened": "Этап вновь открыт",
        "step.requirement_added": "Требование добавлено к этапу",
        "step.responsible_changed": "Ответственный за этап изменён",
        "step.started": "Этап начат",
        "step.validator_changed": "Валидатор этапа изменён",
        "case.company_changed": "Связанная компания изменена",
        "case.invitation_resent": "Приглашение повторно отправлено клиенту",
        "case.invitation_resent_by_client": "Клиент запросил новую ссылку-приглашение",
        "profile.field_promoted": "Данные перенесены в карточку клиента",
        "profile.field_pulled": "Данные взяты из карточки клиента",
        "profile.merged": "Карточки клиента объединены",
        "reminder.auto_created": "Напоминание создано автоматически",
        "reminder.escalated": "Напоминание передано ответственному по делу",
        "signature.request_sent": "Запрос на подпись отправлен",
        "signature.request_completed": "Запрос на подпись выполнен",
        "signature.request_cancelled": "Запрос на подпись отменён",
        "signature.request_expired": "Срок запроса на подпись истёк",
        "signature.request_archived": "Запрос на подпись архивирован",
    },
    "pt": {
        "case.created": "Processo criado",
        "case.deleted": "Processo eliminado",
        "case.invitation_sent": "Convite enviado ao cliente",
        "case.journey_assigned": "Percurso atribuído",
        "case.member_invited": "Pessoa convidada para o dossiê",
        "case.owner_changed": "Responsável alterado",
        "case.status_changed": "Estado do processo alterado",
        "case.updated": "Processo modificado",
        "cost.added": "Custo adicionado",
        "cost.deleted": "Custo eliminado",
        "cost.edited": "Custo alterado",
        "document.deleted": "Documento eliminado",
        "document.uploaded": "Documento carregado",
        "document.validated": "Documento validado",
        "external_contact.added": "Contacto externo adicionado",
        "external_contact.removed": "Contacto externo removido",
        "external_contact.updated": "Contacto externo modificado",
        "family_member.added": "Pessoa adicionada ao dossiê",
        "family_member.removed": "Pessoa removida do dossiê",
        "family_member.updated": "Pessoa do dossiê alterada",
        "note.added": "Nota adicionada",
        "note.removed": "Nota eliminada",
        "note.updated": "Nota modificada",
        "person.added": "Pessoa adicionada",
        "person.removed": "Pessoa removida",
        "person.updated": "Pessoa alterada",
        "profile.updated": "Ficha do cliente atualizada",
        "reminder.approved": "Lembrete aprovado",
        "reminder.cancelled": "Lembrete cancelado",
        "reminder.created": "Lembrete criado",
        "reminder.edited": "Lembrete modificado",
        "reminder.sent": "Lembrete enviado",
        "step.added": "Etapa adicionada",
        "step.completed": "Etapa concluída",
        "step.deadline_changed": "Prazo da etapa alterado",
        "step.reopened": "Etapa reaberta",
        "step.requirement_added": "Requisito adicionado à etapa",
        "step.responsible_changed": "Responsável da etapa alterado",
        "step.started": "Etapa iniciada",
        "step.validator_changed": "Validador da etapa alterado",
        "case.company_changed": "Empresa associada alterada",
        "case.invitation_resent": "Convite reenviado ao cliente",
        "case.invitation_resent_by_client": "Novo link de convite pedido pelo cliente",
        "profile.field_promoted": "Dado copiado para a ficha do cliente",
        "profile.field_pulled": "Dado retomado da ficha do cliente",
        "profile.merged": "Fichas do cliente fundidas",
        "reminder.auto_created": "Lembrete criado automaticamente",
        "reminder.escalated": "Lembrete encaminhado ao responsável do processo",
        "signature.request_sent": "Pedido de assinatura enviado",
        "signature.request_completed": "Pedido de assinatura concluído",
        "signature.request_cancelled": "Pedido de assinatura cancelado",
        "signature.request_expired": "Pedido de assinatura expirado",
        "signature.request_archived": "Pedido de assinatura arquivado",
    },
    "it": {
        "case.created": "Pratica creata",
        "case.deleted": "Pratica eliminata",
        "case.invitation_sent": "Invito inviato al cliente",
        "case.journey_assigned": "Percorso assegnato",
        "case.member_invited": "Persona invitata al dossier",
        "case.owner_changed": "Owner modificato",
        "case.status_changed": "Stato della pratica modificato",
        "case.updated": "Pratica modificata",
        "cost.added": "Costo aggiunto",
        "cost.deleted": "Costo eliminato",
        "cost.edited": "Costo modificato",
        "document.deleted": "Documento eliminato",
        "document.uploaded": "Documento caricato",
        "document.validated": "Documento validato",
        "external_contact.added": "Contatto esterno aggiunto",
        "external_contact.removed": "Contatto esterno rimosso",
        "external_contact.updated": "Contatto esterno modificato",
        "family_member.added": "Persona aggiunta al dossier",
        "family_member.removed": "Persona rimossa dal dossier",
        "family_member.updated": "Persona del dossier modificata",
        "note.added": "Nota aggiunta",
        "note.removed": "Nota eliminata",
        "note.updated": "Nota modificata",
        "person.added": "Persona aggiunta",
        "person.removed": "Persona rimossa",
        "person.updated": "Persona modificata",
        "profile.updated": "Scheda cliente aggiornata",
        "reminder.approved": "Promemoria approvato",
        "reminder.cancelled": "Promemoria annullato",
        "reminder.created": "Promemoria creato",
        "reminder.edited": "Promemoria modificato",
        "reminder.sent": "Promemoria inviato",
        "step.added": "Fase aggiunta",
        "step.completed": "Fase completata",
        "step.deadline_changed": "Scadenza della fase modificata",
        "step.reopened": "Fase riaperta",
        "step.requirement_added": "Requisito aggiunto alla fase",
        "step.responsible_changed": "Responsabile della fase modificato",
        "step.started": "Fase avviata",
        "step.validator_changed": "Validatore della fase modificato",
        "case.company_changed": "Società collegata modificata",
        "case.invitation_resent": "Invito inviato di nuovo al cliente",
        "case.invitation_resent_by_client": "Nuovo link di invito richiesto dal cliente",
        "profile.field_promoted": "Dato riportato nella scheda cliente",
        "profile.field_pulled": "Dato ripreso dalla scheda cliente",
        "profile.merged": "Schede cliente unite",
        "reminder.auto_created": "Promemoria creato automaticamente",
        "reminder.escalated": "Promemoria inoltrato al responsabile della pratica",
        "signature.request_sent": "Richiesta di firma inviata",
        "signature.request_completed": "Richiesta di firma completata",
        "signature.request_cancelled": "Richiesta di firma annullata",
        "signature.request_expired": "Richiesta di firma scaduta",
        "signature.request_archived": "Richiesta di firma archiviata",
    },
    "hu": {
        "case.created": "Ügy létrehozva",
        "case.deleted": "Ügy törölve",
        "case.invitation_sent": "Meghívó elküldve az ügyfélnek",
        "case.journey_assigned": "Folyamat hozzárendelve",
        "case.member_invited": "Személy meghívva az ügyhöz",
        "case.owner_changed": "Ügygazda módosítva",
        "case.status_changed": "Az ügy státusza módosítva",
        "case.updated": "Ügy módosítva",
        "cost.added": "Költség hozzáadva",
        "cost.deleted": "Költség törölve",
        "cost.edited": "Költség módosítva",
        "document.deleted": "Dokumentum törölve",
        "document.uploaded": "Dokumentum feltöltve",
        "document.validated": "Dokumentum jóváhagyva",
        "external_contact.added": "Külső kapcsolat hozzáadva",
        "external_contact.removed": "Külső kapcsolat eltávolítva",
        "external_contact.updated": "Külső kapcsolat módosítva",
        "family_member.added": "Személy hozzáadva az ügyhöz",
        "family_member.removed": "Személy eltávolítva az ügyből",
        "family_member.updated": "Az ügyben szereplő személy módosítva",
        "note.added": "Jegyzet hozzáadva",
        "note.removed": "Jegyzet törölve",
        "note.updated": "Jegyzet módosítva",
        "person.added": "Személy hozzáadva",
        "person.removed": "Személy eltávolítva",
        "person.updated": "Személy módosítva",
        "profile.updated": "Ügyfélkarton frissítve",
        "reminder.approved": "Emlékeztető jóváhagyva",
        "reminder.cancelled": "Emlékeztető visszavonva",
        "reminder.created": "Emlékeztető létrehozva",
        "reminder.edited": "Emlékeztető módosítva",
        "reminder.sent": "Emlékeztető elküldve",
        "step.added": "Lépés hozzáadva",
        "step.completed": "Lépés befejezve",
        "step.deadline_changed": "A lépés határideje módosítva",
        "step.reopened": "Lépés újranyitva",
        "step.requirement_added": "Követelmény hozzáadva a lépéshez",
        "step.responsible_changed": "A lépés felelőse módosítva",
        "step.started": "Lépés elindítva",
        "step.validator_changed": "A lépés jóváhagyója módosítva",
        "case.company_changed": "Kapcsolódó cég módosítva",
        "case.invitation_resent": "Meghívó újraküldve az ügyfélnek",
        "case.invitation_resent_by_client": "Az ügyfél új meghívó linket kért",
        "profile.field_promoted": "Adat átvezetve az ügyfélkartonra",
        "profile.field_pulled": "Adat átvéve az ügyfélkartonról",
        "profile.merged": "Ügyfélkartonok összevonva",
        "reminder.auto_created": "Emlékeztető automatikusan létrehozva",
        "reminder.escalated": "Emlékeztető továbbítva az ügy gazdájának",
        "signature.request_sent": "Aláírási kérelem elküldve",
        "signature.request_completed": "Aláírási kérelem teljesítve",
        "signature.request_cancelled": "Aláírási kérelem visszavonva",
        "signature.request_expired": "Aláírási kérelem lejárt",
        "signature.request_archived": "Aláírási kérelem archiválva",
    },
}

# « Label : value » — French typography puts a (non-breaking) space before
# the colon; the other six languages do not.
_COLON: dict[str, str] = {"fr": "\u00a0: "}
# Between the day and the time of an instant. Hungarian dates end with a
# period (« 2026. szeptember 30. »): a comma after it would be wrong.
_DATETIME_SEP: dict[str, str] = {"hu": " "}

for _name, _table, _keys in (
    ("_LABELS", _LABELS, set(_LABELS[DEFAULT_LANG])),
    ("_STATUS", _STATUS, {s.value for s in CaseStatus}),
    ("_SEX", _SEX, {s.value for s in Sex}),
    ("_MARITAL", _MARITAL, {s.value for s in MaritalStatus}),
    ("_RELATIONSHIP", _RELATIONSHIP, set(_RELATIONSHIP[DEFAULT_LANG])),
    ("_ACTIVITY", _ACTIVITY, set(_ACTIVITY[DEFAULT_LANG])),
):
    assert set(_table) == set(SUPPORTED_LANGUAGES), f"{_name} and SUPPORTED_LANGUAGES drifted"
    assert all(set(v) == _keys for v in _table.values()), f"{_name}: a language misses a key"
assert {f"actor_{a.value}" for a in ActorType} <= set(_LABELS[DEFAULT_LANG]), (
    "an ActorType has no actor_* label"
)


# --- value rendering -----------------------------------------------------------------------


def _humanize(key: str) -> str:
    """« case.archived » → « Case archived » — the last resort for a code
    no catalog knows (a future backend value), so it never prints raw."""
    words = key.replace(".", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else key


def _colon(lang: str) -> str:
    return _COLON.get(lang, ": ")


def _utc(value: datetime) -> datetime:
    # A naive datetime is a UTC one here (timestamptz columns read back
    # aware; only hand-built objects are naive).
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _instant(value: datetime, lang: str) -> str:
    """« 30 septembre 2026, 14:05 UTC » — spelled-out day, 24h time, and the
    timezone named, since there is no agency timezone to convert to."""
    moment = _utc(value)
    day = format_date_for_lang(moment.date(), lang)
    return f"{day}{_DATETIME_SEP.get(lang, ', ')}{moment:%H:%M} UTC"


def _full_name(first: str | None, last: str | None) -> str:
    return " ".join(p for p in (first, last) if p) or _DASH


def _country(value: str | None, lang: str) -> str:
    return country_name(value, lang) or _DASH


def _address(street: str | None, postal: str | None, city: str | None) -> str:
    parts = [p for p in (street, " ".join(x for x in (postal, city) if x)) if p]
    return ", ".join(parts) if parts else _DASH


def _relationship(value: str | None, lang: str) -> str | None:
    if not value:
        return None
    return _RELATIONSHIP[lang].get(value.strip().lower(), value)


def _custom_value(definition: CustomFieldDefinition, value: Any, lang: str) -> str:
    """A stored custom-field value as a reader reads it: yes/no, a spelled
    date, a country name, a joined address — never `True`, an ISO date, a
    country code or a dict repr."""
    ftype = definition.field_type
    labels = _LABELS[lang]
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if ftype == CustomFieldType.BOOLEAN.value and isinstance(value, bool):
        return labels["yes"] if value else labels["no"]
    if ftype == CustomFieldType.DATE.value and isinstance(value, str):
        try:
            return format_date_for_lang(date.fromisoformat(value), lang)
        except ValueError:
            return value
    if ftype == CustomFieldType.COUNTRY.value and isinstance(value, str):
        return _country(value, lang)
    if ftype == CustomFieldType.ADDRESS.value and isinstance(value, dict):
        parts = [
            _address(value.get("street"), value.get("postal_code"), value.get("city")),
            country_name(value.get("country"), lang),
        ]
        return ", ".join(p for p in parts if p and p != _DASH) or _DASH
    return str(value)


def _custom_lines(
    person: CasePerson,
    definitions: list[CustomFieldDefinition],
    lang: str,
    agency_default: str,
) -> list[str]:
    """Agency custom fields (label: value) — only active definitions with
    a saved value. The LABEL is resolved for `lang` (BLOC 2); the stored
    value is keyed by the untranslated `key`."""
    stored = person.custom_fields or {}
    out: list[str] = []
    for definition in definitions:
        if definition.key not in stored:
            continue
        rendered = _custom_value(definition, stored[definition.key], lang)
        label = resolve_i18n(definition.label_i18n, lang, agency_default, definition.label)
        out.append(f"{label}{_colon(lang)}{rendered}")
    return out


def _civil_lines(
    person: CasePerson,
    definitions: list[CustomFieldDefinition],
    lang: str,
    agency_default: str,
) -> list[str]:
    """Civil-status + custom-field lines for one person — only filled."""
    labels = _LABELS[lang]
    fields = [
        ("passport_number", person.passport_number),
        (
            "date_of_birth",
            format_date_for_lang(person.date_of_birth, lang) if person.date_of_birth else None,
        ),
        ("nationality", country_name(person.nationality, lang)),
        ("place_of_birth", person.place_of_birth),
        ("sex", _SEX[lang].get(person.sex, person.sex) if person.sex else None),
        (
            "marital_status",
            _MARITAL[lang].get(person.marital_status, _humanize(person.marital_status))
            if person.marital_status
            else None,
        ),
        ("phone", person.phone),
    ]
    filled = [f"{labels[key]}{_colon(lang)}{value}" for key, value in fields if value]
    return filled + _custom_lines(person, definitions, lang, agency_default)


def _activity_line(row: ActivityLog, lang: str) -> str:
    """One journal entry: the event label (+ the status transition when it
    is one) and who did it. An unknown event keeps a readable trace."""
    labels = _LABELS[lang]
    label = _ACTIVITY[lang].get(row.action_type)
    if label is None:
        label = f"{labels['other_event']} ({_humanize(row.action_type).lower()})"
    details = row.details or {}
    if row.action_type == "case.status_changed":
        old, new = details.get("old"), details.get("new")
        if isinstance(old, str) and isinstance(new, str):
            statuses = _STATUS[lang]
            transition = (
                f"{statuses.get(old, _humanize(old))} → {statuses.get(new, _humanize(new))}"
            )
            label = f"{label}{_colon(lang)}{transition}"
    actor = labels.get(f"actor_{row.actor_type}")
    return f"{label} · {actor}" if actor else label


# --- layout --------------------------------------------------------------------------------


def _new_pdf(lang: str) -> FPDF:
    pdf = FPDF()
    pdf.add_font(_FONT, "", str(_FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font(_FONT, "B", str(_FONT_DIR / "DejaVuSans-Bold.ttf"))
    pdf.set_lang(lang)
    pdf.set_creator("Nidria")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    return pdf


def _write(pdf: FPDF, text: str, height: float, *, indent: float = 0) -> None:
    """A wrapped, left-aligned paragraph — a long address, a Hungarian label
    or a family name never runs off the page."""
    pdf.set_x(pdf.l_margin + indent)
    pdf.multi_cell(0, height, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _heading(pdf: FPDF, text: str) -> None:
    pdf.ln(4)
    pdf.set_font(_FONT, "B", 13)
    _write(pdf, text, 9)
    pdf.ln(1)


def build_case_pdf(
    *,
    case: ClientCase,
    principal: ExpatUser,
    owner: Agent | None,
    persons: list[CasePerson],
    custom_field_definitions: list[CustomFieldDefinition],
    activity_rows: list[ActivityLog],
    lang: str = DEFAULT_LANG,
    agency_default: str = DEFAULT_LANG,
    exported_at: datetime | None = None,
) -> bytes:
    lang = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANG
    labels = _LABELS[lang]
    colon = _colon(lang)
    client = _full_name(principal.first_name, principal.last_name)
    title = f"{labels['title']} — {client}"

    pdf = _new_pdf(lang)
    pdf.set_title(title)

    pdf.set_font(_FONT, "B", 16)
    _write(pdf, title, 9)
    pdf.set_font(_FONT, "", 9)
    pdf.set_text_color(_MUTED)
    _write(pdf, f"{labels['exported']} {_instant(exported_at or datetime.now(UTC), lang)}", 5)
    pdf.set_text_color(0)
    pdf.ln(4)

    owner_name = _full_name(owner.first_name, owner.last_name) if owner else _DASH
    info = [
        ("client", f"{client} ({principal.email})"),
        ("origin_country", _country(case.origin_country, lang)),
        ("dest_country", _country(case.dest_country, lang)),
        ("origin_address", _address(case.origin_street, case.origin_postal_code, case.origin_city)),
        ("dest_address", _address(case.dest_street, case.dest_postal_code, case.dest_city)),
        ("status", _STATUS[lang].get(case.status, _humanize(case.status))),
        ("owner", owner_name),
        ("tags", ", ".join(case.tags) if case.tags else _DASH),
        ("source", case.source or _DASH),
        ("created", format_date_for_lang(_utc(case.created_at).date(), lang)),
    ]
    pdf.set_font(_FONT, "", 11)
    for key, value in info:
        _write(pdf, f"{labels[key]}{colon}{value}", 7)

    # People + civil status.
    _heading(pdf, labels["people"])
    for person in persons:
        if person.kind == CasePersonKind.PRINCIPAL.value:
            name = f"{labels['principal']} — {client}"
        else:
            rel = _relationship(person.relationship, lang)
            name = f"{person.full_name or _DASH}{f' ({rel})' if rel else ''}"
        pdf.set_font(_FONT, "B", 10)
        _write(pdf, name, 6)
        pdf.set_font(_FONT, "", 10)
        lines = _civil_lines(person, custom_field_definitions, lang, agency_default)
        for line in lines or [labels["no_details"]]:
            _write(pdf, f"• {line}", 6, indent=4)
        pdf.ln(1)

    # Activity journal — the date column is as wide as its longest stamp.
    _heading(pdf, labels["activity"])
    pdf.set_font(_FONT, "", 10)
    if not activity_rows:
        _write(pdf, labels["no_activity"], 6)
    stamps = [_instant(row.created_at, lang) for row in activity_rows]
    stamp_width = max((pdf.get_string_width(s) for s in stamps), default=0) + 4
    for row, stamp in zip(activity_rows, stamps, strict=True):
        pdf.set_text_color(_MUTED)
        pdf.cell(stamp_width, 6, stamp)
        pdf.set_text_color(0)
        pdf.multi_cell(
            0, 6, _activity_line(row, lang), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )

    return bytes(pdf.output())

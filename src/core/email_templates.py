"""Transactional email templates — French, one builder per email.

Each builder returns an EmailContent(subject, text, html): HTML for the
clients that render it, plain text as the multipart fallback. The HTML
is email-safe (tables + inline styles, max 600px, no external CSS, no
JS, no hosted images — the header is styled text). Links are ALWAYS
built by the callers on settings.frontend_url, never hardcoded here.
"""

import html as html_lib
from dataclasses import dataclass

# Per-language chrome strings (BLOC NOTIF-2). The body strings live in each
# builder's own {fr,en,es} catalog; these are the shared layout bits.
_FOOTER = {
    "fr": (
        "Cet email a été envoyé par Nidria. "
        "Si vous n'êtes pas à l'origine de cette demande, ignorez-le."
    ),
    "en": (
        "This email was sent by Nidria. If you did not initiate this request, please ignore it."
    ),
    "es": ("Este correo fue enviado por Nidria. Si no originó esta solicitud, ignórelo."),
    "ru": (
        "Это письмо отправлено Nidria. Если вы не инициировали этот запрос, проигнорируйте его."
    ),
    "pt": ("Este email foi enviado pela Nidria. Se não originou este pedido, ignore-o."),
    "it": (
        "Questa email è stata inviata da Nidria. Se non hai effettuato questa richiesta, ignorala."
    ),
}

_COPY_PASTE = {
    "fr": "Ou copiez-collez ce lien",
    "en": "Or copy and paste this link",
    "es": "O copie y pegue este enlace",
    "ru": "Или скопируйте и вставьте эту ссылку",
    "pt": "Ou copie e cole este link",
    "it": "Oppure copia e incolla questo link",
}

_HTML_LAYOUT = """\
<!DOCTYPE html>
<html lang="{lang}">
  <body style="margin:0;padding:0;background-color:#f4f5f7;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="background-color:#f4f5f7;padding:24px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="600" cellpadding="0" cellspacing="0" \
style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;\
font-family:Arial,Helvetica,sans-serif;">
            <tr>
              <td style="padding:24px 32px;border-bottom:1px solid #e8e8ec;">
                <span style="font-size:20px;font-weight:bold;color:#1a1a2e;\
letter-spacing:1px;">Nidria</span>
              </td>
            </tr>
            <tr>
              <td style="padding:32px;">
                <h1 style="margin:0 0 16px;font-size:20px;color:#1a1a2e;">{title}</h1>
                <p style="margin:0 0 24px;font-size:14px;line-height:1.6;\
color:#3c3c46;">{intro}</p>
{action_blocks}
              </td>
            </tr>
            <tr>
              <td style="padding:20px 32px;border-top:1px solid #e8e8ec;">
                <p style="margin:0;font-size:12px;color:#8a8a94;line-height:1.5;">{footer}</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""

_HTML_BUTTON = """\
                <table role="presentation" cellpadding="0" cellspacing="0" \
style="margin:0 auto 24px;">
                  <tr>
                    <td style="background-color:#1a1a2e;border-radius:6px;" align="center">
                      <a href="{url}" style="display:inline-block;padding:12px 28px;\
font-size:14px;font-weight:bold;color:#ffffff;text-decoration:none;">{label}</a>
                    </td>
                  </tr>
                </table>
                <p style="margin:0 0 8px;font-size:12px;line-height:1.6;color:#8a8a94;\
word-break:break-all;">{copy_paste}&nbsp;: \
<a href="{url}" style="color:#3b3bd6;">{url}</a></p>
"""

_HTML_VALIDITY = """\
                <p style="margin:0;font-size:12px;line-height:1.6;\
color:#8a8a94;">{validity}</p>
"""

_HTML_BODY_TEXT = """\
                <p style="margin:0 0 24px;font-size:14px;line-height:1.6;\
color:#3c3c46;">{body}</p>
"""


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


def _render(
    *,
    subject: str,
    title: str,
    intro: str,
    button_label: str | None = None,
    button_url: str | None = None,
    body_text: str | None = None,
    validity: str | None = None,
    lang: str = "fr",
) -> EmailContent:
    footer = _FOOTER.get(lang, _FOOTER["fr"])
    action_blocks = ""
    if body_text is not None:
        escaped = html_lib.escape(body_text).replace("\n", "<br>")
        action_blocks += _HTML_BODY_TEXT.format(body=escaped)
    if button_label is not None and button_url is not None:
        action_blocks += _HTML_BUTTON.format(
            label=html_lib.escape(button_label),
            url=html_lib.escape(button_url, quote=True),
            copy_paste=_COPY_PASTE.get(lang, _COPY_PASTE["fr"]),
        )
    if validity is not None:
        action_blocks += _HTML_VALIDITY.format(validity=html_lib.escape(validity))

    html = _HTML_LAYOUT.format(
        lang=lang,
        title=html_lib.escape(title),
        intro=html_lib.escape(intro),
        action_blocks=action_blocks,
        footer=html_lib.escape(footer),
    )

    text_parts = ["Nidria", "", title, "", intro]
    if body_text is not None:
        text_parts += ["", body_text]
    if button_label is not None and button_url is not None:
        text_parts += ["", f"{button_label} : {button_url}"]
    if validity is not None:
        text_parts += ["", validity]
    text_parts += ["", "--", footer]
    return EmailContent(subject=subject, text="\n".join(text_parts), html=html)


# Agent-facing templates — rendered in the AGENT's language (agency default,
# resolved by resolve_notification_lang_agent). Historically FR-only: an
# Italian agent received French. Now six languages, strict parity.
_PASSWORD_RESET = {
    "fr": {
        "subject": "Nidria : Réinitialisez votre mot de passe",
        "title": "Réinitialisez votre mot de passe",
        "intro": (
            "Une demande de réinitialisation de mot de passe a été faite pour votre compte Nidria."
        ),
        "button": "Choisir un nouveau mot de passe",
        "hours": "Ce lien expire dans {n} heures.",
        "minutes": "Ce lien expire dans {n} minutes.",
    },
    "en": {
        "subject": "Nidria: Reset your password",
        "title": "Reset your password",
        "intro": "A password reset was requested for your Nidria account.",
        "button": "Choose a new password",
        "hours": "This link expires in {n} hours.",
        "minutes": "This link expires in {n} minutes.",
    },
    "es": {
        "subject": "Nidria: Restablezca su contraseña",
        "title": "Restablezca su contraseña",
        "intro": "Se solicitó un restablecimiento de contraseña para su cuenta de Nidria.",
        "button": "Elegir una nueva contraseña",
        "hours": "Este enlace caduca en {n} horas.",
        "minutes": "Este enlace caduca en {n} minutos.",
    },
    "ru": {
        "subject": "Nidria: Сбросьте пароль",
        "title": "Сбросьте пароль",
        "intro": "Для вашей учётной записи Nidria запрошен сброс пароля.",
        "button": "Выбрать новый пароль",
        "hours": "Эта ссылка действительна {n} часов.",
        "minutes": "Эта ссылка действительна {n} минут.",
    },
    "pt": {
        "subject": "Nidria: Redefina a sua palavra-passe",
        "title": "Redefina a sua palavra-passe",
        "intro": "Foi solicitada uma redefinição de palavra-passe para a sua conta Nidria.",
        "button": "Escolher uma nova palavra-passe",
        "hours": "Este link expira em {n} horas.",
        "minutes": "Este link expira em {n} minutos.",
    },
    "it": {
        "subject": "Nidria: Reimposta la password",
        "title": "Reimposta la password",
        "intro": "È stata richiesta una reimpostazione della password per il tuo account Nidria.",
        "button": "Scegliere una nuova password",
        "hours": "Questo link scade tra {n} ore.",
        "minutes": "Questo link scade tra {n} minuti.",
    },
}

_AGENT_INVITATION = {
    "fr": {
        "subject": "Nidria : Vous êtes invité(e) à rejoindre {agency}",
        "title": "Vous êtes invité(e) à rejoindre {agency} sur Nidria",
        # ICP multi-métier: never "expatriation".
        "intro": (
            "{agency} vous invite à rejoindre son espace de travail sur Nidria "
            "pour gérer ses dossiers."
        ),
        "button": "Accepter l'invitation",
        "expires": "Ce lien expire dans {days} jours.",
    },
    "en": {
        "subject": "Nidria: You are invited to join {agency}",
        "title": "You are invited to join {agency} on Nidria",
        "intro": ("{agency} invites you to join its workspace on Nidria to manage its cases."),
        "button": "Accept the invitation",
        "expires": "This link expires in {days} days.",
    },
    "es": {
        "subject": "Nidria: Le invitan a unirse a {agency}",
        "title": "Le invitan a unirse a {agency} en Nidria",
        "intro": (
            "{agency} le invita a unirse a su espacio de trabajo en Nidria para "
            "gestionar sus expedientes."
        ),
        "button": "Aceptar la invitación",
        "expires": "Este enlace caduca en {days} días.",
    },
    "ru": {
        "subject": "Nidria: Вас приглашают присоединиться к {agency}",
        "title": "Вас приглашают присоединиться к {agency} в Nidria",
        "intro": (
            "{agency} приглашает вас присоединиться к своему рабочему "
            "пространству в Nidria для управления делами."
        ),
        "button": "Принять приглашение",
        "expires": "Эта ссылка действительна {days} дней.",
    },
    "pt": {
        "subject": "Nidria: Foi convidado(a) para se juntar a {agency}",
        "title": "Foi convidado(a) para se juntar a {agency} na Nidria",
        "intro": (
            "{agency} convida-o(a) a juntar-se ao seu espaço de trabalho na "
            "Nidria para gerir os seus processos."
        ),
        "button": "Aceitar o convite",
        "expires": "Este link expira em {days} dias.",
    },
    "it": {
        "subject": "Nidria: Sei invitato(a) a unirti a {agency}",
        "title": "Sei invitato(a) a unirti a {agency} su Nidria",
        "intro": (
            "{agency} ti invita a unirti al suo spazio di lavoro su Nidria per "
            "gestire le sue pratiche."
        ),
        "button": "Accettare l'invito",
        "expires": "Questo link scade tra {days} giorni.",
    },
}


def password_reset_email(reset_link: str, expires_minutes: int, lang: str = "fr") -> EmailContent:
    """Rendered in the recipient language. Long invitation windows read in
    hours ("24 heures", onboarding); the classic 60-minute reset in minutes."""
    s = _pick(_PASSWORD_RESET, lang)
    if expires_minutes >= 120 and expires_minutes % 60 == 0:
        validity = s["hours"].format(n=expires_minutes // 60)
    else:
        validity = s["minutes"].format(n=expires_minutes)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"],
        button_label=s["button"],
        button_url=reset_link,
        validity=validity,
        lang=lang,
    )


def agent_invitation_email(
    agency_name: str, link: str, expires_days: int, lang: str = "fr"
) -> EmailContent:
    """Rendered in the AGENT's language (agency default)."""
    s = _pick(_AGENT_INVITATION, lang)
    return _render(
        subject=s["subject"].format(agency=agency_name),
        title=s["title"].format(agency=agency_name),
        intro=s["intro"].format(agency=agency_name),
        button_label=s["button"],
        button_url=link,
        validity=s["expires"].format(days=expires_days),
        lang=lang,
    )


# ICP multi-métier (Eric): the client invitation emails NEVER say
# "expatriation". They carry the JOURNEY NAME (resolved in the recipient
# language), or a neutral fallback ("votre dossier") when the case has no
# journey. `{dossier}` is the shared noun phrase, filled by `_dossier`.
_DOSSIER = {
    "fr": {"named": "votre dossier « {name} »", "neutral": "votre dossier"},
    "en": {"named": "your case “{name}”", "neutral": "your case"},
    "es": {"named": "su expediente «{name}»", "neutral": "su expediente"},
    "ru": {"named": "ваше дело «{name}»", "neutral": "ваше дело"},
    "pt": {"named": "o seu processo «{name}»", "neutral": "o seu processo"},
    "it": {"named": "la tua pratica «{name}»", "neutral": "la tua pratica"},
}


def _dossier(journey_name: str | None, lang: str) -> str:
    d = _pick(_DOSSIER, lang)
    return d["named"].format(name=journey_name) if journey_name else d["neutral"]


_CASE_ACTIVATION = {
    "fr": {
        "subject": "Nidria : {agency} vous a ouvert un espace de suivi",
        "title": "{agency} vous a ouvert un espace de suivi",
        "intro": (
            "{agency} vient d'ouvrir {dossier}. Activez votre espace personnel pour en suivre "
            "l'avancement, étape par étape."
        ),
        "button": "Activer mon espace",
        "expires": "Ce lien expire dans {days} jours.",
    },
    "en": {
        "subject": "Nidria: {agency} opened a tracking space for you",
        "title": "{agency} opened a tracking space for you",
        "intro": (
            "{agency} has just opened {dossier}. Activate your personal space to follow its "
            "progress, step by step."
        ),
        "button": "Activate my space",
        "expires": "This link expires in {days} days.",
    },
    "es": {
        "subject": "Nidria: {agency} le abrió un espacio de seguimiento",
        "title": "{agency} le abrió un espacio de seguimiento",
        "intro": (
            "{agency} acaba de abrir {dossier}. Active su espacio personal para seguir su avance, "
            "etapa por etapa."
        ),
        "button": "Activar mi espacio",
        "expires": "Este enlace caduca en {days} días.",
    },
    "ru": {
        "subject": "Nidria: {agency} открыло для вас пространство отслеживания",
        "title": "{agency} открыло для вас пространство отслеживания",
        "intro": (
            "{agency} только что открыло {dossier}. Активируйте личный кабинет, чтобы следить за "
            "ходом дела, шаг за шагом."
        ),
        "button": "Активировать кабинет",
        "expires": "Эта ссылка действительна {days} дней.",
    },
    "pt": {
        "subject": "Nidria: {agency} abriu-lhe um espaço de acompanhamento",
        "title": "{agency} abriu-lhe um espaço de acompanhamento",
        "intro": (
            "{agency} acaba de abrir {dossier}. Ative o seu espaço pessoal para acompanhar o seu "
            "avanço, etapa por etapa."
        ),
        "button": "Ativar o meu espaço",
        "expires": "Este link expira em {days} dias.",
    },
    "it": {
        "subject": "Nidria: {agency} ti ha aperto uno spazio di monitoraggio",
        "title": "{agency} ti ha aperto uno spazio di monitoraggio",
        "intro": (
            "{agency} ha appena aperto {dossier}. Attiva il tuo spazio personale per seguirne "
            "l'avanzamento, passo dopo passo."
        ),
        "button": "Attivare il mio spazio",
        "expires": "Questo link scade tra {days} giorni.",
    },
}

_NEW_CASE = {
    "fr": {
        "subject": "Nidria : Un nouveau dossier vous attend",
        "title": "Un nouveau dossier vous attend",
        "intro": (
            "{agency} vient d'ouvrir {dossier}. Connectez-vous à votre espace pour le consulter."
        ),
        "button": "Accéder à mon espace",
    },
    "en": {
        "subject": "Nidria: A new case is waiting for you",
        "title": "A new case is waiting for you",
        "intro": "{agency} has just opened {dossier}. Log in to your space to view it.",
        "button": "Open my space",
    },
    "es": {
        "subject": "Nidria: Un nuevo expediente le espera",
        "title": "Un nuevo expediente le espera",
        "intro": "{agency} acaba de abrir {dossier}. Inicie sesión en su espacio para consultarlo.",
        "button": "Acceder a mi espacio",
    },
    "ru": {
        "subject": "Nidria: Вас ждёт новое дело",
        "title": "Вас ждёт новое дело",
        "intro": (
            "{agency} только что открыло {dossier}. Войдите в свой кабинет, чтобы ознакомиться "
            "с ним."
        ),
        "button": "Открыть мой кабинет",
    },
    "pt": {
        "subject": "Nidria: Um novo processo aguarda-o",
        "title": "Um novo processo aguarda-o",
        "intro": "{agency} acaba de abrir {dossier}. Inicie sessão no seu espaço para o consultar.",
        "button": "Aceder ao meu espaço",
    },
    "it": {
        "subject": "Nidria: Una nuova pratica ti aspetta",
        "title": "Una nuova pratica ti aspetta",
        "intro": "{agency} ha appena aperto {dossier}. Accedi al tuo spazio per consultarla.",
        "button": "Accedere al mio spazio",
    },
}


def expat_activation_email(
    agency_name: str,
    link: str,
    expires_days: int,
    journey_name: str | None = None,
    lang: str = "fr",
) -> EmailContent:
    """Client activation invite, rendered in the recipient language. The
    intro carries the journey name (resolved) or the neutral fallback."""
    s = _pick(_CASE_ACTIVATION, lang)
    return _render(
        subject=s["subject"].format(agency=agency_name),
        title=s["title"].format(agency=agency_name),
        intro=s["intro"].format(agency=agency_name, dossier=_dossier(journey_name, lang)),
        button_label=s["button"],
        button_url=link,
        validity=s["expires"].format(days=expires_days),
        lang=lang,
    )


def new_case_email(
    agency_name: str,
    login_link: str,
    journey_name: str | None = None,
    lang: str = "fr",
) -> EmailContent:
    """ "A new case awaits you", for an already-active client, in their
    language, carrying the journey name or the neutral fallback."""
    s = _pick(_NEW_CASE, lang)
    return _render(
        subject=s["subject"].format(agency=agency_name),
        title=s["title"].format(agency=agency_name),
        intro=s["intro"].format(agency=agency_name, dossier=_dossier(journey_name, lang)),
        button_label=s["button"],
        button_url=login_link,
        lang=lang,
    )


# Reminder dispatch — the AGENCY's reminder to its client (or an external
# contact). Subject AND intro carry the agency (multi-agency inspection §6:
# a client with dossiers at two agencies must see WHO is reminding them).
# The agency-written message body is passed through untouched.
_REMINDER = {
    "fr": {
        "subject": "Nidria : Rappel de {agency}",
        "title": "Rappel",
        "intro": "{agency} vous envoie un rappel concernant votre dossier :",
        "button": "Accéder à mon espace",
    },
    "en": {
        "subject": "Nidria: Reminder from {agency}",
        "title": "Reminder",
        "intro": "{agency} sends you a reminder about your case:",
        "button": "Open my space",
    },
    "es": {
        "subject": "Nidria: Recordatorio de {agency}",
        "title": "Recordatorio",
        "intro": "{agency} le envía un recordatorio sobre su expediente:",
        "button": "Acceder a mi espacio",
    },
    "ru": {
        "subject": "Nidria: Напоминание от {agency}",
        "title": "Напоминание",
        "intro": "{agency} отправляет вам напоминание по вашему делу:",
        "button": "Открыть мой кабинет",
    },
    "pt": {
        "subject": "Nidria: Lembrete de {agency}",
        "title": "Lembrete",
        "intro": "{agency} envia-lhe um lembrete sobre o seu processo:",
        "button": "Aceder ao meu espaço",
    },
    "it": {
        "subject": "Nidria: Promemoria da {agency}",
        "title": "Promemoria",
        "intro": "{agency} ti invia un promemoria sulla tua pratica:",
        "button": "Accedere al mio spazio",
    },
}


def reminder_email(
    agency_name: str, message_body: str, space_link: str | None, lang: str = "fr"
) -> EmailContent:
    """Rendered in the recipient language. `space_link` is the BRANDED
    client-space URL for an EXPAT recipient; None for an external
    contact (no client space to open)."""
    s = _pick(_REMINDER, lang)
    return _render(
        subject=s["subject"].format(agency=agency_name),
        title=s["title"],
        intro=s["intro"].format(agency=agency_name),
        body_text=message_body,
        button_label=s["button"] if space_link else None,
        button_url=space_link,
        lang=lang,
    )


# Escalation wrapper (working text — final wording to be validated by Eric):
# a reminder whose EXTERNAL contact is unreachable is re-routed to the case
# owner. It WRAPS the original message_body, never rewrites it.
_ESCALATION_PREFIX = {
    "fr": (
        "Le prestataire {name} doit fournir les éléments ci-dessous, mais il "
        "est injoignable dans l'application (aucun accès). Merci de le "
        "contacter directement. Rappel d'origine :"
    ),
    "en": (
        "The provider {name} must provide the items below but is unreachable "
        "in the app (no access). Please contact them directly. Original "
        "reminder:"
    ),
    "es": (
        "El proveedor {name} debe proporcionar los elementos siguientes, pero "
        "es ilocalizable en la aplicación (sin acceso). Contáctelo "
        "directamente. Recordatorio original:"
    ),
    "ru": (
        "Поставщик {name} должен предоставить приведённые ниже элементы, но "
        "недоступен в приложении (нет доступа). Свяжитесь с ним напрямую. "
        "Исходное напоминание:"
    ),
    "pt": (
        "O prestador {name} deve fornecer os elementos abaixo, mas está "
        "inacessível na aplicação (sem acesso). Contacte-o diretamente. "
        "Lembrete original:"
    ),
    "it": (
        "Il fornitore {name} deve fornire gli elementi seguenti, ma è "
        "irraggiungibile nell'applicazione (nessun accesso). Contattalo "
        "direttamente. Promemoria originale:"
    ),
}


def reminder_escalation_email(
    agency_name: str, contact_name: str, message_body: str, lang: str = "fr"
) -> EmailContent:
    """A reminder targeted an UNREACHABLE external contact → re-routed to the
    case owner. WRAPS message_body (never rewrites it), naming the contact so
    the agent reads WHO must do WHAT and that the person has no app access."""
    prefix = _ESCALATION_PREFIX.get(lang, _ESCALATION_PREFIX["fr"]).format(name=contact_name)
    return reminder_email(agency_name, f"{prefix}\n\n{message_body}", None, lang)


# Auto follow-up (J+N) body — SYSTEM-authored (not the agency's free text), so
# it MUST reach the client in THEIR language, resolved like every other
# notification. Same message, six languages, strict parity: a step has not
# progressed for N days. `step` is the (agency-authored) step name, a variable.
_AUTO_REMINDER_BODY = {
    "fr": "Relance automatique : l'étape « {step} » n'a pas progressé depuis {days} jours.",
    "en": "Automatic follow-up: the step “{step}” has not progressed for {days} days.",
    "es": "Recordatorio automático: la etapa «{step}» no ha avanzado desde hace {days} días.",
    "ru": "Автоматическое напоминание: этап «{step}» не продвигался {days} дней.",
    "pt": "Lembrete automático: a etapa «{step}» não avança há {days} dias.",
    "it": "Promemoria automatico: la fase «{step}» non è avanzata da {days} giorni.",
}


def auto_reminder_body(step_name: str, days: int, lang: str = "fr") -> str:
    """The stored body of an auto follow-up reminder, in the recipient
    (client) language. Kept as plain text — it flows through the same
    reminder_email dispatch (chrome localized to the same lang)."""
    template = _AUTO_REMINDER_BODY.get(lang, _AUTO_REMINDER_BODY["fr"])
    return template.format(step=step_name, days=days)


def _pick(catalog: dict[str, dict[str, str]], lang: str) -> dict[str, str]:
    """Select a template's strings for `lang`, falling back to FR. `lang` is
    already the resolved recipient language (BLOC NOTIF-1)."""
    return catalog.get(lang, catalog["fr"])


# Each catalog: lang → {subject, title, intro (named placeholders), button}.
# The SAME placeholders appear in the 3 languages (a missing one would silently
# drop a variable — guarded by the render tests). step_name arrives already
# resolved in the recipient language.
_REQUIREMENT_REQUEST = {
    "fr": {
        "subject": "Nidria : De nouvelles informations sont attendues",
        "title": "De nouvelles informations sont attendues",
        "intro": (
            "{agency} a besoin d'informations ou de documents pour l'étape « {step} » de votre "
            "dossier. Connectez-vous à votre espace pour les fournir."
        ),
        "button": "Compléter mon dossier",
    },
    "en": {
        "subject": "Nidria: New information is required",
        "title": "New information is required",
        "intro": (
            "{agency} needs information or documents for the step “{step}” of your case. Log in "
            "to your space to provide them."
        ),
        "button": "Complete my case",
    },
    "es": {
        "subject": "Nidria: Se requiere nueva información",
        "title": "Se requiere nueva información",
        "intro": (
            "{agency} necesita información o documentos para la etapa «{step}» de su expediente. "
            "Inicie sesión en su espacio para proporcionarlos."
        ),
        "button": "Completar mi expediente",
    },
    "ru": {
        "subject": "Nidria: Ожидается новая информация",
        "title": "Ожидается новая информация",
        "intro": (
            "{agency} требуются сведения или документы для этапа «{step}» вашего дела. Войдите в "
            "свой кабинет, чтобы их предоставить."
        ),
        "button": "Заполнить моё дело",
    },
    "pt": {
        "subject": "Nidria: São necessárias novas informações",
        "title": "São necessárias novas informações",
        "intro": (
            "{agency} precisa de informações ou documentos para a etapa «{step}» do seu processo. "
            "Inicie sessão no seu espaço para os fornecer."
        ),
        "button": "Completar o meu processo",
    },
    "it": {
        "subject": "Nidria: Sono richieste nuove informazioni",
        "title": "Sono richieste nuove informazioni",
        "intro": (
            "{agency} ha bisogno di informazioni o documenti per la fase «{step}» della tua "
            "pratica. Accedi al tuo spazio per fornirli."
        ),
        "button": "Completare la mia pratica",
    },
}

_STEP_REOPENED = {
    "fr": {
        "subject": "Nidria : Votre agence a besoin de précisions",
        "title": "Votre agence a besoin de précisions",
        "intro": (
            "{agency} a rouvert l'étape « {step} » de votre dossier et a besoin de précisions ou "
            "d'un complément. Connectez-vous à votre espace pour la mettre à jour."
        ),
        "button": "Mettre à jour mon dossier",
    },
    "en": {
        "subject": "Nidria: Your agency needs clarification",
        "title": "Your agency needs clarification",
        "intro": (
            "{agency} reopened the step “{step}” of your case and needs clarification or "
            "additional details. Log in to your space to update it."
        ),
        "button": "Update my case",
    },
    "es": {
        "subject": "Nidria: Su agencia necesita aclaraciones",
        "title": "Su agencia necesita aclaraciones",
        "intro": (
            "{agency} reabrió la etapa «{step}» de su expediente y necesita aclaraciones o "
            "información adicional. Inicie sesión en su espacio para actualizarla."
        ),
        "button": "Actualizar mi expediente",
    },
    "ru": {
        "subject": "Nidria: Вашему агентству нужны уточнения",
        "title": "Вашему агентству нужны уточнения",
        "intro": (
            "{agency} вновь открыло этап «{step}» вашего дела и нуждается в уточнениях или "
            "дополнении. Войдите в свой кабинет, чтобы его обновить."
        ),
        "button": "Обновить моё дело",
    },
    "pt": {
        "subject": "Nidria: A sua agência precisa de esclarecimentos",
        "title": "A sua agência precisa de esclarecimentos",
        "intro": (
            "{agency} reabriu a etapa «{step}» do seu processo e precisa de esclarecimentos ou de "
            "informações adicionais. Inicie sessão no seu espaço para a atualizar."
        ),
        "button": "Atualizar o meu processo",
    },
    "it": {
        "subject": "Nidria: La tua agenzia ha bisogno di chiarimenti",
        "title": "La tua agenzia ha bisogno di chiarimenti",
        "intro": (
            "{agency} ha riaperto la fase «{step}» della tua pratica e ha bisogno di chiarimenti o "
            "di informazioni aggiuntive. Accedi al tuo spazio per aggiornarla."
        ),
        "button": "Aggiornare la mia pratica",
    },
}

_READY_TO_VALIDATE = {
    "fr": {
        "subject": "Nidria : Un dossier est prêt à valider",
        "title": "Un dossier est prêt à valider",
        "intro": (
            "Toutes les informations attendues pour l'étape « {step} » du dossier {case} ont été "
            "fournies. Vous pouvez la valider."
        ),
        "button": "Ouvrir le dossier",
    },
    "en": {
        "subject": "Nidria: A case is ready to validate",
        "title": "A case is ready to validate",
        "intro": (
            "All the information expected for the step “{step}” of case {case} has been provided. "
            "You can validate it."
        ),
        "button": "Open the case",
    },
    "es": {
        "subject": "Nidria: Un expediente está listo para validar",
        "title": "Un expediente está listo para validar",
        "intro": (
            "Toda la información esperada para la etapa «{step}» del expediente {case} ha sido "
            "proporcionada. Puede validarla."
        ),
        "button": "Abrir el expediente",
    },
    "ru": {
        "subject": "Nidria: Дело готово к проверке",
        "title": "Дело готово к проверке",
        "intro": (
            "Все ожидаемые сведения по этапу «{step}» дела {case} предоставлены. Вы можете его "
            "проверить."
        ),
        "button": "Открыть дело",
    },
    "pt": {
        "subject": "Nidria: Um processo está pronto para validação",
        "title": "Um processo está pronto para validação",
        "intro": (
            "Todas as informações esperadas para a etapa «{step}» do processo {case} foram "
            "fornecidas. Pode validá-la."
        ),
        "button": "Abrir o processo",
    },
    "it": {
        "subject": "Nidria: Una pratica è pronta per la convalida",
        "title": "Una pratica è pronta per la convalida",
        "intro": (
            "Tutte le informazioni attese per la fase «{step}» della pratica {case} sono state "
            "fornite. Puoi convalidarla."
        ),
        "button": "Aprire la pratica",
    },
}

_NEW_COMMENT_CLIENT = {
    "fr": {
        "subject": "Nidria : Nouveau message de votre conseiller",
        "title": "Vous avez un nouveau message",
        "intro": (
            "{author} de {agency} vous a écrit au sujet de l'étape « {step} ». Répondez depuis "
            "votre espace."
        ),
        "button": "Voir la conversation",
    },
    "en": {
        "subject": "Nidria: New message from your advisor",
        "title": "You have a new message",
        "intro": (
            "{author} from {agency} wrote to you about the step “{step}”. Reply from your space."
        ),
        "button": "View the conversation",
    },
    "es": {
        "subject": "Nidria: Nuevo mensaje de su asesor",
        "title": "Tiene un nuevo mensaje",
        "intro": (
            "{author} de {agency} le escribió sobre la etapa «{step}». Responda desde su espacio."
        ),
        "button": "Ver la conversación",
    },
    "ru": {
        "subject": "Nidria: Новое сообщение от вашего консультанта",
        "title": "У вас новое сообщение",
        "intro": (
            "{author} из {agency} написал(а) вам по поводу этапа «{step}». Ответьте из своего "
            "кабинета."
        ),
        "button": "Посмотреть переписку",
    },
    "pt": {
        "subject": "Nidria: Nova mensagem do seu consultor",
        "title": "Tem uma nova mensagem",
        "intro": (
            "{author} da {agency} escreveu-lhe sobre a etapa «{step}». Responda a partir do seu "
            "espaço."
        ),
        "button": "Ver a conversa",
    },
    "it": {
        "subject": "Nidria: Nuovo messaggio dal tuo consulente",
        "title": "Hai un nuovo messaggio",
        "intro": (
            "{author} di {agency} ti ha scritto in merito alla fase «{step}». Rispondi dal tuo "
            "spazio."
        ),
        "button": "Vedere la conversazione",
    },
}

_NEW_COMMENT_AGENT = {
    "fr": {
        "subject": "Nidria : Nouveau message de votre client",
        "title": "Nouveau message d'un client",
        "intro": (
            "{client} a écrit au sujet de l'étape « {step} ». Ouvrez le dossier pour répondre."
        ),
        "button": "Ouvrir le dossier",
    },
    "en": {
        "subject": "Nidria: New message from your client",
        "title": "New message from a client",
        "intro": "{client} wrote about the step “{step}”. Open the case to reply.",
        "button": "Open the case",
    },
    "es": {
        "subject": "Nidria: Nuevo mensaje de su cliente",
        "title": "Nuevo mensaje de un cliente",
        "intro": "{client} escribió sobre la etapa «{step}». Abra el expediente para responder.",
        "button": "Abrir el expediente",
    },
    "ru": {
        "subject": "Nidria: Новое сообщение от вашего клиента",
        "title": "Новое сообщение от клиента",
        "intro": "{client} написал(а) по поводу этапа «{step}». Откройте дело, чтобы ответить.",
        "button": "Открыть дело",
    },
    "pt": {
        "subject": "Nidria: Nova mensagem do seu cliente",
        "title": "Nova mensagem de um cliente",
        "intro": "{client} escreveu sobre a etapa «{step}». Abra o processo para responder.",
        "button": "Abrir o processo",
    },
    "it": {
        "subject": "Nidria: Nuovo messaggio dal tuo cliente",
        "title": "Nuovo messaggio da un cliente",
        "intro": (
            "{client} ha scritto in merito alla fase «{step}». Apri la pratica per rispondere."
        ),
        "button": "Aprire la pratica",
    },
}


def requirement_request_email(
    agency_name: str, step_name: str, space_link: str, lang: str = "fr"
) -> EmailContent:
    """(a) A step became active and needs info/documents from the client.
    Rendered in the recipient language `lang` (BLOC NOTIF-2). step_name is
    already resolved in `lang`."""
    s = _pick(_REQUIREMENT_REQUEST, lang)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"].format(agency=agency_name, step=step_name),
        button_label=s["button"],
        button_url=space_link,
        lang=lang,
    )


def step_reopened_email(
    agency_name: str, step_name: str, space_link: str, lang: str = "fr"
) -> EmailContent:
    """(c) The agency reopened a step — distinct tone from the first request.
    Rendered in the recipient language `lang`."""
    s = _pick(_STEP_REOPENED, lang)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"].format(agency=agency_name, step=step_name),
        button_label=s["button"],
        button_url=space_link,
        lang=lang,
    )


def ready_to_validate_email(
    case_label: str, step_name: str, app_link: str, lang: str = "fr"
) -> EmailContent:
    """(b) agency_validation step has all requirements provided — the owner
    agent can close it. Rendered in the recipient (agent) language `lang`."""
    s = _pick(_READY_TO_VALIDATE, lang)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"].format(step=step_name, case=case_label),
        button_label=s["button"],
        button_url=app_link,
        lang=lang,
    )


def new_comment_to_client(
    agency_name: str, author_first_name: str, step_name: str, space_link: str, lang: str = "fr"
) -> EmailContent:
    """Agent posted on a step thread → notify the client. Uses the agent's
    FIRST NAME (a deliberate, scoped exception to the anti-staffing rule: a
    conversation is not a status — a name humanizes the reply). Rendered in the
    client language `lang`."""
    s = _pick(_NEW_COMMENT_CLIENT, lang)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"].format(author=author_first_name, agency=agency_name, step=step_name),
        button_label=s["button"],
        button_url=space_link,
        lang=lang,
    )


def new_comment_to_agent(
    client_name: str, step_name: str, app_link: str, lang: str = "fr"
) -> EmailContent:
    """Client posted on a step thread → notify the case owner agent. Rendered
    in the agent language `lang`."""
    s = _pick(_NEW_COMMENT_AGENT, lang)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"].format(client=client_name, step=step_name),
        button_label=s["button"],
        button_url=app_link,
        lang=lang,
    )


_REFERRAL_GRANTED = {
    "fr": {
        "subject": "Nidria : votre filleul s'est abonné : -{rate} % pendant 12 mois",
        "title": "Votre parrainage porte ses fruits",
        "intro": (
            "{referred} vient de s'abonner à Nidria grâce à votre parrainage. "
            "Votre remise passe à -{rate} % sur votre abonnement pendant 12 mois, "
            "dès votre prochaine facture (jusqu'à -50 % avec vos prochains parrainages)."
        ),
    },
    "en": {
        "subject": "Nidria: your referral subscribed : {rate}% off for 12 months",
        "title": "Your referral paid off",
        "intro": (
            "{referred} just subscribed to Nidria thanks to your referral. "
            "Your discount rises to {rate}% on your subscription for 12 months, "
            "starting with your next invoice (up to 50% with your next referrals)."
        ),
    },
    "es": {
        "subject": "Nidria: su recomendado se ha suscrito : -{rate} % durante 12 meses",
        "title": "Su recomendación ha dado frutos",
        "intro": (
            "{referred} acaba de suscribirse a Nidria gracias a su recomendación. "
            "Su descuento sube al {rate} % en su suscripción durante 12 meses, "
            "desde su próxima factura (hasta el 50 % con sus próximas recomendaciones)."
        ),
    },
    "ru": {
        "subject": "Nidria: ваш приглашённый оформил подписку : скидка {rate} % на 12 месяцев",
        "title": "Ваша рекомендация принесла плоды",
        "intro": (
            "{referred} только что оформил подписку на Nidria по вашей рекомендации. "
            "Ваша скидка выросла до {rate} % на 12 месяцев, "
            "начиная со следующего счёта (до 50 % с новыми рекомендациями)."
        ),
    },
    "pt": {
        "subject": "Nidria: o seu indicado assinou : -{rate} % durante 12 meses",
        "title": "A sua indicação deu frutos",
        "intro": (
            "{referred} acaba de assinar a Nidria graças à sua indicação. "
            "O seu desconto sobe para {rate} % na sua assinatura durante 12 meses, "
            "a partir da próxima fatura (até 50 % com as suas próximas indicações)."
        ),
    },
    "it": {
        "subject": "Nidria: il tuo invitato si è abbonato : -{rate}% per 12 mesi",
        "title": "Il tuo passaparola ha dato i suoi frutti",
        "intro": (
            "{referred} si è appena abbonato a Nidria grazie al tuo invito. "
            "Il tuo sconto sale al {rate}% sul tuo abbonamento per 12 mesi, "
            "a partire dalla prossima fattura (fino al 50% con i tuoi prossimi inviti)."
        ),
    },
}


_JOURNEY_KICKOFF = {
    "fr": {
        "subject": "Nidria : votre parcours démarre, des éléments sont attendus de vous",
        "title": "Votre parcours démarre",
        "intro": (
            "{agency} a lancé votre parcours : {total} élément(s) sont attendus de vous "
            "pour commencer. Voici ce qui vous sera demandé, étape par étape :"
        ),
        "line": "{step} : {count} élément(s)",
        "button": "Ouvrir mon espace",
    },
    "en": {
        "subject": "Nidria: your journey starts, some items are expected from you",
        "title": "Your journey starts",
        "intro": (
            "{agency} has launched your journey: {total} item(s) are expected from you "
            "to begin. Here is what will be asked, step by step:"
        ),
        "line": "{step}: {count} item(s)",
        "button": "Open my space",
    },
    "es": {
        "subject": "Nidria: su proceso comienza, se esperan elementos de usted",
        "title": "Su proceso comienza",
        "intro": (
            "{agency} ha lanzado su proceso: se esperan {total} elemento(s) de usted "
            "para comenzar. Esto es lo que se le pedirá, etapa por etapa:"
        ),
        "line": "{step}: {count} elemento(s)",
        "button": "Abrir mi espacio",
    },
    "ru": {
        "subject": "Nidria: ваш процесс начинается, от вас ожидаются документы",
        "title": "Ваш процесс начинается",
        "intro": (
            "{agency} запустило ваш процесс: от вас ожидается {total} элемент(ов) "
            "для начала. Вот что потребуется, по этапам:"
        ),
        "line": "{step}: {count} элемент(ов)",
        "button": "Открыть моё пространство",
    },
    "pt": {
        "subject": "Nidria: o seu percurso começa, aguardamos elementos seus",
        "title": "O seu percurso começa",
        "intro": (
            "{agency} lançou o seu percurso: aguardamos {total} elemento(s) seus "
            "para começar. Eis o que será pedido, etapa a etapa:"
        ),
        "line": "{step}: {count} elemento(s)",
        "button": "Abrir o meu espaço",
    },
    "it": {
        "subject": "Nidria: il tuo percorso inizia, alcuni elementi sono attesi da te",
        "title": "Il tuo percorso inizia",
        "intro": (
            "{agency} ha avviato il tuo percorso: {total} elemento(i) sono attesi da te "
            "per iniziare. Ecco cosa ti sarà chiesto, tappa per tappa:"
        ),
        "line": "{step}: {count} elemento(i)",
        "button": "Apri il mio spazio",
    },
}


def journey_kickoff_email(
    agency_name: str, items: list[tuple[str, int]], space_link: str, lang: str = "fr"
) -> EmailContent:
    """ONE mail at journey assignment (anti-burst J1): what the startable
    steps expect from the client, grouped by step — instead of N unitary
    requirement mails as the agent starts them."""
    s = _pick(_JOURNEY_KICKOFF, lang)
    total = sum(count for _, count in items)
    lines = "\n".join("- " + s["line"].format(step=step, count=count) for step, count in items)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"].format(agency=agency_name, total=total),
        body_text=lines,
        button_label=s["button"],
        button_url=space_link,
        lang=lang,
    )


_DIGEST = {
    "fr": {
        "subject": "Nidria : votre dossier a avancé",
        "title": "Votre dossier a avancé",
        "intro": "{agency} : {summary}.",
        "period_weekly": "cette semaine",
        "period_daily": "aujourd'hui",
        "completed": "{n} étape(s) terminée(s)",
        "started": "{n} étape(s) démarrée(s)",
        "documents": "{n} document(s) validé(s)",
        "line_completed": "Terminée : {step}",
        "line_started": "Démarrée : {step}",
        "button": "Voir mon dossier",
    },
    "en": {
        "subject": "Nidria: your file has moved forward",
        "title": "Your file has moved forward",
        "intro": "{agency}: {summary}.",
        "period_weekly": "this week",
        "period_daily": "today",
        "completed": "{n} step(s) completed",
        "started": "{n} step(s) started",
        "documents": "{n} document(s) validated",
        "line_completed": "Completed: {step}",
        "line_started": "Started: {step}",
        "button": "View my file",
    },
    "es": {
        "subject": "Nidria: su expediente ha avanzado",
        "title": "Su expediente ha avanzado",
        "intro": "{agency}: {summary}.",
        "period_weekly": "esta semana",
        "period_daily": "hoy",
        "completed": "{n} etapa(s) completada(s)",
        "started": "{n} etapa(s) iniciada(s)",
        "documents": "{n} documento(s) validado(s)",
        "line_completed": "Completada: {step}",
        "line_started": "Iniciada: {step}",
        "button": "Ver mi expediente",
    },
    "ru": {
        "subject": "Nidria: ваше дело продвинулось",
        "title": "Ваше дело продвинулось",
        "intro": "{agency}: {summary}.",
        "period_weekly": "за эту неделю",
        "period_daily": "сегодня",
        "completed": "этапов завершено: {n}",
        "started": "этапов начато: {n}",
        "documents": "документов подтверждено: {n}",
        "line_completed": "Завершено: {step}",
        "line_started": "Начато: {step}",
        "button": "Открыть моё дело",
    },
    "pt": {
        "subject": "Nidria: o seu processo avançou",
        "title": "O seu processo avançou",
        "intro": "{agency}: {summary}.",
        "period_weekly": "esta semana",
        "period_daily": "hoje",
        "completed": "{n} etapa(s) concluída(s)",
        "started": "{n} etapa(s) iniciada(s)",
        "documents": "{n} documento(s) validado(s)",
        "line_completed": "Concluída: {step}",
        "line_started": "Iniciada: {step}",
        "button": "Ver o meu processo",
    },
    "it": {
        "subject": "Nidria: la tua pratica è avanzata",
        "title": "La tua pratica è avanzata",
        "intro": "{agency}: {summary}.",
        "period_weekly": "questa settimana",
        "period_daily": "oggi",
        "completed": "{n} tappa(e) completata(e)",
        "started": "{n} tappa(e) avviata(e)",
        "documents": "{n} documento(i) convalidato(i)",
        "line_completed": "Completata: {step}",
        "line_started": "Avviata: {step}",
        "button": "Vedi la mia pratica",
    },
}


def digest_email(
    agency_name: str,
    period: str,
    completed_steps: list[str],
    started_steps: list[str],
    documents_validated: int,
    space_link: str,
    lang: str = "fr",
) -> EmailContent:
    """The periodic progress digest (weekly|daily): a readable summary of
    the WHITELISTED client-relevant events — never an internal action.
    `period` is "weekly" | "daily" (localized label inside)."""
    s = _pick(_DIGEST, lang)
    parts: list[str] = []
    if completed_steps:
        parts.append(s["completed"].format(n=len(completed_steps)))
    if started_steps:
        parts.append(s["started"].format(n=len(started_steps)))
    if documents_validated:
        parts.append(s["documents"].format(n=documents_validated))
    period_label = s["period_weekly"] if period == "weekly" else s["period_daily"]
    summary = f"{period_label}, " + ", ".join(parts)
    lines = [s["line_completed"].format(step=step) for step in completed_steps]
    lines += [s["line_started"].format(step=step) for step in started_steps]
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"].format(agency=agency_name, summary=summary),
        body_text="\n".join("- " + line for line in lines) if lines else None,
        button_label=s["button"],
        button_url=space_link,
        lang=lang,
    )


def referral_granted_email(referred_name: str, rate: int, lang: str = "fr") -> EmailContent:
    """Rendered in the REFERRER's language (agency default). `rate` is the
    REAL tier the new credit locked in (bareme by rank) — never hardcoded."""
    s = _pick(_REFERRAL_GRANTED, lang)
    return _render(
        subject=s["subject"].format(rate=rate),
        title=s["title"],
        intro=s["intro"].format(referred=referred_name, rate=rate),
        lang=lang,
    )


_SIGNUP_CODE = {
    "fr": {
        "subject": "Nidria : votre code de verification",
        "title": "Votre code de verification",
        "intro": ("Voici votre code pour creer votre espace Nidria. Il expire dans 15 minutes."),
        "phishing": "Si vous n'avez pas demande ce code, ignorez cet email.",
    },
    "en": {
        "subject": "Nidria: your verification code",
        "title": "Your verification code",
        "intro": "Here is your code to create your Nidria workspace. It expires in 15 minutes.",
        "phishing": "If you did not request this code, please ignore this email.",
    },
    "es": {
        "subject": "Nidria: su codigo de verificacion",
        "title": "Su codigo de verificacion",
        "intro": ("Aqui tiene su codigo para crear su espacio Nidria. Caduca en 15 minutos."),
        "phishing": "Si no ha solicitado este codigo, ignore este correo.",
    },
    "ru": {
        "subject": "Nidria: ваш код подтверждения",
        "title": "Ваш код подтверждения",
        "intro": (
            "Вот ваш код для создания рабочего пространства Nidria. Срок действия: 15 минут."
        ),
        "phishing": "Если вы не запрашивали этот код, просто проигнорируйте это письмо.",
    },
    "pt": {
        "subject": "Nidria: o seu codigo de verificacao",
        "title": "O seu codigo de verificacao",
        "intro": ("Aqui esta o seu codigo para criar o seu espaco Nidria. Expira em 15 minutos."),
        "phishing": "Se nao solicitou este codigo, ignore este email.",
    },
    "it": {
        "subject": "Nidria: il tuo codice di verifica",
        "title": "Il tuo codice di verifica",
        "intro": "Ecco il tuo codice per creare il tuo spazio Nidria. Scade tra 15 minuti.",
        "phishing": "Se non hai richiesto questo codice, ignora questa email.",
    },
}

_SIGNUP_EXISTING = {
    "fr": {
        "subject": "Nidria : vous avez deja un compte",
        "title": "Vous avez deja un compte",
        "intro": (
            "Une creation d'espace a ete demandee avec cette adresse, mais un "
            "compte existe deja. Connectez-vous ci-dessous ; mot de passe oublie ? "
            "La page de connexion propose la reinitialisation."
        ),
        "button": "Se connecter",
        "phishing": "Si vous n'etes pas a l'origine de cette demande, ignorez cet email.",
    },
    "en": {
        "subject": "Nidria: you already have an account",
        "title": "You already have an account",
        "intro": (
            "A workspace creation was requested with this address, but an account "
            "already exists. Log in below; forgot your password? The login page "
            "offers a reset."
        ),
        "button": "Log in",
        "phishing": "If you did not make this request, please ignore this email.",
    },
    "es": {
        "subject": "Nidria: ya tiene una cuenta",
        "title": "Ya tiene una cuenta",
        "intro": (
            "Se solicito crear un espacio con esta direccion, pero ya existe una "
            "cuenta. Inicie sesion a continuacion."
        ),
        "button": "Iniciar sesion",
        "phishing": "Si usted no realizo esta solicitud, ignore este correo.",
    },
    "ru": {
        "subject": "Nidria: у вас уже есть аккаунт",
        "title": "У вас уже есть аккаунт",
        "intro": (
            "С этим адресом запрошено создание пространства, но аккаунт уже "
            "существует. Войдите по кнопке ниже."
        ),
        "button": "Войти",
        "phishing": "Если это были не вы, просто проигнорируйте это письмо.",
    },
    "pt": {
        "subject": "Nidria: ja tem uma conta",
        "title": "Ja tem uma conta",
        "intro": (
            "Foi pedida a criacao de um espaco com este endereco, mas ja existe "
            "uma conta. Inicie sessao abaixo."
        ),
        "button": "Iniciar sessao",
        "phishing": "Se nao fez este pedido, ignore este email.",
    },
    "it": {
        "subject": "Nidria: hai gia un account",
        "title": "Hai gia un account",
        "intro": (
            "E stata richiesta la creazione di uno spazio con questo indirizzo, "
            "ma esiste gia un account. Accedi qui sotto."
        ),
        "button": "Accedi",
        "phishing": "Se non hai effettuato questa richiesta, ignora questa email.",
    },
}


def signup_code_email(code: str, lang: str = "fr") -> EmailContent:
    """The 6-digit code, BIG (body_text renders large in the layout), the
    15-minute validity, and the standard anti-phishing line."""
    s = _pick(_SIGNUP_CODE, lang)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"],
        body_text=code,
        validity=s["phishing"],
        lang=lang,
    )


def signup_existing_account_email(login_url: str, lang: str = "fr") -> EmailContent:
    s = _pick(_SIGNUP_EXISTING, lang)
    return _render(
        subject=s["subject"],
        title=s["title"],
        intro=s["intro"],
        button_label=s["button"],
        button_url=login_url,
        validity=s["phishing"],
        lang=lang,
    )

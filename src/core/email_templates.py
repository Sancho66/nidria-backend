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
    text_parts += ["", "—", footer]
    return EmailContent(subject=subject, text="\n".join(text_parts), html=html)


def password_reset_email(reset_link: str, expires_minutes: int) -> EmailContent:
    return _render(
        subject="Nidria — Réinitialisez votre mot de passe",
        title="Réinitialisez votre mot de passe",
        intro=(
            "Une demande de réinitialisation de mot de passe a été faite pour votre compte Nidria."
        ),
        button_label="Choisir un nouveau mot de passe",
        button_url=reset_link,
        validity=f"Ce lien expire dans {expires_minutes} minutes.",
    )


def agent_invitation_email(agency_name: str, link: str, expires_days: int) -> EmailContent:
    return _render(
        subject=f"Nidria — Vous êtes invité(e) à rejoindre {agency_name}",
        title=f"Vous êtes invité(e) à rejoindre {agency_name} sur Nidria",
        intro=(
            f"{agency_name} vous invite à rejoindre son espace de travail "
            "sur Nidria pour gérer ses dossiers d'expatriation."
        ),
        button_label="Accepter l'invitation",
        button_url=link,
        validity=f"Ce lien expire dans {expires_days} jours.",
    )


def expat_activation_email(agency_name: str, link: str, expires_days: int) -> EmailContent:
    return _render(
        subject=f"Nidria — {agency_name} vous a ouvert un espace de suivi",
        title=f"{agency_name} vous a ouvert un espace de suivi",
        intro=(
            "Un dossier d'expatriation a été ouvert pour vous. Activez votre "
            "espace personnel pour suivre son avancement, étape par étape."
        ),
        button_label="Activer mon espace",
        button_url=link,
        validity=f"Ce lien expire dans {expires_days} jours.",
    )


def new_case_email(agency_name: str, login_link: str) -> EmailContent:
    return _render(
        subject="Nidria — Un nouveau dossier vous attend",
        title="Un nouveau dossier vous attend",
        intro=(
            f"{agency_name} a ouvert un nouveau dossier d'expatriation pour "
            "vous. Connectez-vous à votre espace pour le consulter."
        ),
        button_label="Accéder à mon espace",
        button_url=login_link,
    )


def reminder_email(message_body: str) -> EmailContent:
    return _render(
        subject="Nidria — Rappel",
        title="Rappel",
        intro="Un rappel concernant votre dossier d'expatriation :",
        body_text=message_body,
    )


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
        "subject": "Nidria — De nouvelles informations sont attendues",
        "title": "De nouvelles informations sont attendues",
        "intro": (
            "{agency} a besoin d'informations ou de documents pour l'étape « {step} » de votre "
            "dossier. Connectez-vous à votre espace pour les fournir."
        ),
        "button": "Compléter mon dossier",
    },
    "en": {
        "subject": "Nidria — New information is required",
        "title": "New information is required",
        "intro": (
            "{agency} needs information or documents for the step “{step}” of your case. Log in "
            "to your space to provide them."
        ),
        "button": "Complete my case",
    },
    "es": {
        "subject": "Nidria — Se requiere nueva información",
        "title": "Se requiere nueva información",
        "intro": (
            "{agency} necesita información o documentos para la etapa «{step}» de su expediente. "
            "Inicie sesión en su espacio para proporcionarlos."
        ),
        "button": "Completar mi expediente",
    },
    "ru": {
        "subject": "Nidria — Ожидается новая информация",
        "title": "Ожидается новая информация",
        "intro": (
            "{agency} требуются сведения или документы для этапа «{step}» вашего дела. Войдите в "
            "свой кабинет, чтобы их предоставить."
        ),
        "button": "Заполнить моё дело",
    },
    "pt": {
        "subject": "Nidria — São necessárias novas informações",
        "title": "São necessárias novas informações",
        "intro": (
            "{agency} precisa de informações ou documentos para a etapa «{step}» do seu processo. "
            "Inicie sessão no seu espaço para os fornecer."
        ),
        "button": "Completar o meu processo",
    },
    "it": {
        "subject": "Nidria — Sono richieste nuove informazioni",
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
        "subject": "Nidria — Votre agence a besoin de précisions",
        "title": "Votre agence a besoin de précisions",
        "intro": (
            "{agency} a rouvert l'étape « {step} » de votre dossier et a besoin de précisions ou "
            "d'un complément. Connectez-vous à votre espace pour la mettre à jour."
        ),
        "button": "Mettre à jour mon dossier",
    },
    "en": {
        "subject": "Nidria — Your agency needs clarification",
        "title": "Your agency needs clarification",
        "intro": (
            "{agency} reopened the step “{step}” of your case and needs clarification or "
            "additional details. Log in to your space to update it."
        ),
        "button": "Update my case",
    },
    "es": {
        "subject": "Nidria — Su agencia necesita aclaraciones",
        "title": "Su agencia necesita aclaraciones",
        "intro": (
            "{agency} reabrió la etapa «{step}» de su expediente y necesita aclaraciones o "
            "información adicional. Inicie sesión en su espacio para actualizarla."
        ),
        "button": "Actualizar mi expediente",
    },
    "ru": {
        "subject": "Nidria — Вашему агентству нужны уточнения",
        "title": "Вашему агентству нужны уточнения",
        "intro": (
            "{agency} вновь открыло этап «{step}» вашего дела и нуждается в уточнениях или "
            "дополнении. Войдите в свой кабинет, чтобы его обновить."
        ),
        "button": "Обновить моё дело",
    },
    "pt": {
        "subject": "Nidria — A sua agência precisa de esclarecimentos",
        "title": "A sua agência precisa de esclarecimentos",
        "intro": (
            "{agency} reabriu a etapa «{step}» do seu processo e precisa de esclarecimentos ou de "
            "informações adicionais. Inicie sessão no seu espaço para a atualizar."
        ),
        "button": "Atualizar o meu processo",
    },
    "it": {
        "subject": "Nidria — La tua agenzia ha bisogno di chiarimenti",
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
        "subject": "Nidria — Un dossier est prêt à valider",
        "title": "Un dossier est prêt à valider",
        "intro": (
            "Toutes les informations attendues pour l'étape « {step} » du dossier {case} ont été "
            "fournies. Vous pouvez la valider."
        ),
        "button": "Ouvrir le dossier",
    },
    "en": {
        "subject": "Nidria — A case is ready to validate",
        "title": "A case is ready to validate",
        "intro": (
            "All the information expected for the step “{step}” of case {case} has been provided. "
            "You can validate it."
        ),
        "button": "Open the case",
    },
    "es": {
        "subject": "Nidria — Un expediente está listo para validar",
        "title": "Un expediente está listo para validar",
        "intro": (
            "Toda la información esperada para la etapa «{step}» del expediente {case} ha sido "
            "proporcionada. Puede validarla."
        ),
        "button": "Abrir el expediente",
    },
    "ru": {
        "subject": "Nidria — Дело готово к проверке",
        "title": "Дело готово к проверке",
        "intro": (
            "Все ожидаемые сведения по этапу «{step}» дела {case} предоставлены. Вы можете его "
            "проверить."
        ),
        "button": "Открыть дело",
    },
    "pt": {
        "subject": "Nidria — Um processo está pronto para validação",
        "title": "Um processo está pronto para validação",
        "intro": (
            "Todas as informações esperadas para a etapa «{step}» do processo {case} foram "
            "fornecidas. Pode validá-la."
        ),
        "button": "Abrir o processo",
    },
    "it": {
        "subject": "Nidria — Una pratica è pronta per la convalida",
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
        "subject": "Nidria — Nouveau message de votre conseiller",
        "title": "Vous avez un nouveau message",
        "intro": (
            "{author} de {agency} vous a écrit au sujet de l'étape « {step} ». Répondez depuis "
            "votre espace."
        ),
        "button": "Voir la conversation",
    },
    "en": {
        "subject": "Nidria — New message from your advisor",
        "title": "You have a new message",
        "intro": (
            "{author} from {agency} wrote to you about the step “{step}”. Reply from your space."
        ),
        "button": "View the conversation",
    },
    "es": {
        "subject": "Nidria — Nuevo mensaje de su asesor",
        "title": "Tiene un nuevo mensaje",
        "intro": (
            "{author} de {agency} le escribió sobre la etapa «{step}». Responda desde su espacio."
        ),
        "button": "Ver la conversación",
    },
    "ru": {
        "subject": "Nidria — Новое сообщение от вашего консультанта",
        "title": "У вас новое сообщение",
        "intro": (
            "{author} из {agency} написал(а) вам по поводу этапа «{step}». Ответьте из своего "
            "кабинета."
        ),
        "button": "Посмотреть переписку",
    },
    "pt": {
        "subject": "Nidria — Nova mensagem do seu consultor",
        "title": "Tem uma nova mensagem",
        "intro": (
            "{author} da {agency} escreveu-lhe sobre a etapa «{step}». Responda a partir do seu "
            "espaço."
        ),
        "button": "Ver a conversa",
    },
    "it": {
        "subject": "Nidria — Nuovo messaggio dal tuo consulente",
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
        "subject": "Nidria — Nouveau message de votre client",
        "title": "Nouveau message d'un client",
        "intro": (
            "{client} a écrit au sujet de l'étape « {step} ». Ouvrez le dossier pour répondre."
        ),
        "button": "Ouvrir le dossier",
    },
    "en": {
        "subject": "Nidria — New message from your client",
        "title": "New message from a client",
        "intro": "{client} wrote about the step “{step}”. Open the case to reply.",
        "button": "Open the case",
    },
    "es": {
        "subject": "Nidria — Nuevo mensaje de su cliente",
        "title": "Nuevo mensaje de un cliente",
        "intro": "{client} escribió sobre la etapa «{step}». Abra el expediente para responder.",
        "button": "Abrir el expediente",
    },
    "ru": {
        "subject": "Nidria — Новое сообщение от вашего клиента",
        "title": "Новое сообщение от клиента",
        "intro": "{client} написал(а) по поводу этапа «{step}». Откройте дело, чтобы ответить.",
        "button": "Открыть дело",
    },
    "pt": {
        "subject": "Nidria — Nova mensagem do seu cliente",
        "title": "Nova mensagem de um cliente",
        "intro": "{client} escreveu sobre a etapa «{step}». Abra o processo para responder.",
        "button": "Abrir o processo",
    },
    "it": {
        "subject": "Nidria — Nuovo messaggio dal tuo cliente",
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

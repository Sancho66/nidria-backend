"""Transactional email templates — French, one builder per email.

Each builder returns an EmailContent(subject, text, html): HTML for the
clients that render it, plain text as the multipart fallback. The HTML
is email-safe (tables + inline styles, max 600px, no external CSS, no
JS, no hosted images — the header is styled text). Links are ALWAYS
built by the callers on settings.frontend_url, never hardcoded here.
"""

import html as html_lib
from dataclasses import dataclass

_FOOTER = (
    "Cet email a été envoyé par Nidria. "
    "Si vous n'êtes pas à l'origine de cette demande, ignorez-le."
)

_HTML_LAYOUT = """\
<!DOCTYPE html>
<html lang="fr">
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
word-break:break-all;">Ou copiez-collez ce lien&nbsp;: \
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
) -> EmailContent:
    action_blocks = ""
    if body_text is not None:
        escaped = html_lib.escape(body_text).replace("\n", "<br>")
        action_blocks += _HTML_BODY_TEXT.format(body=escaped)
    if button_label is not None and button_url is not None:
        action_blocks += _HTML_BUTTON.format(
            label=html_lib.escape(button_label), url=html_lib.escape(button_url, quote=True)
        )
    if validity is not None:
        action_blocks += _HTML_VALIDITY.format(validity=html_lib.escape(validity))

    html = _HTML_LAYOUT.format(
        title=html_lib.escape(title),
        intro=html_lib.escape(intro),
        action_blocks=action_blocks,
        footer=html_lib.escape(_FOOTER),
    )

    text_parts = ["Nidria", "", title, "", intro]
    if body_text is not None:
        text_parts += ["", body_text]
    if button_label is not None and button_url is not None:
        text_parts += ["", f"{button_label} : {button_url}"]
    if validity is not None:
        text_parts += ["", validity]
    text_parts += ["", "—", _FOOTER]
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


def requirement_request_email(agency_name: str, step_name: str, space_link: str) -> EmailContent:
    """(a) A step became active and needs info/documents from the client."""
    return _render(
        subject="Nidria — De nouvelles informations sont attendues",
        title="De nouvelles informations sont attendues",
        intro=(
            f"{agency_name} a besoin d'informations ou de documents pour "
            f"l'étape « {step_name} » de votre dossier. Connectez-vous à votre "
            "espace pour les fournir."
        ),
        button_label="Compléter mon dossier",
        button_url=space_link,
    )


def step_reopened_email(agency_name: str, step_name: str, space_link: str) -> EmailContent:
    """(c) The agency reopened a step — distinct tone from the first
    request: this is a follow-up, not an initial ask."""
    return _render(
        subject="Nidria — Votre agence a besoin de précisions",
        title="Votre agence a besoin de précisions",
        intro=(
            f"{agency_name} a rouvert l'étape « {step_name} » de votre dossier "
            "et a besoin de précisions ou d'un complément. Connectez-vous à "
            "votre espace pour la mettre à jour."
        ),
        button_label="Mettre à jour mon dossier",
        button_url=space_link,
    )


def ready_to_validate_email(case_label: str, step_name: str, app_link: str) -> EmailContent:
    """(b) agency_validation step has all requirements provided — the
    owner agent can close it."""
    return _render(
        subject="Nidria — Un dossier est prêt à valider",
        title="Un dossier est prêt à valider",
        intro=(
            f"Toutes les informations attendues pour l'étape « {step_name} » du "
            f"dossier {case_label} ont été fournies. Vous pouvez la valider."
        ),
        button_label="Ouvrir le dossier",
        button_url=app_link,
    )

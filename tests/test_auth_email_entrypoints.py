"""Client mail entry points, using rendered multipart content and inert links."""

import html
import unittest

from src.core.email_templates import (
    expat_activation_email,
    new_case_email,
    password_reset_email,
)


class AuthEmailEntrypointsTest(unittest.TestCase):
    def test_client_reset_preserves_action_and_names_the_login_space(self) -> None:
        labels = {
            "fr": "Accéder à mon espace client",
            "en": "Open my client space",
            "es": "Acceder a mi espacio de cliente",
            "hu": "Belépés az ügyfélfelületemre",
            "it": "Accedere al mio spazio cliente",
            "pt": "Aceder ao meu espaço de cliente",
            "ru": "Войти в мой клиентский кабинет",
        }
        for lang, label in labels.items():
            with self.subTest(lang=lang):
                action = "https://example.test/space/reset-password/opaque-token"
                login = "https://example.test/space/login"
                mail = password_reset_email(action, 60, lang, login_link=login)
                self.assertIn(action, mail.text)
                self.assertIn(f'href="{action}"', mail.html)
                self.assertIn(f"{label} : {login}", mail.text)
                self.assertIn(html.escape(label), mail.html)
                self.assertIn(f'href="{login}"', mail.html)
                self.assertNotIn('href="https://example.test/login"', mail.html)

    def test_invitation_keeps_activation_and_branded_login_for_both_recipients(self) -> None:
        for lang in ("fr", "en", "es", "hu", "it", "pt", "ru"):
            for pending in (None, [("Identity", 2)]):
                with self.subTest(lang=lang, member=pending is not None):
                    action = "https://example.test/space/activate/token?agency=test"
                    login = "https://example.test/space/login?agency=test&source=invite"
                    mail = expat_activation_email(
                        "Agency", action, 7, lang=lang, pending_items=pending, login_link=login
                    )
                    self.assertIn(action, mail.text)
                    self.assertIn(login, mail.text)
                    self.assertIn(f'href="{html.escape(login)}"', mail.html)
                    self.assertIn(f'href="{html.escape(action)}"', mail.html)
                    if pending:
                        self.assertIn("Identity", mail.text)

    def test_active_client_goes_directly_to_client_login(self) -> None:
        mail = new_case_email("Agency", "https://example.test/space/login", lang="fr")
        self.assertIn("Accéder à mon espace client", mail.text)
        self.assertIn('href="https://example.test/space/login"', mail.html)
        self.assertNotIn("/activate/", mail.text)

    def test_agent_reset_is_not_redirected_to_client_space(self) -> None:
        for lang in ("fr", "en", "es", "hu", "it", "pt", "ru"):
            with self.subTest(lang=lang):
                mail = password_reset_email("https://example.test/reset-password/token", 60, lang)
                self.assertNotIn("/space/", mail.text)
                self.assertNotIn("/space/", mail.html)
        self.assertIn("Choisir un nouveau mot de passe", password_reset_email("url", 60).text)

    def test_existing_optional_call_and_language_fallback_remain_supported(self) -> None:
        mail = expat_activation_email("Agency", "opaque-link", 7, lang="unsupported")
        self.assertIn("Activer mon espace client", mail.text)
        self.assertIn("opaque-link", mail.text)
        self.assertNotIn("None", mail.html)

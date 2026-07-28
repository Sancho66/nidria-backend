"""DocuSealProvider — l'implémentation du port (méga-lot 28/07, lot 3).

Constats de doc (docuseal.com/docs/api) :
- Auth : header `X-Auth-Token` ; base https://api.docuseal.com (pas d'hôte
  sandbox séparé : le bac à sable est une clé de compte test — la base URL
  reste configurable pour l'on-prem).
- POST /submissions : `template_id`, `send_email` (false CHEZ NOUS,
  toujours — les emails sont au système v4, jamais à DocuSeal), `order`
  ("random" = signature PARALLÈLE), `expire_at`, `submitters[]` avec
  `external_id` = NOTRE id signature_signer. Réponse : liste de submitters
  avec `id`, `slug` (embed https://docuseal.com/s/{slug}), `submission_id`.
- Annulation : DELETE /submissions/{id} (archive).
- Récupération : GET /submissions/{id} → documents[] + audit_log_url —
  leurs URLs EXPIRENT : on télécharge les octets immédiatement, on ne
  stocke jamais une URL.

Source du document (simplification assumée, au rapport) : les lots ne
définissent pas d'où vient le PDF à signer — le provider compose donc un
template minimal via POST /templates/html (titre = le libellé de
l'exigence + un champ signature par signataire), puis crée la soumission
dessus. Le jour où la vraie source existe (pièce du dossier, attachment
d'étape), seul CE fichier change — le port et le domaine sont déjà prêts.
"""

import logging
from datetime import datetime
from typing import Any

import httpx

from src.core.config import get_settings
from src.core.exceptions import UpstreamError
from src.signatures.provider import (
    CompletedFiles,
    CreatedSigner,
    CreatedSubmission,
    ProviderSigner,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


def _signature_field_html(role: str) -> str:
    return (
        f'<signature-field name="Signature — {role}" role="{role}" '
        'style="width: 220px; height: 80px; display: inline-block; margin: 12px">'
        "</signature-field>"
    )


class DocuSealProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.docuseal_api_key:
            raise UpstreamError(
                "DOCUSEAL_API_KEY is not configured.", code="signatures.provider_unconfigured"
            )
        self._base = settings.docuseal_base_url.rstrip("/")
        self._headers = {"X-Auth-Token": settings.docuseal_api_key}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.request(
                    method, f"{self._base}{path}", headers=self._headers, **kwargs
                )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"DocuSeal unreachable: {exc}", code="signatures.provider_unreachable"
            ) from exc
        if response.status_code >= 400:
            raise UpstreamError(
                f"DocuSeal error {response.status_code}: {response.text[:300]}",
                code="signatures.provider_error",
            )
        if response.content:
            return response.json()
        return None

    async def _download(self, url: str) -> bytes:
        """Télécharge une URL de fichier DocuSeal — IMMÉDIATEMENT (elles
        expirent), octets en retour, l'URL n'est jamais persistée."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"DocuSeal download failed: {exc}", code="signatures.provider_unreachable"
            ) from exc
        if response.status_code >= 400:
            raise UpstreamError(
                f"DocuSeal download error {response.status_code}",
                code="signatures.provider_error",
            )
        return response.content

    # --- port -----------------------------------------------------------------------

    async def create(
        self,
        *,
        document_name: str,
        signers: list[ProviderSigner],
        expires_at: datetime | None,
    ) -> CreatedSubmission:
        roles = [f"Signer {i + 1}" for i in range(len(signers))]
        fields_html = "".join(_signature_field_html(role) for role in roles)
        template = await self._request(
            "POST",
            "/templates/html",
            json={
                "name": document_name,
                "html": f"<h1>{document_name}</h1>{fields_html}",
            },
        )
        payload: dict[str, Any] = {
            "template_id": template["id"],
            "send_email": False,  # JAMAIS un email DocuSeal — notifications v4
            "order": "random",  # signature parallèle (constaté : random = parallèle)
            "submitters": [
                {
                    "role": role,
                    "external_id": signer.signer_id,
                    "name": signer.name,
                    "email": signer.email,
                }
                for role, signer in zip(roles, signers, strict=True)
            ],
        }
        if expires_at is not None:
            payload["expire_at"] = expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        submitters = await self._request("POST", "/submissions", json=payload)
        if not isinstance(submitters, list) or not submitters:
            raise UpstreamError(
                "DocuSeal returned no submitters.", code="signatures.provider_error"
            )
        submission_id = str(submitters[0].get("submission_id") or "")
        created = [
            CreatedSigner(
                signer_id=str(s.get("external_id") or ""),
                provider_ref=str(s.get("id") or ""),
                slug=str(s.get("slug") or ""),
            )
            for s in submitters
        ]
        return CreatedSubmission(provider_ref=submission_id, signers=created)

    async def cancel(self, provider_ref: str) -> None:
        await self._request("DELETE", f"/submissions/{provider_ref}")

    async def download_completed(self, provider_ref: str) -> CompletedFiles:
        submission = await self._request("GET", f"/submissions/{provider_ref}")
        documents = submission.get("documents") or []
        if not documents:
            raise UpstreamError(
                "DocuSeal submission has no completed documents.",
                code="signatures.provider_error",
            )
        first = documents[0]
        pdf = await self._download(str(first.get("url")))
        filename = str(first.get("name") or "document") + ".pdf"
        audit_url = submission.get("audit_log_url")
        audit_pdf = await self._download(str(audit_url)) if audit_url else None
        return CompletedFiles(
            document_pdf=pdf,
            document_filename=filename,
            audit_pdf=audit_pdf,
            audit_filename="audit-log.pdf" if audit_pdf is not None else None,
        )

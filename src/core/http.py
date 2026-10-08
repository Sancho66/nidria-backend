"""Shared HTTP helpers: a file download response, and the caller's IP."""

import mimetypes
from urllib.parse import quote

from fastapi import Request, Response


def client_ip(request: Request) -> str | None:
    """The caller's IP: the first X-Forwarded-For hop (the original client,
    behind Fly's proxy), else the direct peer, else None. One definition for
    every reader: the consent trace and the Lemlist webhook's rate limit."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def file_download_response(
    filename: str, content: bytes, media_type: str | None = None
) -> Response:
    """An attachment Response with an RFC 6266 Content-Disposition: an ASCII
    fallback `filename="…"` + a UTF-8 `filename*=` that modern browsers
    prefer. HTTP headers are latin-1, so a raw non-ASCII name (accents,
    curly apostrophe ’ …) would crash the Response — this is the single
    source of that fix, shared by the documents and journey-step-attachment
    download endpoints (never recopy the disposition logic — it would
    recopy the latin-1 bug)."""
    media_type = media_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    ascii_fallback = (
        filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or "document"
    )
    disposition = f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )

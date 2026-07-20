"""Shared HTTP response helpers."""

import mimetypes
from urllib.parse import quote

from fastapi import Response


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

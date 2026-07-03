"""Document storage, mock-aware (same pattern as src/core/email.py).

Mock mode (default via MOCK_SERVICES / MOCK_STORAGE): an in-memory
{path: bytes} store, inspectable by tests. Real mode: Supabase Storage
with the SERVICE ROLE key on the private bucket. All functions are
blocking — call via asyncio.to_thread from async code.
"""

import logging
import re
import unicodedata

from supabase import Client, create_client

from src.core.config import get_settings

logger = logging.getLogger(__name__)

# Mock-mode sink, cleared per test by an autouse fixture.
mock_store: dict[str, bytes] = {}


def sanitize_filename(filename: str) -> str:
    """STRICT sanitization for the storage KEY only — the original
    filename stays in DB for display (the path is technical, the name
    is data). Kills path traversal (basename only) and anything beyond
    ASCII [A-Za-z0-9._-]."""
    name = filename.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if "." in name:
        stem, ext = name.rsplit(".", 1)
    else:
        stem, ext = name, ""
    stem = stem.strip("._") or "document"
    ext = ext.strip("._")
    return f"{stem}.{ext}" if ext else stem


def _is_mocked() -> bool:
    settings = get_settings()
    if settings.mock_storage is not None:
        return settings.mock_storage
    return settings.mock_services


def _client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError("Supabase Storage is not configured (URL / service role key).")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def upload(path: str, content: bytes, content_type: str) -> None:
    if _is_mocked():
        # Faithful to Supabase: a same-path upload is refused (409
        # Duplicate) — writers must delete the previous blob first. The
        # mock mirrors it so tests catch the 500 this causes in prod.
        if path in mock_store:
            raise FileExistsError(f"Storage object already exists: {path}")
        logger.info("MOCK storage upload path=%s size=%d", path, len(content))
        mock_store[path] = content
        return
    bucket = get_settings().supabase_storage_bucket
    _client().storage.from_(bucket).upload(path, content, {"content-type": content_type})


def download(path: str) -> bytes:
    if _is_mocked():
        if path not in mock_store:
            raise FileNotFoundError(path)
        return mock_store[path]
    bucket = get_settings().supabase_storage_bucket
    return _client().storage.from_(bucket).download(path)


def delete(path: str) -> None:
    if _is_mocked():
        mock_store.pop(path, None)
        return
    bucket = get_settings().supabase_storage_bucket
    _client().storage.from_(bucket).remove([path])

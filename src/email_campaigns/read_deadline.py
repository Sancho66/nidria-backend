"""A campaign's deadline follows its read-only provider calls into worker threads."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

_deadline: ContextVar[float | None] = ContextVar("campaign_deadline", default=None)


@contextmanager
def campaign_deadline(seconds: float) -> Iterator[None]:
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining() -> float | None:
    limit = _deadline.get()
    if limit is None:
        return None
    seconds = limit - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Campaign read deadline reached")
    return seconds


def wait_before_retry(seconds: float) -> None:
    left = remaining()
    if left is not None and left <= seconds:
        raise TimeoutError("Campaign read deadline reached")
    time.sleep(seconds)


class _DeadlineStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream) -> None:
        self.stream = stream

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.stream:
            remaining()
            yield chunk

    def close(self) -> None:
        self.stream.close()


def bound_response(response: httpx.Response) -> None:
    # Per-socket timeouts alone cannot bound a body that keeps trickling bytes.
    if remaining() is not None and isinstance(response.stream, httpx.SyncByteStream):
        response.stream = _DeadlineStream(response.stream)

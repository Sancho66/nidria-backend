"""`core.http.client_ip`: the caller's IP, one definition for the consent
trace and the Lemlist webhook's rate limit (decision of 08/10/2026).

Behind Fly's proxy the direct peer is the proxy: the first X-Forwarded-For
hop is the original client. Without the header, the peer; without a peer,
None.
"""

from starlette.requests import Request

from src.core.http import client_ip


def _request(headers: dict[str, str], client: tuple[str, int] | None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": client,
        }
    )


def test_the_first_forwarded_hop_is_the_client() -> None:
    proxied = _request({"X-Forwarded-For": " 203.0.113.7 , 10.0.0.1"}, ("10.0.0.1", 443))
    assert client_ip(proxied) == "203.0.113.7"
    assert client_ip(_request({"x-forwarded-for": "198.51.100.9"}, None)) == "198.51.100.9"


def test_without_the_header_the_peer_else_none() -> None:
    assert client_ip(_request({}, ("192.0.2.4", 5000))) == "192.0.2.4"
    assert client_ip(_request({}, None)) is None
    # An empty header is no hop: the peer.
    assert client_ip(_request({"x-forwarded-for": ""}, ("192.0.2.4", 5000))) == "192.0.2.4"

"""The agency's opposition list (`email_suppression`, decision of 08/10/2026).

Alexandre's decision: no refusal to delete a campaign; a DURABLE opposition
list instead. Each time an unsubscribe or a bounce is applied to a
membership, the person's address goes on the list of the campaign's agency;
the readiness rules read it (`readiness.load_client_facts`), so a person
whose address is listed is never added nor launched again, whatever was
deleted since (the campaign, the client record, a record merged away) and
whichever record carries the address now. The list keeps a fingerprint of
the address only, never the address itself; it goes with its agency.

One fingerprint, two faces that must agree: `email_hash` in Python (what is
written, and what the rules look up) and `email_hash_sql` in SQL (what the
add wizard's company counts compare in the database). Both remove the same
blanks (ASCII: space, tab, line breaks), then lower-case, then hash the
UTF-8 bytes with sha256 in hexadecimal. An address is a person's EFFECTIVE
one (rule N2, `client_profiles.identity`), plus the address the provider
named when it differs.

The reasons follow the blocking statuses of a membership: UNSUBSCRIBED
(`EMAIL_UNSUBSCRIBED`) outranks BOUNCED (`EMAIL_BOUNCED`), never the reverse
(decision A1); `EmailRepository.suppress` writes that order.
"""

import hashlib
import re

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

REASON_UNSUBSCRIBED = "UNSUBSCRIBED"
REASON_BOUNCED = "BOUNCED"
#: A blocking membership status -> the reason it puts on the list.
REASON_OF_STATUS = {"EMAIL_UNSUBSCRIBED": REASON_UNSUBSCRIBED, "EMAIL_BOUNCED": REASON_BOUNCED}
#: A reason on the list -> the blocking status it reads as (readiness).
STATUS_OF_REASON = {reason: status for status, reason in REASON_OF_STATUS.items()}

#: The blanks an address loses before it is hashed, the same set on both
#: faces: Python's `\s` and PostgreSQL's `\s` disagree beyond ASCII.
_BLANKS = r"[ \t\n\r\f\v]+"
_BLANKS_RE = re.compile(_BLANKS)


def normalized_address(address: str | None) -> str | None:
    """The address as it is fingerprinted: without any blank, lower-cased;
    None when nothing is left."""
    if address is None:
        return None
    cleaned = _BLANKS_RE.sub("", address).lower()
    return cleaned or None


def email_hash(address: str | None) -> str | None:
    """The fingerprint the opposition list keeps of an address: sha256, in
    hexadecimal, of the normalized address; None for no address."""
    cleaned = normalized_address(address)
    if cleaned is None:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def email_hash_sql(address: ColumnElement[str]) -> ColumnElement[str]:
    """`email_hash` in SQL, for an address column: the same blanks removed,
    lower-cased, sha256 of its UTF-8 bytes in hexadecimal. An empty address
    gives the fingerprint of the empty string, which no address has."""
    cleaned = func.lower(func.regexp_replace(address, _BLANKS, "", "g"))
    result: ColumnElement[str] = func.encode(func.sha256(func.convert_to(cleaned, "UTF8")), "hex")
    return result


__all__ = [
    "REASON_BOUNCED",
    "REASON_OF_STATUS",
    "REASON_UNSUBSCRIBED",
    "STATUS_OF_REASON",
    "email_hash",
    "email_hash_sql",
    "normalized_address",
]

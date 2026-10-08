"""EmailTemplateRenderer: a template plus a client's data, the exact email.

Pure and deterministic: no database, no network. A token is `{{namespace.key}}`
(spaces inside the braces allowed); anything else between double braces is an
error, never a guess. A variable that is unknown or has no value is reported:
the token stays visible in the output so a preview shows where, and the result
is not sendable.

A client, company or agency token may carry a fallback, `{{company.name |
"votre société"}}`: the text the email uses when the value is missing, which is
then not an error. The fallback is chosen by whoever writes the template; an AI
variable takes none.

Ported from Prism's `renderer.py` (reference commit 629a2b0). Changes
(decision A7 and spec section 8.3): the recipient is a client record
(`no_contact` is `no_client`, `contact.email` is `client.email`); a client with
no company is not blocked: `no_company` is reported only when a `company.*`
token of the template has no value and no fallback while the client has no
linked company at all (with several companies the value is simply missing);
variable sources are `client`, `company`, `agency` and `ai`; tokens are looked
up in one catalog (no project type). `parse_token`, `tokens_in`,
`content_hash` and `body_to_html` are Prism's, unchanged.

Errors are codes (`RenderError.kind`, plus the token): the frontend composes
the sentence. `RenderError.message` is only a technical English detail.
"""

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from src.email_campaigns.variables import RenderContext, lookup, value_of

ErrorKind = Literal[
    "missing_variable",
    "unknown_variable",
    "malformed_token",
    "ai_not_generated",
    "no_client",
    "no_company",
    "no_email",
]
ValueSource = Literal["client", "company", "agency", "ai"]

_BRACES = re.compile(r"\{\{(.*?)\}\}", re.S)
#: `namespace.key`, optionally `| "fallback"` (straight or curly double quotes).
_INNER = re.compile(r'^\s*([a-z]+(?:\.[a-z0-9_]+)+)\s*(?:\|\s*(?:"([^"]*)"|“([^”]*)”)\s*)?$')
_AI_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class RenderError:
    kind: ErrorKind
    token: str | None
    message: str


@dataclass(frozen=True)
class ResolvedVariable:
    token: str
    value: str | None
    source: ValueSource
    #: The template's fallback, when it replaced a missing value.
    fallback: str | None = None


@dataclass(frozen=True)
class RenderResult:
    subject: str
    body: str
    variables: list[ResolvedVariable] = field(default_factory=list)
    errors: list[RenderError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def resolved_map(self) -> dict[str, str | None]:
        """Token: the text the email carries (the value, else its fallback)."""
        return {v.token: v.value if v.value is not None else v.fallback for v in self.variables}


def parse_token(inner: str) -> tuple[str, str | None] | None:
    """What is between the braces, as (token, fallback or None); None when it
    is not a token."""
    match = _INNER.match(inner)
    if match is None:
        return None
    fallback = match.group(2) if match.group(2) is not None else match.group(3)
    return match.group(1), fallback


def tokens_in(*templates: str) -> list[str]:
    """Every well-formed token, in order of first appearance."""
    seen: list[str] = []
    for template in templates:
        for match in _BRACES.finditer(template or ""):
            parsed = parse_token(match.group(1))
            if parsed is not None and parsed[0] not in seen:
                seen.append(parsed[0])
    return seen


def is_valid_ai_key(key: str) -> bool:
    return bool(_AI_KEY.match(key))


def content_hash(subject: str, body: str, ai_prompts: dict[str, str]) -> str:
    """Identifies a step's content: subject, body and every AI prompt."""
    payload = json.dumps(
        {"subject": subject, "body": body, "ai": dict(sorted(ai_prompts.items()))},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class EmailTemplateRenderer:
    """Resolve the tokens of a subject and a body for one context."""

    def __init__(self, *, require_ai: bool = True) -> None:
        # Previews render before AI values exist: they report `ai_not_generated`
        # without blocking the deterministic part.
        self.require_ai = require_ai

    def render(self, subject: str, body: str, ctx: RenderContext) -> RenderResult:
        errors: list[RenderError] = []
        variables: dict[str, ResolvedVariable] = {}
        identity = ctx.identity
        if identity is None:
            errors.append(RenderError("no_client", None, "No client selected."))
        elif not identity.email:
            errors.append(RenderError("no_email", "client.email", "The client has no email."))
        no_company = identity is not None and not ctx.linked_companies

        def substitute(match: re.Match[str]) -> str:
            raw = match.group(0)
            parsed = parse_token(match.group(1))
            if parsed is None:
                _once(errors, RenderError("malformed_token", raw, f"Not a variable: {raw}"))
                return raw
            token, fallback = parsed
            namespace, _, key = token.partition(".")
            if namespace == "ai" and fallback is not None:
                _once(
                    errors,
                    RenderError("malformed_token", raw, f"An AI variable takes no fallback: {raw}"),
                )
                return raw
            if namespace == "ai":
                if ctx.ai_keys is not None and key not in ctx.ai_keys:
                    _once(
                        errors,
                        RenderError(
                            "unknown_variable",
                            token,
                            f"{token} is not an AI variable of this step.",
                        ),
                    )
                    return raw
                value = ctx.ai_values.get(key)
                if value is None or not str(value).strip():
                    if self.require_ai:
                        _once(
                            errors,
                            RenderError(
                                "ai_not_generated", token, f"{token} has not been generated."
                            ),
                        )
                    variables.setdefault(token, ResolvedVariable(token, None, "ai"))
                    return raw
                variables[token] = ResolvedVariable(token, str(value).strip(), "ai")
                return str(value).strip()
            definition = lookup(token)
            if definition is None:
                _once(errors, RenderError("unknown_variable", token, f"Unknown variable {token}."))
                return raw
            text = value_of(definition, ctx)
            source = definition.source
            if text is None and fallback is not None:
                variables[token] = ResolvedVariable(token, None, source, fallback)
                return fallback
            variables[token] = ResolvedVariable(token, text, source)
            if text is None:
                if source == "company" and no_company:
                    # Why the value is missing, said once: the client has no
                    # company at all (several companies: only the value is).
                    _once(
                        errors,
                        RenderError("no_company", None, "The client has no linked company."),
                    )
                _once(errors, RenderError("missing_variable", token, f"No value for {token}."))
                return raw
            return text

        rendered_subject = _BRACES.sub(substitute, subject or "")
        rendered_body = _BRACES.sub(substitute, body or "")
        return RenderResult(
            subject=rendered_subject.strip(),
            body=rendered_body.strip("\n"),
            variables=list(variables.values()),
            errors=errors,
        )


def _once(errors: list[RenderError], error: RenderError) -> None:
    if all((e.kind, e.token) != (error.kind, error.token) for e in errors):
        errors.append(error)


#: A web address in plain text: `http(s)://...` or `www....`, up to a space,
#: a quote or an angle bracket.
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
#: Punctuation that ends the sentence, not the address.
_URL_TRAIL = ".,;:!?)]}*"


def _linked(line: str) -> str:
    """One line of plain text as HTML: escaped, each web address a real link
    (`<a href>`), so the provider can track its clicks."""
    out: list[str] = []
    last = 0
    for match in _URL.finditer(line):
        url = match.group(0)
        while url and url[-1] in _URL_TRAIL:
            # A closing bracket is the address's own when it opened one.
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            url = url[:-1]
        if not url or url.lower() in ("http://", "https://", "www."):
            continue
        href = url if url.lower().startswith(("http://", "https://")) else f"https://{url}"
        out.append(html.escape(line[last : match.start()]))
        out.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(url)}</a>')
        last = match.start() + len(url)
    out.append(html.escape(line[last:]))
    return "".join(out)


def body_to_html(body: str) -> str:
    """The plain-text body as the provider's HTML: paragraphs on blank lines,
    line breaks kept, everything escaped, and every web address a link: the
    only HTML a body gets (Lemlist rewrites links to count their clicks)."""
    paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    return "".join(
        "<p>" + "<br>".join(_linked(line) for line in p.split("\n")) + "</p>" for p in paragraphs
    )

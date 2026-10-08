"""The readable text of an email received as HTML.

A reply's HTML comes from the client's mail client through the provider: it
is untrusted and never rendered by the app. Nidria keeps it as received and
shows this text instead: the new message, without the quoted history below
it when the mail client marked it (Gmail, Apple Mail, Outlook, Yahoo,
Thunderbird).

Ported as is from Prism's `html_text.py` (reference commit 629a2b0). Known
limit: the plain-text quote headers recognised are the English and French
ones only; a reply written in another language keeps its quoted history.
"""

import re
from html.parser import HTMLParser

_SKIPPED = {"style", "script", "head", "title"}
_VOID = {"br", "img", "hr", "meta", "link", "input", "wbr", "col", "area", "base", "source"}
_BREAKS = {
    "p",
    "div",
    "br",
    "li",
    "tr",
    "table",
    "ul",
    "ol",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
}
#: Classes and ids mail clients put on the history they quote.
_QUOTE_MARKERS = (
    "gmail_quote",
    "yahoo_quoted",
    "moz-cite-prefix",
    "divrplyfwdmsg",
    "appendonsend",
    "mail-quote",
)
#: A line that introduces quoted history in plain text.
_QUOTE_LINE = re.compile(
    r"^\s*(?:On .{3,200} wrote:|Le .{3,200} a écrit\s?:|-{2,}\s*Original Message\s*-{2,}"
    r"|-{2,}\s*Message d'origine\s*-{2,}|From:\s.+|De\s?:\s.+)\s*$",
    re.IGNORECASE,
)
_MAX_TEXT = 50_000


class _Text(HTMLParser):
    def __init__(self, drop_quotes: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.drop_quotes = drop_quotes
        self._skipped = 0
        self._quoted = 0

    def _opens_quote(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag == "blockquote":
            return True
        marks = " ".join((value or "").lower() for name, value in attrs if name in ("class", "id"))
        return any(marker in marks for marker in _QUOTE_MARKERS)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skipped += 1
            return
        if self.drop_quotes and tag not in _VOID:
            if self._quoted:
                self._quoted += 1
                return
            if self._opens_quote(tag, attrs):
                self._quoted = 1
                return
        if tag in _BREAKS and not self._quoted:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self._quoted and not self._skipped:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skipped = max(self._skipped - 1, 0)
            return
        if self._quoted and tag not in _VOID:
            self._quoted -= 1
            return
        if tag in _BREAKS and tag != "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipped and not self._quoted:
            self.parts.append(data)


def _clean(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r", "").replace("\xa0", " ").split("\n")]
    joined = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _cut_plain_quote(text: str) -> str:
    """Drop the history a plain-text reply quotes: from its introducing line
    ("On ... wrote:") or from the first run of "> " lines."""
    kept: list[str] = []
    for line in text.split("\n"):
        if _QUOTE_LINE.match(line) or line.lstrip().startswith(">"):
            break
        kept.append(line)
    return "\n".join(kept)


def html_to_text(html: str | None, *, drop_quotes: bool = True) -> str:
    """The message as plain text; with `drop_quotes`, without the history it
    quotes, unless nothing would be left: then everything."""
    if not html:
        return ""
    parser = _Text(drop_quotes)
    parser.feed(html)
    parser.close()
    text = _clean("".join(parser.parts))
    if drop_quotes:
        reply = _clean(_cut_plain_quote(text))
        if reply:
            text = reply
        elif not text:
            return html_to_text(html, drop_quotes=False)
    return text[:_MAX_TEXT]

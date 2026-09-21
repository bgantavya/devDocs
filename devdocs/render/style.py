"""The output contract for model-written prose, enforced rather than requested.

Models are asked for a small line-tagged reply instead of free Markdown:

    PURPOSE: <sentences>          file, module
    SYMBOL <qualname>: <sentence> file
    NOTE: <sentence>              file, module, architecture
    RELATION: <sentence>          module
    OVERVIEW: <sentences>         architecture
    FLOW: <sentence>              architecture (ordered steps)
    EXTEND: <sentence>            architecture

Tags cost a few tokens where Markdown or JSON would cost many, and the reply
parses leniently. The guard then enforces what a prompt can only ask for:
plain professional English, no emoji or filler, bounded length, and — the one
that matters most — no names the code does not define. A `code` span the
symbol table cannot vouch for removes the claim it appears in, because an
invented API is worse in docs than a missing sentence.

Lenient means the deviations models actually make still parse: bold, bulleted
or backticked tags, a reply wrapped in a code fence, bullets under a NOTE, and
untagged prose where the purpose belongs. Code spans are the one thing the
guard never edits: every rewrite works on the prose around them.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice

from devdocs.render.text import code_spans

__all__ = ["Reply", "StyleGuard", "acceptable", "parse_reply"]

KINDS = ("file", "module", "architecture")

# Tag to Reply field, per kind.
_TAGS: dict[str, dict[str, str]] = {
    "file": {"PURPOSE": "purpose", "SYMBOL": "symbols", "NOTE": "notes"},
    "module": {"PURPOSE": "purpose", "RELATION": "relations", "NOTE": "notes"},
    "architecture": {
        "OVERVIEW": "purpose", "FLOW": "flow", "EXTEND": "extend", "NOTE": "notes",
    },
}
_ONE_SENTENCE = frozenset({"SYMBOL", "NOTE", "RELATION", "FLOW", "EXTEND"})

_HEAD = re.compile(
    r"[ \t]*(?:#{1,6}[ \t]+)?(?:(?:[-*+•]|\d{1,3}[.)])[ \t]+)?"
    r"(?P<em>\*\*|__|[*_`])?"
    r"(?P<tag>purpose|overview|symbol|note|relation|flow|extend)s?(?![^\W_])",
    re.IGNORECASE,
)
_SEP = re.compile(r"[ \t]*(?::|[\u2013\u2014]|-(?=\s|$))[ \t]*")
_NAME = re.compile(
    r"[ \t]*:?[ \t]*(?:\*\*|\*)?"
    r"(?:`+(?P<tick>[^`]+)`+|(?P<bare>[\w$#<>]+(?:(?:\.|::)[\w$#<>]+)*))"
    r"(?:\([^()]*\))?(?:\*\*|\*)?"
)
_CALL = re.compile(r"\([^()]*\)$")
_LIST = re.compile(r"[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]+")
_HEADING = re.compile(r"[ \t]*#{1,6}(?:[ \t]|$)")
_RULE = re.compile(r"[ \t]*(?:[-*_][ \t]*){3,}$")
_FENCE = re.compile(r"[ \t]{0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
_PREAMBLE = re.compile(
    r"(?:sure|certainly|of course|absolutely|here(?:['\u2019]s|\s+is|\s+are)"
    r"|below\s+(?:is|are))\b",
    re.IGNORECASE,
)
_ENDED = (".", "!", "?", ":", ";", ",")


@dataclass(frozen=True)
class Reply:
    """A parsed model reply. Every string is one cleaned sentence or more."""

    purpose: str = ""                           # PURPOSE, or OVERVIEW
    symbols: tuple[tuple[str, str], ...] = ()   # (qualname, sentence)
    notes: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    flow: tuple[str, ...] = ()
    extend: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()               # what the guard removed, and why


def parse_reply(text: str, kind: str) -> Reply:
    """Read a tagged reply leniently; tags not valid for `kind` are ignored.

    An untagged line continues the field above it, a bullet under a
    one-sentence tag starts another item of that tag, and a blank line ends
    the field. Only whitespace is normalised; cleaning is StyleGuard's job.
    """
    tags = _TAGS.get(kind, _TAGS["file"])
    drafts: list[_Draft] = []
    lead: list[str] = []
    current: _Draft | None = None
    bullets = ""            # one-sentence tag whose bullets start new items
    tagged = False
    for line in _unfence(text.splitlines()):
        head = _tag_line(line)
        blank = head is None and _is_blank(line)
        if head is not None and head.bare and head.tag not in tags:
            head, blank = None, True    # a heading named after another kind's tag
        if blank:
            if current is not None and current.text:
                current = None
            if not tagged:
                lead.append("")
            continue
        if head is not None:
            tagged = True
            bullets = head.tag if head.tag in _ONE_SENTENCE else ""
            current = _start(tags.get(head.tag, ""), head.text, head.tag)
            if current is not None:
                drafts.append(current)
            continue
        marker = _LIST.match(line)
        if marker is not None and bullets:
            current = _start(tags.get(bullets, ""), line[marker.end():], bullets)
            if current is not None:
                drafts.append(current)
        elif current is not None:
            current.add(line[marker.end():] if marker else line, item=bool(marker))
        elif not tagged:
            lead.append(line)
        else:
            bullets = ""

    purposes = [draft.text for draft in drafts if draft.field == "purpose"]
    purpose = _joined(purposes) if purposes else _lead(lead)

    def texts(name: str) -> tuple[str, ...]:
        return tuple(_squash(d.text) for d in drafts if d.field == name and d.text)

    return Reply(
        purpose=_squash(purpose),
        symbols=tuple(
            (d.name, _squash(d.text))
            for d in drafts if d.field == "symbols" and d.text
        ),
        notes=texts("notes"),
        relations=texts("relations"),
        flow=texts("flow"),
        extend=texts("extend"),
    )


def acceptable(reply: Reply, kind: str) -> bool:
    """True when the reply is worth keeping: it has a purpose or overview."""
    return bool(reply.purpose.strip())


@dataclass
class _Draft:
    """One field as it is read: a tag's text plus the lines that continue it."""

    field: str
    name: str = ""
    text: str = ""

    def add(self, text: str, *, item: bool = False) -> None:
        """Append a line; an `item` (a bullet) reads as a sentence of its own."""
        text = text.strip()
        if not text:
            return
        if self.text and item and not self.text.endswith(_ENDED):
            self.text += "."
        self.text = f"{self.text} {text}" if self.text else text


@dataclass(frozen=True)
class _Head:
    """A line that opens with a tag. `bare` marks a tag with no separator."""

    tag: str
    text: str
    bare: bool = False


def _tag_line(line: str) -> _Head | None:
    """The tag `line` opens with, however decorated, else None.

    For SYMBOL, `text` keeps the name and its separator for `_symbol`.
    """
    match = _HEAD.match(line)
    if match is None:
        return None
    tag = match.group("tag").upper()
    emphasis = match.group("em") or ""
    rest = _drop_prefix(line[match.end():], emphasis)
    if tag == "SYMBOL":
        return _Head(tag, rest)
    separator = _SEP.match(rest)
    if separator is None:
        return None if rest.strip() else _Head(tag, "", bare=True)
    return _Head(tag, _drop_prefix(rest[separator.end():], emphasis).strip())


def _drop_prefix(text: str, prefix: str) -> str:
    return text[len(prefix):] if prefix and text.startswith(prefix) else text


def _start(field_name: str, text: str, tag: str) -> _Draft | None:
    """A new draft for `tag`, or None when the tag is ignored or unreadable."""
    if not field_name:
        return None
    if tag != "SYMBOL":
        draft = _Draft(field_name)
        draft.add(text)
        return draft
    symbol = _symbol(text)
    if symbol is None:
        return None
    draft = _Draft(field_name, symbol[0])
    draft.add(symbol[1])
    return draft


def _symbol(rest: str) -> tuple[str, str] | None:
    """(name, text) from what follows SYMBOL, e.g. "Cart.add: …" or
    "`Cart.add()` — …"; None when no name can be read."""
    match = _NAME.match(rest)
    if match is None:
        return None
    ticked = match.group("tick")
    name = _CALL.sub("", (ticked or match.group("bare")).strip()).strip()
    rest = rest[match.end():]
    separator = _SEP.match(rest)
    if separator is not None:
        rest = rest[separator.end():]
    elif rest.strip() and not (ticked and rest[0].isspace()):
        return None
    text = re.sub(r"^(?:\*\*|__|\*)", "", rest).strip()
    return (name, text) if name else None


def _is_blank(line: str) -> bool:
    """True for lines that end a field: empty, a rule, or a non-tag heading."""
    return not line.strip() or bool(_RULE.match(line) or _HEADING.match(line))


def _unfence(lines: list[str]) -> list[str]:
    """`lines` without code fences.

    A fenced block wraps the reply when it holds a tag line and either opens
    the reply or no tag stands outside a fence; its lines are kept. Any other
    block is code, never prose, and is dropped. Each fence reads as a blank.
    """
    blocks: list[tuple[int, int]] = []      # opener and closer line indexes
    index = 0
    while index < len(lines):
        opener = _FENCE.match(lines[index])
        if opener is None or "`" in opener.group("info"):
            index += 1
            continue
        marker = opener.group("marker")
        end = index + 1
        while end < len(lines) and not _closes(lines[end], marker):
            end += 1
        blocks.append((index, end))
        index = end + 1
    if not blocks:
        return lines

    fenced = [False] * len(lines)
    for start, end in blocks:
        fenced[start:end + 1] = [True] * (min(end + 1, len(lines)) - start)
    outside = any(
        not fenced[i] and _tag_line(line) is not None for i, line in enumerate(lines)
    )
    first = next((i for i, line in enumerate(lines) if line.strip()), -1)
    out: list[str] = []
    done = 0
    for start, end in blocks:
        out += [*lines[done:start], ""]
        block = lines[start + 1:end]
        wraps = start == first or not outside
        if wraps and any(_tag_line(line) is not None for line in block):
            out += ["" if _FENCE.match(line) else line for line in block]
            out.append("")
        done = end + 1
    return out + lines[done:]


def _closes(line: str, marker: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= len(marker) and set(stripped) == {marker[0]}


def _joined(parts: Iterable[str]) -> str:
    """Separate statements joined as sentences."""
    draft = _Draft("")
    for part in parts:
        draft.add(part, item=True)
    return draft.text


def _lead(lines: Sequence[str]) -> str:
    """Untagged text before the first tag, for a reply that ignored the format.

    Paragraphs that only introduce something else ("Sure! Here is…:") are
    left out.
    """
    paragraphs: list[str] = []
    draft = _Draft("")
    for line in [*lines, ""]:
        if line.strip():
            marker = _LIST.match(line)
            draft.add(line[marker.end():] if marker else line, item=bool(marker))
            continue
        text = draft.text
        if text and not text.endswith(":") and not _PREAMBLE.match(text):
            paragraphs.append(text)
        draft = _Draft("")
    return _joined(paragraphs)


def _squash(text: str) -> str:
    """`text` on one line with single spaces; code spans keep their spacing."""
    return "".join(
        chunk if code else re.sub(r"\s+", " ", chunk)
        for code, chunk in code_spans(text)
    ).strip()


@dataclass(frozen=True)
class _Limits:
    """Per-kind bounds: purpose sentences and characters, and note count."""

    sentences: int
    chars: int
    notes: int


_LIMITS = {
    "file": _Limits(3, 600, 5),
    "module": _Limits(4, 800, 4),
    "architecture": _Limits(6, 1200, 4),
}
_ITEM_CHARS = 240
_ITEMS = (                         # Reply field, label, count cap (0: per kind)
    ("notes", "note", 0),
    ("relations", "relation", 6),
    ("flow", "flow", 8),
    ("extend", "extend", 4),
)
_KEYWORDS = frozenset({
    "true", "false", "null", "nil", "none", "undefined", "NaN", "None", "True",
    "False", "self", "this", "cls", "super", "Self",
})
_EXTENSIONS = (
    "py|pyi|pyx|ipynb|js|mjs|cjs|jsx|ts|mts|cts|tsx|go|rs|java|kt|kts|rb|php|cs"
    "|swift|c|h|cc|cpp|cxx|hpp|hh|sh|bash|zsh|fish|ps1|bat|cmd|sql|scala|ex|exs"
    "|erl|lua|dart|vue|svelte|toml|ini|cfg|conf|yaml|yml|json|jsonc|json5|env"
    "|lock|xml|html|htm|css|scss|sass|less|md|mdx|rst|txt|adoc|csv|tsv|log"
    "|proto|graphql|gql|tf|hcl|gradle|properties|mod|sum|db|sqlite|pem|crt|key"
    "|svg|png|jpg|jpeg|gif|ico|pdf|zip|gz|tgz|tar|whl|jar|so|dll|dylib|wasm"
)
_LITERAL = re.compile(
    r"\+?\d.*"                        # numbers, versions, status codes
    r"|[\"'].*"                       # quoted strings
    r"|.*[\s/\\].*"                   # paths, URLs, expressions
    r"|[-.].*"                        # flags, extensions, relative imports
    r"|[A-Z][A-Z0-9_]*"               # env vars, constants, HTTP methods
    rf"|[\w.-]+\.(?i:{_EXTENSIONS})",  # file names
    re.DOTALL,
)
_SIGILS = re.compile(r"^[@*&!~^%]+")
_SUFFIX = re.compile(r"(?:\([^()]*\)|<[^<>]*>|\[[^\[\]]*\])$")
_RECEIVER = re.compile(r"^(?:self|this|cls|super|Self)(?:\.|::|->)")
_IDENTIFIER = re.compile(
    r"(?:[^\W\d]|\$)[\w$]*(?:(?:\.|::|#|->)(?:[^\W\d]|\$)[\w$]*)*"
)
_SEGMENT = re.compile(r"\.|::|#|->")
_HEDGE = re.compile(
    r"\b(?:probably|likely|presumably|possibly|perhaps|it\s+seems|seems\s+to"
    r"|appears\s+to|might\s+be|may\s+be|i\s+think|i\s+believe|unclear)\b",
    re.IGNORECASE,
)
_META = re.compile(
    r"\b(?:as\s+an\s+ai|provided\s+(?:code|source)|code\s+snippet"
    r"|the\s+given\s+file|based\s+on\s+the\s+source|from\s+the\s+code\s+above)\b",
    re.IGNORECASE,
)


class StyleGuard:
    """Cleans and grounds a parsed reply.

    `known(name)` answers whether an identifier exists in the project (its
    symbols, file names, imported modules) or the language's builtins; it is
    supplied by the pipeline, which owns the symbol tables.
    """

    def __init__(self, known: Callable[[str], bool] | None = None) -> None:
        self.known = known

    def clean(self, text: str) -> str:
        """Normalise one field's text; "" when nothing professional remains.

        Runs to a fixed point, so cleaning its own output changes nothing.
        """
        for _ in range(8):
            cleaned = _clean(text)
            if cleaned == text:
                break
            text = cleaned
        return text

    def apply(
        self, reply: Reply, kind: str, *, allowed_symbols: Collection[str] = ()
    ) -> Reply:
        """Clean every field, enforce limits, and drop ungrounded claims.

        Each removal is listed in `dropped` as "<field>: <reason>[: <text>]",
        field by field in reply order. Fields of another kind are dropped too.
        """
        limits = _LIMITS.get(kind, _LIMITS["file"])
        used = set(_TAGS.get(kind, _TAGS["file"]).values())
        dropped = list(reply.dropped)
        label = "overview" if kind == "architecture" else "purpose"
        purpose = self._purpose(reply.purpose, label, limits, dropped)

        symbols: tuple[tuple[str, str], ...] = ()
        if "symbols" in used:
            symbols = self._symbols(reply.symbols, allowed_symbols, dropped)
        else:
            dropped += [f"symbol: not used for {kind}: {n}" for n, _ in reply.symbols]

        items: dict[str, tuple[str, ...]] = {}
        for name, item_label, cap in _ITEMS:
            texts: tuple[str, ...] = getattr(reply, name)
            if name in used:
                items[name] = self._items(
                    texts, item_label, cap or limits.notes, dropped
                )
            else:
                items[name] = ()
                dropped += [f"{item_label}: not used for {kind}: {t}" for t in texts]

        return Reply(
            purpose=purpose,
            symbols=symbols,
            notes=items["notes"],
            relations=items["relations"],
            flow=items["flow"],
            extend=items["extend"],
            dropped=tuple(dropped),
        )

    def _purpose(
        self, text: str, label: str, limits: _Limits, dropped: list[str]
    ) -> str:
        """The purpose without hedged or meta sentences, within the kind's
        sentence and character limits; ungrounded spans lose their backticks."""
        cleaned = self.clean(text)
        if not cleaned:
            if text.strip():
                dropped.append(f"{label}: empty")
            return ""
        kept: list[_Text] = []
        for sentence in _Text.of(cleaned).sentences():
            flaw = _flaw(sentence.body)
            if flaw:
                dropped.append(f"{label}: {flaw}: {sentence}")
            else:
                kept.append(self._ground(sentence, label, dropped))
        return _fit(kept, limits.sentences, limits.chars, label, dropped)

    def _symbols(
        self,
        pairs: Iterable[tuple[str, str]],
        allowed: Collection[str],
        dropped: list[str],
    ) -> tuple[tuple[str, str], ...]:
        """Descriptions of allowed symbols only, under their full qualnames;
        the first usable description of a symbol wins."""
        resolve = _resolver(allowed)
        kept: dict[str, str] = {}
        for name, text in pairs:
            qualname = resolve(name)
            if qualname is None:
                dropped.append(f"symbol: unknown symbol {name}")
                continue
            label = f"symbol {qualname}"
            if qualname in kept:
                dropped.append(f"{label}: duplicate: {text}")
                continue
            first = self._first(text, label, dropped)
            if first is None:
                continue
            sentence, rest = first
            if rest:
                dropped.append(f"{label}: too long: {rest}")
            described = _bounded(self._ground(sentence, label, dropped), label, dropped)
            if described:
                kept[qualname] = described
        return tuple(kept.items())

    def _items(
        self, texts: Iterable[str], label: str, cap: int, dropped: list[str]
    ) -> tuple[str, ...]:
        """One-sentence items: unhedged, grounded, bounded, unique, at most `cap`."""
        kept: list[str] = []
        seen: set[str] = set()
        for text in texts:
            first = self._first(text, label, dropped)
            if first is None:
                continue
            sentence, rest = first
            flaw = _flaw(sentence.body) or self._ungrounded(sentence)
            if flaw:
                dropped.append(f"{label}: {flaw}: {sentence}")
                continue
            if rest:
                dropped.append(f"{label}: too long: {rest}")
            item = _bounded(sentence, label, dropped)
            if not item:
                continue
            if item.casefold() in seen:
                dropped.append(f"{label}: duplicate: {item}")
            elif len(kept) >= cap:
                dropped.append(f"{label}: too many: {item}")
            else:
                seen.add(item.casefold())
                kept.append(item)
        return tuple(kept)

    def _first(
        self, text: str, label: str, dropped: list[str]
    ) -> tuple[_Text, str] | None:
        """The cleaned first sentence of `text` and what followed it."""
        sentences = _Text.of(self.clean(text)).sentences()
        if not sentences:
            dropped.append(f"{label}: empty")
            return None
        return sentences[0], " ".join(str(s) for s in sentences[1:])

    def _ground(self, sentence: _Text, label: str, dropped: list[str]) -> _Text:
        """`sentence` with the backticks of every ungrounded span removed."""
        if self.known is None or not sentence.spans:
            return sentence
        pieces = sentence.body.split(_SLOT)
        body = [pieces[0]]
        spans: list[str] = []
        for span, piece in zip(sentence.spans, pieces[1:], strict=False):
            name = _inner(span)
            if self._grounded(name):
                body.append(_SLOT)
                spans.append(span)
            else:
                dropped.append(f"{label}: ungrounded {name}")
                body.append(name.replace(_SLOT, ""))
            body.append(piece)
        return _Text("".join(body), tuple(spans))

    def _ungrounded(self, sentence: _Text) -> str:
        """"ungrounded <name>" for the first span `known` cannot vouch for."""
        for span in sentence.spans:
            name = _inner(span)
            if not self._grounded(name):
                return f"ungrounded {name}"
        return ""

    def _grounded(self, name: str) -> bool:
        """True when `name` looks literal or `known` vouches for it: whole, by
        its last segment, or by its first."""
        if self.known is None or name in _KEYWORDS or _LITERAL.fullmatch(name):
            return True
        core = _identifier(name)
        if not core:
            return True
        parts = [part for part in _SEGMENT.split(core) if part]
        try:
            return any(
                self.known(candidate)
                for candidate in dict.fromkeys((core, parts[-1], parts[0]))
            )
        except Exception:
            return True     # a broken lookup must not erase prose


def _inner(span: str) -> str:
    """A code span's text, without its backticks or padding."""
    return span.strip("`").strip()


def _identifier(name: str) -> str:
    """The identifier a code span names, or "" when it names none.

    Sigils, a receiver (`self.`), and trailing calls, generics or indexing
    are stripped: `@app.route`, `self._lock`, `Cart.add()`, `Vec<T>`.
    """
    core = _SIGILS.sub("", name)
    previous = ""
    while core != previous:
        previous = core
        core = _SUFFIX.sub("", core)
    core = _RECEIVER.sub("", core)
    return core if _IDENTIFIER.fullmatch(core) else ""


def _parts(name: str) -> tuple[str, ...]:
    return tuple(part for part in _SEGMENT.split(name) if part)


def _resolver(allowed: Collection[str]) -> Callable[[str], str | None]:
    """Maps a name a reply used to the allowed qualname it means.

    Exact names win; `Cart::add` matches `Cart.add`. Otherwise a name that
    is a suffix of exactly one allowed qualname (`add` of `Cart.add`), or has
    one as its suffix (`cart.Cart.add`), resolves to it.
    """
    names = sorted(set(allowed))
    exact = set(names)
    by_parts: dict[tuple[str, ...], str] = {}
    by_last: dict[str, list[tuple[str, ...]]] = {}
    for name in names:
        parts = _parts(name)
        if parts and parts not in by_parts:
            by_parts[parts] = name
            by_last.setdefault(parts[-1], []).append(parts)

    def resolve(name: str) -> str | None:
        name = _CALL.sub("", name.strip().strip("`").strip()).strip()
        if name in exact:
            return name
        parts = _parts(name)
        if not parts:
            return None
        if parts in by_parts:
            return by_parts[parts]
        matches = [
            by_parts[candidate]
            for candidate in by_last.get(parts[-1], ())
            if candidate[-len(parts):] == parts or parts[-len(candidate):] == candidate
        ]
        return matches[0] if len(matches) == 1 else None

    return resolve


def _flaw(body: str) -> str:
    """"hedged" or "meta" when a sentence's prose (not its code) is either."""
    if _HEDGE.search(body):
        return "hedged"
    if _META.search(body):
        return "meta"
    return ""


def _fit(
    sentences: Sequence[_Text], count: int, chars: int, label: str, dropped: list[str]
) -> str:
    """The leading sentences that fit `count` and `chars`; a first sentence
    longer than `chars` is cut at a word."""
    kept: list[str] = []
    size = 0
    closed = False
    for sentence in sentences:
        text = str(sentence)
        extra = len(text) + (1 if kept else 0)
        if not closed and len(kept) < count and size + extra <= chars:
            kept.append(text)
            size += extra
            continue
        if not closed and not kept:
            head, text = _cut(sentence, chars)
            if head:
                kept.append(head)
        closed = True
        if text:
            dropped.append(f"{label}: too long: {text}")
    return " ".join(kept)


def _bounded(sentence: _Text, label: str, dropped: list[str]) -> str:
    """`sentence` within the one-sentence limit; "" when no word fits."""
    head, tail = _cut(sentence, _ITEM_CHARS)
    if tail:
        dropped.append(f"{label}: too long: {tail}")
    return head


_SLOT = ""            # stands in for a code span while the prose is edited
_CONTENT = re.compile(r"[^\W_]|")
_TRAILING_WORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on",
    "or", "that", "the", "to", "which", "with",
})


@dataclass(frozen=True)
class _Text:
    """Prose with each code span swapped for `_SLOT`, so no edit reaches code."""

    body: str
    spans: tuple[str, ...] = ()

    @classmethod
    def of(cls, text: str, scrub: Callable[[str], str] | None = None) -> _Text:
        """Protect the code spans of `text`, scrubbing the prose between them."""
        body: list[str] = []
        spans: list[str] = []
        for code, chunk in code_spans(text):
            if code:
                body.append(_SLOT)
                spans.append(chunk)
            else:
                body.append((scrub(chunk) if scrub else chunk).replace(_SLOT, ""))
        return cls("".join(body), tuple(spans))

    def __str__(self) -> str:
        pieces = self.body.split(_SLOT)
        out = [pieces[0]]
        for index, piece in enumerate(pieces[1:]):
            out.append(self.spans[index] if index < len(self.spans) else "")
            out.append(piece)
        return "".join(out)

    def __len__(self) -> int:
        return len(str(self))

    def sentences(self) -> list[_Text]:
        spans = iter(self.spans)
        return [
            _Text(body, tuple(islice(spans, body.count(_SLOT))))
            for body in _split(self.body)
        ]

    def words(self) -> list[str]:
        """The words of a single-spaced text, code restored."""
        spans = iter(self.spans)
        return [
            str(_Text(word, tuple(islice(spans, word.count(_SLOT)))))
            for word in self.body.split(" ")
        ]


def _cut(sentence: _Text, limit: int) -> tuple[str, str]:
    """`sentence` cut at a word to fit `limit` with a closing ".", and the
    words cut off; ("", everything) when not even one word fits."""
    text = str(sentence)
    if len(text) <= limit:
        return text, ""
    words = sentence.words()
    kept: list[str] = []
    size = 0
    for word in words:
        extra = len(word) + (1 if kept else 0)
        if size + extra > limit - 1:
            break
        kept.append(word)
        size += extra
    while kept and kept[-1].strip(",;:").lower() in _TRAILING_WORDS:
        kept.pop()
    head = " ".join(kept).rstrip(",;:\u2013\u2014- ")
    if not _CONTENT.search(head):
        return "", text
    if not head.endswith((".", "!", "?")):
        head += "."
    return head, " ".join(words[len(kept):])


def _clean(text: str) -> str:
    """One pass of `StyleGuard.clean`."""
    prose = _Text.of(text, _scrub)
    body = _unwrap_quotes(" ".join(_unmark(prose.body).split()))
    body = _punctuate(" ".join(filter(None, map(_tidy, _split(body)))))
    if not _CONTENT.search(body):
        return ""
    return str(_Text(_terminate(_capitalise(body)), prose.spans))


def _scrub(chunk: str) -> str:
    """Prose in NFC without emoji, pictographs or invisible characters."""
    return "".join(
        char for char in unicodedata.normalize("NFC", chunk) if not _decorative(char)
    )


@lru_cache(maxsize=4096)
def _decorative(char: str) -> bool:
    """True for emoji, pictographs, private-use glyphs, and invisible format
    and control characters. Emoji blocks are matched by range as well, so an
    emoji newer than Python's Unicode tables is removed all the same."""
    code = ord(char)
    if 0x1F000 <= code <= 0x1FBFF or 0x2600 <= code <= 0x27BF:
        return True
    if 0xFE00 <= code <= 0xFE0F or 0xE0000 <= code <= 0xE01EF or code == 0x20E3:
        return True
    category = unicodedata.category(char)
    if category in ("So", "Co", "Cs"):
        return char != "°"
    if category == "Cf":
        return char != "‌"     # ZWNJ spells words in Persian and Indic scripts
    return category == "Cc" and char not in "\t\n\r"


_HEADING_LINE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", re.M)
_MARKER = re.compile(r"^[ \t]*(?:(?:[-*+•]|\d{1,3}[.)])[ \t]+|>[ \t]*)+", re.M)
_IMAGE = re.compile(
    r'!\[[^\[\]]{0,300}\]\([^()\s]{0,500}(?:\s+"[^"]{0,300}")?\)'
)
_LINK = re.compile(
    r'\[([^\[\]]{0,300})\]\([^()\s]{0,500}(?:\s+"[^"]{0,300}")?\)'
)
_REFERENCE = re.compile(r"\[([^\[\]]{1,300})\]\[[^\[\]]{0,100}\]")
_FOOTNOTE = re.compile(r"\[\^[^\[\]\s]{1,50}\]")
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^<>\s]{1,500})>")
_HTML = re.compile(
    r"<!--(?:(?!-->)[^]){0,2000}-->"
    r"|</?(?P<name>[A-Za-z][A-Za-z0-9]{0,15})(?:\s[^<>]{0,500})?/?>"
)
_BLOCK_TAGS = frozenset({
    "article", "blockquote", "br", "dd", "div", "dl", "dt", "footer", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
})
_INLINE_TAGS = frozenset({
    "a", "abbr", "aside", "audio", "b", "big", "button", "caption", "center",
    "cite", "code", "col", "colgroup", "del", "details", "em", "figcaption",
    "figure", "font", "form", "i", "iframe", "img", "input", "ins", "kbd",
    "label", "link", "main", "mark", "meta", "nav", "option", "picture", "q",
    "s", "samp", "script", "select", "small", "source", "span", "strike",
    "strong", "style", "sub", "summary", "sup", "tbody", "textarea", "tfoot",
    "thead", "tt", "u", "var", "video", "wbr",
})
_BOLD = re.compile(r"\*\*(?=[^\s*])(.{1,300}?)(?<=[^\s*])\*\*")
_STRONG = re.compile(r"(?<![\w\\])__(?=[^\s_])(.{1,300}?)(?<=[^\s_])__(?!\w)")
_EMPHASIS = re.compile(r"(?<![\w*\\])\*(?=[^\s*])(.{1,300}?)(?<=[^\s*])\*(?![\w*])")
_ITALIC = re.compile(r"(?<![\w\\])_(?=[^\s_])(.{1,300}?)(?<=[^\s_])_(?!\w)")
_STRIKE = re.compile(r"~~(?=\S)(.{1,300}?)(?<=\S)~~")
_DUNDER = re.compile(r"[a-z0-9_]+")
_DUNDERS = frozenset({
    "abs", "add", "aenter", "aexit", "aiter", "all", "anext", "annotations",
    "await", "bool", "bytes", "call", "class", "contains", "copy", "debug",
    "deepcopy", "del", "delattr", "delitem", "dict", "dir", "doc", "enter", "eq",
    "exit", "file", "float", "format", "ge", "getattr", "getattribute",
    "getitem", "gt", "hash", "index", "init", "init_subclass", "int", "iter",
    "le", "len", "lt", "main", "missing", "module", "name", "ne", "new", "next",
    "post_init", "qualname", "repr", "reversed", "set_name", "setattr",
    "setitem", "slots", "str", "version",
})


def _unmark(body: str) -> str:
    """Prose without Markdown or HTML. Emphasis and link text stay; the syntax
    goes, and so does every backtick no code span claimed."""
    previous = ""
    while body != previous:
        previous = body
        body = _MARKER.sub("", _HEADING_LINE.sub(r"\1", body))
        body = _IMAGE.sub("", body)
        body = _LINK.sub(r"\1", body)
        body = _REFERENCE.sub(r"\1", body)
        body = _FOOTNOTE.sub("", body)
        body = _AUTOLINK.sub(r"\1", body)
        body = _HTML.sub(_html, body)
        body = _BOLD.sub(r"\1", body)
        body = _STRONG.sub(_strong, body)
        body = _EMPHASIS.sub(r"\1", body)
        body = _ITALIC.sub(r"\1", body)
        body = _STRIKE.sub(r"\1", body)
    return body.replace("`", "")


def _html(match: re.Match[str]) -> str:
    """A known HTML tag removed (a block tag leaves a space); `<T>` stays."""
    name = (match.group("name") or "").lower()
    if name in _BLOCK_TAGS:
        return " "
    return "" if not name or name in _INLINE_TAGS else match.group()


def _strong(match: re.Match[str]) -> str:
    """`__text__` as text, except a dunder name: a known one such as
    `__init__`, or one used as code (`obj.__dict__`, `__main__.py`)."""
    inner = match.group(1)
    start, end = match.span()
    attached = match.string[start - 1:start] == "." or (
        match.string[end:end + 1] in (".", "(")
    )
    if _DUNDER.fullmatch(inner) and (inner in _DUNDERS or attached):
        return match.group()
    return inner


def _unwrap_quotes(body: str) -> str:
    """`body` without double quotes wrapped around all of it."""
    if (
        len(body) > 1 and body[0] in "\"\u201c" and body[-1] in "\"\u201d"
        and not any(quote in body[1:-1] for quote in "\"\u201c\u201d")
    ):
        return body[1:-1].strip()
    return body


_BOUNDARY = re.compile(r"[.!?]+[\"'\u201d\u2019)\]]*(?=\s)")
_ABBREVIATION = re.compile(
    r"\b(?:e\.g|i\.e|etc|vs|cf|inc|ltd|approx|al|mr|mrs|ms|dr|jr|sr)\.$",
    re.IGNORECASE,
)


def _split(body: str) -> list[str]:
    """The sentences of `body`. A code span is one `_SLOT`, so it never
    splits; nor do e.g., i.e., etc., vs., cf., Inc. or a version like 3.12."""
    sentences: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(body):
        stop = match.start() + 1
        if _ABBREVIATION.search(body, max(0, stop - 8), stop):
            continue
        sentences.append(body[start:match.end()].strip())
        start = match.end()
    sentences.append(body[start:].strip())
    return [sentence for sentence in sentences if sentence]


_OPENER = re.compile(
    r"(?:in\s+summary|in\s+conclusion|overall|essentially|basically|simply\s+put)"
    r"\s*,\s*"
    r"|(?:please\s+)?note\s+that\s+"
    r"|it(?:\s+is|['\u2019]s)\s+(?:worth\s+noting|important\s+to\s+note)\s+that\s+",
    re.IGNORECASE,
)
_FILLER = re.compile(
    r"this\s+(?:[\w-]+\s+)?(?:file|module|package|class|function|method|script"
    r"|component|library|code|service|utility)\s+(?:"
    r"(?P<verb>provides|contains|defines|implements|handles|manages|exposes|wraps"
    r"|exports|offers|represents|encapsulates|holds|stores|builds|creates|parses"
    r"|runs|uses)"
    r"|is\s+(?P<duty>responsible\s+for)|(?P<role>serves|acts)\s+as"
    r"|is\s+(?P<article>an?))(?![\w-])",
    re.IGNORECASE,
)
_SWAP = re.compile(
    r"\b(?:(?:utili[sz]|leverag)(?P<end>e|es|ed|ing)|in\s+order\s+to)\b",
    re.IGNORECASE,
)
_USE = {"e": "use", "es": "uses", "ed": "used", "ing": "using"}
_ADJECTIVE = (
    r"(?:robust|seamless|powerful|cutting-edge|state-of-the-art|comprehensive"
    r"|elegant|effortless|world-class|best-in-class|next-generation|intuitive"
    r"|sleek|blazing-fast)(?![\w-])"
)
_INTENSIFIER = r"(?:(?:very|highly|extremely|incredibly|truly|really|quite)\s+)?"
_JOINER = r"(?:\s*,\s*and|\s*,|\s+and)"
_ARTICLE = r"(?:(?<![\w-])(?P<article>an?)\s+)?"
_PREPOSITION = (
    r"(?:against|to|for|with|in|on|at|of|under|over|enough|than|when|while"
    r"|because|if|as|and|or|but|yet|so|from|by)(?![\w-])"
)
_FOLLOWED = re.compile(           # "robust, typed config", "robust and fast"
    rf"{_ARTICLE}(?<![\w-]){_INTENSIFIER}{_ADJECTIVE}{_JOINER}\s+(?=(?P<next>\S+))",
    re.IGNORECASE,
)
_LISTED = re.compile(             # "a fast and robust cache"
    rf"{_JOINER}\s+{_INTENSIFIER}{_ADJECTIVE}", re.IGNORECASE
)
_ATTRIBUTIVE = re.compile(        # "a robust parser"
    rf"{_ARTICLE}(?<![\w-]){_INTENSIFIER}{_ADJECTIVE}\s+(?!{_PREPOSITION})"
    r"(?=(?P<next>[^\s.,;:!?]\S*))",
    re.IGNORECASE,
)
_ADVERB = re.compile(
    r"(?:^|\s+)(?:seamlessly|elegantly|effortlessly)(?![\w-])", re.IGNORECASE
)
_COPULA = re.compile(
    r"(?:^|\s)(?:is|are|was|were|be|been|being|remains?|stays?|seems?|looks?"
    r"|becomes?)\s+$",
    re.IGNORECASE,
)
_AND = re.compile(r"\band\s+$", re.IGNORECASE)
_AN_LETTERS = "aefhilmnorsx"    # letter names that open with a vowel sound
_SPOKEN = frozenset({           # acronyms read as words
    "FIFO", "LAN", "LIFO", "MIME", "NASA", "NAT", "RAID", "RAM", "REPL", "REST",
    "ROM", "SAML", "SASS", "SIM", "SOAP",
})
_SILENT_H = ("heir", "honest", "honor", "honour", "hour")


def _tidy(sentence: str) -> str:
    """One sentence without opening filler, padding phrases or marketing words.

    A sentence whose opening word changed is capitalised again.
    """
    text = _SWAP.sub(_swap, sentence)
    while True:
        opener = _OPENER.match(text)
        if opener is not None:
            text = text[opener.end():]
            continue
        filler = _FILLER.match(text)
        if filler is None:
            break
        text = _subject(filler) + text[filler.end():]
    text = _strip_marketing(text).strip(" ,;:")
    if text and text.split(" ", 1)[0] != sentence.split(" ", 1)[0]:
        text = _capitalise(text)
    return text


def _subject(match: re.Match[str]) -> str:
    """What replaces "This module provides", "This class is a" and the like."""
    if match.group("verb"):
        return str(match.group("verb")).capitalize()
    if match.group("duty"):
        return "Responsible for"
    if match.group("role"):
        return f"{str(match.group('role')).capitalize()} as"
    return str(match.group("article")).capitalize()


def _swap(match: re.Match[str]) -> str:
    """"utilizes" or "leverages" as "uses", "in order to" as "to"."""
    ending = match.group("end")
    word = _USE[ending.lower()] if ending else "to"
    return word.capitalize() if match.group()[0].isupper() else word


def _strip_marketing(text: str) -> str:
    """`text` without marketing adjectives and adverbs, grammar intact:
    "a fast and robust cache" reads "a fast cache". A predicate adjective
    ("is robust") stays, since removing it would break the sentence."""
    previous = ""
    while text != previous:
        previous = text
        text = _FOLLOWED.sub(_drop_followed, text)
        text = _LISTED.sub("", text)
        text = _ATTRIBUTIVE.sub(_drop_adjective, text)
        text = _ADVERB.sub("", text)
    return text


def _drop_followed(match: re.Match[str]) -> str:
    """Like `_drop_adjective`, but "fast and robust, typed" is left for
    `_LISTED`, which reads "fast, typed"."""
    if _AND.search(match.string, max(0, match.start() - 8), match.start()):
        return match.group()
    return _drop_adjective(match)


def _drop_adjective(match: re.Match[str]) -> str:
    article = match.group("article")
    if article:
        return f"{_article(article, match.group('next'))} "
    before = match.string[max(0, match.start() - 24):match.start()]
    return match.group() if _COPULA.search(before) else ""


def _article(article: str, word: str) -> str:
    """`article` ("a" or "an") agreed with the `word` after it.

    An acronym goes by the name of its first letter ("an API", "a URL") unless
    it is read as a word ("a REST API"); "un-" reads as a vowel ("an unknown")
    but "uni-", "us-", "ut-" do not ("a unique", "a user"). Left unchanged
    where spelling cannot tell the sound: other u- and x- words, code, other
    scripts.
    """
    letters = re.match(r"[\"'\u201c\u2018(]*([A-Za-z]+)", word)
    if letters is None:
        return article
    token = letters.group(1)
    lower = token.lower()
    if token.isupper():
        spoken = token in _SPOKEN or lower[0] not in _AN_LETTERS
        wanted = "a" if spoken else "an"
    elif lower[0] in "aeio":
        wanted = "a" if lower.startswith(("one", "once", "eu", "ewe")) else "an"
    elif lower[0] == "h":
        wanted = "an" if lower.startswith(_SILENT_H) else "a"
    elif lower.startswith(("uni", "unary", "us", "ut", "ur")):
        wanted = "an" if lower.startswith(("unin", "unim")) else "a"
    elif lower.startswith(("un", "up", "ul", "um")):
        wanted = "an"
    elif lower[0] in "ux":
        return article
    else:
        wanted = "a"
    return wanted.capitalize() if article[:1].isupper() else wanted


_SPACE_BEFORE = re.compile(r"\s+(?=[,.;:!?](?:\s|$)|[)\]])")
_SPACE_AFTER = re.compile(r"(?<=[(\[])\s+")
_EMPTY_BRACKETS = re.compile(r"(?:^|(?<=\s))(?:\(\s*\)|\[\s*\])")
_EXTRA_COMMA = re.compile(r",+(?=[,.;:!?])")
_DOUBLE_DOT = re.compile(r"(?<!\.)\.\.(?!\.)")
_FIRST_WORD = re.compile(r"[\"'\u201c\u2018(\[]*(?P<word>[^\W_][\w'\u2019-]*)")
_TERMINAL = re.compile(r"[.!?][\"'\u201d\u2019)\]]*$")
_TAIL = re.compile(r"(?:[\s,;:\u2013\u2014-]|\.{2,}|\u2026)+$")


def _punctuate(body: str) -> str:
    """Spacing and punctuation repaired after removals."""
    previous = ""
    while body != previous:
        previous = body
        body = _SPACE_BEFORE.sub("", body)
        body = _SPACE_AFTER.sub("", body)
        body = _EMPTY_BRACKETS.sub("", body)
        body = _EXTRA_COMMA.sub("", body)
        body = _DOUBLE_DOT.sub(".", body)
        body = " ".join(body.split()).lstrip(",;: ")
    return body


def _capitalise(body: str) -> str:
    """`body` with its first letter capitalised, unless it opens with code or
    with a mixed-case name such as iOS."""
    match = _FIRST_WORD.match(body)
    if match is None:
        return body
    word = match.group("word")
    if not word[0].islower() or any(char.isupper() for char in word[1:]):
        return body
    start = match.start("word")
    return f"{body[:start]}{word[0].upper()}{body[start + 1:]}"


def _terminate(body: str) -> str:
    """`body` ending in ".", "!" or "?"; a trailing ellipsis becomes "."."""
    body = _TAIL.sub("", body)
    return body if not body or _TERMINAL.search(body) else f"{body}."

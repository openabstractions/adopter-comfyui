"""OA's redaction of credential-shaped text, for ComfyUI's log records.

line() is the Python reading of OA's Go redact.Line
(abstraction-facade/go-core/redact): terminal escapes, control and space
characters become one space, format characters go, and anything shaped like
a credential becomes [redacted]. The Go bypass table, redacttest.Cases, is
the one test both answer (adopters/comfyui/connector, TestTheNodePackRedactsAsOADoes).

text() applies the same rules to each line of a log message or a traceback
and keeps its line breaks and indentation.

Standard library only.
"""

import re
import unicodedata

PLACEHOLDER = "[redacted]"

_TERMINAL_ESCAPE = re.compile("\x1b\\[[0-?]*[ -/]*[@-~]|\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)|\x1b[@-_]")

# Go's secretName. Each part around the secret word is bounded to 64
# characters: Python's backtracking engine is otherwise cubic on a long run
# of name characters ("pw-" * 2730), and Go's carries the same bound so both
# redact the same names. A name's tail is taken whole, as an atomic group
# written with a lookahead and a backreference: every pattern below needs a
# character outside [\w.-] after the name, so a shorter tail never matches,
# and retrying it only costs time.
_SECRET_NAME = (r"(?:[\w.-]{0,64}(?:token|key|secret|passw(?:or)?d|passphrase|credential|signature)"
                r"(?=(?P<tail>[\w.-]{0,64}))(?P=tail)"
                r"|(?:[\w.-]{0,64}[_.-])?(?:auth|pass|pwd|pw|sig)(?=(?P<part>(?:[_.-][\w.-]{0,64})?))(?P=part)\b)")
_FLAGS = re.IGNORECASE | re.ASCII

_AUTHORIZATION_NAME = re.compile(r"\b(?:(?:proxy-)?authorization|(?:set-)?cookie)(?:\\?[\"'])?\s*[:=]\s*", _FLAGS)
_AUTHORIZATION_BARE = re.compile(r"\b(?:proxy-)?authorization\s+", _FLAGS)
_FLAG_NAME = re.compile(r"(?:^|\s)--?" + _SECRET_NAME + r"(?:\s*=\s*|\s+)", _FLAGS)
_FIELD_NAME = re.compile(r"\b" + _SECRET_NAME + r"(?:\\?[\"'])?\s*(?:=>|[:=>])\s*", _FLAGS)
_PHRASE_NAME = re.compile(r"\b" + _SECRET_NAME + r"\s+(?:is|was|are)(?:\s*:)?\s+", _FLAGS)
_BEARER_NAME = re.compile(r"\bbearer\b[\s:=]*|\bbearer", _FLAGS)

_URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s@]+@", _FLAGS)
_PREFIXED_KEY = re.compile(r"\b(hf_|sk-|gh[pousr]_|github_pat_|glpat-|xox[abprs]-)[A-Za-z0-9_\-]+", re.ASCII)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*", re.ASCII)
_AWS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", re.ASCII)
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_%-]{32,}")
_DIGEST_PREFIX = re.compile(r"\bsha256:\Z", _FLAGS)
_HEX = re.compile(r"[0-9A-Fa-f]+\Z")

_STRONG_WORDS = ("secret", "pass", "credential", "auth", "pw", "sig")
_AUTH_SCHEMES = frozenset({"bearer", "basic", "digest", "token", "negotiate"})
_COMMON_WORDS = frozenset({
    "a", "an", "the", "not", "no", "missing", "invalid", "required", "empty", "expired", "wrong", "incorrect",
    "set", "unset", "none", "null", "too", "being", "now", "already",
})
_WORD_END = re.compile("[ \"',;&)\\]}<]")

_WORD, _REST, _PHRASE, _SCHEME_REST = range(4)


def _spacing(ch):
    """Whether Go's normalize turns ch into a space."""
    return ch.isspace() or unicodedata.category(ch) in ("Cc", "Zs", "Zl", "Zp")


def _normalize(text):
    text = _TERMINAL_ESCAPE.sub(" ", text)
    out, space = [], True
    for ch in text:
        if _spacing(ch):
            if not space:
                out.append(" ")
                space = True
        elif unicodedata.category(ch) == "Cf":
            continue
        else:
            out.append(ch)
            space = False
    return "".join(out).strip()


def _soften(text):
    """One line made visible as _normalize does, without joining or trimming spaces."""
    text = _TERMINAL_ESCAPE.sub(" ", text)
    return "".join(" " if _spacing(ch) else ch for ch in text if unicodedata.category(ch) != "Cf")


def _weak_name(name):
    name = name.lower()
    return not any(word in name for word in _STRONG_WORDS)


def _short_count(value):
    return 0 < len(value) <= 8 and all("0" <= ch <= "9" for ch in value)


def _word_end(text):
    found = _WORD_END.search(text)
    return found.start() if found else len(text)


def _value_span(text, i, form):
    rest = text[i:]
    if rest.startswith('\\"'):
        end = rest.find('\\"', 2)
        return (i + 2, i + end) if end >= 0 else (i + 2, len(text))
    if rest.startswith('"'):
        j = 1
        while j < len(rest):
            if rest[j] == "\\":
                j += 1
            elif rest[j] == '"':
                return i + 1, i + j
            j += 1
        return i + 1, len(text)
    if rest.startswith("'"):
        end = rest.find("'", 1)
        return (i + 1, i + end) if end >= 0 else (i + 1, len(text))
    if form == _REST:
        return i, len(text)
    if form == _SCHEME_REST:
        if rest[:_word_end(rest)].lower() in _AUTH_SCHEMES:
            return i, len(text)
        return i, i
    end = _word_end(rest)
    if rest[:end].lower() in _AUTH_SCHEMES and rest[end:].startswith(" ") and end + 1 < len(rest):
        end += 1 + _word_end(rest[end + 1:])
    return i, i + end


def _redact_values(text, name, form):
    out, last = [], 0
    for match in name.finditer(text):
        if match.start() < last:
            continue
        start, end = _value_span(text, match.end(), form)
        value = text[start:end]
        if value == "" or text[start:].startswith(PLACEHOLDER) or (
                form == _PHRASE and value.rstrip(".:!").lower() in _COMMON_WORDS):
            continue
        if form not in (_REST, _SCHEME_REST) and _short_count(value) and _weak_name(match.group(0)):
            continue
        out.append(text[last:start])
        out.append(PLACEHOLDER)
        last = end
    if last == 0:
        return text
    out.append(text[last:])
    return "".join(out)


def _secret_shaped(run):
    if not any("0" <= ch <= "9" for ch in run):
        return False
    return max((len(part) for part in re.split(r"[/_-]", run) if part), default=0) >= 16


def _redact_long_tokens(text):
    out, last = [], 0
    for match in _LONG_TOKEN.finditer(text):
        run = match.group(0)
        if not _secret_shaped(run) or (len(run) == 64 and _HEX.match(run)
                                       and _DIGEST_PREFIX.search(text[:match.start()])):
            continue
        out.append(text[last:match.start()])
        out.append(PLACEHOLDER)
        last = match.end()
    if last == 0:
        return text
    out.append(text[last:])
    return "".join(out)


def _rules(text):
    text = _redact_values(text, _AUTHORIZATION_NAME, _REST)
    text = _redact_values(text, _AUTHORIZATION_BARE, _SCHEME_REST)
    # Every URL credential match needs both delimiters. A long run of
    # scheme characters without them otherwise retries at each word boundary.
    if "://" in text and "@" in text:
        text = _URL_CREDENTIALS.sub(lambda m: m.group(1) + PLACEHOLDER + "@", text)
    text = _redact_values(text, _FLAG_NAME, _WORD)
    text = _redact_values(text, _FIELD_NAME, _WORD)
    text = _redact_values(text, _PHRASE_NAME, _PHRASE)
    text = _redact_values(text, _BEARER_NAME, _WORD)
    text = _PREFIXED_KEY.sub(lambda m: m.group(1) + PLACEHOLDER, text)
    text = _JWT.sub(PLACEHOLDER, text)
    text = _AWS_KEY_ID.sub(PLACEHOLDER, text)
    return _redact_long_tokens(text)


def line(text, limit=0):
    """Text as OA may show it on one line, as Go redact.Line makes it. A
    positive limit cuts the result to at most limit UTF-8 bytes on a
    character boundary."""
    text = _rules(_normalize(text))
    if limit > 0:
        data = text.encode("utf-8")
        if len(data) > limit:
            text = data[:limit].decode("utf-8", "ignore").strip()
    return text


def text(value):
    """Multi-line text with each line redacted as line() redacts it; line
    breaks and indentation stay."""
    if not value:
        return value
    return "\n".join(_rules(_soften(part.rstrip("\r"))) for part in value.split("\n"))

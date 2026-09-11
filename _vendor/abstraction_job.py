"""job — a handle to work that is happening somewhere else.

This is the Python side of the same abstraction the Go package implements. The
two are NOT a port of one another and they are not generated from a shared IDL.
They are two independent implementations that agree about one thing: the record
on disk, and the rules for taking ownership of it.

That is deliberate, and it is the whole test. If a Go process can be killed
mid-job and a Python process can pick the job up and finish it, then the
abstraction is real. If the only way to make that work is for both sides to be
written by the same generator, it is a serialisation format, not an abstraction.

So the API here looks like Python — snake_case, dataclasses, exceptions — while
the bytes on disk are identical to what Go writes. What crosses the boundary is
the record. The method signatures are each language's own business.

WHAT THIS MODULE DOES NOT KNOW: what the work IS. `spec` and `checkpoint` are
opaque here and `kind` says who can read them. A download's artifact, sources
and destination live in a download's spec, not in this file — which is why
downloading can grow mirrors, chunk manifests and webseeds without changing the
record every language has to agree about.

Standard library only, on purpose: an abstraction that requires a dependency to
read a JSON file has misjudged its own weight.
"""

from __future__ import annotations

import binascii
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

try:
    import abstraction_cas as _cas
except ImportError:
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "abstraction-cas", "python"))
    import abstraction_cas as _cas

# The data features this implementation understands, and writes.
#
# A name is namespaced and versioned: "abstraction.job/base@1". The version is
# part of the name rather than a separate field, so an incompatible change to one
# feature is a NEW name that old readers correctly fail to recognise, while every
# other feature in the record stays readable.
#
# This replaced an integer version. A version conflates WHAT CHANGED with WHAT
# YOU MUST UNDERSTAND, so the only safe response to any unknown version was to
# refuse the whole record -- even when the addition was decoration a reader could
# have ignored. See CRITICAL below for the half that makes this more than a
# rename.
FEATURE_BASE = "abstraction.job/base@1"
FEATURE_INTENT = "abstraction.job/intent@1"
FEATURE_DELEGATION = "abstraction.job/delegation@1"
FEATURE_STEP = "abstraction.job/step@1"
# That a finished record is closed to its own lease holder: no update, no
# release, no intent. It names a RULE rather than a field, which is why it went
# out under an unchanged FEATURE_BASE and why a published reader walks a complete
# job back to pending on a record this tree wrote. Declared whenever the state
# is terminal, and critical there, because a reader that does not know the rule
# is exactly the reader that breaks it.
FEATURE_TERMINAL = "abstraction.job/terminal@1"
# That the issuer has asked the holder for the lease back: ``lease.recall`` and
# its rules -- a renew never extends past ``until``, the holder cannot re-claim
# to shed it, and the lease lapsing at ``until`` is the eviction. Critical
# whenever present.
FEATURE_RECALL = "abstraction.job/recall@1"
# A checkpoint that names WHICH byte ranges a predecessor proved, rather than
# only how many leading ones. A single prefix describes one stream appending to
# the end of a file and nothing else, so a caller fetching sixteen ranged parts
# at once had to delete its parallelism to use this library.
FEATURE_RANGES = "abstraction.download/ranges@1"
# That the record says which schema its opaque halves follow and what may be
# asked of a job of this kind. Critical whenever present: a reader that ignored
# it would carry on and then write the record back without it, and the thing
# destroyed would be the description of what was destroyed.
FEATURE_ENVELOPE = "abstraction.job/envelope@1"

# What this implementation can read. A record naming anything else in `critical`
# is refused.
KNOWN_FEATURES = frozenset(
    {FEATURE_BASE, FEATURE_INTENT, FEATURE_DELEGATION, FEATURE_STEP, FEATURE_TERMINAL,
     FEATURE_RECALL, FEATURE_RANGES, FEATURE_ENVELOPE}
)

# Features this layer declares but cannot derive, because what they describe lives
# inside a field that is opaque here.
#
# Everything else in `content` is rediscovered on every write, so a declaration
# cannot drift from the data. A feature describing the CHECKPOINT's contents
# cannot be: working it out would mean reading the checkpoint, and this module
# does not know what a checkpoint is. So the declaration is carried instead --
# the same treatment an unknown extension gets, for the same reason.
CARRIED_FEATURES = frozenset({FEATURE_RANGES})

# The features the names table marks never critical, whoever asked. A marking on
# one is removed rather than refused [JOB-C2]: ignoring them costs a reader
# nothing it can measure, and refusing would stop a reader that would have done
# the work correctly.
NEVER_CRITICAL = frozenset({FEATURE_STEP, FEATURE_RANGES})

# The integer this format used to carry, mapped onto the features each version
# implied. No longer written; still read, because stores full of version 3 and 4
# records exist on real disks and on a NAS. The mapping is exact rather than a
# guess: those versions are frozen and it is known what each could contain.
LEGACY_SCHEMAS = {
    3: (FEATURE_BASE,),
    4: (FEATURE_BASE, FEATURE_INTENT),
    5: (FEATURE_BASE, FEATURE_INTENT, FEATURE_STEP),
}

# --- the base envelope ------------------------------------------------------
#
# NOTHING IN THIS MODULE RESOLVES A SCHEMA IDENTIFIER. There is no function here
# from a schema name to a schema; the only questions askable of one are whether
# it is well formed and whether it is a string the caller already knows. That is
# the whole safety argument, and ``valid_schema`` is the half that makes it
# structural rather than a promise: a legal identifier cannot be a URL, a UNC
# name or a path, so a supervisor that wanted to fetch one would first have to
# be handed something this refuses. A supervisor that fetches an address out of
# somebody else's record is running attacker-chosen content as a service on the
# owner's machine.

# The schema a bare action name belongs to. The bare namespace is this layer's;
# anyone else's name is qualified, or two vendors both define "cancel" and a
# supervisor cannot tell which one it just performed.
BASE_SCHEMA = FEATURE_BASE

# What may be asked of ANY job through this layer's own operations, and which a
# kind declares it actually honours. Capability says what an IMPLEMENTATION
# promises; this says what a KIND honours, and they are different questions -- a
# store that survives process exit still cannot pause a kind whose worker has no
# pause. All four are asks, never instructions: each names an existing Store
# operation, and none of them can describe how to carry anything out.
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_CANCEL = "cancel"
ACTION_RECALL = "recall"
BASE_ACTIONS = frozenset({ACTION_PAUSE, ACTION_RESUME, ACTION_CANCEL, ACTION_RECALL})

_MAX_SCHEMA = 128
_MAX_LABEL = 63
_MAX_ACTION = 64
_LOWER_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def _label(s: str, extra: str = "-", limit: int = _MAX_LABEL) -> bool:
    """One part of a name: lower-case letters, digits and interior separators.

    Deliberately the smallest alphabet that can name anything, because
    everything it leaves out is what a URL, a path and a command line are made
    of.
    """
    if not s or len(s) > limit:
        return False
    if s[0] not in _LOWER_ALNUM or s[-1] not in _LOWER_ALNUM:
        return False
    return all(c in _LOWER_ALNUM or c in extra for c in s)


def valid_schema(s) -> bool:
    """Is ``s`` a schema identifier [JOB-V1]?

        namespace "/" name "@" version

    spelled exactly as this record's content names already are, so there is one
    grammar for names here and not two. There is no scheme, no authority, no
    percent-escape, no backslash, no query and exactly one "/" and one "@",
    which is what makes ``https://example.invalid/x@1``, ``\\\\host\\share``,
    ``../../etc`` and ``file:///x`` illegal values rather than discouraged ones.
    """
    if not isinstance(s, str) or not s or len(s) > _MAX_SCHEMA:
        return False
    if s.count("/") != 1 or s.count("@") != 1:
        return False
    ns, rest = s.split("/", 1)
    name, version = rest.split("@", 1)
    if not all(_label(part) for part in ns.split(".")):
        return False
    if not _label(name):
        return False
    return (
        1 <= len(version) <= 9
        and version[0] in "123456789"
        and all(c in "0123456789" for c in version)
    )


def split_action(name):
    """An action name as (schema, bare, ok) [JOB-V2]. A bare name returns "" for
    the schema, which means the base schema.

    The separator is "#" because it is the one component of a URI reference
    defined never to be sent anywhere (RFC 3986 section 3.5): a fragment names
    something inside a document rather than a document to go and get. It is also
    excluded from both grammars either side of it, so the split is exact.
    """
    if not isinstance(name, str):
        return "", "", False
    n = name.count("#")
    if n == 0:
        return "", name, _label(name, "-_", _MAX_ACTION)
    if n == 1:
        schema, bare = name.split("#", 1)
        return schema, bare, valid_schema(schema) and _label(bare, "-_", _MAX_ACTION)
    return "", "", False


def _envelope_unmoved(before, after) -> None:
    """[JOB-V4]: the envelope is a property of the KIND, so a lease holder cannot
    move it.

    The service binding gets this for free -- it enumerates the fields a write
    may carry and the envelope is not among them -- but the in-process bindings
    run the caller's own callable against the record, so here the rule has to be
    checked rather than arranged. Without it "supported action" becomes live
    state by the ordinary route: somebody sets it from a worker that has just
    found something out, and every supervisor afterwards reads a fact about one
    process as a fact about the kind.
    """
    if before is None and after is None:
        return
    if (
        before is not None
        and after is not None
        and before.schema == after.schema
        and list(before.actions) == list(after.actions)
    ):
        return
    raise Invalid("the envelope is a property of the kind and is written once, at submit")


def _reject_unknown_keys(obj, known, where: str) -> None:
    """Refuse a field this implementation does not know.

    Absent or not an object is nothing to check: the callers below all tolerate
    a missing section, and a section of the wrong type is caught where it is
    read.
    """
    if not isinstance(obj, dict):
        return
    for key in obj:
        if key not in known:
            raise Invalid(
                f"unknown field {key!r} in {where} "
                "-- refusing a record written by something newer"
            )


def _offset(value) -> int:
    """Refuse 4194304.5 rather than rounding it.

    JSON has one number type. A decoder that widens an offset to a float
    re-emits it as one, and the byte-for-byte comparison three implementations
    are held to then compares two spellings of the same number.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid(f"offset {value!r} is not a whole number")
    return value


# ---------------------------------------------------------------------------
# A checkpoint of ranges.
#
# WHAT ONE INTEGER COULD NOT SAY. A checkpoint used to hold a single verified
# prefix: "the first N bytes are proven", and nothing else. Every transfer that
# could be described was therefore one stream appending to the end of a file.
# That is not how bytes are fetched -- sixteen concurrent ranged parts landing at
# scattered offsets in a sparse file is ordinary -- and "parts 0, 2 and 5 done,
# 1, 3 and 4 partway" had no representation at all. An adopter with a parallel
# fetcher could only take this library by deleting its parallelism.
#
# A range set says it. The prefix is the degenerate case: one range at zero.
#
#     "checkpoint": {
#       "verified_prefix": 4194304,
#       "verified": [[0, 4194304], [8388608, 12582912]]
#     }
#
# WHY THE PREFIX STAYS. Not redundancy and not politeness. A reader that has
# never heard of `verified` resumes from `verified_prefix` and re-fetches the
# rest, which is exactly what it does today. That is the whole reason this is an
# addition rather than a break, and it is why FEATURE_RANGES is never critical: an
# old reader ignoring the ranges loses some bytes to a second fetch, and marking
# it critical would stop every existing reader dead for no safety gain. The
# prefix is DERIVED from the set on write, so nothing has to remember to keep
# the two agreeing.
#
# WHAT A RANGE MEANS. Proven, not merely written: the bytes are on disk AND
# checked, against a piece digest where the kind's spec carries one and against
# the transport's own framing where it does not. Bytes in flight when a process
# is killed are exactly the ones a successor must not trust; that rule is
# unchanged, and now applies per range instead of to one tail.
#
# WHERE THIS SITS. A checkpoint is opaque to this module. These helpers do not
# change that -- they are a canonical FORM offered to whoever writes one, not a
# meaning read out of every record relayed. `to_json` leaves a checkpoint
# exactly as it found it; only a caller that asks for ranges gets them
# rewritten.

VERIFIED_PREFIX_KEY = "verified_prefix"
VERIFIED_KEY = "verified"


class Range(tuple):
    """A half-open byte interval: `start` is included, `end` is not.

    A tuple, so a caller may hand in plain ``(0, 400)`` pairs and get pairs
    back, and so equality means what it looks like it means.
    """

    __slots__ = ()

    def __new__(cls, start, end):
        return super().__new__(cls, (int(start), int(end)))

    @property
    def start(self) -> int:
        return self[0]

    @property
    def end(self) -> int:
        return self[1]

    @property
    def size(self) -> int:
        return self[1] - self[0]

    def __repr__(self) -> str:
        return f"[{self[0]},{self[1]})"


def canonical_ranges(ranges) -> List[Range]:
    """Sort, merge and validate a set of ranges: the merge-on-write the format
    promises.

    Callers hand in whatever they have -- out of order, overlapping, duplicated,
    adjacent -- and get the one spelling of that state every implementation
    agrees on.

    ADJACENT ranges merge as well as overlapping ones, and that is not
    tidiness. ``[[0,4],[4,8]]`` and ``[[0,8]]`` are the same proven bytes; if
    both were legal, two implementations could write one state as different
    bytes and a conformance test comparing files would call them a
    disagreement. Merging touching ranges is what makes the form canonical
    rather than merely sorted.
    """
    kept = []
    for r in ranges or ():
        try:
            start, end = r
        except (TypeError, ValueError):
            raise Invalid(f"a verified range is a pair [start, end), got {r!r}") from None
        if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool) or isinstance(end, bool):
            raise Invalid(f"a byte offset is a whole number, got {r!r}")
        if start < 0 or end < 0:
            raise Invalid(f"a byte offset cannot be negative: [{start},{end})")
        if end < start:
            raise Invalid(f"a range ends before it starts: [{start},{end})")
        # An empty range is not an error -- a fetcher that recorded a
        # zero-length part is not lying, it has just proven nothing -- but it
        # carries no information, and two sets differing only by one are the
        # same set.
        if end == start:
            continue
        kept.append(Range(start, end))
    kept.sort()

    out: List[Range] = []
    for r in kept:
        if out and r.start <= out[-1].end:
            if r.end > out[-1].end:
                out[-1] = Range(out[-1].start, r.end)
            continue
        out.append(r)
    return out


def verified_prefix(ranges) -> int:
    """The end of the range starting at zero, or 0 when there is none.

    In a canonical set at most one range can start at zero and it is the first,
    so this is the whole rule.
    """
    rs = canonical_ranges(ranges)
    return rs[0].end if rs and rs[0].start == 0 else 0


def ranges_total(ranges) -> int:
    """How many proven bytes the set holds."""
    return sum(r[1] - r[0] for r in canonical_ranges(ranges))


def ranges_cover(ranges, start: int, end: int) -> bool:
    """Whether every byte of [start, end) is proven. An empty interval is
    covered by anything, including the empty set."""
    if end <= start:
        return True
    return any(r[0] <= start and end <= r[1] for r in canonical_ranges(ranges))


def ranges_missing(ranges, start: int, end: int) -> List[Range]:
    """The gaps in [start, end) that are not proven yet -- what a fetcher still
    has to ask for, which is the question a resume asks."""
    out: List[Range] = []
    at = start
    for r in canonical_ranges(ranges):
        if r.end <= at:
            continue
        if r.start >= end:
            break
        if r.start > at:
            out.append(Range(at, r.start))
        at = max(at, r.end)
        if at >= end:
            break
    if at < end:
        out.append(Range(at, end))
    return out


def ranges_from_checkpoint(checkpoint) -> List[Range]:
    """The proven ranges a checkpoint carries.

    Three inputs, one answer:

      * ``verified`` present: those ranges, canonicalised.
      * only ``verified_prefix``: ``[(0, prefix)]``, because a prefix IS a
        range. This is what lets a record written before ranges existed be read
        as one, and what makes "the prefix is the degenerate case" true in code
        rather than only in prose.
      * neither, or no checkpoint at all: the empty set.

    When both are present the prefix is UNIONED IN rather than checked against
    the ranges. A prefix-only writer that took the job over and advanced the
    prefix without touching ``verified`` left a record where the two disagree,
    and the union is the only reading that loses nothing: both fields are claims
    that bytes are proven, and neither is a claim that other bytes are not.
    """
    if not checkpoint:
        return []
    if not isinstance(checkpoint, dict):
        raise Invalid("a checkpoint carrying ranges must be a JSON object")
    found = []
    raw = checkpoint.get(VERIFIED_KEY)
    if raw is not None:
        if not isinstance(raw, (list, tuple)):
            raise Invalid(f"{VERIFIED_KEY!r} is a list of [start, end) pairs")
        for pair in raw:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise Invalid(f"a verified range is a pair [start, end), got {pair!r}")
            found.append((pair[0], pair[1]))
    prefix = checkpoint.get(VERIFIED_PREFIX_KEY)
    if prefix is not None:
        if not isinstance(prefix, int) or isinstance(prefix, bool):
            raise Invalid(f"{VERIFIED_PREFIX_KEY!r} is a whole number of bytes")
        if prefix > 0:
            found.append((0, prefix))
    return canonical_ranges(found)


def checkpoint_with_ranges(checkpoint, ranges) -> Dict[str, Any]:
    """A checkpoint carrying `ranges` canonically, keeping every key it does not
    own.

    The form is pinned, because three implementations have to produce the same
    bytes for the same state::

        {"verified_prefix": P, "verified": [[s, e], ...], <everything else, by key>}

    The two range keys come first and in that order -- a reader skimming a
    record should see the number that matters first -- and the caller's other
    keys follow sorted by name. Sorted rather than left as found because Go
    reaches a checkpoint through a map, which has no order to preserve, so "as
    found" is not something all three languages can agree to do.
    """
    if checkpoint is not None and not isinstance(checkpoint, dict):
        raise Invalid("a checkpoint carrying ranges must be a JSON object")
    canon = canonical_ranges(ranges)
    out: Dict[str, Any] = {
        VERIFIED_PREFIX_KEY: verified_prefix(canon),
        VERIFIED_KEY: [[r.start, r.end] for r in canon],
    }
    for name in sorted(checkpoint or {}):
        if name in (VERIFIED_PREFIX_KEY, VERIFIED_KEY):
            continue
        out[name] = checkpoint[name]
    return out


def understands(critical) -> tuple:
    """The first critical feature this implementation cannot read, and whether it
    can proceed.

    Only `critical` is consulted. `content` is informational: a name there that
    nobody here knows is data to carry, not a reason to stop.
    """
    for name in critical or ():
        if name not in KNOWN_FEATURES:
            return name, False
    return "", True


def _name_list(value, where: str) -> list:
    """A declaration list, or a refusal.

    A bare string here reads as a list of one-character names, and the record
    then fails the subset rule for a reason nobody wrote down.
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise Invalid(f"{where} must be an array of strings")
    return list(value)


def _read_critical(critical, content) -> list:
    """The rules that read `critical`, in the one order [JOB-D10] fixes.

    Stripping before refusing is what makes [JOB-C2] true against [JOB-D8]: a
    writer that wrongly marked an advisory feature critical cannot stop a reader
    that would otherwise have done the work, and the marking is gone from what
    that reader writes back. It is not permission to ignore criticality -- a name
    outside the table still refuses the whole record.
    """
    kept = []
    for name in critical or ():
        if name in NEVER_CRITICAL:
            continue
        if name not in KNOWN_FEATURES:
            raise UnknownSchema(
                f"this record requires {name!r}, which this implementation cannot read"
            )
        if name not in content:
            raise Invalid(
                f"critical names {name!r} and content does not carry it -- "
                "a declaration about nothing"
            )
        kept.append(name)
    return kept


# What somebody wants to happen, as against what is happening.
RUN = "run"
PAUSE = "pause"
CANCEL = "cancel"
_WANTS = (RUN, PAUSE, CANCEL)

# States. The one that earns its place is TRANSFERRED: the work is finished and
# proven, but the result has not been taken delivery of. It exists because the
# process that does the work and the process that wants the result are not the
# same process — which is the entire premise.
PENDING = "pending"
RUNNING = "running"
TRANSFERRED = "transferred"
COMPLETE = "complete"
FAILED = "failed"
CANCELLED = "cancelled"

_STATES = {PENDING, RUNNING, TRANSFERRED, COMPLETE, FAILED, CANCELLED}
_TERMINAL = {COMPLETE, FAILED, CANCELLED}


class JobError(Exception):
    """Base for every refusal this module makes."""


class NotFound(JobError):
    pass


class Invalid(JobError):
    pass


class UnknownSchema(Invalid):
    pass


class NotSupported(JobError):
    """An action a kind does not declare, or one this supervisor has not built.

    A different question from UnknownSchema and the two are not
    interchangeable: UnknownSchema means leave this job alone, and this means
    the job is mine and this is not one of the things it does.
    """


class LeaseHeld(JobError):
    """Someone else owns this job right now."""


class StaleEpoch(JobError):
    """The caller's epoch is not the record's epoch: it lost ownership."""


class LeaseExpired(JobError):
    """The caller's lease ran out. Re-claim; do not renew."""


class Terminal(JobError):
    pass


def _rfc3339(t: datetime) -> str:
    """Format the way Go's encoding/json writes time.Time.

    Go emits RFC 3339 with a 'Z' for UTC. Python's isoformat() emits '+00:00',
    which Go parses happily, but writing the same shape both ways keeps the files
    diffable and stops anyone concluding the two implementations disagree when
    they are looking at nothing but a timezone suffix.
    """
    t = t.astimezone(timezone.utc)
    s = t.isoformat(timespec="microseconds")
    return s[:-6] + "Z" if s.endswith("+00:00") else s


def _parse_time(s: str) -> datetime:
    if not s:
        return datetime.fromtimestamp(0, timezone.utc)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    # `invalid` is a verdict this layer offers and ValueError is not, so a
    # caller catching this layer's refusals catches nothing here. Two of the
    # corpus timestamps left this function as a bare ValueError, and
    # abstraction-download/python imports it by name.
    except ValueError:
        raise Invalid(f"timestamp {s!r} is not one this reader can read") from None


# The record's escape policy [JOB-E6]: escape what JSON requires, U+2028 and
# U+2029, and nothing else. `encode_basestring` rather than its `_ascii` twin
# for the same reason -- escaping every high byte is a defence against a
# transport that cannot carry them, which a UTF-8 file is not, and it spelled one
# accented filename differently from the Go and C++ writers of the same record.
_ESCAPED = {0x2028: "\\u2028", 0x2029: "\\u2029"}

# A Python str can hold a surrogate only unpaired -- a well-formed pair decodes
# to one astral character -- so every one of these is a byte that was not
# well-formed UTF-8, and the policy spells it as a visible escape rather than as
# the U+FFFD glyph a reader would mistake for content.
_ILL_FORMED = re.compile("[\ud800-\udfff]")


# [JOB-E7]: an opaque payload's bytes are its author's, so the three types below
# remember how a scalar was written as well as what it means. Without them 1.50
# comes back 1.5, 1e2 comes back 100.0 and "a\/b" comes back "a/b" -- three
# re-spellings of somebody else's value, in a record three implementations
# compare byte for byte.
class _Text(str):
    pass


class _Int(int):
    pass


class _Float(float):
    pass


def _spelled(kind, value, raw):
    v = kind(value)
    v.raw = raw
    return v


def _spelled_string(text, end, strict=True):
    value, stop = json.decoder.scanstring(text, end, strict)
    # json's own reader turns "\ud834" into a lone surrogate and hands it back as
    # if it were a character. It is half of one, and [JOB-E8] refuses it wherever
    # it appears -- outside a payload [JOB-E6] would spell the loss as �,
    # and inside one no reader may rewrite what it carries.
    if _ILL_FORMED.search(value):
        raise json.JSONDecodeError("Unpaired surrogate escape", text, end - 1)
    return _spelled(_Text, value, text[end - 1:stop]), stop


def _refuse_constant(word):
    raise ValueError(f"{word} is not a JSON value")


_WHITESPACE = re.compile(r"[ \t\n\r]*")


def _spelled_object(s_and_end, strict, scan_once, object_hook, object_pairs_hook, memo):
    """json's own object reader, with the member names read through the hook.

    The stdlib one reads a name with the module-level `scanstring`, which no
    decoder hook reaches, so `{"a\\/b": 1}` would come back with its key
    re-spelled while every value around it kept its own. Values and failures are
    still json's; the duplicate policy is not, because json's is last-one-wins
    and [JOB-E9] refuses instead.
    """
    s, end = s_and_end
    pairs = []
    seen = set()
    end = _WHITESPACE.match(s, end).end()
    if s[end:end + 1] != "}":
        while True:
            end = _WHITESPACE.match(s, end).end()
            if s[end:end + 1] != '"':
                raise json.JSONDecodeError(
                    "Expecting property name enclosed in double quotes", s, end
                )
            name_at = end
            key, end = _spelled_string(s, end + 1, strict)
            # Compared after escape decoding, so a name spelled two ways is one
            # name [JOB-E9]. The spelling travels beside it and is written back
            # untouched, so refusing here costs [JOB-E7] nothing: what it buys is
            # that no reader has to choose between the first and the last.
            if key in seen:
                raise json.JSONDecodeError("Duplicate object member name", s, name_at)
            seen.add(key)
            end = _WHITESPACE.match(s, end).end()
            if s[end:end + 1] != ":":
                raise json.JSONDecodeError("Expecting ':' delimiter", s, end)
            end = _WHITESPACE.match(s, end + 1).end()
            try:
                value, end = scan_once(s, end)
            except StopIteration as err:
                raise json.JSONDecodeError("Expecting value", s, err.value) from None
            pairs.append((key, value))
            end = _WHITESPACE.match(s, end).end()
            nextchar = s[end:end + 1]
            end += 1
            if nextchar == "}":
                break
            if nextchar != ",":
                raise json.JSONDecodeError("Expecting ',' delimiter", s, end - 1)
    else:
        end += 1
    if object_pairs_hook is not None:
        return object_pairs_hook(pairs), end
    obj = dict(pairs)
    return (obj if object_hook is None else object_hook(obj)), end


# The pure-Python scanner, because the C one builds its own string reader and no
# hook reaches inside it. It is slower, and it runs out of stack at a shallower
# nesting than json.loads -- still far past the 128 the C++ reader accepts, so
# the set all three implementations agree on is untouched.
class _SpellingDecoder(json.JSONDecoder):
    def __init__(self):
        super().__init__(
            parse_float=lambda t: _spelled(_Float, t, t),
            parse_int=lambda t: _spelled(_Int, t, t),
            # NaN, Infinity and -Infinity are json's extension to the grammar and
            # not values [JOB-E8] admits, in a payload or anywhere else. Go and
            # C++ have never read them; leaving them on made this the one reader
            # that could open a record the other two call malformed.
            parse_constant=_refuse_constant,
        )
        self.parse_string = _spelled_string
        self.parse_object = _spelled_object
        self.scan_once = json.scanner.py_make_scanner(self)


_SPELLING = _SpellingDecoder()


def parse_opaque(text: str):
    """Parse a payload this layer carries but does not own, spelling and all.

    The counterpart of Go taking `--spec` as a json.RawMessage: a caller handing
    a spec in is handing bytes, and [JOB-E7] says they come back out unchanged.
    """
    return _SPELLING.decode(text)


class _Opaque:
    """A payload this layer does not read, and therefore may not re-spell."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def _policy(text: str) -> str:
    """The two rules [JOB-E6] adds to what JSON already requires.

    Neither U+2028/U+2029 nor an unpaired surrogate can appear in ASCII, and
    almost every string in a record is ASCII, so the scan is worth skipping.
    """
    if text.isascii():
        return text
    return _ILL_FORMED.sub(r"\\ufffd", text.translate(_ESCAPED))


def _quote(s: str) -> str:
    return _policy(json.encoder.encode_basestring(s))


def _write(value, level: int, verbatim: bool = False) -> str:
    """The record's encoding [JOB-E1]: two-space indent, keys in insertion order.

    ``verbatim`` marks the subtrees this layer carries rather than owns. Inside
    one, a scalar that remembers its spelling is written back with it; outside,
    every string is spelled by [JOB-E6] however it arrived, which is what brings
    a record written by an older implementation to the canonical form.
    """
    if isinstance(value, _Opaque):
        return _write(value.value, level, True)
    if verbatim:
        raw = getattr(value, "raw", None)
        if raw is not None:
            return _policy(raw)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, int):
        return int.__repr__(value)
    if isinstance(value, float):
        return json.dumps(value)
    pad = "  " * (level + 1)
    close = "\n" + "  " * level
    if isinstance(value, dict):
        if not value:
            return "{}"
        members = [
            pad + _write(k, level + 1, verbatim) + ": " + _write(v, level + 1, verbatim)
            for k, v in value.items()
        ]
        return "{\n" + ",\n".join(members) + close + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        return "[\n" + ",\n".join(pad + _write(v, level + 1, verbatim) for v in value) + close + "]"
    raise TypeError("cannot write %s into a record" % type(value).__name__)


@dataclass
class Step:
    """Which phase of a multi-phase job is happening now.

    In the record and not the kind's checkpoint, because a checkpoint is opaque:
    only a reader that knows the kind could render it. Progress is the one thing
    a GENERIC reader has to be able to display — a supervisor's status output, a
    download manager listing work of every kind — without knowing what the work
    is.

    What made it necessary: a download delegated to a NAS is two transfers, and
    only the first was visible. The far side fetched 40 GB and reported done;
    then this machine copied those 40 GB back across a share and re-hashed them,
    with the record still showing the first transfer's numbers throughout. A
    person watching saw a finished download doing nothing, for minutes, twice.

    ADVISORY ONLY, which is the same rule progress already has. Nothing may
    decide anything on a step — not what to do next, not whether work is
    finished, not whether to retry. The moment something branches on
    ``ordinal == 2``, a workflow engine has been smuggled into a record whose
    whole value is that it describes work without prescribing it.

    ``name`` is opaque to this layer, exactly like kind and spec.
    """

    name: str = ""
    ordinal: int = 0  # counts from one
    of: int = 0  # 0 when the writer cannot say how many
    # This phase's own units, which need not be the job's: hashing counts the
    # same bytes a second time, and the overall numbers must not double for it.
    done: int = 0
    total: int = 0


@dataclass
class Progress:
    """Deliberately thin: two numbers and a timestamp, in units the kind defines.

    Best-effort and explicitly NOT monotonic — a job resuming from a checkpoint
    after a crash can legitimately report a smaller `done` than before. Nothing
    may make a decision on it. Anything richer belongs in the kind's checkpoint,
    with the one exception of `step` — see Step for why that exception exists.
    """

    done: int = 0
    total: int = 0  # 0 means unknown
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Absent means what every record before schema 5 meant: one unnamed phase.
    step: Optional["Step"] = None


@dataclass
class Lease:
    """The right to work on a job, for a bounded time.

    epoch is the part that matters: it rises by one on every claim, and every
    write must present the epoch it holds. A process that slept through its own
    expiry wakes up believing it is still the owner; its writes carry a stale
    epoch and are refused. Without that, two owners work one job and each
    believes the result is correct.
    """

    owner: str = ""
    epoch: int = 0
    expires_at: datetime = field(default_factory=lambda: datetime.fromtimestamp(0, timezone.utc))
    recall: Optional["Recall"] = None

    def held(self, now: datetime) -> bool:
        return bool(self.owner) and now < self.expires_at

    def recalled(self) -> bool:
        return self.recall is not None


@dataclass
class Recall:
    """The issuer asking for the lease back -- the half of a lease this design
    lacked. Ours expired; none could be revoked early with a reason the holder
    could act on.

    Not an intent. Intent is what the user wants of the job; a recall is what
    the issuer demands of the resource, addressed to one holding -- the epoch it
    was decided against -- so a claim replaces it with nothing.

    The fallback is the lease lapsing at ``until``: the recall moves
    ``expires_at`` earlier and renew never extends past it. WDDM's shape, not
    Android's -- residency ends whether or not the holder trimmed, and the
    holder finds out on its next write. A lease cannot kill a process on another
    machine; it can stop believing in it.

    ``reason`` is opaque here, chosen by the issuer for the holder's kind, as
    ``kind`` scopes ``spec``. Required: a recall without one is a cancel wearing
    the wrong field.
    """

    reason: str = ""
    by: str = ""
    at: datetime = field(default_factory=lambda: datetime.fromtimestamp(0, timezone.utc))
    until: datetime = field(default_factory=lambda: datetime.fromtimestamp(0, timezone.utc))


@dataclass
class Delegation:
    """The work has been handed to something outside this process — a system
    service, a daemon on a NAS — which is now doing it.

    Not an accommodation for one tool; it is the architecture. An application's
    worker hands off to a system service when one is present, and that service
    hands off to the NAS when one is configured. The application never learns
    which did the work.

    When this is set, `progress` is a CACHE of what the external system last
    reported, not a measurement anyone here made. The external system is truth.
    """

    system: str = ""          # "bits", "nas" — who can interpret external_id
    external_id: str = ""     # that system's own handle; for BITS, a job GUID
    delivered: bool = False   # the delegate was told to hand the result over


@dataclass
class Intent:
    """The desired state, separate from the observed one.

    Why this exists, which is not "so there can be a pause button": every write
    to a record needs the lease, and whoever wants a change is almost never
    holding it — a person clicks cancel in an application while a service on
    another machine moves the bytes. Before this field that was inexpressible,
    and the only alternative was to steal the job, which is the single thing the
    lease exists to prevent.

    Every system that has met this problem separates desired from observed:
    Kubernetes has spec against status with a deletionTimestamp anyone may set,
    Temporal records cancellation-requested apart from the run state, systemd
    distinguishes wanted from active, BITS exposes a state its own service polls.

    The rules, which are the contract rather than this implementation's habits:

      1. Anyone may write it, lease or no lease. It is the ONE field exempt.
      2. Only the lease holder may write `state`. Unchanged.
      3. An owner MUST check it at least as often as it checkpoints and move
         toward it. An owner that reads a record and ignores this is not an
         implementation of this abstraction.
      4. CANCEL must be honoured by everything; stopping is universal.
      5. PAUSE must be honoured by implementations that advertise it. One that
         cannot must FAIL the job with a reason rather than carry on, because a
         pause that quietly does nothing is worse than a refusal.
      6. A paused job is NOT an orphan — see `orphans`.
      7. Once `state` is terminal this is history.
    """

    want: str = RUN
    # Who asked. Not decoration: a job sitting against somebody's wish is one of
    # the few things that cannot be worked out from outside, and "which process
    # asked for this" is the first question anyone has.
    by: str = ""
    at: Optional[datetime] = None


@dataclass
class Envelope:
    """What a job of this kind is, and what may be asked of it.

    Two fields and no third. An envelope is a property of the KIND, so nothing
    in it may report what is true of this instance now: a supervisor that read
    "pause" here and concluded this job can be paused at this moment would be
    reading live state off a static declaration, and the job's worker may have
    died an hour ago. The stores refuse an update that moves it [JOB-V4].

    ``schema`` is a NAME, never an address -- see the note above valid_schema.
    ``actions`` are names a supervisor matches against what it already
    implements; matching is the only operation defined on them.
    """

    schema: str = ""
    actions: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if not valid_schema(self.schema):
            raise Invalid(
                f"{self.schema!r} is not a schema identifier; a schema is named, never fetched"
            )
        if not isinstance(self.actions, list) or not all(
            isinstance(a, str) for a in self.actions
        ):
            raise Invalid("envelope actions must be an array of strings")
        seen = set()
        for name in self.actions:
            if name in seen:
                raise Invalid(f"action {name!r} is declared twice")
            seen.add(name)
            schema, bare, ok = split_action(name)
            if not ok:
                raise Invalid(f"{name!r} is not an action name")
            if not schema:
                # The bare namespace is this layer's. A kind that could put its
                # own word here would be squatting a name a later version of
                # this layer may define, and the supervisor would perform the
                # wrong one.
                if bare not in BASE_ACTIONS:
                    raise Invalid(
                        f"{name!r} is bare and is not one of this layer's actions; "
                        "a kind's own action carries the schema that declares it"
                    )
            elif schema == BASE_SCHEMA:
                # One spelling of one action, so that supports() is a comparison
                # and not a negotiation.
                raise Invalid(f"{name!r} spells a base action the long way; write it bare")
            elif schema != self.schema:
                # Every action name resolves to a schema THIS RECORD declares.
                # Without it a record could name actions in a namespace it has
                # no claim to, and a supervisor matching on the name alone would
                # act on somebody else's authority.
                raise Invalid(
                    f"action {name!r} names a schema this record does not declare "
                    f"({self.schema!r})"
                )

    def copy(self) -> "Envelope":
        return Envelope(schema=self.schema, actions=list(self.actions))


@dataclass
class Supervisor:
    """What a caller has actually built, in its own process: the schemas it was
    written against and the actions it implements.

    A value the caller constructs and never something read out of a record. That
    is the shape the whole design turns on -- an unknown schema is answered from
    a list the caller already holds, so the answer to "I have never heard of
    this" is a refusal and there is nowhere for it to be a fetch.
    """

    schemas: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)


@dataclass
class Record:
    """The whole job, and the cross-language contract.

    Everything a different process — in a different language, after a reboot —
    needs in order to continue this work has to be in here, because nothing else
    survives.
    """

    id: str
    kind: str = ""
    state: str = PENDING
    # What this record carries, and the subset a reader MUST understand or
    # refuse it entirely. See FEATURE_BASE and the `critical` rule: not knowing
    # the step feature is harmless, not knowing the intent feature means working on
    # a job somebody asked to stop.
    content: List[str] = field(default_factory=list)
    critical: List[str] = field(default_factory=list)
    # Data this layer does not understand, keyed by a name that says who does.
    # A reader that cannot read one MUST preserve it on write: dropping it
    # destroys another participant's data, invisibly, because nobody here can
    # see what was lost.
    extensions: Dict[str, Any] = field(default_factory=dict)
    # Which schema `spec` and `checkpoint` follow and what may be asked of a job
    # of this kind. The difference between leaving a stranger's job alone and
    # being able to act on it without ever opening it. See Envelope.
    envelope: Optional["Envelope"] = None
    spec: Dict[str, Any] = field(default_factory=dict)
    checkpoint: Optional[Dict[str, Any]] = None
    progress: Progress = field(default_factory=Progress)
    lease: Lease = field(default_factory=Lease)
    delegation: Optional[Delegation] = None
    requires: List[str] = field(default_factory=list)
    error: str = ""
    # What somebody WANTS to happen, as against state, which is what IS
    # happening. Absent means run. Anyone may write it, lease or no lease;
    # the owner honours it. See Intent.
    intent: Optional["Intent"] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ---- the contract -----------------------------------------------------

    def validate(self) -> None:
        if not self.content:
            raise Invalid("a record must say what it contains")
        missing, ok = understands(self.critical)
        if not ok:
            raise UnknownSchema(f"requires {missing!r}")
        self.checkpoint_ranges()
        for name, value in (self.extensions or {}).items():
            if not str(name).strip():
                raise Invalid("an extension needs a name saying who understands it")
        if self.intent is not None and self.intent.want not in _WANTS:
            # Refused rather than treated as run. Guessing here means carrying
            # on with a job somebody asked to stop, using a word this
            # implementation is too old to understand.
            raise Invalid(f"intent {self.intent.want!r}")
        if not self.id.strip():
            raise Invalid("id is required")
        if not self.kind.strip():
            raise Invalid("kind is required — an opaque spec nobody can identify is unusable")
        if self.state not in _STATES:
            raise Invalid(f"state {self.state!r}")
        if self.envelope is not None:
            self.envelope.validate()
        if self.spec is None or not isinstance(self.spec, dict):
            raise Invalid("spec must be present and be a JSON object")
        if self.progress.done < 0 or self.progress.total < 0:
            raise Invalid("progress cannot be negative")
        st = self.progress.step
        if st is not None:
            # A step with no name tells a person nothing, which is the only
            # thing a step is for.
            if not st.name.strip():
                raise Invalid("a step needs a name")
            if st.ordinal < 1:
                raise Invalid(f"step ordinal counts from one, got {st.ordinal}")
            if st.of > 0 and st.ordinal > st.of:
                raise Invalid(f"step {st.ordinal} of {st.of}")
            if st.done < 0 or st.total < 0:
                raise Invalid("step progress cannot be negative")
        if self.delegation is not None:
            if not self.delegation.system.strip() or not self.delegation.external_id.strip():
                raise Invalid("delegation needs both a system and an external id")
        rc = self.lease.recall
        if rc is not None:
            if not rc.reason.strip():
                raise Invalid("a recall needs a reason the holder can act on")
            if rc.until.timestamp() == 0:
                raise Invalid("a recall needs a deadline")

    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def wants(self) -> str:
        """The desired state, which is RUN unless somebody said otherwise.

        Callers use this rather than testing ``intent`` for None, so that
        "nobody asked for anything" and "somebody asked for it to run" are
        the same answer everywhere — including for version 3 records, which
        have no intent at all.
        """
        if self.intent is None or not self.intent.want:
            return RUN
        return self.intent.want

    def paused(self) -> bool:
        """Somebody asked this to stop and it has not finished."""
        return self.wants() == PAUSE and not self.terminal()

    def stranded(self) -> bool:
        """Whether this record's own state and intent leave work for a sweep.

        Written once and asked by every store, because the two answers here were
        each bought with a real loss and neither is recoverable from the words
        "nobody holds the lease".

        A TRANSFERRED job is not stranded, and the difference cost a NAS 313 MB:
        transferred is deliberately not terminal, so a sweep saw a finished,
        digest-proven job with a lapsed lease and fetched the whole thing again,
        then again thirty seconds later, forever. What it waits for is an
        acknowledgement and no amount of re-downloading produces one.

        A PAUSED job is not stranded either -- it looks abandoned and is not,
        and adopting it would restart work seconds after a person stopped it.
        Unless nobody ever carried the pause out: an owner that honours one
        releases the lease and the record stops being RUNNING, so a record still
        RUNNING under nobody is an owner that died between the two. Skipping
        that one makes the state permanent, because nothing may claim the job
        and therefore nothing may pause, resume or cancel it either.
        """
        if self.state == TRANSFERRED:
            return False
        return not self.paused() or self.state == RUNNING

    def delegated(self) -> bool:
        """Something outside this process is doing the work.

        A caller finding this true must not start working itself: the external
        system does not participate in the lease, so two workers is exactly the
        damage the lease exists to prevent.
        """
        return self.delegation is not None

    # ---- the envelope -----------------------------------------------------

    def schema(self) -> str:
        """The schema this job's opaque halves follow, or "" when the record does
        not say. A name to compare, not a place to look."""
        return self.envelope.schema if self.envelope is not None else ""

    def actions(self) -> List[str]:
        """The action names the kind declares, in the record's own order."""
        return list(self.envelope.actions) if self.envelope is not None else []

    def supports(self, action: str) -> bool:
        """Does the KIND declare this action?

        Not whether it can be performed now: this is a static declaration, and
        the worker may have died an hour ago. A bare name and a qualified one
        are different actions and never match each other, which is the point of
        qualifying at all.
        """
        return self.envelope is not None and action in self.envelope.actions

    def ask(self, action: str, supervisor: "Supervisor") -> None:
        """May this supervisor perform ``action`` on this record?

        It performs nothing, and every refusal names what was refused, because a
        supervisor that silently declines is indistinguishable from one that
        quietly did the wrong thing.

        Invalid        the name is not an action name
        UnknownSchema  the record follows a schema this supervisor does not know
        NotSupported   the kind does not declare it, or this supervisor has not
                       implemented it
        """
        schema, _, ok = split_action(action)
        if not ok:
            raise Invalid(f"{action!r} is not an action name")
        if self.envelope is None:
            raise NotSupported(f"{self.id} declares no envelope, so it declares no actions")
        if self.envelope.schema not in supervisor.schemas:
            raise UnknownSchema(
                f"{self.envelope.schema!r}, which this supervisor was not written against"
            )
        if schema and schema not in supervisor.schemas:
            raise UnknownSchema(f"{schema!r}, which this supervisor was not written against")
        if not self.supports(action):
            raise NotSupported(f"{self.kind} does not declare {action!r}")
        if action not in supervisor.actions:
            raise NotSupported(f"this supervisor does not implement {action!r}")

    def _canonicalise_ranges(self) -> None:
        """Rewrite the two keys this layer owns into the one form all three
        implementations write, and leave every other key alone.

        A checkpoint with no ``verified`` key is untouched, byte for byte: that
        is what keeps a single-stream record identical to the one this format
        has always written, and it is why a kind that never heard of ranges is
        never rewritten.
        """
        cp = self.checkpoint
        if not isinstance(cp, dict) or VERIFIED_KEY not in cp:
            return
        self.checkpoint = checkpoint_with_ranges(cp, ranges_from_checkpoint(cp))
        if FEATURE_RANGES not in self.content:
            self.content = list(self.content) + [FEATURE_RANGES]

    def checkpoint_ranges(self) -> List[Range]:
        """The proven ranges this record's checkpoint carries.

        A record that has never checkpointed, and one whose checkpoint predates
        ranges entirely, both answer without an error: the first with the empty
        set, the second with the prefix as one range.
        """
        return ranges_from_checkpoint(self.checkpoint)

    def set_checkpoint_ranges(self, ranges) -> None:
        """Record what is proven, merged into canonical form, and declare the
        ranges feature.

        Both halves matter. Without the canonical form two writers spell one
        state two ways; without the declaration a reader cannot tell whether an
        absent ``verified`` means "nothing proven beyond the prefix" or "this
        writer had never heard of ranges".
        """
        self.checkpoint = checkpoint_with_ranges(self.checkpoint, ranges)
        if FEATURE_RANGES not in self.content:
            self.content = list(self.content) + [FEATURE_RANGES]

    def add_checkpoint_range(self, start: int, end: int) -> None:
        """Fold one newly proven range into the checkpoint. What a parallel
        fetcher calls as each part lands."""
        self.set_checkpoint_ranges(list(self.checkpoint_ranges()) + [(start, end)])

    def clear_checkpoint_ranges(self) -> None:
        """Remove the ranges and the declaration, leaving every other key alone.

        The declaration is carried rather than derived, so it has to be
        withdrawn explicitly; a record that kept declaring a feature whose data it
        no longer holds sends a reader looking for something that is not there.
        """
        if isinstance(self.checkpoint, dict):
            rest = {k: v for k, v in self.checkpoint.items() if k != VERIFIED_KEY}
            self.checkpoint = {k: rest[k] for k in sorted(rest)} or None
        self.content = [n for n in self.content if n != FEATURE_RANGES]

    def to_json(self) -> bytes:
        # One spelling of one proven set, whoever wrote it and however they
        # wrote it -- see _canonicalise_ranges. Before describe(), so the
        # declaration that follows sees the ranges the record actually carries.
        self._canonicalise_ranges()
        # Describe first, then check, which is the order Go's Encode and C++'s
        # encode() use. This module used to validate first, so a record whose
        # declaration had not been filled in yet was refused for saying nothing
        # about itself -- while the other two implementations derived the
        # declaration and wrote it. Each half was self-consistent, so no unit
        # test in one language could see it.
        self.describe()
        self.validate()
        # Key order and omissions match the Go struct tags exactly. Go decodes
        # with DisallowUnknownFields, so an extra key here is not a cosmetic
        # difference — it makes the record unreadable to the other half of the
        # abstraction.
        #
        # What this implementation writes, it writes as its own version. A
        # record read at 3 and written back at 3 while carrying an intent
        # would tell an older reader it is safe to ignore fields it does not
        # know, which is exactly the risk the schema check refuses.
        d: Dict[str, Any] = {
            "content": list(self.content),
        }
        if self.critical:
            d["critical"] = list(self.critical)
        d.update({"id": self.id, "kind": self.kind})
        if self.envelope is not None:
            env: Dict[str, Any] = {"schema": self.envelope.schema}
            if self.envelope.actions:
                env["actions"] = list(self.envelope.actions)
            d["envelope"] = env
        d.update({
            "state": self.state,
            "spec": _Opaque(self.spec),
        })
        if self.checkpoint is not None:
            d["checkpoint"] = _Opaque(self.checkpoint)
        d["progress"] = {"done": self.progress.done}
        if self.progress.total:
            d["progress"]["total"] = self.progress.total
        d["progress"]["updated_at"] = _rfc3339(self.progress.updated_at)
        if self.progress.step is not None:
            st = {"name": self.progress.step.name, "ordinal": self.progress.step.ordinal}
            if self.progress.step.of:
                st["of"] = self.progress.step.of
            if self.progress.step.done:
                st["done"] = self.progress.step.done
            if self.progress.step.total:
                st["total"] = self.progress.step.total
            d["progress"]["step"] = st
        d["lease"] = {
            "owner": self.lease.owner,
            "epoch": self.lease.epoch,
            "expires_at": _rfc3339(self.lease.expires_at),
        }
        if self.lease.recall is not None:
            rc: Dict[str, Any] = {"reason": self.lease.recall.reason}
            if self.lease.recall.by:
                rc["by"] = self.lease.recall.by
            rc["at"] = _rfc3339(self.lease.recall.at)
            rc["until"] = _rfc3339(self.lease.recall.until)
            d["lease"]["recall"] = rc
        if self.delegation is not None:
            deleg: Dict[str, Any] = {
                "system": self.delegation.system,
                "external_id": self.delegation.external_id,
            }
            if self.delegation.delivered:
                deleg["delivered"] = True
            d["delegation"] = deleg
        if self.requires:
            d["requires"] = self.requires
        if self.error:
            d["error"] = self.error
        if self.intent is not None:
            it: Dict[str, Any] = {"want": self.intent.want}
            if self.intent.by:
                it["by"] = self.intent.by
            if self.intent.at is not None:
                it["at"] = _rfc3339(self.intent.at)
            d["intent"] = it
        if self.extensions:
            # Sorted, because a dict has no order the other implementations
            # share and this output is compared byte for byte against them.
            d["extensions"] = {
                k: _Opaque(self.extensions[k]) for k in sorted(self.extensions)
            }
        d["created_at"] = _rfc3339(self.created_at)
        d["updated_at"] = _rfc3339(self.updated_at)
        return (_write(d, 0) + "\n").encode("utf-8")

    def describe(self) -> None:
        """Fill in content and critical from what this record actually carries.

        Derived rather than remembered, so the declaration cannot drift from the
        data: a record that gained an intent since it was last written says so on
        the next write without anybody updating a list.

        Caller-declared criticals are preserved -- an extension's writer may have
        said "refuse this record if you cannot read my payload", and this layer
        is not entitled to downgrade that. The exception is NEVER_CRITICAL,
        where the feature's own definition says nobody may demand it.
        """
        content = [FEATURE_BASE]
        critical = [FEATURE_BASE]
        if self.intent is not None:
            content.append(FEATURE_INTENT)
            critical.append(FEATURE_INTENT)
        if self.delegation is not None:
            content.append(FEATURE_DELEGATION)
            critical.append(FEATURE_DELEGATION)
        if self.envelope is not None:
            content.append(FEATURE_ENVELOPE)
            critical.append(FEATURE_ENVELOPE)
        # Advisory, and deliberately not critical: a reader that ignores a step
        # is correct about everything that matters.
        if self.progress.step is not None:
            content.append(FEATURE_STEP)
        if self.terminal():
            content.append(FEATURE_TERMINAL)
            critical.append(FEATURE_TERMINAL)
        if self.lease.recall is not None:
            content.append(FEATURE_RECALL)
            critical.append(FEATURE_RECALL)
        # A feature this layer declares but cannot derive, because what it
        # describes lives inside the checkpoint and a checkpoint is opaque here.
        # Rediscovering it would mean reading one, so the declaration is carried
        # instead -- the same treatment an unknown extension gets. Sorted, and
        # placed here rather than among the extension names, so all three
        # implementations put it in the same slot. See CARRIED_FEATURES.
        content.extend(sorted({n for n in self.content or () if n in CARRIED_FEATURES}))
        content.extend(sorted(self.extensions or {}))

        for name in self.critical or ():
            if name not in critical and name in content and name not in NEVER_CRITICAL:
                critical.append(name)

        self.content = content
        self.critical = critical

    @staticmethod
    def from_json(b: bytes) -> "Record":
        """Refuse in this layer's own vocabulary, whatever went wrong inside.

        Which class of refusal happened is the contract, and a record whose
        `id` is null or whose `progress` is a number left this door as an
        AttributeError, which is not a class any caller can branch on. The
        refusal itself is unchanged: everything below already declined these
        records, in a word from somebody else's vocabulary.
        """
        try:
            return Record._decode(b)
        except JobError:
            raise
        except Exception as e:
            raise Invalid(f"{type(e).__name__}: {e}") from None

    @staticmethod
    def _decode(b: bytes) -> "Record":
        try:
            if isinstance(b, (bytes, bytearray)):
                b = b.decode(json.detect_encoding(b), "surrogatepass")
            d = _SPELLING.decode(b)
        except ValueError as e:
            raise Invalid(str(e)) from None
        if not isinstance(d, dict):
            raise Invalid("record must be a JSON object")
        # An unknown field is far more likely to be a newer writer than a typo,
        # and continuing a job whose description we only partly understand is
        # exactly the risk this refuses to take. Go and C++ have always refused
        # here; this side quietly DROPPED the field instead, which is worse than
        # either -- it destroyed a participant's data on the next write, and did
        # it invisibly, because nobody here could read what was lost.
        _reject_unknown_keys(
            d,
            (
                "schema", "content", "critical", "id", "kind", "envelope", "state",
                "spec", "checkpoint", "progress", "lease", "delegation", "requires",
                "error", "intent", "extensions", "created_at", "updated_at",
            ),
            "record",
        )
        _reject_unknown_keys(d.get("envelope"), ("schema", "actions"), "envelope")
        _reject_unknown_keys(d.get("progress"), ("done", "total", "updated_at", "step"), "progress")
        _reject_unknown_keys(
            (d.get("progress") or {}).get("step"),
            ("name", "ordinal", "of", "done", "total"),
            "step",
        )
        _reject_unknown_keys(d.get("lease"), ("owner", "epoch", "expires_at", "recall"), "lease")
        _reject_unknown_keys(
            (d.get("lease") or {}).get("recall"), ("reason", "by", "at", "until"), "recall"
        )
        _reject_unknown_keys(
            d.get("delegation"), ("system", "external_id", "delivered"), "delegation"
        )
        _reject_unknown_keys(d.get("intent"), ("want", "by", "at"), "intent")
        content = _name_list(d.get("content"), "content")
        critical = _name_list(d.get("critical"), "critical")
        if not content and "schema" in d:
            # A legacy record. The mapping is exact, not a guess: those versions
            # are frozen and it is known what each one could contain.
            features = LEGACY_SCHEMAS.get(d.get("schema"))
            if features is None:
                raise UnknownSchema(f"legacy schema {d.get('schema')}")
            content = list(features)
            critical = [m for m in features if m != FEATURE_STEP]
            if d.get("delegation") is not None:
                content.append(FEATURE_DELEGATION)
                critical.append(FEATURE_DELEGATION)
        critical = _read_critical(critical, content)
        p = d.get("progress") or {}
        l = d.get("lease") or {}
        dg = d.get("delegation")
        it = d.get("intent")
        ev = d.get("envelope")
        r = Record(
            id=d.get("id", ""),
            kind=d.get("kind", ""),
            state=d.get("state", ""),
            content=content,
            critical=critical,
            extensions=dict(d.get("extensions") or {}),
            envelope=(
                Envelope(
                    schema=ev.get("schema", ""),
                    actions=list(ev.get("actions") or []),
                )
                if isinstance(ev, dict)
                else None
            ),
            spec=d.get("spec"),
            checkpoint=d.get("checkpoint"),
            progress=Progress(
                step=(
                    Step(
                        name=(p.get("step") or {}).get("name", ""),
                        ordinal=(p.get("step") or {}).get("ordinal", 0),
                        of=(p.get("step") or {}).get("of", 0),
                        done=(p.get("step") or {}).get("done", 0),
                        total=(p.get("step") or {}).get("total", 0),
                    )
                    if isinstance(p.get("step"), dict)
                    else None
                ),
                done=p.get("done", 0),
                total=p.get("total", 0),
                updated_at=_parse_time(p.get("updated_at", "")),
            ),
            lease=Lease(
                owner=l.get("owner", ""),
                epoch=l.get("epoch", 0),
                expires_at=_parse_time(l.get("expires_at", "")),
                recall=(
                    Recall(
                        reason=l["recall"].get("reason", ""),
                        by=l["recall"].get("by", ""),
                        at=_parse_time(l["recall"].get("at", "")),
                        until=_parse_time(l["recall"].get("until", "")),
                    )
                    if isinstance(l.get("recall"), dict)
                    else None
                ),
            ),
            delegation=(
                Delegation(
                    system=dg.get("system", ""),
                    external_id=dg.get("external_id", ""),
                    delivered=dg.get("delivered", False),
                )
                if dg
                else None
            ),
            requires=d.get("requires") or [],
            error=d.get("error", ""),
            intent=(
                Intent(
                    want=it.get("want", RUN),
                    by=it.get("by", ""),
                    at=_parse_time(it["at"]) if it.get("at") else None,
                )
                if it
                else None
            ),
            created_at=_parse_time(d.get("created_at", "")),
            updated_at=_parse_time(d.get("updated_at", "")),
        )
        r.validate()
        return r


def _renewed_until(lease: Lease, now: datetime, ttl_seconds: float) -> datetime:
    """Where a renewal lands: the caller's TTL, or the recall's deadline if that
    comes first. Without the cap a holder keeps its lease alive through any
    recall simply by renewing, and eviction is a suggestion."""
    want = datetime.fromtimestamp(now.timestamp() + ttl_seconds, timezone.utc)
    if lease.recall is not None and lease.recall.until < want:
        return lease.recall.until
    return want


def _recall(r: Record, now: datetime, epoch: int, reason: str, by: str,
            grace_seconds: float) -> None:
    """The rule, shared by every binding. Refusals come in the order a caller
    would ask about them: finished, moved, nobody there."""
    if r.terminal():
        raise Terminal(f"{r.id} is {r.state}")
    if r.lease.epoch != epoch:
        raise StaleEpoch(f"record is at epoch {r.lease.epoch}, recall was decided against {epoch}")
    if not r.lease.held(now):
        raise LeaseExpired(f"nobody holds {r.id}")
    until = datetime.fromtimestamp(now.timestamp() + grace_seconds, timezone.utc)
    r.lease.recall = Recall(reason=reason, by=by, at=now, until=until)
    if until < r.lease.expires_at:
        r.lease.expires_at = until
    r.updated_at = now


def new_id() -> str:
    """An id that sorts by creation time, so a directory listing is in roughly
    submission order without anything having to record that separately."""
    return f"{int(time.time() * 1000)}-{binascii.hexlify(secrets.token_bytes(10)).decode()}"


@runtime_checkable
class Store(Protocol):
    """Where jobs live. Every other abstraction in this project sits on it.

    # What this protocol is, and what it deliberately is not

    It is the SEMANTICS of the lease protocol and nothing else: a claim is
    exclusive, an epoch only ever increases, every write must present the epoch
    it holds, and a successor may continue only from what a predecessor proved.
    That is what two implementations have to agree about, and not one clause of
    it mentions a byte.

    It is NOT a file format and NOT a transport. Records have to be represented
    somehow and a call has to reach whoever executes it somehow, but both belong
    to the BINDING underneath -- files in a directory today, a service over a
    socket next, a machine across the network after that. Changing the
    representation must be invisible from here.

    Go and C++ both had this and Python did not. The whole public surface of
    this module was ``FileStore``, so the only type Python offered named its own
    binding -- and it showed one layer up, where the download runner reached
    through ``store.root`` for a directory that a service binding does not have.

    The test an abstraction has to pass is THE SAME APPLICATION, UNCHANGED,
    RUNNING ON TWO BINDINGS.
    """

    def submit(self, r: "Record") -> str:
        """Record new work and return its id. The id is the handle: a plain
        string that outlives the process which created it."""
        ...

    def load(self, job_id: str) -> "Record":
        """Read a record. Any process may, including one that holds no lease and
        never will -- that is what makes work observable from outside, which a
        callback cannot be, because a callback is bound to the lifetime of the
        process that registered it and that is exactly the lifetime which
        fails."""
        ...

    def list(self) -> List["Record"]:
        """Every job, oldest first."""
        ...

    def claimable(self, r: "Record") -> bool:
        """Whether this job can be taken over right now."""
        ...

    def orphans(self) -> List["Record"]:
        """Work nobody is doing. The reclamation path, and primary rather than a
        fallback: a process that is killed never hands anything over, so a
        design relying on graceful handoff has no answer for the case that loses
        a 40 GB download."""
        ...

    def claim(self, job_id: str, owner: str, ttl_seconds: float) -> "Record":
        """Take ownership for ttl_seconds and return the record carrying the
        caller's new epoch. Exclusive: two callers cannot hold the same epoch."""
        ...

    def renew(self, job_id: str, epoch: int, ttl_seconds: float) -> "Record":
        """Extend a lease the caller still holds. Must refuse once the lease has
        expired even when the epoch still matches, because a process suspended
        for an hour wakes up believing it is still the owner."""
        ...

    def release(self, job_id: str, epoch: int) -> None:
        """Give up a lease early. A courtesy: everything works without it, only
        more slowly. It is an update, so it is refused on a finished job -- there
        is nothing left to hand over, and the lease lapses on its own."""
        ...

    def update(self, job_id: str, epoch: int, mutate: Callable[["Record"], None]) -> "Record":
        """Apply mutate, but only if the caller still holds the lease at the
        epoch it presents AND the record is not already complete, failed or
        cancelled. The single gate every change passes through.

        Terminal is judged on the record as read, so the write that ends a job
        lands and the one after it does not. An owner therefore puts everything
        it wants recorded into the same update as the final state.
        """
        ...

    def set_intent(self, job_id: str, want: str, by: str = "") -> "Record":
        """Say what should happen, WITHOUT a lease.

        The one write that presents no epoch. Whoever wants a job stopped is not
        the process doing it -- a person clicks cancel while a service on another
        machine moves the bytes -- and requiring a lease would mean stealing the
        job in order to stop it, which is what the lease prevents.
        """
        ...

    def recall(self, job_id: str, epoch: int, reason: str, by: str = "",
               grace_seconds: float = 30.0) -> "Record":
        """Ask the holder at ``epoch`` -- the epoch the caller OBSERVED, not one
        it holds -- for the lease back by now+grace, for a reason it can act
        on. The lease lapses at that deadline either way: that is the fallback,
        and what makes this a demand rather than a suggestion. Not an intent.
        """
        ...


@runtime_checkable
class Scratch(Protocol):
    """An OPTIONAL capability: a store whose binding happens to BE a local
    filesystem can offer an area on it.

    It exists to contain a leak, not to bless one. ``store.root`` was an
    attribute anyone could reach for, and the download runner did, to resolve a
    relative sink. A store bound to a service has no directory to give, so the
    caller must ask::

        if isinstance(store, Scratch):
            ...

    and have a real answer for no, rather than assuming a directory exists.
    """

    def root(self) -> str:
        """The local area this binding keeps its own state in. Relative paths in
        a record resolve against it, so a record written by a PC and read by a
        NAS names one directory rather than one machine's view of it."""
        ...

    def work_path(self, job_id: str) -> str:
        """Scratch space for a single job. What a kind puts there is its own
        business; the only guarantee is that the location is derived from the id,
        so a successor can find what a predecessor left."""
        ...


REGISTRY_FILE = "services.json"


def _store_segments(rel: str) -> List[str]:
    """A relative path reduced to the segments a store would see, in the one
    spelling two of them can be compared in.

    A path that climbs above the root returns nothing: that is containment's
    question, answered before this one is asked, and answering it again here
    with a different rule would give two verdicts on one record.
    """
    out: List[str] = []
    for seg in rel.replace("\\", "/").split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if not out:
                return []
            out.pop()
            continue
        out.append(_fold_segment(seg))
    return out


def _fold_segment(seg: str) -> str:
    """One segment in the spelling a filesystem would give it.

    Windows drops a trailing dot or space from a name, so `jobs.` opens `jobs`.
    Dropping it here too keeps the answer the same on both platforms, which is
    the property that matters: a record refused by one and accepted by the other
    would mean the refusal depends on who looked. A name that is NOTHING but
    dots and spaces is left alone -- it is a real name on POSIX, and not one
    Windows will open at all.
    """
    trimmed = seg.strip()
    return (trimmed.rstrip(". ") or trimmed).lower()


def reserved(owner: str, rel: str) -> bool:
    """Does rel -- a path relative to the store root -- name part of the file
    binding's own layout rather than free space inside it?

    A layer above this one lets a caller name where bytes land, and resolves a
    relative destination against the store root. Containment stopped such a path
    climbing OUT of the root; it never stopped one aiming at what is IN it. A
    final of `jobs/<id>.json` overwrites a job record and a final of
    `work/<other>` overwrites another job's partial, and both are contained.

    The layout is this binding's, so the answer is this binding's to give. The
    alternative was the download layer spelling "jobs" and "work" itself, which
    is the coupling the opaque spec exists to prevent.

    ``owner`` is the job asking. Its own scratch -- work/<owner> and everything
    under it -- is not reserved against that job and is reserved against every
    other, so a download can still put its partial where the store told it to.
    An empty owner asks on behalf of no job, which reserves the whole of work/.

    Case is folded and a trailing dot or space trimmed from every segment, on
    every platform. Deliberately unlike the containment comparison, which folds
    case only where the filesystem does: containment compares two paths on THIS
    machine, and this compares a record's path against names the contract fixed.
    A record refused by Windows and accepted by Linux would mean the refusal
    depends on who looked -- and `Jobs/x.json` does land in `jobs/` on NTFS, as
    does `jobs./x.json`.
    """
    segs = _store_segments(rel)
    if not segs:
        return False
    if segs[0] == "jobs":
        return True
    if segs[0] == "work":
        return len(segs) < 2 or segs[1] != _fold_segment(owner or "")
    return segs[0] == REGISTRY_FILE and len(segs) == 1


def root_name(rel: str) -> str:
    """The name rel takes in the store root, or "" if it names something deeper,
    climbs out of the root, or is empty.

    The root is a shared namespace and this binding is not its only occupant:
    the download layer keeps a supervisor heartbeat and a nudge socket beside
    jobs/ and work/. A layer that puts a file there has to be able to ask
    whether a path aims at it, and to ask in the same spelling, or the two of
    them protect different sets of names.
    """
    segs = _store_segments(rel)
    return segs[0] if len(segs) == 1 else ""


class FileStore:
    """Jobs as files in a directory.

    A directory is the one thing a Go process, a Python process, a Windows
    service and a machine that has just rebooted can all agree on without any of
    them running at the same time. No daemon, no database, no socket.

        <root>/jobs/<id>.json        the record, every write a cas change on it
        <root>/jobs/<id>.json.lock   the lock cas takes for that write
        <root>/work/<id>             the name job <id> may spend on scratch
    """

    def __init__(self, root: str, now: Callable[[], datetime] = None):
        self._root = root
        self._now = now or (lambda: datetime.now(timezone.utc))
        os.makedirs(os.path.join(root, "jobs"), exist_ok=True)
        os.makedirs(os.path.join(root, "work"), exist_ok=True)

    # ---- paths ------------------------------------------------------------

    def root(self) -> str:
        """The local area this binding keeps its own state in.

        A method and not a field on purpose. As an attribute it was reachable
        from anywhere without anyone having to admit they needed a directory,
        and the download runner duly reached for it -- so the layer that is not
        supposed to know what a file is could not run on a store that has no
        files. Now it is the Scratch capability, and a caller that wants it must
        ask whether this binding has one.
        """
        return self._root

    def _record_path(self, job_id: str) -> str:
        return os.path.join(self._root, "jobs", job_id + ".json")

    def work_path(self, job_id: str) -> str:
        """The name this job may spend on scratch while it runs.

        A NAME, not a shape. The store guarantees it is derived from the id -- so
        a successor finds what a predecessor left -- and that nothing else will
        use it. Whether the job makes it a file or a directory is the job's
        business: download writes its partial there as a file. The store creates
        work/; it does not create this. `reserved` says the same from the other
        side.
        """
        return os.path.join(self._root, "work", job_id)

    # ---- reading ----------------------------------------------------------

    def load(self, job_id: str) -> Record:
        """Any process may do this, including one that holds no lease and never
        will. That is what makes progress observable from outside — a callback
        cannot, because a callback is bound to the lifetime of the process that
        registered it, and that lifetime is the one that fails."""
        data = _cas.read(self._record_path(job_id))
        if data is None:
            raise NotFound(job_id)
        return Record.from_json(data)

    def list(self) -> List[Record]:
        out = []
        d = os.path.join(self._root, "jobs")
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                out.append(self.load(name[: -len(".json")]))
            except JobError:
                continue  # one unreadable record must not hide every other job
        return out

    def claimable(self, r: Record) -> bool:
        return not r.terminal() and not r.lease.held(self._now())

    def orphans(self) -> List[Record]:
        """The jobs nobody is working on.

        This is the primary reclamation path, not a fallback. A process that is
        SIGKILLed, or a machine that loses power, never gets to hand anything
        over — so a design that relies on graceful handoff has no answer for the
        case that actually loses a 40 GB download.

        Which records qualify is ``Record.stranded``.
        """
        return [r for r in self.list() if self.claimable(r) and r.stranded()]

    # ---- writing ----------------------------------------------------------

    def _change(self, job_id: str, edit: Callable[[Record], None]) -> Record:
        out: List[Record] = []

        def step(cur):
            if cur is None:
                raise NotFound(job_id)
            r = Record.from_json(cur)
            edit(r)
            out.append(r)
            return r.to_json()

        _cas.change(self._record_path(job_id), step)
        return out[0]

    def set_intent(self, job_id: str, want: str, by: str = "") -> Record:
        """Say what should happen, WITHOUT holding the lease.

        The only write here that presents no epoch, and deliberately so: the
        party who wants a job stopped is not the process doing it, and requiring
        a lease would mean stealing the job in order to stop it — the one thing
        the lease exists to prevent.

        Idempotent, and refused once the job is terminal, because nothing
        reopens finished work. Asking for something the current owner cannot do
        is NOT an error here: only the owner knows what it can do, so only the
        owner can say so.
        """
        if want not in _WANTS:
            raise Invalid(f"intent {want!r}")

        def edit(r: Record) -> None:
            if r.terminal():
                raise Terminal(f"{job_id} is {r.state}")
            now = self._now()
            r.intent = Intent(want=want, by=by, at=now)
            r.updated_at = now

        return self._change(job_id, edit)

    def submit(self, r: Record) -> str:
        if not r.id:
            r.id = new_id()
        now = self._now()
        r.describe()
        if not r.state:
            r.state = PENDING
        r.created_at = now
        r.updated_at = now
        r.progress.updated_at = now
        try:
            _cas.write(self._record_path(r.id), None, r.to_json())
        except _cas.Moved:
            raise Invalid(f"{r.id} already exists") from None
        return r.id

    def claim(self, job_id: str, owner: str, ttl_seconds: float) -> Record:
        """Take ownership for ttl_seconds, returning the record with the caller's
        new epoch. Every later write must present that epoch."""
        return self.claim_from(self.load(job_id), owner, ttl_seconds)

    def claim_from(self, seen: Record, owner: str, ttl_seconds: float) -> Record:
        """Take ownership of the job AS THE CALLER LAST READ IT.

        Refused with LeaseHeld if the job changed hands since, so a sweeper that
        already holds a record from ``orphans`` is told rather than quietly
        claiming a job that is no longer the one it decided about. The record
        written is the one on disk under the lock, never the caller's copy, so
        an intent set since the caller read is kept.
        """
        if not owner.strip():
            raise JobError("claim requires an owner")

        def edit(r: Record) -> None:
            if r.lease.epoch != seen.lease.epoch:
                raise LeaseHeld(
                    f"the record moved to epoch {r.lease.epoch} since it was read at {seen.lease.epoch}"
                )
            if r.terminal():
                raise Terminal(f"{r.id} is {r.state}")
            now = self._now()
            if r.lease.held(now) and (r.lease.owner != owner or r.lease.recalled()):
                raise LeaseHeld(f"{r.lease.owner} holds it until {_rfc3339(r.lease.expires_at)}")
            r.lease = Lease(
                owner=owner,
                epoch=r.lease.epoch + 1,
                expires_at=datetime.fromtimestamp(now.timestamp() + ttl_seconds, timezone.utc),
            )
            if r.state in (PENDING, RUNNING):
                r.state = RUNNING

        return self._change(seen.id, edit)

    def renew(self, job_id: str, epoch: int, ttl_seconds: float) -> Record:
        """Extend a lease the caller still holds.

        Refuses once expired even when the epoch still matches and nobody else
        has claimed. That is the sleep case: a process suspended for an hour
        wakes believing it is still the owner. Forcing it to re-claim bumps the
        epoch, so anything it had in flight is refused rather than landing on top
        of work a different owner may since have done.
        """

        def edit(r: Record) -> None:
            if r.lease.epoch != epoch:
                raise StaleEpoch(f"record is at epoch {r.lease.epoch}, caller holds {epoch}")
            if not r.lease.held(self._now()):
                raise LeaseExpired(f"expired at {_rfc3339(r.lease.expires_at)}, re-claim instead")
            r.lease.expires_at = _renewed_until(r.lease, self._now(), ttl_seconds)

        return self._change(job_id, edit)

    def recall(self, job_id: str, epoch: int, reason: str, by: str = "",
               grace_seconds: float = 30.0) -> Record:
        if not reason.strip():
            raise Invalid("a recall needs a reason the holder can act on")
        return self._change(job_id, lambda r: _recall(r, self._now(), epoch, reason, by, grace_seconds))

    def release(self, job_id: str, epoch: int) -> None:
        """Give up a lease early so the job can be taken immediately. The
        graceful path, and only a courtesy: everything still works without it,
        just more slowly."""

        def mutate(r: Record) -> None:
            r.lease.expires_at = self._now()
            r.lease.owner = ""
            # A delegated job stays RUNNING. Letting go of the lease means "I am
            # not the one watching this any more", not "this stopped" — the work
            # is going on inside BITS, or on a NAS, and demoting it to pending
            # tells every supervisor sweeping for stranded work to start it over
            # somewhere else. Delegating releases the lease immediately, so this
            # is not an edge case; it is what delegation does.
            if r.state == RUNNING and not r.delegated():
                r.state = PENDING

        self.update(job_id, epoch, mutate)

    def update(self, job_id: str, epoch: int, mutate: Callable[[Record], None]) -> Record:
        """The single gate every change passes through, so staleness is checked
        in exactly one place instead of once per call site.

        The terminal test reads the state as LOADED, never the state mutate
        leaves behind, and the difference is how a job ever finishes: the write
        that MAKES a record complete, failed or cancelled is an ordinary update
        onto a record that is not yet terminal, and it must land. What is refused
        is the write after it. So an owner puts everything it wants recorded --
        the error text, the last checkpoint -- into the same update as the final
        state, because nothing of its epoch may write again.
        """

        def edit(r: Record) -> None:
            if r.terminal():
                raise Terminal(f"{job_id} is {r.state}")
            if r.lease.epoch != epoch:
                raise StaleEpoch(f"record is at epoch {r.lease.epoch}, caller holds {epoch}")
            if not r.lease.held(self._now()):
                raise LeaseExpired(f"expired at {_rfc3339(r.lease.expires_at)}")
            kind = r.envelope.copy() if r.envelope is not None else None
            mutate(r)
            _envelope_unmoved(kind, r.envelope)
            r.updated_at = self._now()

        return self._change(job_id, edit)


class MemoryStore:
    """Jobs in a dict, for a process that wants the semantics and no disk.

    The point of this binding is not convenience. A socket in front of a
    FileStore is a transport swap and proves little; this shares no code with
    FileStore at all. The lease, the epoch, the exclusivity, the refusal of a
    stale write, and the rule that a paused or transferred job is not an orphan
    are all written again here. If those semantics were really FileStore's
    filesystem tricks all along -- exclusive create, atomic rename -- this is
    where that shows, because a dict has neither.

    Deliberately NOT a Scratch: there is no directory, and a caller that assumed
    one gets told so instead of being handed a path that does not exist. That is
    the whole reason the capability is separate.

    Not durable and not shared between processes, which is exactly what makes it
    honest about being one binding among several rather than the definition.
    """

    def __init__(self, now: Callable[[], datetime] = None):
        self._jobs: Dict[str, bytes] = {}
        self._now = now or (lambda: datetime.now(timezone.utc))

    # Stored encoded, and decoded on every read. Not a detail: handing out the
    # same object twice would let a caller mutate a record the store still
    # believes it owns, so an in-memory store would be the one binding where a
    # write did not have to pass the epoch gate. Encoding also means this
    # binding is held to the same record rules as any other -- a Record that
    # would not survive a round trip does not survive here either.
    def _get(self, job_id: str) -> Record:
        raw = self._jobs.get(job_id)
        if raw is None:
            raise NotFound(job_id)
        return Record.from_json(raw)

    def _put(self, r: Record) -> None:
        self._jobs[r.id] = r.to_json()

    # ---- reading ----------------------------------------------------------

    def load(self, job_id: str) -> Record:
        return self._get(job_id)

    def list(self) -> List[Record]:
        return [Record.from_json(raw) for _, raw in sorted(self._jobs.items())]

    def claimable(self, r: Record) -> bool:
        return not r.terminal() and not r.lease.held(self._now())

    def orphans(self) -> List[Record]:
        return [r for r in self.list() if self.claimable(r) and r.stranded()]

    # ---- writing ----------------------------------------------------------

    def submit(self, r: Record) -> str:
        if not r.id:
            r.id = new_id()
        if r.id in self._jobs:
            raise Invalid(f"{r.id} already exists")
        now = self._now()
        r.describe()
        if not r.state:
            r.state = PENDING
        r.created_at = now
        r.updated_at = now
        r.progress.updated_at = now
        self._put(r)
        return r.id

    def claim(self, job_id: str, owner: str, ttl_seconds: float) -> Record:
        if not owner.strip():
            raise JobError("claim requires an owner")
        r = self._get(job_id)
        if r.terminal():
            raise Terminal(f"{job_id} is {r.state}")
        now = self._now()
        if r.lease.held(now) and (r.lease.owner != owner or r.lease.recalled()):
            raise LeaseHeld(f"{r.lease.owner} holds it until {_rfc3339(r.lease.expires_at)}")
        r.lease = Lease(
            owner=owner,
            epoch=r.lease.epoch + 1,
            expires_at=datetime.fromtimestamp(now.timestamp() + ttl_seconds, timezone.utc),
        )
        if r.state in (PENDING, RUNNING):
            r.state = RUNNING
        self._put(r)
        return r

    def renew(self, job_id: str, epoch: int, ttl_seconds: float) -> Record:
        r = self._get(job_id)
        if r.lease.epoch != epoch:
            raise StaleEpoch(f"record is at epoch {r.lease.epoch}, caller holds {epoch}")
        if not r.lease.held(self._now()):
            raise LeaseExpired(f"expired at {_rfc3339(r.lease.expires_at)}, re-claim instead")
        r.lease.expires_at = _renewed_until(r.lease, self._now(), ttl_seconds)
        self._put(r)
        return r

    def recall(self, job_id: str, epoch: int, reason: str, by: str = "",
               grace_seconds: float = 30.0) -> Record:
        if not reason.strip():
            raise Invalid("a recall needs a reason the holder can act on")
        r = self._get(job_id)
        _recall(r, self._now(), epoch, reason, by, grace_seconds)
        self._put(r)
        return r

    def release(self, job_id: str, epoch: int) -> None:
        def mutate(r: Record) -> None:
            r.lease.expires_at = self._now()
            r.lease.owner = ""
            # A delegated job stays RUNNING: letting go of the lease means "I am
            # not the one watching this any more", not "this stopped".
            if r.state == RUNNING and not r.delegated():
                r.state = PENDING

        self.update(job_id, epoch, mutate)

    def update(self, job_id: str, epoch: int, mutate: Callable[[Record], None]) -> Record:
        r = self._get(job_id)
        if r.terminal():
            raise Terminal(f"{job_id} is {r.state}")
        if r.lease.epoch != epoch:
            raise StaleEpoch(f"record is at epoch {r.lease.epoch}, caller holds {epoch}")
        if not r.lease.held(self._now()):
            raise LeaseExpired(f"expired at {_rfc3339(r.lease.expires_at)}")
        kind = r.envelope.copy() if r.envelope is not None else None
        mutate(r)
        _envelope_unmoved(kind, r.envelope)
        r.updated_at = self._now()
        self._put(r)
        return r

    def set_intent(self, job_id: str, want: str, by: str = "") -> Record:
        if want not in _WANTS:
            raise Invalid(f"intent {want!r}")
        r = self._get(job_id)
        if r.terminal():
            raise Terminal(f"{job_id} is {r.state}")
        now = self._now()
        r.intent = Intent(want=want, by=by, at=now)
        r.updated_at = now
        self._put(r)
        return r


# ---------------------------------------------------------------- watching ---

POLL_EVERY = 0.75


@dataclass
class Notice:
    records: List[Record]
    quiet: bool = False
    silence: float = 0.0


class Subscription:
    """A live view of the jobs of one kind. ``records`` is what was true at the
    last read; ``next`` is what reads."""

    def __init__(self, inner):
        self._inner = inner

    def records(self) -> List[Record]:
        return list(self._inner.current() or [])

    def next(self, timeout: Optional[float] = None) -> Notice:
        n = self._inner.next(timeout)
        return Notice(list(n.now or []), n.quiet, n.silence)

    def __iter__(self):
        return self

    def __next__(self) -> Notice:
        from abstraction_watch import Closed

        try:
            return self.next()
        except Closed:
            raise StopIteration

    def close(self) -> None:
        self._inner.close()


def watch(store: Store, kind: str, budget: float = 0.0) -> Subscription:
    """The jobs of one kind, and quiet once nothing visible has changed for
    ``budget`` seconds. The file binding has nothing to push, so it is asked,
    at most every POLL_EVERY."""
    from abstraction_watch import poll

    every = POLL_EVERY if budget <= 0 else min(POLL_EVERY, budget)

    def read():
        mine = [r for r in store.list() if r.kind == kind]
        return mine, _fingerprint(mine)

    return Subscription(poll(read, every, budget))


def _fingerprint(records: List[Record]) -> str:
    """What "changed" means: identity, state, progress, owner and error. Not
    updated_at -- a lease renewal moves it and nothing a person can see."""
    return "".join(
        f"{r.id}|{r.state}|{r.progress.done}/{r.progress.total}|{r.lease.owner}|{r.error}\n"
        for r in records
    )


class KeepAwake:
    """The machine stays out of idle sleep while this process holds the lease
    ``claimed`` carries, and not a moment longer: ended by ``release``, by the
    lease ending in the store -- released, lapsed, or the job turning terminal
    -- and by the operating system if the process dies. The lease is the
    lifetime; nothing here has one of its own."""

    def __init__(self, store: Store, claimed: Record):
        self._store = store
        self._id = claimed.id
        self._epoch = claimed.lease.epoch
        self._until = claimed.lease.expires_at
        self._lock = threading.Lock()
        self._free: Optional[Callable[[], None]] = None
        self._sub: Optional[Subscription] = None
        self._follower: Optional[threading.Thread] = None
        if not self._alive(claimed):
            return
        self._free = _keep_awake(claimed.lease.owner, f"{claimed.kind} {claimed.id}")
        if self._free is None:
            return
        self._sub = watch(store, claimed.kind)
        self._follower = threading.Thread(target=self._follow, daemon=True)
        self._follower.start()

    def held(self) -> bool:
        with self._lock:
            return self._free is not None

    def release(self) -> None:
        with self._lock:
            free, self._free = self._free, None
            sub, self._sub = self._sub, None
        if sub is not None:
            sub.close()
        if free is not None:
            free()
        follower = self._follower
        if follower is not None and follower is not threading.current_thread():
            follower.join()

    def _follow(self) -> None:
        from abstraction_watch import Closed

        while True:
            sub = self._sub
            if sub is None:
                return
            try:
                left = (self._until - datetime.now(timezone.utc)).total_seconds()
                n = sub.next(max(0.0, left))
                r = next((r for r in n.records if r.id == self._id), None)
            except TimeoutError:
                try:
                    r = self._store.load(self._id)
                except JobError:
                    r = None
            except Closed:
                return
            if not self._alive(r):
                self.release()
                return
            self._until = r.lease.expires_at

    def _alive(self, r: Optional[Record]) -> bool:
        return (
            r is not None
            and r.lease.epoch == self._epoch
            and r.lease.held(datetime.now(timezone.utc))
            and not r.terminal()
        )


def _keep_awake(who: str, why: str) -> Optional[Callable[[], None]]:
    if os.name == "nt":
        return _keep_awake_windows(who, why)
    if sys.platform == "darwin":
        cmd = ["caffeinate", "-i", "sh", "-c", "echo; exec cat"]
    else:
        cmd = ["systemd-inhibit", "--what=idle:sleep", "--mode=block",
               f"--who={who}", f"--why={why}", "sh", "-c", "echo; exec cat"]
    # The inhibitor is a child that lives exactly as long as its stdin: our end
    # of the pipe closes when this process exits, however it exits, and cat goes
    # with it. That is the lifetime a D-Bus inhibitor fd has, without a D-Bus
    # client. The echo arrives once the inhibitor is in place, so the hold is
    # real on return.
    try:
        child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if not child.stdout.read(1):
        child.stdin.close()
        child.wait()
        return None

    def free() -> None:
        child.stdin.close()
        child.wait()

    return free


def _keep_awake_windows(who: str, why: str) -> Optional[Callable[[], None]]:
    import ctypes
    from ctypes import wintypes

    class ReasonContext(ctypes.Structure):
        _fields_ = [("version", wintypes.ULONG), ("flags", wintypes.ULONG),
                    ("reason", wintypes.LPWSTR)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.PowerCreateRequest.restype = wintypes.HANDLE
    k.PowerCreateRequest.argtypes = [ctypes.POINTER(ReasonContext)]
    k.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    k.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    system_required = 1
    ctx = ReasonContext(0, 1, f"{who}: {why}")
    h = k.PowerCreateRequest(ctypes.byref(ctx))
    if not h or h == wintypes.HANDLE(-1).value:
        return None
    if not k.PowerSetRequest(h, system_required):
        k.CloseHandle(h)
        return None

    def free() -> None:
        k.PowerClearRequest(h, system_required)
        k.CloseHandle(h)

    return free

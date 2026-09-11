"""The download abstraction, in Python.

This is the second implementation, and the second implementation is the point.

An interface with one implementation is a design. It is only an abstraction once
something written independently can pick up work the first one started, and the
only way to find out is to make them do it. That is not a theoretical exercise:
the job layer shipped a cross-language bug where Go's RFC3339Nano trimmed
trailing zeros and Python did not, so two implementations wrote the same instant
as different strings. Nothing but a test that ran both could have found it.

So this deliberately does NOT read the Go source while running. It reads the same
records, from the same directory, and either the contract is written down
properly or it does not work.

    import abstraction_download as dl

    svc = dl.discover()
    svc.deliver(svc.submit(dl.Spec(
        artifact=dl.Artifact(digest="sha256:...", size=328597408),
        sources=[dl.Source(scheme="https", locator="https://...")],
        sink=dl.Sink(final="models/x.gguf"),
    )))

An application names no store, no runner, no partial file and no path to this
machine. Everything below Client is for the programs INSIDE this layer.

Standard library only, like the job layer, and for the same reason: an
abstraction that needs three packages installed before anybody can try it is one
nobody tries.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from datetime import datetime, timezone

try:
    import abstraction_config as _config
except ImportError:
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "abstraction-config", "python"))
    import abstraction_config as _config

from abstraction_job import (
    Invalid,
    KeepAwake,
    LeaseHeld,
    FEATURE_RANGES,
    Record,
    StaleEpoch,
    Scratch,
    Store,
    new_id,
    reserved,
    root_name,
    CANCEL,
    RUN,
    RUNNING,
    TRANSFERRED,
    COMPLETE,
    CANCELLED,
    FAILED,
    watch,
)
# The heartbeat is written by whichever implementation is supervising, so its
# timestamps are the job layer's format and are parsed by the job layer's reader
# rather than by a second one that could drift from it.
from abstraction_job import _parse_time

# KIND is the job.Record.kind this module understands. A process that finds a job
# of an unknown kind leaves it alone rather than guessing at its spec.
KIND = "download"

# Capabilities, matching the Go names exactly. These cross the boundary as
# strings in Record.requires, so a typo here is a job no Python worker will ever
# claim and no error anybody will ever see.
CAP_RESUME = "resume"
CAP_SURVIVES_PROCESS_EXIT = "survives_process_exit"
CAP_VERIFIES = "verifies_content"
CAP_DELEGATES = "delegates"

# What each transport here actually promises, so that Record.requires can be
# honoured rather than read and dropped.
#
# None of them claims CAP_SURVIVES_PROCESS_EXIT: every one runs inside the
# caller's process and dies with it. That is exactly the gap the service tier
# fills, and a job asking for a transfer that outlives its caller must be left
# for something that can do it rather than served by something that cannot.
TRANSPORTS = {
    "http": frozenset({CAP_RESUME}),
    "https": frozenset({CAP_RESUME}),
    "file": frozenset({CAP_RESUME}),
    "smb": frozenset({CAP_RESUME}),
}

# CREDENTIAL_ATTR names a credential by NAME in a source's attrs. The value never
# appears in a record: it is resolved from the environment at the moment of the
# request. See credentials, below.
CREDENTIAL_ATTR = "credential"

# CREDENTIAL_HEADER_ATTR optionally names the header the secret goes into,
# matching the Go implementation. Defaults to Authorization with a Bearer
# prefix, which is what every registry this project has met actually wants.
CREDENTIAL_HEADER_ATTR = "credential_header"

# RESOLVED_HEADERS are the header names this layer decides for itself, which a
# record therefore may not decide for it. Authorization and its proxy twin are
# where a resolved secret goes; Cookie is the other place one is conventionally
# spelled. Range is refused for a different reason -- the bounds of a transfer
# are what the resume logic and the server have to agree about, and a record
# that sets them tells the server a different story.
RESOLVED_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "range"}
)

# What this layer keeps in the store root, beside the store's own jobs/ and
# work/. The heartbeat says a supervisor is alive and where its bus is. Named
# once, because reserved_sink protects exactly this name and a second spelling
# would leave one of them unprotected.
HEARTBEAT = "supervisor.json"


class DownloadError(Exception):
    pass


class Permanent(DownloadError):
    """Trying this job again, unchanged, is pointless.

    A failure declares its class HERE, where it is defined, and not in a list
    somewhere else. The list is what drifts: this layer kept one in Go and one
    in Python and they disagreed by a row for as long as both existed, which no
    test could see because each language only ever read its own. See
    ``permanent`` and abstraction-download/CONTRACT.md § Two endings.
    """


class DigestMismatch(DownloadError):
    """The bytes are not what was asked for. The partial is deleted rather than
    left for a successor to resume onto a prefix already known to be wrong."""


class ShortTransfer(DownloadError):
    pass


class Unreachable(DownloadError):
    """This machine will not open a connection to the host, whatever the source
    would have said.

    Not Permanent: the refusal is the machine's, not the job's. A person turns
    the host back on, or a machine that may reach it adopts the record, and the
    same job runs unchanged.
    """


class UnportableSink(DownloadError):
    """An absolute sink met by a runner over a shared store.

    Not Permanent: the record is valid on the machine whose filesystem the path
    names, and this is a machine's policy about a store several machines can
    write, so the job stays adoptable -- like Unreachable and a foreign path."""


_PLAIN_HOST = re.compile(r"\A[a-z0-9._\-:]*\Z")


def host_of(locator: str) -> str:
    """The host a locator opens a connection to, folded to lower case, without a
    port, without the brackets of an IPv6 literal and without the DNS root's
    trailing dot.

    "" when the locator names none -- a local path reaches nothing -- and "" for
    an authority this machine will not read. Anything deciding where a secret or
    a connection goes calls ``vouched_host`` and hears the reason instead.
    """
    try:
        return vouched_host(locator)
    except Unreachable:
        return ""


def vouched_host(locator: str) -> str:
    """The host, or Unreachable saying why this machine cannot tell.

    ``urllib.parse`` answers, because ``urllib.request`` is what opens the
    socket. It is not enough on its own: it follows WHATWG URL in deleting tabs
    and newlines from an authority before reading it, so ``hf.co<TAB>.evil.com``
    parses to a perfectly plain host that appears nowhere in the locator. The
    authority is therefore compared with the bytes it was made from.
    """
    if locator.startswith("\\\\"):
        return _plain_host(re.split(r"[/\\?#]", locator[2:], 1)[0], locator)
    try:
        parts = urllib.parse.urlsplit(locator)
        host, netloc = parts.hostname, parts.netloc
        userinfo = parts.username is not None
        # Reading .port is what validates it. `hf.co:443:8080` has a host by
        # urlsplit, which stops at the first colon, and a different one by
        # http.client, which stops at the last.
        parts.port
    except ValueError as e:
        raise Unreachable(f"download: {locator} is not a URI: {e}")
    if userinfo:
        raise Unreachable(
            f"download: {locator} carries userinfo in its authority, and the "
            "three transports this layer speaks for end userinfo at three "
            "different bytes"
        )
    if netloc != _authority_bytes(locator):
        raise Unreachable(
            f"download: the authority of {locator} is not the authority the "
            "parser read, so the host this machine would authorise is not the "
            "host it would connect to"
        )
    host = _plain_host(host or "", locator)
    if not host and parts.scheme.lower() in ("http", "https"):
        raise Unreachable(f"download: {locator} names no host")
    return host


def _authority_bytes(locator: str) -> str:
    """The authority exactly as written, by RFC 3986 section 3.2's delimiters
    and nothing else. Its answer is only ever used to refuse."""
    after = locator.split("://", 1)[1] if "://" in locator else (
        locator[2:] if locator.startswith("//") else None
    )
    return "" if after is None else re.split(r"[/?#]", after, 1)[0]


def _plain_host(host: str, locator: str) -> str:
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not _PLAIN_HOST.match(host):
        raise Unreachable(
            f"download: {host!r} in {locator} is not a plain host name, so the "
            "host this machine would authorise is not the host it would "
            "connect to"
        )
    return host


def _config_dir() -> str:
    if os.name == "nt":
        return os.environ.get("APPDATA") or tempfile.gettempdir()
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def refused_hosts(path: Optional[str] = None) -> Callable[[str], Optional[str]]:
    """The hosts this machine will not reach, one reason each, in the file the
    window writes -- read at the moment a connection would open, so a switch
    takes effect on the next one and nothing restarts. A name covers its
    subdomains. An unreadable file refuses everything, with the reason."""
    path = path or os.path.join(_config_dir(), "openabstractions", "download", "refused.json")

    def check(host: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                hosts = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            return f"{path} is unreadable: {e}"
        for name, why in hosts.items():
            if host == name or host.endswith("." + name):
                return why
        return None

    return check


class NoSource(Permanent):
    pass


class RefusedBySource(Permanent):
    """The source answered and said no.

    A gated repository, a deleted file, a bad token. The request as written will
    never work, so this is the one download failure that ends the job instead of
    leaving it for a successor -- see `permanent`.
    """


class EscapesRoot(Permanent):
    """A relative sink path that resolves somewhere other than under the store
    root.

    This is the one place in the layer where the writer's authority and the
    caller's choice come apart. A PC submits the record; a NAS adopts it and does
    the writing, with the NAS's account, to a destination the PC named. Joining
    the two without checking resolved
    "../../../Users/victim/.ssh/authorized_keys" to exactly that file -- an
    autostart directory just as easily. A confused deputy, reachable by anyone
    who can put a record in a shared store.

    Refused, never clamped. Clamping would write 40 GB somewhere the caller did
    not ask for and report success, which is the failure this layer exists to
    refuse, and the caller would never learn its record was wrong.
    """


class ReservedPath(Permanent):
    """A sink that stays inside the store root and aims at the store's contents.

    Containment stopped a sink climbing OUT of the root. It never stopped one
    naming what is IN it: a final of `jobs/<id>.json` overwrites a job record,
    and a final of `work/<other>` overwrites another job's partial. Both are
    contained, and both were accepted by every check that existed. The confused
    deputy is the same one and the target is now the store itself, so a single
    record could empty a shared store or hand another job an arbitrary spec.
    """


class Asked(DownloadError):
    """Somebody asked this job to pause or cancel and the transfer has stopped.

    Not a failure and never recorded as one. It carries the want up to the one
    place that knows how to converge on it, past the per-source loop that would
    otherwise try the next mirror -- stopping because a person said so is not a
    reason to go looking for another server.
    """

    def __init__(self, want: str):
        super().__init__(f"download: asked to {want}")
        self.want = want


class ForeignPathError(DownloadError):
    """An absolute sink written in the other platform's convention.

    The contract said an absolute path is left alone, so a Windows path handed
    to Linux "fails with no such file rather than quietly creating a directory".
    That is true of a path being READ and false of a sink: opening
    "D:\\models\\x.gguf" with O_CREAT on Linux succeeds and makes a file of that
    literal name in the working directory, with a ".part" beside it. The runner
    creates the parent first, so the mirror case is no better -- "/mnt/models"
    on Windows becomes "C:\\mnt\\models".

    So an absolute sink is honoured only by a machine whose convention it is
    written in. A record meant to be adopted by another machine uses a RELATIVE
    sink, which is what resolving against the store root is for.

    Not Permanent, deliberately: a path unusable HERE is perfectly usable on the
    machine whose convention it is, which is a reason to leave the job for
    somebody else rather than to end it.
    """


# Statuses that say no rather than not now. abstraction-download/CONTRACT.md § Two endings is
# the list every implementation answers to; this is a transcription of it.
#
# Listed rather than ranged, because the cost of the two mistakes is not
# symmetric: calling a retryable status a refusal abandons a download that would
# have worked, and this layer exists to not lose downloads. So an unrecognised
# 4xx is treated as "not now" -- 416 and 412 are the resume offset or the file
# version being wrong and restart cleanly, 408 and 429 and 425 say try later,
# and 409 and 423 are somebody else's lock.
#
# 401 IS a refusal: a gated Hugging Face repository answers it until the person
# adds a token, and adding a token is a new request.
REFUSALS = frozenset({400, 401, 402, 403, 404, 405, 406, 410, 414, 451})


def _refused(code: int) -> bool:
    return code in REFUSALS


def permanent(exc: BaseException) -> bool:
    """Whether trying this job again, unchanged, is pointless.

    It is the whole of the difference between the two endings a failed download
    can have. A dropped connection, a full disk, a NAS that rebooted: the record
    keeps its error, the lease lapses, and the next runner resumes from the last
    proven byte -- that is the case this project exists for and nothing here
    gives up on it. A refusal is not that. Nobody is going to fetch a 404, and a
    job that stays adoptable is fetched again on every sweep, forever.

    Two names, not a membership list: this layer's own refusals say so in their
    class, and the job layer's Invalid means the record itself will never be
    readable, which no successor can improve on.
    """
    return isinstance(exc, (Permanent, Invalid))


# The name a record carries the last failure's class under, in `extensions` and
# therefore in `content`.
#
# It is abstraction-download/download.thrift's `failure_names`, whose generated Python
# reader is py/rec.py beside this module. That module is NOT imported here: the
# published distribution is one file (`py-modules = ["abstraction_download"]`)
# and the only way to add the generated one to it is to claim the top-level
# import name `py`, which belongs to somebody else on PyPI. It is the authority
# this module is checked against instead -- test_abstraction_download.py runs
# both over the same corpus and refuses a byte of difference.
FAILURE_EXTENSION = "abstraction.download/failure@1"


def _set_failure(rec: Record, exc: BaseException) -> None:
    """Record why an attempt ended AND whether trying again could ever help.

    Both, together, because the caller reads them back as one thing. `error` is
    prose and a sentence is not a class: an error rebuilt from it alone is a
    bare string, so `permanent` on it is false however the attempt ended -- and
    the two endings are the whole retry model.

    The key order is the definition's, because the record writer emits a carried
    value in the order it was built and three implementations compare records
    byte for byte.
    """
    rec.error = str(exc)
    payload: Dict[str, Any] = {"error": str(exc)}
    if permanent(exc):
        payload["permanent"] = True
    rec.extensions[FAILURE_EXTENSION] = payload


def _clear_failure(rec: Record) -> None:
    """Take back both halves. Leaving the class behind when the sentence goes
    would answer "is this over" about an attempt nobody can read."""
    rec.error = ""
    rec.extensions.pop(FAILURE_EXTENSION, None)


def last_failure(rec: Record) -> Optional[BaseException]:
    """The error a record's last attempt ended with, class intact, or None for a
    record that has not failed.

    A record written before this key existed, or by a writer that does not know
    it, yields a retryable error -- the same answer that record has always
    given. So does a payload this reader cannot make sense of: the definition
    refuses an unknown field rather than granting it, so an unreadable class and
    an absent one are one answer and neither is a guess.
    """
    if not rec.error:
        return None
    payload = rec.extensions.get(FAILURE_EXTENSION)
    text, is_permanent = _read_failure(payload)
    if text:
        return Permanent(text) if is_permanent else DownloadError(text)
    return DownloadError(rec.error)


def _read_failure(payload: Any) -> Tuple[str, bool]:
    """The payload's two fields, or ("", False) for anything this reader will
    not stand behind. The shape is abstraction-download/download.thrift's Failure."""
    if not isinstance(payload, dict):
        return "", False
    if set(payload) - {"error", "permanent"}:
        return "", False
    text = payload.get("error")
    if not isinstance(text, str) or not text:
        return "", False
    mark = payload.get("permanent", False)
    if not isinstance(mark, bool):
        return "", False
    return str(text), mark


# How long a job that recorded a failure is left alone before anybody tries it
# again, and the ceiling that wait grows to.
RETRY_DELAY_SECONDS = 15.0
RETRY_MAX_SECONDS = 15 * 60.0


def retry_after(rec: Record) -> float:
    """The earliest, as a POSIX timestamp, that a failed job may be picked up
    again.

    The attempt count is the lease epoch. It rises by one every time an owner
    claims the job, it is already in the record in all three languages, and it
    costs nothing to read -- so backing off needs no new field and no change to
    a contract three implementations have to agree on forever.

    Only a job that recorded a failure waits. An owner that was killed never got
    to write one, so the crash-and-resume case -- the case this project exists
    for -- is adopted as fast as it always was.
    """
    if not rec.error:
        return 0.0
    wait = RETRY_DELAY_SECONDS
    epoch = rec.lease.epoch
    while epoch > 1 and wait < RETRY_MAX_SECONDS:
        wait *= 2
        epoch -= 1
    return rec.updated_at.timestamp() + min(wait, RETRY_MAX_SECONDS)


# ---------------------------------------------------------------- the spec ---


@dataclass
class Artifact:
    """What the job is for: an identity, and how big it is.

    digest is an INPUT, not a result. Every transfer tool in the survey either
    verifies nothing, or verifies only size and timestamp. If the caller knows
    what the bytes should be, this layer must be able to refuse anything else.
    """

    digest: str = ""
    size: int = 0


@dataclass
class Source:
    """Somewhere the bytes can be had. Deliberately NOT a URL — an SMB path, a
    peer and a local file are all sources, and none are expressible as an http
    URL without lying.

    ``attrs`` describe the source to THIS LAYER and never reach the wire.
    ``headers`` are what the caller intends to send, named one at a time. The
    Go implementation kept both in one bag and copied anything it did not
    recognise into the request, so every attribute anyone added was one
    forgotten branch away from a third party. This implementation never had the
    bug; it now cannot have it, because there is nowhere for an attribute to
    become a header.

    A secret belongs in neither. A credential is named in ``attrs`` and
    resolved on this machine at the moment of the request.
    """

    scheme: str = ""
    locator: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    priority: int = 0


@dataclass
class Sink:
    """Where the bytes land. 40 GB does not fit in a return value, and the
    process doing the writing may not be ours at all."""

    partial: str = ""
    final: str = ""

    def resolve(self, root: str, owner: str = "") -> Tuple[str, str]:
        """Turn these into paths on THIS machine.

        A relative path means "under the store root". That is what lets a record
        written by Windows into \\\\nas\\share\\store be acted on by a container
        that mounts the same directory as /store.

        Three things are enforced rather than assumed: that a relative path
        stays under the root (EscapesRoot), that it does not name the store's
        own files (ReservedPath), and that an absolute one is written in this
        platform's convention (ForeignPathError). ``owner`` is the job this sink
        belongs to, because work/<owner> is the one reserved path it may write.
        """
        return _resolve_sink(root, owner, self.partial), _resolve_sink(root, owner, self.final)


def local_root(store) -> str:
    """The store's own directory, or "" when its binding is not a filesystem.

    Asking is the point. ``store.root`` used to be an attribute, so this layer
    -- which is not supposed to know what a file is -- could read a directory off
    any store without admitting it needed one, and would have handed a service
    binding's absence straight to os.path.join.
    """
    return store.root() if isinstance(store, Scratch) else ""


def local_sink(store, owner: str, sink: "Sink") -> Tuple[str, str]:
    """Resolve a sink's relative paths into paths on THIS machine.

    A relative path in a record means "under the store's own area", so a record
    written by a PC and adopted by a NAS names one directory rather than one
    machine's view of it.

    It raises -- rather than guessing -- for a path this machine must not write.
    The record was written by another machine and the writing is done with this
    one's authority, so the refusal happens here, at the boundary, and is
    carried no further.

    ``owner`` is the id of the job whose sink this is. work/<owner> is the
    store's scratch for that job and the one reserved path it may write, so a
    caller that passes the wrong id gets its own partial refused rather than
    someone else's accepted.
    """
    return sink.resolve(local_root(store), owner)


@dataclass
class Validators:
    """Which VERSION of an artifact the proven bytes came from.

    Nothing in a bare Range request says which file it means, so a source that
    replaced the artifact answers the range honestly and the answer is a valid
    range of something else. Appended to the prefix on disk that is a file of
    exactly the right length holding two versions spliced at an arbitrary
    offset: no transport error, no short read, and nothing a download without a
    digest could ever catch.

    Only strong validators go in here -- see ``strong_validators``.
    """

    etag: str = ""
    last_modified: str = ""

    def if_range(self) -> str:
        """What to send as If-Range beside a Range request, or "".

        If-Range rather than If-Match, which is Chromium's choice and for the
        same reason: a failed If-Match is a 412 with an empty body, so the
        client learns the file changed and must spend a second round trip
        asking for it. A failed If-Range is the new file, in the same response.
        """
        return self.etag or self.last_modified

    def to_dict(self) -> dict:
        """The wire form, spelled the way Go's `omitempty` spells it. Key order
        and omissions are part of the record, which is compared byte for byte."""
        d = {}
        if self.etag:
            d["etag"] = self.etag
        if self.last_modified:
            d["last_modified"] = self.last_modified
        return d


def strong_validators(headers) -> Validators:
    """The validators in a response that are worth acting on.

    A weak ETag -- `W/"..."` -- is dropped rather than recorded. Weak asserts
    semantic equivalence, not byte equality: two responses may share a weak tag
    and differ in their bytes, which is exactly the distinction a resume depends
    on. Recording one produces a token that makes a server answer 206 for a file
    whose bytes moved -- worse than having none, because none at least leaves
    the 200 path and its restart available.

    Last-Modified is used only when there is no usable ETag, and only when it
    parses as an HTTP date, so a malformed header is not echoed back to a server
    that would have to guess what it meant.
    """
    etag = (headers.get("ETag") or "").strip()
    if etag[:2].lower() == "w/" or len(etag) < 2 or etag[0] != '"' or etag[-1] != '"':
        etag = ""
    if etag:
        return Validators(etag=etag)
    lm = (headers.get("Last-Modified") or "").strip()
    return Validators(last_modified=lm) if http_date(lm) else Validators()


def _shaped(s: str, pattern: str) -> bool:
    """A fixed layout: `9` a digit, `a` a letter, `#` either a digit or the
    space asctime pads a one-digit day with, anything else itself."""
    if len(s) != len(pattern):
        return False
    for c, p in zip(s, pattern):
        if p == "9" and not c.isdigit():
            return False
        if p == "a" and not c.isalpha():
            return False
        if p == "#" and not c.isdigit() and c != " ":
            return False
        if p not in "9a#" and c != p:
            return False
    return True


def _month_at(s: str, at: int) -> bool:
    i = "JanFebMarAprMayJunJulAugSepOctNovDec".find(s[at:at + 3])
    return i >= 0 and i % 3 == 0


def http_date(s: str) -> bool:
    """Whether s is one of the three spellings of an HTTP-date RFC 9110 requires
    a recipient to accept: IMF-fixdate, which is the only one a sender may use,
    and the two obsolete forms a recipient still meets.

    This recognises a shape and does not parse a time, because the value is
    never interpreted: it is echoed back verbatim as If-Range, which the origin
    server evaluates by exact match. Written out rather than handed to
    `parsedate_to_datetime` because the three languages' date parsers accept
    three different sets -- this one took RFC 2822 with numeric zones, C++ took
    only IMF-fixdate, Go took a three-letter zone where the grammar says GMT --
    so "an HTTP date" implemented as "whatever the standard library takes" is
    three rules wearing one name.
    """
    if _shaped(s, "aaa, 99 aaa 9999 99:99:99 GMT"):
        return _month_at(s, 8)
    if _shaped(s, "aaa aaa #9 99:99:99 9999"):
        return _month_at(s, 4)
    day, sep, tail = s.partition(", ")
    if not sep or not day:
        return False
    return (_shaped(day, "a" * len(day)) and _shaped(tail, "99-aaa-99 99:99:99 GMT")
            and _month_at(tail, 3))


def content_range_start(value: str) -> int:
    """The first byte position of a `Content-Range`, or -1 when it says nothing
    this layer can act on. `bytes 1000-40959/40960` gives 1000."""
    unit, _, rest = value.strip().partition(" ")
    if unit.lower() != "bytes":
        return -1
    span, slash, _ = rest.strip().partition("/")
    first, dash, _ = span.partition("-")
    if not slash or not dash or not first.strip().isdigit():
        return -1
    return int(first.strip())


def answers_from(headers, start: int) -> str:
    """Why a 206 is not an answer to the request that was sent, or "".

    A single range beginning where the next byte will be written is the only
    206 this layer can use. Both ways of failing put real artifact bytes at an
    offset nobody asked about, and neither is visible to a length check or a
    transport error -- only Content-Range says so, and only if somebody reads
    it.

    A `multipart/byteranges` body is worse than misplaced: its boundary line and
    per-part headers are content this layer would author into the artifact
    itself. RFC 9110 lets a server answer a single range that way and a
    coalescing proxy does, but we never send a multi-range request, so it is
    never an answer to ours.
    """
    kind = (headers.get("Content-Type") or "").strip().lower()
    if kind.startswith("multipart/"):
        return f"download: one range was answered with {kind}"
    got = content_range_start(headers.get("Content-Range") or "")
    if got < 0:
        return "download: a 206 arrived with no usable Content-Range"
    if got != start:
        return f"download: asked for bytes from {start}, got a range starting at {got}"
    return ""


def unwanted_coding(headers) -> str:
    """A content coding the request did not ask for, or "".

    Every request carries `Accept-Encoding: identity`, so anything else coming
    back is the server overriding what was asked. It matters because a digest is
    over the artifact and a coding changes what "the bytes" are: given one legal
    gzip response, one implementation decoded it and completed, one hashed the
    envelope, and one failed a third way.
    """
    coding = (headers.get("Content-Encoding") or "").strip()
    return "" if coding.lower() in ("", "identity") else coding


@dataclass
class Checkpoint:
    """How many leading bytes of the partial are KNOWN to be the artifact's real
    bytes, and which version of the artifact they came from. A new owner may
    resume only from here. There is no field meaning 'trust the part I did not
    check', so curl's mistake is not expressible."""

    verified_prefix: int = 0
    validators: Validators = field(default_factory=Validators)


@dataclass
class Spec:
    artifact: Artifact = field(default_factory=Artifact)
    sources: List[Source] = field(default_factory=list)
    sink: Sink = field(default_factory=Sink)

    def validate(self, owner: str = "") -> None:
        """Check a spec on behalf of the job that will carry it.

        ``owner`` is needed because one relative path in the store --
        work/<owner> -- is reserved against every job except the one it belongs
        to. Left out, nothing owns anything and the whole work area is reserved,
        which is the right answer for a caller that has no id yet.
        """
        if not self.sources:
            raise DownloadError("download: at least one source is required")
        for i, s in enumerate(self.sources):
            if not s.scheme.strip():
                raise DownloadError(f"download: source {i}: scheme is required")
            if not s.locator.strip():
                raise DownloadError(f"download: source {i}: locator is required")
            for name in s.headers:
                lower = name.strip().lower()
                if not lower or lower in RESOLVED_HEADERS or lower == s.attrs.get(
                    CREDENTIAL_HEADER_ATTR, ""
                ).strip().lower():
                    raise DownloadError(
                        f"download: source {i}: header {name!r} is resolved on the "
                        "machine that fetches, so a record may not carry it"
                    )
        if not self.sink.final.strip():
            raise DownloadError("download: sink final path is required")
        # Final first, then partial, so a record with both wrong names the same
        # field in every implementation.
        for p in (self.sink.final, self.sink.partial):
            refusal = escapes_root(p)
            if refusal:
                raise EscapesRoot(refusal)
            refusal = reserved_sink(owner, p)
            if refusal:
                raise ReservedPath(refusal)
        if self.artifact.digest and not self.artifact.digest.startswith("sha256:"):
            raise DownloadError(
                f"download: digest {self.artifact.digest!r} is not sha256:<hex>"
            )

    def to_dict(self) -> dict:
        d: dict = {"artifact": {}, "sources": [], "sink": {}}
        if self.artifact.digest:
            d["artifact"]["digest"] = self.artifact.digest
        if self.artifact.size:
            d["artifact"]["size"] = self.artifact.size
        for s in self.sources:
            one: dict = {"scheme": s.scheme, "locator": s.locator}
            if s.attrs:
                one["attrs"] = dict(s.attrs)
            if s.headers:
                one["headers"] = dict(s.headers)
            if s.priority:
                one["priority"] = s.priority
            d["sources"].append(one)
        d["sink"] = {"partial": self.sink.partial, "final": self.sink.final}
        return d

    @staticmethod
    def from_dict(d: dict) -> "Spec":
        a = d.get("artifact") or {}
        sk = d.get("sink") or {}
        return Spec(
            artifact=Artifact(digest=a.get("digest", ""), size=int(a.get("size", 0) or 0)),
            sources=[
                Source(
                    scheme=s.get("scheme", ""),
                    locator=s.get("locator", ""),
                    attrs=dict(s.get("attrs") or {}),
                    headers=dict(s.get("headers") or {}),
                    priority=int(s.get("priority", 0) or 0),
                )
                for s in (d.get("sources") or [])
            ],
            sink=Sink(partial=sk.get("partial", ""), final=sk.get("final", "")),
        )


# --------------------------------------------------------------- portability ---

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def _relative_everywhere(p: str) -> bool:
    """Is p relative under BOTH conventions?

    os.path.isabs alone answers for the OS running it: on Linux
    "D:\\models\\x.gguf" is "relative", and joining it onto the store root would
    silently produce a directory literally named "D:\\models" on the NAS. A path
    absolute anywhere is treated as absolute everywhere, so a mistake surfaces as
    a plain "no such file" rather than as a strange one.
    """
    if not p:
        return False
    if p.startswith("/") or p.startswith("\\"):
        return False  # POSIX absolute, or a UNC path
    if _WINDOWS_DRIVE.match(p):
        return False
    return not ntpath.isabs(p) and not posixpath.isabs(p)


def _resolve_under(root: str, p: str) -> str:
    if not p or not _relative_everywhere(p):
        return p
    # Forgive a backslash in a RELATIVE path: records written before separators
    # were normalised are on disk already, and a backslash cannot legally appear
    # in a Windows filename anyway, so reading it as a separator is the only
    # interpretation ever right.
    resolved = os.path.join(root, *[seg for seg in p.replace("\\", "/").split("/") if seg])
    if not _under(root, resolved):
        raise EscapesRoot("download: sink path escapes the store root: " + p)
    return resolved


def _under(root: str, resolved: str) -> bool:
    """Does resolved name a location inside root?

    Asked of the RESULT of the join, never of the input. Scanning the input for
    ".." is the check everybody writes and it is defeated by a path that spells
    the climb some other way, and it fires on paths like "a/../b" that are
    perfectly contained. Resolve first, then ask where the answer landed.

    Every shortcut in the comparison itself is wrong somewhere, so none is taken:

      - "C:\\store2" starts with "C:\\store" and is a different directory, so the
        prefix has to end on a separator boundary.
      - Windows ignores case in a path and POSIX does not, so folding is
        conditional on the OS rather than done to be safe.
      - "\\\\nas\\share\\store", "C:\\", "/" and a trailing separator must not
        change the answer, so both sides are reduced to one spelling first.

    What this does NOT close: a directory inside the root that is itself a
    symlink or a junction pointing out of it. "models/x.gguf" is then contained
    by every lexical measure and the bytes still land elsewhere. Closing it needs
    the path resolved at the moment of the write -- the file does not exist yet
    when this runs, and resolving it here would only move the race earlier -- and
    none of Go, Python and C++ has a portable "open without following a link".
    This is lexical containment and claims nothing more.
    """
    r = _comparable_path(root)
    c = _comparable_path(resolved)
    if r in ("", "."):
        # The store's binding is not a filesystem, so there is no root to be
        # inside -- local_root answers "" for exactly that case. All that can be
        # said is that the path did not climb out of wherever it eventually gets
        # resolved, which the cleaning has already made visible.
        return c != ".." and not c.startswith("../")
    if c == r:
        return True
    if not r.endswith("/"):
        r += "/"
    return c.startswith(r)


def _comparable_path(p: str) -> str:
    """A path reduced for the LEXICAL containment check above: cleaned, forward
    slashes, no trailing separator, and lowercased when the host is Windows.

    That fold is a guess about the host, not a reading of the volume, and it is
    wrong in both directions: measured, it merged K (U+212A) with k against NTFS
    and split Kelvin from kelvin against APFS. It stays only because containment is a question about a store
    root's own subtree and both sides are folded alike. Whether two paths name
    one file is NOT answered here: that is ``same_path``, which asks the volume.
    """
    if not p:
        return ""
    s = os.path.normpath(p).replace("\\", "/")
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    if os.name == "nt":
        s = s.lower()
    return s


def same_path(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` name one file.

    The question belongs to the filesystem holding them, never to their bytes:
    NTFS folds "café" against its uppercase and keeps U+212A apart, APFS
    folds both, ext4 folds neither, and one volume can be mounted differently
    from the next. ``os.path.samestat`` is the standard library's answer and it
    compares device and inode, which is where the answer actually lives -- Go
    spells it os.SameFile, C++17 spells it std::filesystem::equivalent, Java
    spells it Files.isSameFile. See abstraction-download/CONTRACT.md, "Two paths, one file".

    A destination usually does not exist yet, which those four cannot answer.
    Its PARENT does, so the parent is settled the same way and the final
    component is settled by asking the parent directory to demonstrate its own
    rule.
    """
    if not a or not b:
        return a == b
    a = os.path.normpath(os.path.abspath(a))
    b = os.path.normpath(os.path.abspath(b))
    if a == b:
        return True

    sa, missing_a = _stat_of(a)
    sb, missing_b = _stat_of(b)
    if sa is not None and sb is not None:
        return os.path.samestat(sa, sb)
    # One spelling resolves and the other does not, so the volume holding them
    # does not treat them as one name. This is an answer, not a failure.
    if sa is not None or sb is not None:
        return False
    if not (missing_a and missing_b):
        return False

    dir_a, name_a = os.path.split(a)
    dir_b, name_b = os.path.split(b)
    if not name_a or not name_b or not same_path(dir_a, dir_b):
        return False
    if name_a == name_b:
        return True
    if not _may_fold(name_a, name_b):
        return False
    return _folds_together(_nearest_dir(dir_a), name_a, name_b)


def _stat_of(p: str):
    """The stat, and whether the path is definitely absent. An unreadable path
    is neither, and answers no to everything."""
    try:
        return os.stat(p), False
    except (FileNotFoundError, NotADirectoryError):
        return None, True
    except OSError:
        return None, False


def _may_fold(x: str, y: str) -> bool:
    """The filter that keeps the probe off the common path. Two names holding no
    character above U+007F and still differing after A-Z is folded cannot be one
    file anywhere: case mapping and Unicode normalisation are both the identity
    on ASCII apart from the letters. So "llama.gguf" against "mistral.gguf"
    costs nothing, and only a real candidate reaches the volume."""
    if not x.isascii() or not y.isascii():
        return True
    return x.lower() == y.lower()


def _nearest_dir(d: str) -> str:
    """The closest ancestor that exists. On Windows a directory's case
    sensitivity is a per-directory flag inherited from its parent at creation,
    so the nearest existing ancestor is the one that will decide for the
    directories still to be made under it."""
    while True:
        if os.path.isdir(d):
            return d
        up = os.path.dirname(d)
        if up == d:
            return ""
        d = up


_FOLDED: Dict[str, bool] = {}
_FOLDED_LOCK = threading.Lock()


def _folds_together(d: str, x: str, y: str) -> bool:
    """Asks ``d`` whether it keeps ``x`` and ``y`` as two names, by making one
    and looking for the other. Both are carried on a name of our own so that
    neither destination is created as a side effect of being asked about."""
    if not d:
        return False
    if x > y:
        x, y = y, x
    key = "\0".join((d, x, y))
    with _FOLDED_LOCK:
        if key in _FOLDED:
            return _FOLDED[key]

    tag = ".abstraction-samepath-" + os.urandom(8).hex() + "-"
    made = os.path.join(d, tag + x)
    try:
        fd = os.open(made, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        mine = os.stat(made)
        other, _ = _stat_of(os.path.join(d, tag + y))
        same = other is not None and os.path.samestat(mine, other)
    except OSError:
        return False
    finally:
        try:
            os.remove(made)
        except OSError:
            pass
    with _FOLDED_LOCK:
        _FOLDED[key] = same
    return same


# _PROBE_ROOT is a stand-in store root, used to answer the containment question
# about a path that has no root to hand -- a record being read rather than run.
# One segment deep, because containment is measured from the store root and a
# deeper stand-in would absorb a ".." that a real root would not.
_PROBE_ROOT = "/probe"


def escapes_root(p: str) -> str:
    """The refusal for a relative sink path that would resolve outside the store
    root -- whichever root it is resolved against -- or "" if there is none.

    Root-independent, and that is a fact about the question rather than a
    shortcut: containment is measured from the store root, so one ".." climbs out
    of it no matter how deep the root sits on any particular machine. That is
    what lets a reader answer this about a RECORD, which deliberately names no
    root.

    Absolute paths answer "". They are never joined onto the root, so they cannot
    escape it in this sense; what a machine adopting a record should do with one
    is a separate question and is not decided here.
    """
    try:
        _resolve_under(_PROBE_ROOT, p)
    except EscapesRoot as e:
        return str(e)
    return ""


# The names this layer keeps in the store root, beside the store's own jobs/,
# work/ and services.json. Spelled from the constants the writers use rather
# than beside them, so a heartbeat that gets renamed cannot leave this list
# pointing at a file nobody writes any more.
# supervisor.sock is bound by nothing since the bus, but CONTRACT.md and the
# C++ reader still reserve it, and a name reserved in two languages out of
# three is a conformance divergence.
_BESIDE_THE_STORE = frozenset({HEARTBEAT, HEARTBEAT + ".tmp", "supervisor.sock"})


def reserved_sink(owner: str, p: str) -> str:
    """The refusal for a sink that names the store's own layout, or this layer's
    own files beside it -- or "" if there is none.

    ``owner`` is the id of the job the sink belongs to. Its own scratch is not
    reserved against it -- that is where its partial goes -- and is reserved
    against every other job. An empty owner reserves the whole work area, which
    is the right answer for a caller that has no id yet.

    Absolute paths are not this check's business: they are never joined onto the
    root, so they name the store's contents only by a coincidence no rule here
    could see. What a machine should do with an absolute sink is
    ``foreign_path``'s question.
    """
    if not _relative_everywhere(p):
        return ""
    if reserved(owner, p) or root_name(p) in _BESIDE_THE_STORE:
        return "download: sink path is reserved by the store: " + p
    return ""


def foreign_path(p: str) -> str:
    """The refusal for an absolute sink written in a convention this machine
    does not use -- or "" if there is none. See ForeignPathError.

    A record carrying one is not invalid; it is valid on the machine that wrote
    it. So this is asked by the machine about to do the writing and never at
    submission, the same division escapes_root draws between a record and a run.
    """
    if not p or _relative_everywhere(p) or _windows_shaped(p) == (os.name == "nt"):
        return ""
    return "download: sink path names another platform's filesystem: " + p


def _windows_shaped(p: str) -> bool:
    """Is an absolute path spelled the way Windows spells one -- a drive letter,
    or the two leading separators of a UNC path? Anything else absolute is rooted
    at a single "/", which is POSIX's spelling.

    "//server/share" counts because ``portable`` writes a UNC root that way, and
    a record whose UNC sink stopped being recognised as Windows would be refused
    on the only host that can write it. POSIX reaches that spelling only for a
    path whose leading "//" POSIX itself leaves implementation-defined; refusing
    that one on Linux is the safe direction, and _comparable_path has always read
    "\\\\server\\share" as "//server/share" anyway.
    """
    return bool(p) and (
        p.startswith("\\") or p.startswith("//") or bool(_WINDOWS_DRIVE.match(p))
    )


def _resolve_sink(root: str, owner: str, p: str) -> str:
    """_resolve_under plus the two refusals that need to know which machine is
    asking and which job is asking. Kept apart from _resolve_under because
    escapes_root answers about a RECORD, which has neither."""
    resolved = _resolve_under(root, p)
    refusal = foreign_path(p)
    if refusal:
        raise ForeignPathError(refusal)
    refusal = reserved_sink(owner, p)
    if refusal:
        raise ReservedPath(refusal)
    return resolved


def normal_digest(d: str) -> str:
    """A digest reduced to the part that carries the meaning.

    One implementation writes "sha256:<hex>", another the bare hex, and Ollama
    names its blobs "sha256-<hex>". The contract is that they name the same
    artifact, so a comparison is between these and never between spellings:
    comparing spellings deleted a correct 1.5 GB download and reported it as
    "got sha256:1fc70f… want 1fc70f…", the same digest twice.

    Anything unrecognised becomes "" rather than itself, so a comparison cannot
    succeed on two things neither side understood.
    """
    s = (d or "").strip().lower()
    for prefix in ("sha256:", "sha256-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        return ""
    return "sha256:" + s


def same_digest(a: str, b: str) -> bool:
    """Whether two digests name the same artifact, however each was spelled."""
    return bool(normal_digest(a)) and normal_digest(a) == normal_digest(b)


def portable(p: str) -> str:
    """Put a path into the one spelling a record uses: "/" is the only separator,
    everywhere, whatever wrote it.

    os.path.join on Windows produces "models\\x.gguf", and on Linux that is not a
    directory and a file -- it is ONE file whose name contains a backslash. The
    job would "succeed" and put the weights somewhere nobody would ever look.

    Absolute paths used to be exempt, on the argument that they already name one
    machine. What that missed is that the separator then records WHICH machine
    wrote the record, and that two spellings of one destination do not compare
    equal, so "are we already fetching this?" answers no and the artifact is
    fetched twice. Nothing is lost by the rewrite: a drive letter and a UNC root
    still say Windows afterwards -- _windows_shaped reads "//server/share" as UNC
    for exactly this reason -- and Windows accepts either separator.

    A POSIX-rooted path is returned untouched, because there a backslash is a
    legal character in a file's name and rewriting it would name a different file.
    """
    if p.startswith("/"):
        return p
    return p.replace("\\", "/")


# -------------------------------------------------------------- submitting ---


def partial_for(final: str, job_id: str) -> str:
    """Where the bytes accumulate before they earn the final name, for a caller
    that did not choose somewhere.

    A wire-visible choice: the name goes into the record, and a successor in
    another language finds what a predecessor left by reading it.
    scripts/spec-conformance.sh compares the three implementations' answers.

    Two cases, because "beside the store" and "beside the artifact" are both
    right and neither is right for the other one.

    A RELATIVE final resolves under the store root on whichever machine picks the
    job up, so the partial goes in the store's own work directory. Baking this
    machine's answer into the record is what would stop a NAS from adopting it.

    An ABSOLUTE final names one machine's filesystem already, and the store may
    be on a different volume -- a model going to D:\\ with the store on C:.
    Delivery across volumes cannot be a rename, so it degrades to a copy INTO THE
    FINAL NAME, and a crash halfway through leaves a truncated file under the
    name an application reads as an installed model. That is the exact failure
    this layer exists to refuse. Beside the artifact, delivery is a rename on one
    filesystem and the final name does not exist until the bytes are all there.

    An empty final names no volume at all, so it answers like a relative one.
    Validate refuses it long before a record is written; the three
    implementations still have to give one answer, because a rule with an
    unspecified corner is a rule two of them will implement differently.
    """
    if not final or _relative_everywhere(final):
        return "work/" + job_id
    return final + ".part"


def submit(store: Store, spec: Spec, requires: Optional[List[str]] = None) -> str:
    """Create a download job."""
    job_id = new_id()
    spec.validate(job_id)
    spec.sink.partial = portable(spec.sink.partial)
    spec.sink.final = portable(spec.sink.final)
    if not spec.sink.partial:
        spec.sink.partial = partial_for(spec.sink.final, job_id)

    rec = Record(id=job_id, kind=KIND, spec=spec.to_dict(), requires=list(requires or []))
    rec.progress.total = spec.artifact.size
    return store.submit(rec)


def proven(store: Store, digest: str, except_id: str = "") -> List[Source]:
    """Every file this store has already verified against ``digest``: the final
    of a record that transferred these exact bytes, still there at the size it
    proved. The record is the proof, so nothing is re-hashed to offer it."""
    if not digest:
        return []
    out: List[Source] = []
    seen: List[os.stat_result] = []
    for rec in store.list():
        if rec.id == except_id or rec.kind != KIND or rec.state not in (TRANSFERRED, COMPLETE):
            continue
        try:
            spec = spec_of(rec)
            _, final = local_sink(store, rec.id, spec.sink)
        except DownloadError:
            continue
        if not same_digest(spec.artifact.digest, digest):
            continue
        size = spec.artifact.size or rec.progress.done
        if size <= 0 or not os.path.isfile(final) or os.path.getsize(final) != size:
            continue
        # Every candidate has been stat'd by the time it gets here, so the volume
        # can be asked outright and there is no spelling to reduce. same_path is
        # for the harder case where the file is not there yet.
        here = os.stat(final)
        if any(os.path.samestat(here, s) for s in seen):
            continue
        seen.append(here)
        out.append(Source(scheme="file", locator=final, priority=-200 + len(out),
                          attrs={"store": "job", "job": rec.id}))
    return out


def spec_of(rec: Record) -> Spec:
    if rec.kind != KIND:
        raise DownloadError(f"download: job {rec.id} is kind {rec.kind!r}, not {KIND!r}")
    return Spec.from_dict(rec.spec or {})


def checkpoint_of(rec: Record) -> Checkpoint:
    cp = rec.checkpoint or {}
    v = cp.get("validators") or {}
    return Checkpoint(
        verified_prefix=int(cp.get("verified_prefix", 0) or 0),
        validators=Validators(
            etag=v.get("etag", ""), last_modified=v.get("last_modified", "")
        ),
    )


def set_checkpoint(
    rec: Record, verified_prefix: int, validators: Optional[Validators] = None
) -> None:
    """Write what this implementation has to say about proven bytes, and stop
    saying what it no longer has.

    A prefix is all a single stream can report, so the checkpoint is the shape
    this format has always written and the ranges model is withdrawn. That
    second half is not tidiness. The declaration is CARRIED rather than derived
    -- the job layer cannot rediscover it, because what it describes lives
    inside a field that is opaque there -- so a record that arrives declaring
    ranges keeps declaring them through every write here unless somebody takes
    the name back off. It would then name a model whose `verified` key this
    write just replaced, and a reader trusting the declaration could no longer
    tell "no holes" from "written by somebody who never heard of holes", which
    is the one distinction the declaration exists to make. See
    abstraction-download/testdata/scenarios/ranges-withdrawal.txt, and Go's withdrawRanges.

    `validators` go down in the same write as the bytes they identify. A
    checkpoint that records how far it got but not WHICH version it got that far
    through is the checkpoint this key exists to stop existing. The key is
    written even when it is empty, because the record is compared byte for byte
    across implementations and one absence is allowed one spelling.
    """
    rec.checkpoint = {
        "verified_prefix": verified_prefix,
        "validators": (validators or Validators()).to_dict(),
    }
    rec.content = [n for n in rec.content if n != FEATURE_RANGES]


# ------------------------------------------------------------- credentials ---


def _cred_env(name: str) -> str:
    return name.upper().replace("-", "_").replace(".", "_")


def _credential_bound(name: str, host: str) -> bool:
    """Whether credential NAME may be sent to host, per
    $ABSTRACTION_CRED_<NAME>_HOSTS -- a comma-separated list of host patterns,
    each covering its subdomains the way the reach list does.

    The binding lives beside the secret, on the machine, and nowhere else. Not
    in the record, which is the thing an attacker writes; not in the store's
    configuration, which a shared store lets anyone write. An unbound credential
    reaches nobody -- fail closed, because the other default is the owner's token
    on whatever host a record names."""
    host = (host or "").lower()
    if not host:
        return False
    env = "ABSTRACTION_CRED_" + _cred_env(name) + "_HOSTS"
    for pat in os.environ.get(env, "").split(","):
        pat = pat.strip().lower()
        if pat and (host == pat or host.endswith("." + pat)):
            return True
    return False


def credentials(name: str, host: str) -> Dict[str, str]:
    """Resolve a credential NAME into headers for the host it is about to be
    sent to.

    The record holds only the name. The secret comes from the environment at the
    moment of the request, because a record is deliberately readable by every
    other process — that is what makes progress observable — and so it is the
    last place a secret may live.

    host is not decoration. A record is written by anyone who can write the
    store and it chooses the source's host, so a credential resolved without
    regard to where it is going is a credential the record can point at a server
    the attacker controls. The secret is refused for every host it is not bound
    to on this machine.
    """
    if not name:
        return {}
    env = "ABSTRACTION_CRED_" + _cred_env(name)
    token = os.environ.get(env, "")
    if not token and name.lower() == "hf":
        token = os.environ.get("HF_TOKEN", "")
    if not token:
        # Refuse rather than fetch anonymously. A public 404 in place of a gated
        # file is a confusing failure; "you did not set this" is not.
        raise DownloadError(
            f"download: source needs credential {name!r} but ${env} is not set"
        )
    if not _credential_bound(name, host):
        # Refuse rather than send the secret onward. Not permanent: a machine
        # that binds this credential to this host runs the same record.
        raise DownloadError(
            f"download: credential {name!r} is not configured for host {host!r} -- "
            f"bind it on the fetching machine with ${env}_HOSTS, never in the record"
        )
    return {"Authorization": "Bearer " + token}


def headers_for(src: Source) -> Dict[str, str]:
    """Everything that goes on the wire for one source.

    What the record named, plus the secret resolved here and now. ``attrs`` are
    not consulted: they describe the source to this layer, and a bag that was
    both would send the next attribute anybody adds to a third party.
    """
    out = dict(src.headers)
    name = src.attrs.get(CREDENTIAL_ATTR, "")
    got = credentials(name, vouched_host(src.locator)) if name else {}
    into = src.attrs.get(CREDENTIAL_HEADER_ATTR, "").strip()
    if got and into:
        out[into] = next(iter(got.values()))
        return out
    out.update(got)
    return out


# ------------------------------------------------------------------ runner ---


def store_root() -> str:
    """Where jobs live on this machine.

    The config layer answers, in one place, for every language: the machine
    file an administrator wrote, then this user's file, then the environment
    for one run. This module used to carry its own four rungs and read no file
    at all, so a store chosen in the control panel was invisible here and work
    was submitted into a directory nobody was watching.
    """
    return _config.job_store()[0]


@dataclass
class Supervisor:
    """A process that watches this store and finishes work nobody is doing.

    Presence is the configuration. An application does not choose a downloader:
    it asks whether anything is watching, and if something is, it submits the
    work and stops caring who moves the bytes.
    """

    owner: str = ""
    host: str = ""
    pid: int = 0
    seen: Optional[datetime] = None
    every: str = ""
    tier: str = ""
    user: str = ""
    endpoint: str = ""
    """The account the supervisor runs as, spelled the way the platform spells
    it: the SID on Windows, the numeric uid elsewhere.

    "The same machine" is not the question an application with an absolute sink
    is asking. A sink inside a person's own tree is that person's to write, and
    a machine-wide supervisor running as a service account shares the filesystem
    and not the rights. Empty means the supervisor did not say, which is "I
    cannot tell" and never a match.
    """


def supervisor_of(store) -> Tuple[Supervisor, bool]:
    """The live supervisor for this store, if there is one.

    "Live" means the heartbeat is younger than three of its own intervals --
    enough to survive a slow sweep or a jittery clock, short enough that a killed
    supervisor stops attracting work within a minute or two rather than forever.
    A supervisor that was killed leaves a stale file behind, and an application
    that trusted it would submit work into a store nobody is watching, which
    looks exactly like a download that started and never progressed.

    A store whose binding is not a filesystem has no heartbeat to read, and
    answers no rather than pretending.
    """
    root = local_root(store)
    if not root:
        return Supervisor(), False
    try:
        with open(os.path.join(root, HEARTBEAT), "rb") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return Supervisor(), False

    s = Supervisor(
        owner=d.get("owner", ""),
        host=d.get("host", ""),
        pid=int(d.get("pid", 0) or 0),
        every=d.get("every", ""),
        tier=d.get("tier", "") or "",
        user=d.get("user", "") or "",
        endpoint=d.get("endpoint", "") or "",
    )
    try:
        s.seen = _parse_time(d["seen"])
    except Exception:
        return s, False

    every = _parse_go_duration(s.every)
    if every <= 0:
        every = 30.0
    age = (datetime.now(timezone.utc) - s.seen).total_seconds()
    return s, age <= 3 * every


def _parse_go_duration(text: str) -> float:
    """Go writes "30s", "1m30s", "2h45m0s". Parsed here because the heartbeat is
    written by whichever implementation happens to be supervising, and this one
    must read what that one wrote."""
    if not text:
        return 0.0
    units = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
    total = 0.0
    for value, unit in re.findall(r"([0-9]*\.?[0-9]+)(ns|us|ms|s|m|h)", text):
        total += float(value) * units[unit]
    return total


def owner(program: Optional[str] = None) -> str:
    """program@host:pid.

    The program name is read from the executable rather than supplied, for the
    same reason the Go side does it: asking an application to state its own name
    is asking for a claim.
    """
    if program is None:
        import sys

        program = os.path.basename(sys.argv[0] or "python")
        if program.endswith(".py"):
            program = program[:-3]
    return f"{program}@{socket.gethostname()}:{os.getpid()}"


def account() -> str:
    """The account this process runs as, in the one spelling every
    implementation of this layer uses: the SID on Windows, the numeric uid
    elsewhere.

    The platform's own identifier rather than a name, because the two sides of
    the comparison are written by different programs in different languages and
    a name is spelled differently by each of them -- ``COMPANY\\bob``, ``bob``,
    ``Bob`` -- while a SID and a uid are one string both can produce. The Go
    side reads the same value out of the same Win32 call. A false negative costs
    a download performed in the wrong process; a false positive sends bytes to a
    path the writer cannot write.

    Returns "" when the platform will not say, which is unknown rather than
    nobody, and every reader of it must refuse rather than assume.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    try:
        return _windows_sid()
    except Exception:
        return ""


def _windows_sid() -> str:
    """The current process token's user SID, as ``S-1-5-21-...``.

    ctypes rather than a package: this layer takes nothing from a registry, and
    the alternative spellings Python offers without it -- ``getpass.getuser``,
    ``%USERNAME%`` -- are names read out of the environment, which is not what
    the supervisor wrote down and not something two accounts cannot share.
    """
    import ctypes
    from ctypes import wintypes

    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.GetTokenInformation.restype = wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_wchar_p)]
    adv.ConvertSidToStringSidW.restype = wintypes.BOOL
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return ""
    try:
        need = wintypes.DWORD(0)
        adv.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(need))
        if not need.value:
            return ""
        buf = ctypes.create_string_buffer(need.value)
        if not adv.GetTokenInformation(token, TOKEN_USER, buf, need.value, ctypes.byref(need)):
            return ""
        # TOKEN_USER is a SID_AND_ATTRIBUTES: the SID pointer is its first field.
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = ctypes.c_wchar_p()
        if not adv.ConvertSidToStringSidW(psid, ctypes.byref(text)):
            return ""
        try:
            return text.value or ""
        finally:
            k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        k32.CloseHandle(token)


def could_deliver_here(sup: Supervisor) -> bool:
    """Could this supervisor write a sink only this machine's filesystem has?

    It shares the filesystem, and it runs as the account whose tree the path is
    in. Both, and said by the supervisor rather than assumed about it.

    The fence was drawn wrong twice, in opposite directions, and both were the
    same mistake -- a correct statement about one tier generalised into a rule
    about every tier.

    Too wide: the reason the refusal was written down was a NAS, and a NAS
    genuinely cannot resolve ``C:\\ComfyUI\\models\\x.safetensors``. A jobd on
    this machine can. Under the wide rule the submitting process ran the
    transfer itself, so a ComfyUI download did not survive ComfyUI closing --
    which is the claim this project makes.

    Not wide enough: "same machine" answers a question about the filesystem, and
    the question is about authority. A machine-wide supervisor running as a
    service account shares this filesystem and not this account's rights, so a
    sink in somebody's own models tree would be written by a service reaching
    into user space. Refused here rather than discovered at the write.

    Only the per-user case is admitted, and only when the supervisor says which
    it is. A supervisor that names no host, or no account, is answering nothing,
    and a missing answer is not a yes.
    """
    if not announced_here(sup):
        return False
    mine = account()
    return bool(mine) and bool(sup.user) and sup.user.lower() == mine.lower()


class Runner:
    """Claim a job, get the bytes, prove them, deliver them.

    Everything that must be identical across implementations lives here rather
    than in the transport: hashing, resume, progress persistence, lease renewal,
    the final rename. That is what lets a transfer begun by Go be finished by
    Python, which is the entire premise.
    """

    def __init__(
        self,
        store: Store,
        owner_name: Optional[str] = None,
        lease_ttl: float = 30.0,
        persist_every: int = 8 << 20,
        persist_interval: float = 5.0,
        response_timeout: float = 60.0,
        reach: Optional[Callable[[str], Optional[str]]] = None,
        shared_store: bool = False,
    ):
        self.store = store
        self.owner = owner_name or owner()
        self.lease_ttl = lease_ttl
        self.response_timeout = response_timeout
        # Asked for every host before a connection is opened to it, at the same
        # last moment a credential is resolved. None reaches everything.
        self.reach = reach
        # The store may be written by machines other than this one -- a NAS
        # share. On such a store an absolute sink is refused: it names THIS
        # machine's filesystem and the record naming it was written by somebody
        # else. A relative sink resolves under the store root and stays
        # contained; an absolute one is legitimate only for a caller writing to
        # its own machine, never an adopted record. See _refuse_unportable_sink.
        self.shared_store = shared_store
        # Bytes OR time. A byte threshold alone is silently wrong on a slow link:
        # a real 313 MB download killed after 12 seconds had checkpointed nothing
        # because it had not reached 8 MiB, and a slow connection is exactly when
        # resuming matters most.
        self.persist_every = persist_every
        self.persist_interval = persist_interval

    def run(self, job_id: str) -> None:
        rec = self.store.claim(job_id, self.owner, self.lease_ttl)
        epoch = rec.lease.epoch
        hold = KeepAwake(self.store, rec)
        try:
            self._run(rec, epoch)
        except Exception as e:
            # Record why, so a human reading the job later does not have to find
            # the log of a process that no longer exists -- and record whether
            # this is over. A refusal that stays adoptable is fetched again on
            # every sweep for as long as the store exists, and nothing waiting
            # on the record can ever stop waiting.
            def note(r: Record) -> None:
                _set_failure(r, e)
                if permanent(e):
                    r.state = FAILED

            try:
                self.store.update(job_id, epoch, note)
                # And let go. This owner has stopped working, so holding the
                # lease until it lapses only makes the job unadoptable for
                # thirty seconds while nobody is moving any bytes -- and it
                # makes a waiting requester unable to tell "failed" from "still
                # going", because both look like a job somebody holds.
                self.store.release(job_id, epoch)
            except Exception:
                pass
            hold.release()
            raise
        # Let go: the bytes are proven, and the only thing left is for whoever
        # wanted them to say so, which means claiming this job.
        try:
            self.store.release(job_id, epoch)
        except Exception:
            pass
        hold.release()

    def _refuse_unportable_sink(self, sink: "Sink") -> None:
        """Stop a runner over a shared store from writing an absolute sink a
        foreign record chose. Lexical containment already refuses a relative
        sink that climbs out of the root; this closes the half it never covered,
        where the sink names a filesystem directly and never touches the root."""
        if not self.shared_store:
            return
        for p in (sink.final, sink.partial):
            if p and not _relative_everywhere(p):
                raise UnportableSink(
                    "download: a shared store's record may only name a relative sink: " + p
                )

    def _run(self, rec: Record, epoch: int) -> None:
        # Before a byte moves. This owner may have just adopted a job whose
        # predecessor died between a pause being asked for and the pause being
        # carried out: that record is left RUNNING under a lapsed lease, and the
        # only way out of it is for the next owner to honour what was asked
        # instead of starting the transfer and finding out at its first
        # checkpoint. It pairs with Record.stranded, which is what offers such a
        # record to a sweep at all; either half alone is wrong.
        want = rec.wants()
        if want != RUN:
            return self._honour(want, rec.id, epoch)

        spec = spec_of(rec)
        # Whatever this store has already proven goes ahead of every source the
        # record names -- on the machine that adopts a delegated job, that is
        # the delegate's own earlier delivery, which the record cannot name.
        spec.sources = proven(self.store, spec.artifact.digest, rec.id) + spec.sources
        cp = checkpoint_of(rec)
        partial, final = local_sink(self.store, rec.id, spec.sink)
        self._refuse_unportable_sink(spec.sink)

        os.makedirs(os.path.dirname(partial) or ".", exist_ok=True)

        # Resume point: the SMALLER of what the checkpoint proved and what the
        # file actually holds. They differ after a crash, and trusting either
        # alone is how a resumed download ends up the right length and the wrong
        # bytes.
        on_disk = os.path.getsize(partial) if os.path.exists(partial) else 0
        start = min(cp.verified_prefix, on_disk)
        _truncate(partial, start)

        h = hashlib.sha256()
        if start:
            # Hold the lease across it. This is local work with nothing to
            # report and it is proportional to the file: rehashing a 40 GB
            # partial at disk speed outlasts a thirty-second lease many times
            # over, and nothing renewed here — so the FIRST checkpoint after a
            # resume was refused for a lease that lapsed while this owner sat
            # reading its own file.
            _hash_prefix(partial, start, h, self._renewing(rec.id, epoch))

        try:
            total, h, seen = self._fetch(
                rec, spec, epoch, partial, h, start, cp.validators
            )
        except Asked as asked:
            return self._honour(asked.want, rec.id, epoch)

        if spec.artifact.size and total != spec.artifact.size:
            raise ShortTransfer(
                f"download: got {total} bytes, expected {spec.artifact.size}"
            )

        if spec.artifact.digest:
            got = "sha256:" + h.hexdigest()
            if not same_digest(got, spec.artifact.digest):
                # Do not keep bytes that failed: leaving them means the next
                # runner resumes onto a prefix already known to be wrong.
                try:
                    os.remove(partial)
                except OSError:
                    pass

                def reset(r: Record) -> None:
                    r.progress.done = 0
                    set_checkpoint(r, 0)

                self.store.update(rec.id, epoch, reset)
                raise DigestMismatch(
                    f"download: got {got}, want {spec.artifact.digest}"
                )

        _deliver(partial, final)

        # Transferred, not complete: the bytes are here and proven, but whoever
        # wanted them has not said so yet.
        def done(r: Record) -> None:
            r.progress.done = total
            r.state = TRANSFERRED
            _clear_failure(r)
            set_checkpoint(r, total, seen)

        self.store.update(rec.id, epoch, done)

    def _honour(self, want: str, job_id: str, epoch: int) -> None:
        """Carry out what somebody asked for, now that this owner has stopped.

        This is the half of the contract that makes intent a contract rather
        than a field: the job layer says an owner must converge on what was
        asked, and this is where an owner does it.

        Pause needs no write of its own. run() releases the lease on the way
        out, which is what turns the record back into a pending one -- and
        holding it would tell every reader, including the status line a person
        is watching, the opposite of what they asked for.
        """
        if want != CANCEL:
            return

        def cancelled(r: Record) -> None:
            r.state = CANCELLED
            _clear_failure(r)

        self.store.update(job_id, epoch, cancelled)

    def take_delivery(self, job_id: str) -> None:
        """The requester saying "I have it". Without this a finished job waits in
        the store forever for somebody who never comes."""
        rec = self.store.load(job_id)
        if rec.state == COMPLETE:
            return
        if rec.state != TRANSFERRED:
            raise DownloadError(f"download: {job_id} is {rec.state}, not {TRANSFERRED}")
        def mark(r: Record) -> None:
            r.state = COMPLETE

        # Twice, because losing this write is losing a race and not losing the
        # bytes. The runner that just finished releases its own lease a moment
        # after writing TRANSFERRED, under the SAME owner name and often from
        # the same process, so a claim taken here is allowed to interleave with
        # that release -- and the release, read-modify-write against the record,
        # can land on top of the epoch this claim took. The transfer is proven
        # either way, and reporting a model that arrived as one that failed is
        # the worse of the two answers.
        for _ in range(2):
            try:
                held = self.store.claim(job_id, self.owner, self.lease_ttl)
            except Exception:
                return  # somebody else is mid-delivery; the bytes are still proven
            try:
                self.store.update(job_id, held.lease.epoch, mark)
            except StaleEpoch:
                continue
            # No release afterwards. COMPLETE is terminal, so the store refuses
            # every write from this epoch including the one that hands the lease
            # back -- and there is nothing to hand it back to, because nothing
            # may claim a finished job either. The lease lapses on its own.
            return

    def adopt(self) -> int:
        """Finish work nobody is doing. The primary reclamation path, not a
        fallback: a SIGKILLed process never hands anything over."""
        n = 0
        for o in self.store.orphans():
            if o.kind != KIND or o.delegated():
                continue
            if time.time() < retry_after(o):
                continue
            try:
                self.run(o.id)
                n += 1
            except Exception:
                continue  # one bad job must not stop the rest being rescued
        return n

    # -- transport ---------------------------------------------------------

    def _fetch(
        self, rec: Record, spec: Spec, epoch: int, partial: str, h, start: int,
        seen: Validators,
    ) -> Tuple[int, "hashlib._Hash", Validators]:
        """Get the bytes, and answer with the hash and the validators that cover
        them.

        Both come back because a restart replaces both. Everything derived from
        the discarded prefix goes at once -- the file, the rolling hash, the
        recorded progress, the validators that identified it, the offset the
        next source would resume from -- and a caller left holding the old ones
        would verify the artifact against bytes no longer in the file, and
        record a version the file no longer holds.
        """
        ordered = sorted(spec.sources, key=lambda s: s.priority)
        failures: List[Exception] = []

        def restart():
            """Begin again at zero, with the file already open.

            A source has said the artifact it holds is not the one the prefix
            came from, so the prefix is not a prefix of anything anybody can
            still fetch. Losing it costs the bytes already moved; refusing it
            costs the download, and this layer exists to not lose downloads.
            The remaining sources see the reset state, because a mirror asked to
            continue from an offset nothing on disk backs would splice two files
            together.
            """
            nonlocal h, start, seen
            _truncate(partial, 0)
            h, start, seen = hashlib.sha256(), 0, Validators()

            def reset(r: Record) -> None:
                r.progress.done = 0
                set_checkpoint(r, 0)

            self.store.update(rec.id, epoch, reset)
            return h

        for src in ordered:
            try:
                total, got = self._fetch_one(
                    rec, src, epoch, partial, h, start, seen, restart
                )
                return total, h, got
            except Asked:
                raise
            except Exception as e:  # try the next source; mirrors exist for this
                failures.append(e)
        if not failures:
            raise NoSource("download: no source could be used")
        # A source that says no does not speak for a mirror that merely dropped
        # the connection. The job is only over when nothing left could work, so
        # a single retryable failure anywhere in the list is what gets raised.
        retryable = [e for e in failures if not permanent(e)]
        raise (retryable or failures)[-1]

    def _fetch_one(
        self, rec: Record, src: Source, epoch: int, partial: str, h, start: int,
        seen: Validators, restart,
    ) -> Tuple[int, Validators]:
        if src.scheme not in TRANSPORTS:
            raise NoSource(f"download: no transport for scheme {src.scheme!r}")
        missing = sorted(set(rec.requires) - TRANSPORTS[src.scheme])
        if missing:
            raise NoSource(
                f"download: {src.scheme} does not offer {', '.join(missing)}"
            )
        self._may_reach(vouched_host(src.locator))
        if src.scheme in ("http", "https"):
            return self._fetch_http(rec, src, epoch, partial, h, start, seen, restart)
        return self._fetch_file(rec, src, epoch, partial, h, start), Validators()

    def _fetch_http(
        self, rec: Record, src: Source, epoch: int, partial: str, h, start: int,
        seen: Validators, restart,
    ) -> Tuple[int, Validators]:
        # Twice at most. The second attempt exists only for a source whose
        # answer cannot be written at the offset it was asked for and is not
        # itself a whole artifact, so a fresh unranged request is the only way
        # to get one.
        for attempt in (0, 1):
            try:
                resp = self._open(self._request(src, start, seen))
            except urllib.error.HTTPError as e:
                if not (start and e.code == 416):
                    raise
                # The offset on disk is past the end of what the source holds, so
                # the prefix cannot be a prefix of it and no retry can make it one.
                # Nothing about the record changes between attempts, so leaving this
                # as "not now" asked the same question forever and got the same 416
                # until somebody deleted the partial by hand. README.md § Two
                # endings: 416 and 412 restart cleanly. Found by
                # abstraction-download/testdata/scenarios/wire-416.txt.
                h, start, seen = restart(), 0, Validators()
                continue
            with resp:
                coding = unwanted_coding(resp.headers)
                if coding:
                    raise DownloadError(
                        f"download: {src.locator} applied Content-Encoding "
                        f"{coding} to a request that asked for identity"
                    )

                # A 200 answering a ranged request means the server sent the whole
                # file from zero. Appending it to the prefix on disk splices two
                # files together at an arbitrary offset, which is what `curl -C -`
                # does. So rewind and take it: the body IS a complete artifact, the
                # earlier bytes are simply wasted, and the transfer proceeds. Refusing
                # instead throws away a download that was going to succeed, and it
                # kept doing so on every retry, because nothing about the record ever
                # changed. Chromium reaches the same conclusion in
                # components/download/internal/common/download_utils.cc near line 349.
                if start and resp.status != 206:
                    h, start = restart(), 0
                elif resp.status == 206:
                    # Every 206, ranged or not: a first fetch sends no Range and
                    # a CDN may still answer 206, which is acceptable exactly
                    # when it names the offset being written at -- zero. One
                    # rule, not two.
                    why = answers_from(resp.headers, start)
                    if why and (not start or attempt):
                        raise DownloadError(why)
                    if why:
                        h, start, seen = restart(), 0, Validators()
                        continue

                return self._take(rec, resp, epoch, partial, h, start)
        raise DownloadError(f"download: {src.locator} would not serve the artifact whole")

    def _take(self, rec: Record, resp, epoch: int, partial: str, h,
              start: int) -> Tuple[int, Validators]:
        """Read the body of a response this layer has decided to accept."""
        # What this response says about the version being served, recorded
        # now so the next attempt can ask for the same one. On a first
        # download this is the only chance to learn it; on a resume it
        # confirms it.
        seen = strong_validators(resp.headers)

        # Worked out BEFORE the copy. Content-Length on a 200 is the whole
        # artifact; on a 206 it is what remains, so the offset has to be
        # added back.
        declared = 0
        try:
            length = int(resp.headers.get("Content-Length") or 0)
            if length > 0:
                declared = start + length
        except ValueError:
            declared = 0

        total = self._drain(rec, resp, epoch, partial, h, start, seen)

        # The transport's own promise, checked even when the spec makes
        # none. A spec without a size or a digest is the normal case for a
        # bare URL out of a model list, and without this there was NOTHING
        # to catch a truncated transfer: the partial was delivered under the
        # final name and presented to the application as a finished
        # download. That is precisely the failure this layer exists to
        # refuse, reproduced by the layer itself.
        #
        # Go gets this for free -- its HTTP client errors on a body shorter
        # than Content-Length -- and urllib does not, which is why this
        # cannot be left to the language underneath.
        if declared and total != declared:
            raise ShortTransfer(
                f"download: got {total} bytes, the server said {declared}"
            )
        return total, seen

    def _request(self, src: Source, start: int, seen: Validators) -> "urllib.request.Request":
        req = urllib.request.Request(src.locator)
        for k, v in headers_for(src).items():
            req.add_header(k, v)
        # On every request, not only the ranged ones. Offsets on this side are
        # counted after decoding and the server's before it, so a range over a
        # compressed body names a different byte at each end -- but the digest
        # is over the artifact, and a coding applied to a whole body changes
        # what "the bytes" are just as much.
        req.add_header("Accept-Encoding", "identity")
        if start:
            req.add_header("Range", f"bytes={start}-")
            # Say which version the bytes on disk came from. Without this a
            # source whose file has changed answers the range honestly, with a
            # valid range of a DIFFERENT file, and nothing in the response says
            # so. With it the server answers 206 only if the file is unchanged
            # and 200 -- the whole new file -- if it is not.
            if seen.if_range():
                req.add_header("If-Range", seen.if_range())
        return req

    def _open(self, req: "urllib.request.Request"):
        """urlopen, with a bounded wait and a name for a refusal.

        The timeout is not a nicety. urllib blocks forever on a socket that was
        accepted and never answered, so a source that goes quiet stops the
        transfer, stops the checkpoints, stops the lease renewals, and leaves
        every waiter on the record waiting on a process that will never write
        again. It bounds the wait for BYTES, not for the download: a slow link
        that keeps delivering resets it on every read.
        """
        try:
            opener = urllib.request.build_opener(_Reaching(self._may_reach))
            return opener.open(req, timeout=self.response_timeout)
        except urllib.error.HTTPError as e:
            e.close()
            if _refused(e.code):
                raise RefusedBySource(f"download: {req.full_url}: {e}") from e
            raise

    def _may_reach(self, host: str) -> None:
        why = self.reach(host) if self.reach and host else None
        if why:
            raise Unreachable(f"download: this machine will not reach {host}: {why}")

    def _fetch_file(
        self, rec: Record, src: Source, epoch: int, partial: str, h, start: int
    ) -> int:
        with open(src.locator, "rb") as f:
            f.seek(start)
            return self._drain(rec, f, epoch, partial, h, start, Validators())

    def _drain(
        self, rec: Record, reader, epoch: int, partial: str, h, start: int,
        seen: Validators,
    ) -> int:
        total = start
        since_persist = 0
        last_persist = time.monotonic()
        with open(partial, "r+b" if os.path.exists(partial) else "w+b") as out:
            out.seek(start)
            try:
                while True:
                    chunk = reader.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    total += len(chunk)
                    since_persist += len(chunk)

                    now = time.monotonic()
                    if (
                        since_persist >= self.persist_every
                        or now - last_persist >= self.persist_interval
                    ):
                        out.flush()
                        os.fsync(out.fileno())
                        want = self._persist(rec.id, epoch, total, seen)
                        since_persist = 0
                        last_persist = now
                        # The record was just read and written, so what somebody
                        # wants is in hand at no extra cost. Stopping here rather
                        # than at the end of the transfer is the difference
                        # between a pause button that works and one that takes
                        # effect in forty minutes.
                        if want != RUN:
                            raise Asked(want)
            finally:
                # However this ends. Every byte counted here was written AND
                # hashed, so it is proven whether the transfer went on to
                # succeed, fail, or be killed -- and recording it is the whole
                # difference between resuming and starting over.
                #
                # Periodic checkpointing alone is not enough, and the gap is
                # worst exactly where it is least expected: a transfer that dies
                # before the FIRST checkpoint has proven nothing, so the resume
                # point is min(0, what is on disk) = 0, and a partial file
                # sitting right there is fetched again from zero. On a fast link
                # that is every download under 8 MiB; on a slow one it is the
                # first five seconds of a 40 GB model.
                out.flush()
                os.fsync(out.fileno())
                if total > start:
                    try:
                        self._persist(rec.id, epoch, total, seen)
                    except Exception:
                        # A store that will not take the checkpoint must not
                        # replace the real error with its own.
                        pass
        return total

    def _persist(self, job_id: str, epoch: int, done: int, seen: Validators) -> str:
        """Write down what has been proven, renew the lease on the same beat,
        and answer with what somebody wants this job to do.

        Renewal rides this callback deliberately: without it a transfer that is
        progressing slowly lets its own lease expire and is adopted as an orphan
        while still running.
        """

        def mutate(r: Record) -> None:
            r.progress.done = done
            r.state = RUNNING
            set_checkpoint(r, done, seen)

        try:
            self.store.renew(job_id, epoch, self.lease_ttl)
            return self.store.update(job_id, epoch, mutate).wants()
        except Exception:
            # A failed checkpoint is not a failed download. The worst case is
            # re-fetching from the last one that worked -- and a want nobody
            # could read is not a want to act on.
            return RUN

    def _renewing(self, job_id: str, epoch: int) -> Callable[[], None]:
        """A callback that holds the lease across a long local read.

        Cheap enough to call once per chunk, which is what a caller reading
        gigabytes needs, and it writes the record at most three times per TTL.

        Driven by the read returning bytes rather than by a bare timer, and that
        is the whole safety argument: an owner blocked in a read produces no
        chunks, so it beats nothing, renews nothing, and its lease lapses — which
        is what lets the work move to somebody else. A timer would keep renewing
        for a process that had stopped moving, and nobody could ever take over.
        See docs/stall-detection.md.

        A refusal is not swallowed. Being told the lease is gone is the fence:
        this owner must stop acting on the work at this epoch, and raising out of
        the hash is exactly that — nothing has been written to the artifact yet.
        """
        every = max(self.lease_ttl / 3.0, 0.001)
        last = time.monotonic()

        def beat() -> None:
            nonlocal last
            now = time.monotonic()
            if now - last < every:
                return
            last = now
            self.store.renew(job_id, epoch, self.lease_ttl)

        return beat


# ------------------------------------------------------------------ client ---


class _Reaching(urllib.request.HTTPRedirectHandler):
    """Every host a redirect leads to is asked about where the socket opens,
    not only the one the record named."""

    def __init__(self, may_reach: Callable[[str], None]):
        self.may_reach = may_reach

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.may_reach(vouched_host(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# The bus is the one connection a process has to a supervisor, on the transport
# the identity layer binds: a named pipe on Windows, a unix socket elsewhere.
# Every request arrives with the program that made it, and a caller the kernel
# cannot name is refused. The store stays the truth; the bus carries "look" and
# "who" and nothing that would be a second copy of it.

ANSWERED = "answered"
REFUSED = "refused"
NOBODY = "nobody"
UNREACHABLE = "unreachable"

# A supervisor that has not answered a one-line request in this long is not one
# a submit waits for: the sweep is still coming. The same figure as Go's busWait.
BUS_DEADLINE = 2.0
_BUS_LINE = 64 * 1024


def nudge(store) -> str:
    """Ask the supervisor watching this store to sweep now.

    ANSWERED: an identified caller was heard. REFUSED: this caller was not.
    NOBODY: the announced supervisor does not answer. UNREACHABLE: it can only
    be reached through the store, which the sweep already is.
    """
    return ask(store, "look")[0]


def who(store) -> Tuple[str, dict]:
    """Ask the supervisor who it is and who it takes this process for."""
    return ask(store, "who")


def ask(store, op: str) -> Tuple[str, dict]:
    sup, live = supervisor_of(store)
    if not live:
        return NOBODY, {}
    if not sup.endpoint or not announced_here(sup):
        return UNREACHABLE, {}
    request = json.dumps({"op": op}, separators=(",", ":")).encode("utf-8") + b"\n"
    line = _exchange(sup.endpoint, request, time.monotonic() + BUS_DEADLINE)
    if line is None:
        return NOBODY, {}
    try:
        answer = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return NOBODY, {}
    if not isinstance(answer, dict):
        return NOBODY, {}
    if answer.get("error"):
        return REFUSED, answer
    return ANSWERED, answer


def announced_here(sup: Supervisor) -> bool:
    return bool(sup.host) and sup.host.lower() == socket.gethostname().lower()


def _exchange(endpoint: str, request: bytes, deadline: float) -> Optional[bytes]:
    """Connect, send one line, read one line. None means nobody answered."""
    if sys.platform == "win32":
        return _win_exchange(endpoint, request, deadline)
    return _unix_exchange(endpoint, request, deadline)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _line_from(chunks: List[bytes]) -> Optional[bytes]:
    buf = b"".join(chunks)
    cut = buf.find(b"\n")
    if cut < 0 or cut > _BUS_LINE:
        return None
    return buf[:cut]


def _unix_exchange(path: str, request: bytes, deadline: float) -> Optional[bytes]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_remaining(deadline))
        sock.connect(path)
        sock.settimeout(_remaining(deadline))
        sock.sendall(request)
        chunks: List[bytes] = []
        total = 0
        while total <= _BUS_LINE:
            left = _remaining(deadline)
            if left <= 0:
                return None
            sock.settimeout(left)
            chunk = sock.recv(4096)
            if not chunk:
                return None
            chunks.append(chunk)
            total += len(chunk)
            line = _line_from(chunks)
            if line is not None:
                return line
        return None
    except OSError:
        return None
    finally:
        sock.close()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _FILE_FLAG_OVERLAPPED = 0x40000000
    # Identification level: the supervisor may learn who we are and cannot act
    # as us.
    _SECURITY_SQOS_PRESENT = 0x00100000
    _SECURITY_IDENTIFICATION = 0x00010000
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _ERROR_PIPE_BUSY = 231
    _ERROR_IO_PENDING = 997
    _WAIT_OBJECT_0 = 0

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _LPOVERLAPPED = ctypes.POINTER(_OVERLAPPED)
    # Without argtypes ctypes truncates a 64-bit HANDLE to an int.
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.CreateEventW.restype = wintypes.HANDLE
    _k32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), _LPOVERLAPPED,
    ]
    _k32.WriteFile.restype = wintypes.BOOL
    _k32.ReadFile.argtypes = _k32.WriteFile.argtypes
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.GetOverlappedResult.argtypes = [
        wintypes.HANDLE, _LPOVERLAPPED, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
    ]
    _k32.GetOverlappedResult.restype = wintypes.BOOL
    _k32.CancelIoEx.argtypes = [wintypes.HANDLE, _LPOVERLAPPED]
    _k32.CancelIoEx.restype = wintypes.BOOL
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _k32.WaitNamedPipeW.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    def _open_pipe(path: str, deadline: float, sqos: int = _SECURITY_IDENTIFICATION):
        flags = _FILE_FLAG_OVERLAPPED | _SECURITY_SQOS_PRESENT | sqos
        for attempt in (0, 1):
            h = _k32.CreateFileW(path, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, flags, None)
            if h != _INVALID_HANDLE:
                return h
            # Busy is not absent: every instance is talking to somebody. One
            # wait, inside the budget.
            if ctypes.get_last_error() != _ERROR_PIPE_BUSY or attempt == 1:
                return None
            left = int(_remaining(deadline) * 1000)
            if left <= 0 or not _k32.WaitNamedPipeW(path, left):
                return None
        return None

    def _overlapped_io(fn, handle, buf, nbytes: int, deadline: float) -> Optional[int]:
        # A synchronous read on a pipe cannot be interrupted, so the deadline is
        # FILE_FLAG_OVERLAPPED plus CancelIoEx, and the cancelled operation is
        # waited for before its buffer goes out of scope: the kernel may still
        # be writing into it.
        ov = _OVERLAPPED()
        event = _k32.CreateEventW(None, True, False, None)
        if not event:
            return None
        ov.hEvent = event
        moved = wintypes.DWORD(0)
        try:
            if not fn(handle, buf, nbytes, ctypes.byref(moved), ctypes.byref(ov)):
                if ctypes.get_last_error() != _ERROR_IO_PENDING:
                    return None
                if _k32.WaitForSingleObject(event, int(_remaining(deadline) * 1000)) != _WAIT_OBJECT_0:
                    _k32.CancelIoEx(handle, ctypes.byref(ov))
                    _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(moved), True)
                    return None
                if not _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(moved), False):
                    return None
            return moved.value
        finally:
            _k32.CloseHandle(event)

    def _win_exchange(path: str, request: bytes, deadline: float, sqos: int = _SECURITY_IDENTIFICATION) -> Optional[bytes]:
        handle = _open_pipe(path, deadline, sqos)
        if handle is None:
            return None
        try:
            out = ctypes.create_string_buffer(request, len(request))
            sent = 0
            while sent < len(request):
                n = _overlapped_io(_k32.WriteFile, handle, ctypes.byref(out, sent), len(request) - sent, deadline)
                if not n:
                    return None
                sent += n
            chunks: List[bytes] = []
            total = 0
            inbuf = ctypes.create_string_buffer(4096)
            while total <= _BUS_LINE:
                n = _overlapped_io(_k32.ReadFile, handle, inbuf, 4096, deadline)
                if not n:
                    return None
                chunks.append(inbuf.raw[:n])
                total += n
                line = _line_from(chunks)
                if line is not None:
                    return line
            return None
        finally:
            _k32.CloseHandle(handle)

else:

    def _win_exchange(path: str, request: bytes, deadline: float, sqos: int = 0) -> Optional[bytes]:
        raise NotImplementedError


class Client:
    """Downloading, for an application that holds no store, no runner and no
    opinion about who does the work.

    Everything below is what an application would otherwise write for itself,
    and the ComfyUI node is the proof that it does: it hand-rolled in-flight
    dedupe, the partial naming convention, store construction and take-delivery,
    four decisions this layer exists to make. A per-application entry point that
    does not port is the thing a facade exists to prevent.

    Submit, and who executes is settled below this line. If a supervisor is
    watching, it takes the work and this process may exit. If not, this process
    does it -- and if this process then exits mid-transfer, the record and the
    partial are durable, the lease lapses, and the next launch adopts it.

    Ids rather than handles, and jobs() a snapshot rather than a live
    collection: Go's Client returns a job.Job and a job.Subscription, and the
    Python job layer has neither.
    """

    def __init__(self, store: Store, runner: Optional[Runner] = None):
        self.store = store
        self.runner = runner or Runner(store)
        # The threads this process started. There is no Close on a Client in
        # either language -- submitting starts work on nothing anybody can join
        # -- and a test that walks away from a transfer leaves it writing into a
        # directory the test is deleting. Kept so a test can settle.
        self._workers: List[threading.Thread] = []

    def get(self, source: str, destination: str = "") -> str:
        """Fetch source to destination. A directory takes the name from the
        source. Returns immediately; the work outlives this call and may outlive
        this process."""
        if not source.strip():
            raise NoSource("download: no source")
        dest = destination or "."
        if dest.endswith(("/", "\\")) or os.path.isdir(dest):
            dest = os.path.join(dest, _name_from(source))
        # Absolute, because the process that finally moves these bytes may have a
        # different working directory, a different user, or be on another machine.
        return self.submit(
            Spec(
                sources=[Source(scheme=_scheme_from(source), locator=source)],
                sink=Sink(final=os.path.abspath(dest)),
            )
        )

    def submit(self, spec: Spec, requires: Optional[List[str]] = None) -> str:
        """get for a caller that knows more: a digest to verify against, several
        sources, capabilities an implementation must have to qualify."""
        delivered = self._delivered(spec)
        if delivered:
            return delivered
        existing = self._in_flight(spec)
        if existing:
            # Nudge it along: if its owner died, this is what gets it picked up.
            self._begin(existing, spec)
            return existing
        # Bytes the store has already proven go in front of the network.
        spec.sources = proven(self.store, spec.artifact.digest) + spec.sources
        job_id = submit(self.store, spec, requires)
        self._begin(job_id, spec)
        return job_id

    def open(self, job_id: str) -> Record:
        return self.store.load(job_id)

    def jobs(self) -> List[Record]:
        return [r for r in self.store.list() if r.kind == KIND]

    def where(self) -> str:
        """What would answer, for a status line. Display text, never a branch."""
        sup, live = supervisor_of(self.store)
        if live:
            return sup.tier or "the system downloader"
        return "here"

    def take_delivery(self, job_id: str) -> None:
        self.runner.take_delivery(job_id)

    def deliver(self, job_id: str, timeout: Optional[float] = None) -> Record:
        """Wait until the bytes are here, then say so.

        The synchronous case, which is most adopters: ComfyUI-Manager calls
        download_url and expects the file to exist when it returns. Without this
        every one of them writes the same poll loop and then forgets the
        take-delivery, and the store fills with finished transfers nobody
        collected -- which is what BITS does for 90 days.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        sub = watch(self.store, KIND, 2 * self.runner.lease_ttl)
        try:
            while True:
                quiet, timed_out = False, False
                try:
                    left = None if deadline is None else max(0.0, deadline - time.monotonic())
                    quiet = sub.next(left).quiet
                except TimeoutError:
                    timed_out = True
                rec = self.store.load(job_id)
                if rec.state == TRANSFERRED:
                    self.take_delivery(job_id)
                    return self.store.load(job_id)
                if rec.terminal():
                    if rec.error:
                        raise DownloadError(rec.error)
                    return rec
                # An attempt that failed and let go of the job. Not a terminal
                # state -- the partial is still there and a successor will resume
                # it -- but it is the end of THIS request.
                if rec.error and self.store.claimable(rec):
                    raise DownloadError(rec.error)
                if quiet and self._unattended(rec):
                    # Nobody holds a lease, nobody is delegated to, and the
                    # record has not moved for longer than it takes a lease to
                    # lapse twice. That is not a slow download; it is a download
                    # nobody is performing, and it is what a supervisor dying
                    # after the nudge, or a worker that never won the claim,
                    # leaves behind. Waiting on it is the hang this method
                    # exists to make impossible.
                    raise DownloadError(
                        f"download: {job_id} is {rec.state} and nobody is working on it"
                    )
                if timed_out:
                    raise DownloadError(f"download: {job_id} is still {rec.state}")
        finally:
            sub.close()

    def _unattended(self, rec: Record) -> bool:
        """Nothing is working this job right now.

        A delegated job holds no lease here and never will, so "claimable" says
        yes about a transfer a NAS is actively performing. What answers for it is
        the supervisor: it is the thing that reconciles the delegate into the
        record, and if it is gone then so is every report the far side would ever
        have made.
        """
        if rec.delegated():
            return not supervisor_of(self.store)[1]
        return self.store.claimable(rec)

    def _delivered(self, spec: Spec) -> str:
        """The finished record that already put these exact bytes at this
        destination, or "". With a digest there is no "download it again": the
        bytes are the identity, and the record is the proof they are there."""
        if not spec.sink.final:
            return ""
        _, dest = local_sink(self.store, "", spec.sink)
        for src in proven(self.store, spec.artifact.digest):
            if same_path(src.locator, dest):
                return src.attrs["job"]
        return ""

    def _in_flight(self, spec: Spec) -> str:
        """The id of unfinished work for the same artifact and destination, or "".

        Asking twice for the same thing is one piece of work, not two. Without
        this, running the same command again starts a SECOND transfer of the same
        artifact, with its own partial beginning at zero, racing the first one to
        the same destination -- and repeating the command is the obvious way to
        resume an interrupted download.

        Identity is the destination plus the source. Not the digest: a caller
        often does not know one, which is exactly the case that needs this most.
        Only work still in flight matches, because "download it again" is a real
        request and a finished record is history rather than a claim.

        The destination is compared through ``same_path``, which asks the volume.
        The record on disk was written by whatever wrote it -- an older version
        of this layer, another language, an adopter that joined a native
        directory to a file name with a hardcoded "/" -- so comparing the
        spellings makes two names for one file two pieces of work, which is the
        duplicate fetch this whole layer exists to stop.
        """
        if not spec.sources:
            return ""
        for rec in self.store.list():
            if rec.kind != KIND or rec.terminal():
                continue
            try:
                got = spec_of(rec)
            except DownloadError:
                continue
            if not got.sources or not same_path(got.sink.final, spec.sink.final):
                continue
            if got.sources[0].locator == spec.sources[0].locator:
                return rec.id
        return ""

    def _begin(self, job_id: str, spec: Spec) -> None:
        """The decision that used to be the application's.

        A job nobody else could deliver is not offered to anybody else. A
        relative sink resolves against whichever store adopts the job, so any
        machine watching can finish it; an ABSOLUTE one names one filesystem,
        and only a supervisor that shares it -- and runs as the account whose
        tree it is in -- can deliver it. ``could_deliver_here`` is that
        question, and it is the same one the Go client asks.

        It is not the whole fence. A supervisor sweeping a shared store still
        finds this job as an orphan if this process dies mid-transfer, and
        nothing in the record tells it not to.
        """
        self._clear_last_error(job_id)
        bound_here = not _relative_everywhere(spec.sink.final)
        sup, live = supervisor_of(self.store)
        if live and (not bound_here or could_deliver_here(sup)):
            # The heartbeat predicts and the connection decides: a beat outlives
            # the process that wrote it, an endpoint does not.
            if nudge(self.store) != NOBODY:
                return
        worker = threading.Thread(target=self._run_here, args=(job_id,), daemon=True)
        self._workers.append(worker)
        worker.start()

    def _clear_last_error(self, job_id: str) -> None:
        """A new attempt is not the previous attempt's failure.

        The error string outlives the attempt that wrote it, deliberately: a
        person reading the job later should not have to find the log of a
        process that no longer exists. But it is then the record's answer to
        "what is happening now", which is wrong the moment somebody tries
        again -- a waiting requester would be handed the old failure before the
        new attempt had made a single request, and a UI would show an error for
        a job that is progressing.

        Best effort. A refused claim means somebody else is working on it, and
        their outcome is the current one.
        """
        try:
            if not self.store.load(job_id).error:
                return
            held = self.store.claim(job_id, self.runner.owner, self.runner.lease_ttl)
        except Exception:
            return

        def clear(r: Record) -> None:
            _clear_failure(r)

        try:
            self.store.update(job_id, held.lease.epoch, clear)
        finally:
            self.store.release(job_id, held.lease.epoch)

    def _run_here(self, job_id: str) -> None:
        """Work the job in this process, waiting out a dead owner's lease.

        The waiting is the point. A process killed mid-transfer does not release
        its lease -- that is the design -- so the obvious way to resume, running
        the same command again, arrives INSIDE the previous owner's lease window
        and is refused. It will lapse, because the owner is gone. Anything else
        stops the loop, and the record is where the outcome lives either way.
        """
        deadline = time.monotonic() + 2 * self.runner.lease_ttl + 5
        while True:
            try:
                self.runner.run(job_id)
                return
            except LeaseHeld:
                pass
            except Exception:
                return  # on the record already; nobody is listening here
            if time.monotonic() > deadline:
                return
            rec = self.store.load(job_id)
            if rec.terminal() or rec.state == TRANSFERRED:
                return
            time.sleep(1)


def discover(store: Optional[Store] = None) -> Client:
    """The whole integration, for an application that knows nothing.

        svc = abstraction_download.discover()
        svc.deliver(svc.get(url, "models/x.gguf"))

    It reads where this machine keeps its jobs -- from the environment a setup
    step wrote once, not from anything the caller supplies -- and hands back a
    client. An application names no path and holds no store.
    """
    if store is None:
        from abstraction_job import FileStore

        store = FileStore(store_root())
    return Client(store, Runner(store, reach=refused_hosts()))


def _name_from(locator: str) -> str:
    name = posixpath.basename(locator.split("?")[0])
    return name if name and name not in ("/", ".") else "download.bin"


def _scheme_from(locator: str) -> str:
    head = locator.split("://", 1)
    return head[0] if len(head) == 2 and head[0] else "https"


# ------------------------------------------------------------------ helpers ---


def _truncate(path: str, size: int) -> None:
    if not os.path.exists(path):
        if size == 0:
            return
        raise DownloadError(f"download: cannot resume from {size}: {path} is missing")
    with open(path, "r+b") as f:
        f.truncate(size)


def _hash_prefix(path: str, n: int, h, beat: Optional[Callable[[], None]] = None) -> None:
    """Rebuild the rolling hash over the prefix being kept.

    This is the cost of resuming honestly: a sequential read of what we already
    have, at disk speed, instead of re-downloading it at network speed. It is
    also the only way the final digest check covers bytes an earlier owner wrote.

    beat, if given, is called once per chunk and is how the caller keeps a lease
    it would otherwise lose: the read is minutes long on a large partial and
    reports nothing to anybody.
    """
    left = n
    with open(path, "rb") as f:
        while left > 0:
            chunk = f.read(min(1 << 20, left))
            if not chunk:
                raise DownloadError("download: partial is shorter than claimed")
            h.update(chunk)
            left -= len(chunk)
            if beat is not None:
                beat()


def _deliver(partial: str, final: str) -> None:
    os.makedirs(os.path.dirname(final) or ".", exist_ok=True)
    try:
        os.replace(partial, final)
    except OSError:
        # Across filesystems rename fails; copy then remove.
        with open(partial, "rb") as src, open(final, "wb") as dst:
            while True:
                b = src.read(1 << 20)
                if not b:
                    break
                dst.write(b)
        os.remove(partial)


# -------------------------------------------------------------- drop folder ---
#
# The Python half of abstraction-download/go/wanted.go: a text file put in wanted/ inside
# the store is a request, and the folder answers it by renaming the file.

FILES_DIR = "files"
WANTED_DIR = "wanted"
REQUEST_LIMIT = 64 << 10
WANTED_ACCEPTED, WANTED_DONE, WANTED_FAILED, WANTED_REFUSED = ".accepted", ".done", ".failed", ".refused"


class RequestRefused(DownloadError):
    """A dropped file this layer will not act on, with the line and the reason."""


def parse_wanted(text: str) -> List[Spec]:
    """One dropped file as work: ``#`` lines ignored, a file beginning ``{`` is
    a spec as the contract page spells it, anything else is one download per
    line -- a URL, then in either order an optional sha256:<hex> and an
    optional destination inside the store."""
    lines = [(i + 1, raw.strip()) for i, raw in enumerate(text.split("\n"))]
    lines = [(n, l) for n, l in lines if l and not l.startswith("#")]
    if not lines:
        raise RequestRefused("download: request refused: nothing to fetch")
    if lines[0][1].startswith("{"):
        try:
            spec = Spec.from_dict(json.loads("\n".join(l for _, l in lines)))
        except (ValueError, TypeError, AttributeError) as e:
            raise RequestRefused(f"download: request refused: not a spec: {e}")
        try:
            _wanted_spec(spec)
        except DownloadError as e:
            raise RequestRefused(f"download: request refused: spec: {e}")
        return [spec]
    specs = []
    for n, line in lines:
        try:
            specs.append(_wanted_line(line))
        except DownloadError as e:
            raise RequestRefused(f"download: request refused: line {n}: {e}")
    return specs


def _wanted_line(line: str) -> Spec:
    f = line.split()
    locator = f[0]
    if "://" not in locator:
        raise DownloadError(f"not a URL: {locator}")
    digest = dest = ""
    for x in f[1:]:
        if normal_digest(x) and not digest:
            digest = normal_digest(x)
        elif x.lower().startswith("sha256:"):
            raise DownloadError(f"digest is not sha256:<64 hex>: {x}")
        elif not dest:
            dest = x
        else:
            raise DownloadError(f"one destination per line: {x}")
    if not dest:
        dest = FILES_DIR + "/"
    if dest.endswith("/") or dest.endswith("\\"):
        dest += _name_from(locator)
    spec = Spec(
        artifact=Artifact(digest=digest),
        sources=[Source(scheme=_scheme_from(locator), locator=locator)],
        sink=Sink(final=portable(dest)),
    )
    _wanted_spec(spec)
    return spec


def _wanted_spec(s: Spec) -> None:
    """What the drop folder accepts, which is less than a record may carry:
    anyone who can write to a share can write here, and the fetching is done
    with the supervisor's authority on the supervisor's disk."""
    for p in (s.sink.final, s.sink.partial):
        _wanted_sink(p)
    for i, src in enumerate(s.sources):
        if src.scheme not in ("http", "https"):
            raise DownloadError(
                f"source {i + 1}: {src.scheme} is not fetched for a dropped request, only http and https"
            )
        if src.attrs or src.headers:
            raise DownloadError(f"source {i + 1}: a dropped request names no credential and sets no header")
    s.validate()


def _wanted_sink(p: str) -> None:
    if not p:
        return
    if not _relative_everywhere(p):
        raise DownloadError(f"destination is outside the store: {p}")
    if escapes_root(p):
        raise DownloadError(f"destination escapes the store: {p}")
    if reserved_sink("", p):
        raise DownloadError(f"destination is reserved by the store: {p}")
    if _into_wanted(p):
        raise DownloadError(f"destination is the drop folder itself: {p}")


def _into_wanted(p: str) -> bool:
    first = posixpath.normpath(p.replace("\\", "/")).split("/", 1)[0]
    return root_name(first) == WANTED_DIR


def _is_answered(name: str) -> bool:
    return name.endswith((WANTED_ACCEPTED, WANTED_DONE, WANTED_FAILED, WANTED_REFUSED))


def _ignored(name: str) -> bool:
    """What editors, Finder and Explorer write into any folder they are shown,
    none of it a request."""
    return name.startswith(".") or name.endswith("~") or name.lower() in ("desktop.ini", "thumbs.db")


def _request_lines(text: str) -> List[str]:
    """What the person wrote: everything before the first answer."""
    out = []
    for line in text.rstrip("\n").split("\n"):
        line = line.rstrip("\r")
        if line.startswith("# accepted ") or line.startswith("# refused "):
            break
        out.append(line)
    return out


def _write_answer(p: str, lines: List[str]) -> None:
    tmp = os.path.join(os.path.dirname(p), "." + os.path.basename(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, p)


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Wanted:
    """A store's drop folder: how a person with no program asks."""

    def __init__(self, store: Store, submit: Callable[[Spec], str]):
        self.store = store
        self.submit = submit

    def dir(self) -> str:
        root = local_root(self.store)
        if not root:
            raise DownloadError("download: this store has no local area")
        d = os.path.join(root, WANTED_DIR)
        os.makedirs(d, exist_ok=True)
        return d

    def take_in(self) -> List[str]:
        """Turn every new file into work and answer it in place: the file
        becomes <name>.accepted naming its jobs, or <name>.refused saying which
        line was wrong and why. Returns the ids of the jobs taken."""
        d = self.dir()
        ids: List[str] = []
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isdir(p) or _is_answered(name) or _ignored(name):
                continue
            ids += self._take(p)
        return ids

    def _take(self, p: str) -> List[str]:
        if os.path.getsize(p) > REQUEST_LIMIT:
            os.replace(p, p + WANTED_REFUSED)
            return []
        with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
        request = _request_lines(text)
        try:
            specs = parse_wanted(text)
        except RequestRefused as e:
            why = str(e).replace("download: request refused: ", "", 1)
            self._answer(p, p + WANTED_REFUSED, request + ["# refused " + _stamp() + ": " + why])
            return []
        os.replace(p, p + WANTED_ACCEPTED)
        lines = request + ["# accepted " + _stamp()]
        ids: List[str] = []
        for s in specs:
            try:
                job_id = self.submit(s)
            except Exception as e:
                lines.append("# refused: " + str(e))
                continue
            ids.append(job_id)
            lines.append(f"# job {job_id} -> {s.sink.final}")
        if not ids:
            self._answer(p + WANTED_ACCEPTED, p + WANTED_REFUSED, lines)
            return []
        _write_answer(p + WANTED_ACCEPTED, lines)
        return ids

    def _answer(self, src: str, dst: str, lines: List[str]) -> None:
        """Write the new name and then remove the old, so a crash between the
        two leaves the request visible twice rather than gone."""
        _write_answer(dst, lines)
        os.remove(src)

    def answer(self) -> None:
        """Move every accepted request on: to .done once every job it named has
        delivered, to .failed once one has ended without delivering, and
        otherwise rewrite its progress."""
        d = self.dir()
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if name.endswith(WANTED_ACCEPTED) and not os.path.isdir(p):
                self._follow(p)

    def _follow(self, p: str) -> None:
        with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
        lines = text.rstrip("\n").split("\n")
        last = max((i for i, l in enumerate(lines) if l.startswith("# job ")), default=-1)
        base = p[: -len(WANTED_ACCEPTED)]
        if last < 0:
            self._answer(p, base + WANTED_REFUSED, lines + ["# refused " + _stamp() + ": no job was recorded"])
            return
        kept = lines[: last + 1]
        status: List[str] = []
        done = failed = 0
        for line in kept:
            if not line.startswith("# job "):
                continue
            job_id, _, final = line[len("# job "):].partition(" -> ")
            s, end = self._progress(job_id, final)
            status.append(s)
            done += end == WANTED_DONE
            failed += end == WANTED_FAILED
        if done == len(status):
            self._answer(p, base + WANTED_DONE, kept + status)
        elif done + failed == len(status):
            self._answer(p, base + WANTED_FAILED, kept + status)
        elif "\n".join(kept + status) + "\n" != text:
            _write_answer(p, kept + status)

    def _progress(self, job_id: str, final: str) -> Tuple[str, str]:
        """One job's line for the person watching, and which ending it has
        reached, if any."""
        from abstraction_job import JobError

        try:
            rec = self.store.load(job_id)
        except JobError:
            return f"# failed: {final} — its job is gone from the store", WANTED_FAILED
        if rec.state in (COMPLETE, TRANSFERRED):
            line = f"# done {_stamp()}: {final}, {rec.progress.done} bytes"
            try:
                digest = spec_of(rec).artifact.digest
            except DownloadError:
                digest = ""
            if digest:
                line += f", {digest} verified"
            return line, WANTED_DONE
        if rec.state in (FAILED, CANCELLED):
            return f"# failed {_stamp()}: {final} — {rec.error or rec.state}", WANTED_FAILED
        line = "# " + rec.state
        if rec.progress.total > 0:
            line += f" {100 * rec.progress.done // rec.progress.total}%"
        if rec.error:
            line += " — last attempt: " + rec.error
        return line, ""

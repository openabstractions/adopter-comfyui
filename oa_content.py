"""Read one OA delivery by digest: the content check every OA node shares.

A delivery names content in the caller's authorized content store by its
SHA-256 digest. read_delivery opens it through the content-reader@1 binding the
machine resolves, copies at most the declared size, and returns the bytes only
when their length and digest equal the delivery's. OAImageGenerate and
OAVideoGenerate both read through it.
"""

import hashlib
import io
import re

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
VIDEO_MEDIA_TYPES = ("video/mp4", "video/webm", "video/avi")
_SNIFF_BYTES = 512


class _Bounded(io.BytesIO):
    """A copy destination that refuses bytes beyond the delivery's size."""

    def __init__(self, limit):
        super().__init__()
        self._limit = limit

    def write(self, data):
        if self.tell() + len(data) > self._limit:
            raise RuntimeError("OA content exceeds its delivery size")
        return super().write(data)


def read_delivery(machine, delivery, media_types, max_bytes, what):
    """Return the delivered bytes after checking location, type, size and digest.

    ``what`` names the content in each refusal ("image", "video").
    """
    size = getattr(delivery, "size", None)
    if delivery.location != "local" or delivery.media_type not in media_types \
            or not DIGEST.fullmatch(delivery.digest or "") \
            or type(size) is not int or not 0 < size <= max_bytes:
        raise RuntimeError("OA %s delivery is unsupported or exceeds %d MiB" % (what, max_bytes >> 20))
    from abstraction.facade import Scope
    reader = machine.resolve_storage(scope=Scope.LOCAL)
    opened = reader.open(delivery.digest)
    if opened.outcome != "opened" or opened.resource is None or opened.resource.size != size:
        raise RuntimeError("OA %s content open %s" % (what, opened.outcome))
    resource = opened.resource
    output = _Bounded(size)
    try:
        reader.copy(resource, output)
    finally:
        reader.close(resource)
    data = output.getvalue()
    if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != delivery.digest:
        raise RuntimeError("OA %s content digest mismatch" % what)
    return data


def video_media(data):
    """The video media type OA's runtime sniffs from these bytes, or "".

    The same rule as Go's net/http sniffer, which the runtime applies to every
    video delivery (abstraction-inference CONTRACT.md INF-G3): an AVI RIFF
    header, an MP4 ftyp box with an mp4 brand, or an EBML header (WebM), read
    from the first 512 bytes.
    """
    data = bytes(data[:_SNIFF_BYTES])
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "video/avi"
    if len(data) >= 12:
        box = int.from_bytes(data[:4], "big")
        if len(data) >= box and box % 4 == 0 and data[4:8] == b"ftyp":
            for start in range(8, box, 4):
                if start != 12 and data[start:start + 3] == b"mp4":
                    return "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    return ""

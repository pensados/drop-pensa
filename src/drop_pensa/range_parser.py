"""
HTTP Range request parsing per RFC 9110 §14.

We support single byte ranges only:
  - bytes=N-M    inclusive byte range
  - bytes=N-     from N to end of file
  - bytes=-N     last N bytes

Multi-range requests (bytes=0-99,200-299) are rare in practice and add
implementation complexity (multipart/byteranges responses). We treat
them as unsatisfiable for now.

Functions return (start, end) inclusive byte offsets, or raise
RangeNotSatisfiable on bad inputs.
"""
from __future__ import annotations

from dataclasses import dataclass


class RangeNotSatisfiable(Exception):
    """Range header was malformed or pointed outside the file."""


@dataclass(frozen=True)
class ByteRange:
    """A validated single byte range, both bounds inclusive."""
    start: int
    end: int  # inclusive
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def is_full_file(self) -> bool:
        """True iff this range spans the entire file."""
        return self.start == 0 and self.end == self.total - 1

    def content_range_header(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"


def parse_range_header(header: str | None, total: int) -> ByteRange | None:
    """
    Parse a Range request header against a file of `total` bytes.

    Returns None when the header is absent — caller should serve the
    full file with a 200. Raises RangeNotSatisfiable when the header
    is malformed, asks for multi-ranges we don't support, or points
    outside the file.

    Edge cases handled:
      - empty file (total == 0): any byte range request is unsatisfiable
      - suffix range larger than the file: clamps to the whole file
        (per RFC 9110 §14.1.2)
    """
    if header is None or not header.strip():
        return None

    # Only "bytes" units. Other unit names (which servers may register)
    # are rare and we don't support them.
    spec = header.strip()
    if not spec.lower().startswith("bytes="):
        raise RangeNotSatisfiable("only 'bytes' range unit is supported")

    payload = spec[len("bytes="):].strip()
    if "," in payload:
        # Multi-range request. RFC permits but the response would need
        # to be multipart/byteranges; we punt.
        raise RangeNotSatisfiable("multi-range requests not supported")

    if "-" not in payload:
        raise RangeNotSatisfiable("malformed range")

    start_s, end_s = payload.split("-", 1)
    start_s, end_s = start_s.strip(), end_s.strip()

    # Empty file: refuse any range. Per RFC 9110, a server may also
    # return the whole (zero-length) entity, but 416 is clearer.
    if total == 0:
        raise RangeNotSatisfiable("empty file")

    try:
        if start_s == "" and end_s == "":
            raise RangeNotSatisfiable("empty range")

        if start_s == "":
            # Suffix range: bytes=-N → last N bytes
            n = int(end_s)
            if n <= 0:
                raise RangeNotSatisfiable("suffix length must be positive")
            start = max(0, total - n)
            end = total - 1
        elif end_s == "":
            # Open-ended: bytes=N-
            start = int(start_s)
            end = total - 1
        else:
            start = int(start_s)
            end = int(end_s)
    except ValueError:
        raise RangeNotSatisfiable("non-integer in range")

    if start < 0 or end < 0:
        raise RangeNotSatisfiable("negative offsets not allowed")
    if start > end:
        raise RangeNotSatisfiable("inverted range")
    if start >= total:
        raise RangeNotSatisfiable("start beyond end of file")
    # Clamp end to last byte; over-spec is allowed by RFC.
    if end >= total:
        end = total - 1

    return ByteRange(start=start, end=end, total=total)

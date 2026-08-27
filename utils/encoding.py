"""Decode a text file that may not be UTF-8, without ever corrupting it.

The input sheet is exported from Excel, edited on Windows, pasted from a browser
and mailed around before it reaches the crawler. Any of those steps can leave it
in Windows-1252 rather than UTF-8, and the byte that gives it away is almost
always ``0xA0`` — a non-breaking space that Excel writes wherever a person typed
one. Reading such a file as UTF-8 raises::

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa0 in position 3618

Version 2 turned that into a fatal error and the operator had to convert the
file by hand before every run. This module removes that step for good::

    >>> from utils.encoding import read_text
    >>> decoded = read_text("input/companies.csv")
    >>> decoded.encoding
    'cp1252'

**The original file is never written to.** Decoding is a read; the normalised
form exists only in memory unless a caller explicitly asks for a copy via
:func:`write_normalised_copy`, which writes somewhere else.

The ladder, in order, stopping at the first rung that decodes cleanly:

1. **A byte-order mark**, when the file opens with one. This is proof rather
   than inference, and it covers Excel's "Unicode Text" export, which is
   UTF-16LE and decodes as convincing gibberish under every other rung.
2. ``utf-8-sig`` then ``utf-8`` — the overwhelmingly common case, two attempts.
3. ``cp1252`` — the Windows legacy encoding, and the actual answer for the
   ``0xA0`` sheets. Tried before ``latin-1`` because the two disagree over
   ``0x80``-``0x9F``, where cp1252 has the curly quotes and dashes Excel emits
   and latin-1 has unprintable control codes.
4. **Statistical detection** via ``charset_normalizer`` when it is installed. It
   ships as a dependency of ``requests``, so it is present in practice, but
   nothing here requires it.
5. ``latin-1`` — a total mapping: every byte sequence decodes to *something*.
   This rung cannot fail, which is what guarantees a run is never stopped by an
   encoding.

Detection sits at rung 4, *below* ``cp1252``, and that ordering is deliberate.
Asked about ``b"Acme\\xa0Corp"`` it answers ``cp1006`` — an Urdu code page — because
on a short sample many single-byte encodings are equally plausible and it has no
way to know this is a US company sheet. The deterministic rungs know. Detection
is therefore the fallback for genuinely exotic files, not the arbiter for the
one failure this module exists to fix.

Whatever rung answers, the text is then normalised to the same shape:
non-breaking and other exotic spaces become ordinary spaces, zero-width
characters are dropped, and a stray BOM anywhere is removed. That is what makes
``"Acme\\xa0Corp"`` and ``"Acme Corp"`` the same company downstream.
"""

from __future__ import annotations

import codecs
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Final, List, Optional, Tuple

from loguru import logger

__all__ = [
    "ENCODING_LADDER",
    "DecodedText",
    "decode_bytes",
    "normalise_text",
    "read_text",
    "strip_accents",
    "write_normalised_copy",
]

#: Encodings tried in order. Statistical detection is consulted between the
#: last strict codec and ``latin-1``, which is total and so always succeeds —
#: meaning the ladder as a whole has no failure mode.
ENCODING_LADDER: Final[Tuple[str, ...]] = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

#: How many rungs are tried before statistical detection is consulted. Every
#: codec up to here is strict: it rejects bytes it cannot represent, so being
#: accepted by one is evidence rather than a guess.
_STRICT_RUNGS: Final[int] = 3

#: Byte-order marks, longest first. UTF-32LE must be tested before UTF-16LE:
#: ``FF FE 00 00`` starts with ``FF FE``, so the shorter mark would match a
#: UTF-32 file and decode it as UTF-16 followed by a run of null characters.
_BYTE_ORDER_MARKS: Final[Tuple[Tuple[bytes, str], ...]] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

#: Encodings whose success means the file really was UTF-8. Reported separately
#: so a caller can tell "already fine" from "recovered".
_UTF8_NAMES: Final[frozenset] = frozenset({"utf-8", "utf-8-sig", "utf_8", "utf_8_sig", "ascii"})

#: Characters that look like a space, are not one, and break every comparison
#: they touch. ``\xa0`` is the one that stops a run; the rest travel with it.
_SPACE_LOOKALIKES: Final[Dict[int, Optional[str]]] = {
    0x00A0: " ",  # NO-BREAK SPACE - the Windows-1252 culprit
    0x1680: " ",  # OGHAM SPACE MARK
    0x2000: " ",  # EN QUAD
    0x2001: " ",  # EM QUAD
    0x2002: " ",  # EN SPACE
    0x2003: " ",  # EM SPACE
    0x2004: " ",  # THREE-PER-EM SPACE
    0x2005: " ",  # FOUR-PER-EM SPACE
    0x2006: " ",  # SIX-PER-EM SPACE
    0x2007: " ",  # FIGURE SPACE
    0x2008: " ",  # PUNCTUATION SPACE
    0x2009: " ",  # THIN SPACE
    0x200A: " ",  # HAIR SPACE
    0x202F: " ",  # NARROW NO-BREAK SPACE
    0x205F: " ",  # MEDIUM MATHEMATICAL SPACE
    0x3000: " ",  # IDEOGRAPHIC SPACE
}

#: Characters with no width that survive a copy-paste and poison equality.
#: Mapped to ``None`` so :meth:`str.translate` deletes them outright.
_INVISIBLES: Final[Dict[int, Optional[str]]] = {
    0x200B: None,  # ZERO WIDTH SPACE
    0x200C: None,  # ZERO WIDTH NON-JOINER
    0x200D: None,  # ZERO WIDTH JOINER
    0x2060: None,  # WORD JOINER
    0xFEFF: None,  # ZERO WIDTH NO-BREAK SPACE / BOM
}

#: One translation table, built once, applied to every decoded document.
_CLEANUP: Final[Dict[int, Optional[str]]] = {**_SPACE_LOOKALIKES, **_INVISIBLES}

#: Below this, ``charset_normalizer``'s guess is not worth preferring over the
#: deterministic rungs of the ladder.
_MIN_DETECTION_CONFIDENCE: Final[float] = 0.5


@dataclass(frozen=True)
class DecodedText:
    """The result of decoding a file, and how it was decoded.

    Attributes:
        text: The decoded, normalised content.
        encoding: Name of the codec that succeeded, e.g. ``"utf-8-sig"``.
        how: How that codec was chosen — ``"ladder"``, ``"detected"`` or
            ``"lossy"``. ``"lossy"`` means the last rung was reached and some
            bytes may not mean what they originally did.
        was_utf8: Whether the file was already valid UTF-8, so a caller can
            report "converted from cp1252" rather than "read".
        substitutions: How many characters the normalisation replaced or
            removed. Non-zero on exactly the sheets that used to fail.
        path: The file it came from, or ``None`` when bytes were decoded
            directly.
    """

    text: str
    encoding: str
    how: str
    was_utf8: bool
    substitutions: int
    path: Optional[Path] = None

    @property
    def recovered(self) -> bool:
        """Whether this file would have stopped version 2.

        Returns:
            ``True`` when the file was not valid UTF-8 and needed a fallback.
        """
        return not self.was_utf8

    def describe(self) -> str:
        """Render a one-line account of the decode, for a log or a report.

        Returns:
            Human-readable summary.
        """
        where = self.path.name if self.path is not None else "<bytes>"
        detail = f"{where}: decoded as {self.encoding} ({self.how})"
        if self.substitutions:
            detail += f", {self.substitutions} character(s) normalised"
        return detail


def _canonical(codec: str) -> str:
    """Reduce a codec name to a comparable form.

    Python accepts ``utf-8``, ``utf_8`` and ``UTF8`` for one codec, and the
    detection library returns whichever it likes. Comparing the raw names would
    retry a codec the ladder had already rejected.

    Args:
        codec: A codec name.

    Returns:
        The name lowercased with underscores turned into hyphens.
    """
    return codec.strip().lower().replace("_", "-")


def _detect(data: bytes) -> Optional[str]:
    """Ask ``charset_normalizer`` what this file is, if it is installed.

    Args:
        data: Raw file bytes.

    Returns:
        The detected codec name, or ``None`` when the library is absent, offers
        no answer, or is not confident enough to outrank the fixed ladder.
    """
    try:
        from charset_normalizer import from_bytes
    except ImportError:  # pragma: no cover - requests ships it in practice
        logger.debug("charset_normalizer is not installed; using the fixed ladder only")
        return None

    try:
        best = from_bytes(data).best()
    except Exception:  # noqa: BLE001 - detection must never stop a read
        logger.opt(exception=True).debug("charset_normalizer failed; using the fixed ladder")
        return None

    if best is None:
        return None

    # `chaos` is the library's measure of how wrong the guess looks: 0 is clean.
    confidence = 1.0 - float(getattr(best, "chaos", 0.0) or 0.0)
    if confidence < _MIN_DETECTION_CONFIDENCE:
        logger.debug("Detection confidence {:.2f} is too low to use", confidence)
        return None

    encoding = getattr(best, "encoding", None)
    return str(encoding) if encoding else None


def normalise_text(text: str) -> Tuple[str, int]:
    """Fold exotic whitespace and invisible characters into plain equivalents.

    Applied to every decoded document, whatever it was encoded as, so a value
    compares equal whether the operator typed a space or Excel supplied a
    non-breaking one.

    Args:
        text: Freshly decoded content.

    Returns:
        ``(normalised, substitutions)`` where ``substitutions`` counts the
        characters replaced or removed.
    """
    substitutions = sum(text.count(chr(point)) for point in _CLEANUP)
    if not substitutions:
        return text, 0

    return text.translate(_CLEANUP), substitutions


def _result(data: bytes, codec: str, how: str, path: Optional[Path]) -> Optional[DecodedText]:
    """Try one codec and package the outcome.

    Args:
        data: The file's bytes.
        codec: Codec to attempt.
        how: Label recorded on the result explaining why this codec was tried.
        path: Source file, recorded on the result.

    Returns:
        The decoded result, or ``None`` if this codec does not fit the bytes.
    """
    try:
        text = data.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return None

    normalised, substitutions = normalise_text(text)
    return DecodedText(
        text=normalised,
        encoding=codec,
        how=how,
        was_utf8=_canonical(codec) in {_canonical(name) for name in _UTF8_NAMES},
        substitutions=substitutions,
        path=path,
    )


def decode_bytes(data: bytes, path: Optional[Path] = None) -> DecodedText:
    """Decode raw bytes, falling back until something works.

    Args:
        data: The file's bytes.
        path: Where they came from, recorded on the result for logging.

    Returns:
        The decoded text and the story of how it was decoded. Never raises:
        the final rung of the ladder decodes any byte sequence at all.

    Raises:
        UnicodeDecodeError: Only if :data:`ENCODING_LADDER` is edited to remove
            its total final codec, which would be a programming error.
    """
    tried: List[str] = []

    # 1. A byte-order mark is proof, not inference. Checked first so Excel's
    #    UTF-16 "Unicode Text" export is never mistaken for a single-byte
    #    encoding, which would decode it into plausible-looking nonsense.
    for mark, codec in _BYTE_ORDER_MARKS:
        if not data.startswith(mark):
            continue
        # utf-16/utf-32 consume their own mark; utf-8-sig consumes the UTF-8 one.
        decoded = _result(data, codec, "byte-order mark", path)
        if decoded is not None:
            return decoded
        tried.append(_canonical(codec))
        break

    # 2-3. The strict codecs, in order of likelihood. Each rejects bytes it
    #      cannot represent, so acceptance here is evidence rather than a guess.
    for codec in ENCODING_LADDER[:_STRICT_RUNGS]:
        if _canonical(codec) in tried:
            continue
        decoded = _result(data, codec, "ladder", path)
        if decoded is not None:
            return decoded
        tried.append(_canonical(codec))

    # 4. Statistical detection, for files none of the above fits. Skipped when
    #    it names a codec already rejected above.
    detected = _detect(data)
    if detected and _canonical(detected) not in tried:
        decoded = _result(data, detected, "detected", path)
        if decoded is not None:
            return decoded
        logger.debug("Detected codec {!r} did not decode after all", detected)
        tried.append(_canonical(detected))

    # 5. The total codec. Reaching it means the bytes were never positively
    #    identified, so some of them may now be the wrong character.
    for codec in ENCODING_LADDER[_STRICT_RUNGS:]:
        if _canonical(codec) in tried:
            continue
        decoded = _result(data, codec, "lossy", path)
        if decoded is not None:
            return decoded
        tried.append(_canonical(codec))

    # Unreachable: latin-1 maps all 256 byte values. Kept so a future edit to
    # ENCODING_LADDER that drops it fails loudly rather than silently.
    raise UnicodeDecodeError(  # pragma: no cover - see above
        "unknown",
        data[:16],
        0,
        1,
        f"no codec in {ENCODING_LADDER} could decode this file",
    )


def read_text(path: Path | str) -> DecodedText:
    """Read a file as text, whatever encoding it turns out to be in.

    Args:
        path: File to read.

    Returns:
        The decoded content and how it was decoded.

    Raises:
        FileNotFoundError: If the file does not exist.
        OSError: If it exists but cannot be read.
    """
    source = Path(path)
    decoded = decode_bytes(source.read_bytes(), path=source)

    if decoded.how == "lossy":
        logger.warning(
            "{}: no encoding matched, fell back to {} — some characters may be wrong. "
            "The original file is unchanged.",
            source,
            decoded.encoding,
        )
    elif decoded.recovered:
        logger.warning(
            "{}: not valid UTF-8, decoded as {} instead. The original file is unchanged.",
            source,
            decoded.encoding,
        )
    else:
        logger.debug("{}", decoded.describe())

    if decoded.substitutions:
        logger.info(
            "{}: normalised {} non-breaking or invisible character(s)",
            source,
            decoded.substitutions,
        )

    return decoded


def write_normalised_copy(decoded: DecodedText, destination: Path | str) -> Path:
    """Write the decoded text out as clean UTF-8, somewhere else.

    Offered so an operator can see exactly what the crawler read, and hand that
    file to any other tool without repeating the conversion. It is never the
    input path: the original sheet is the operator's, and the crawler does not
    edit it.

    Args:
        decoded: A previous decode.
        destination: Where to write. Parent directories are created. Must not
            be the file the text was read from.

    Returns:
        The path written.

    Raises:
        ValueError: If ``destination`` is the file ``decoded`` came from.
        OSError: If the copy cannot be written.
    """
    target = Path(destination)

    if decoded.path is not None and target.resolve() == decoded.path.resolve():
        raise ValueError(
            f"Refusing to overwrite the input file {target}; "
            "write the normalised copy somewhere else"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps the row separators exactly as decoded, so a CSV written
    # here re-parses identically rather than gaining blank rows on Windows.
    target.write_text(decoded.text, encoding="utf-8", newline="")
    logger.info("Wrote a normalised UTF-8 copy to {}", target)
    return target


def strip_accents(text: str) -> str:
    """Reduce accented letters to their base form.

    Used by name matching rather than by file reading: ``"Schrödinger"`` and
    ``"Schrodinger"`` are one company, and a sheet spells it both ways.

    Args:
        text: Any string.

    Returns:
        The string with combining marks removed.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))

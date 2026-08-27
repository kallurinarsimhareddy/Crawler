"""Unit tests for :mod:`utils.encoding` and the input sheet's encoding recovery.

The bug these guard against is specific and had a specific cost: a sheet
exported from Excel carries byte ``0xA0`` where somebody typed a non-breaking
space, version 2 read the file as UTF-8 only, and the whole run died with a
``UnicodeDecodeError`` until the operator converted the file by hand. Every test
here is a shape that file can arrive in.

Nothing here touches the network, and every fixture is written to a temporary
directory, so the suite stays offline and leaves no files behind.
"""

from __future__ import annotations

import codecs
import tempfile
import unittest
from pathlib import Path

from crawler.csv_reader import read_companies, read_companies_with_encoding
from utils.encoding import (
    ENCODING_LADDER,
    DecodedText,
    decode_bytes,
    normalise_text,
    read_text,
    strip_accents,
    write_normalised_copy,
)

#: A header the reader accepts, reused by the CSV fixtures below.
HEADER = "Company Name,Website,Career Page URL\n"


class TemporaryDirectoryTest(unittest.TestCase):
    """Base class giving each test its own scratch directory."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        """Write a fixture file.

        Args:
            name: File name inside the scratch directory.
            data: Exact bytes to write.

        Returns:
            The path written.
        """
        path = self.directory / name
        path.write_bytes(data)
        return path


class TestDecodeUtf8(unittest.TestCase):
    """Valid UTF-8 must decode as UTF-8 and be reported as such."""

    def test_plain_ascii(self) -> None:
        decoded = decode_bytes(b"Acme,x")
        self.assertEqual(decoded.text, "Acme,x")
        self.assertTrue(decoded.was_utf8)
        self.assertFalse(decoded.recovered)
        self.assertEqual(decoded.how, "ladder")

    def test_accented_utf8_is_not_mangled(self) -> None:
        decoded = decode_bytes("Café Müller".encode("utf-8"))
        self.assertEqual(decoded.text, "Café Müller")
        self.assertTrue(decoded.was_utf8)

    def test_utf8_byte_order_mark_is_removed(self) -> None:
        decoded = decode_bytes(codecs.BOM_UTF8 + b"Company Name,x")
        self.assertEqual(decoded.text, "Company Name,x")
        self.assertEqual(decoded.encoding, "utf-8-sig")
        self.assertEqual(decoded.how, "byte-order mark")
        self.assertTrue(decoded.was_utf8)

    def test_bom_is_not_left_inside_the_first_field(self) -> None:
        # A surviving BOM turns the header "Company Name" into "﻿Company
        # Name", which no alias matches, and the sheet reads as missing columns.
        decoded = decode_bytes(codecs.BOM_UTF8 + b"Company Name")
        self.assertFalse(decoded.text.startswith("﻿"))


class TestDecodeWindows1252(unittest.TestCase):
    """The actual production failure: Excel's Windows-1252 output."""

    def test_non_breaking_space_no_longer_fails(self) -> None:
        data = "Acme Corp".encode("cp1252")
        # Prove the fixture really is the byte that used to stop a run.
        self.assertIn(0xA0, data)
        with self.assertRaises(UnicodeDecodeError):
            data.decode("utf-8")

        decoded = decode_bytes(data)
        self.assertEqual(decoded.encoding, "cp1252")
        self.assertTrue(decoded.recovered)
        self.assertEqual(decoded.text, "Acme Corp")

    def test_curly_quotes_and_dashes_survive(self) -> None:
        # 0x92, 0x93, 0x94, 0x96 are printable in cp1252 and control codes in
        # latin-1. Decoding as latin-1 would silently produce unprintables.
        decoded = decode_bytes("Bob’s “Shop” – Ltd".encode("cp1252"))
        self.assertEqual(decoded.encoding, "cp1252")
        self.assertEqual(decoded.text, "Bob’s “Shop” – Ltd")

    def test_cp1252_is_preferred_over_statistical_detection(self) -> None:
        """cp1252 must outrank whatever ``charset_normalizer`` guesses.

        On a short sample the detector answers ``cp1006``, an Urdu code page,
        because many single-byte encodings are equally plausible and it has no
        way to know this is a US company sheet. The ladder does know.
        """
        decoded = decode_bytes("Acme Corp,x".encode("cp1252"))
        self.assertEqual(decoded.encoding, "cp1252")
        self.assertEqual(decoded.how, "ladder")


class TestDecodeUtf16(unittest.TestCase):
    """Excel's "Unicode Text" export is UTF-16, and decodes as convincing junk."""

    def test_utf16_is_identified_by_its_mark(self) -> None:
        decoded = decode_bytes("Company Name,x".encode("utf-16"))
        self.assertEqual(decoded.encoding, "utf-16")
        self.assertEqual(decoded.how, "byte-order mark")
        self.assertEqual(decoded.text, "Company Name,x")

    def test_utf32_mark_is_not_read_as_utf16(self) -> None:
        # BOM_UTF32_LE begins with BOM_UTF16_LE, so a naive check matches the
        # shorter mark and yields text interleaved with null characters.
        decoded = decode_bytes("Company Name,x".encode("utf-32"))
        self.assertEqual(decoded.encoding, "utf-32")
        self.assertEqual(decoded.text, "Company Name,x")
        self.assertNotIn("\x00", decoded.text)


class TestDecodeNeverFails(unittest.TestCase):
    """No byte sequence may stop a run."""

    def test_undecodable_bytes_still_produce_text(self) -> None:
        decoded = decode_bytes(bytes([0x81, 0x8D, 0xFF, 0xFE, 0x41]))
        self.assertIsInstance(decoded.text, str)
        self.assertTrue(decoded.recovered)

    def test_every_byte_value_decodes(self) -> None:
        decoded = decode_bytes(bytes(range(256)))
        self.assertIsInstance(decoded.text, str)

    def test_empty_input(self) -> None:
        decoded = decode_bytes(b"")
        self.assertEqual(decoded.text, "")

    def test_ladder_ends_in_a_total_codec(self) -> None:
        """The no-failure guarantee rests on the last rung accepting anything."""
        self.assertEqual(bytes(range(256)).decode(ENCODING_LADDER[-1])[:1], "\x00")


class TestNormalisation(unittest.TestCase):
    """Invisible and look-alike characters must not survive into a comparison."""

    def test_non_breaking_space_becomes_a_space(self) -> None:
        text, count = normalise_text("Acme Corp")
        self.assertEqual(text, "Acme Corp")
        self.assertEqual(count, 1)

    def test_zero_width_characters_are_deleted(self) -> None:
        text, count = normalise_text("Ac​me‍Corp")
        self.assertEqual(text, "AcmeCorp")
        self.assertEqual(count, 2)

    def test_clean_text_is_returned_unchanged(self) -> None:
        text, count = normalise_text("Acme Corp")
        self.assertEqual(text, "Acme Corp")
        self.assertEqual(count, 0)

    def test_newlines_and_tabs_are_left_alone(self) -> None:
        # Normalising these would corrupt the CSV's own structure.
        text, count = normalise_text("a,b\r\nc,d\te")
        self.assertEqual(text, "a,b\r\nc,d\te")
        self.assertEqual(count, 0)

    def test_utf8_encoded_non_breaking_space_is_also_normalised(self) -> None:
        # The file is valid UTF-8, so nothing "fails" — but the character is
        # just as poisonous to a name comparison as the cp1252 spelling.
        decoded = decode_bytes("Acme Corp".encode("utf-8"))
        self.assertTrue(decoded.was_utf8)
        self.assertEqual(decoded.text, "Acme Corp")
        self.assertEqual(decoded.substitutions, 1)


class TestStripAccents(unittest.TestCase):
    """Accent folding, used by company-name matching rather than file reading."""

    def test_folds_to_base_letters(self) -> None:
        self.assertEqual(strip_accents("Schrödinger"), "Schrodinger")
        self.assertEqual(strip_accents("Nestlé"), "Nestle")

    def test_leaves_unaccented_text_alone(self) -> None:
        self.assertEqual(strip_accents("Acme Corp"), "Acme Corp")


class TestReadText(TemporaryDirectoryTest):
    """Reading from disk, and the promise that the original is not touched."""

    def test_reports_the_encoding_it_used(self) -> None:
        path = self.write("sheet.csv", "Acme Corp".encode("cp1252"))
        decoded = read_text(path)
        self.assertEqual(decoded.encoding, "cp1252")
        self.assertEqual(decoded.path, path)
        self.assertIn("cp1252", decoded.describe())

    def test_the_original_file_is_never_modified(self) -> None:
        original = "Acme Corp,x".encode("cp1252")
        path = self.write("sheet.csv", original)

        read_text(path)

        self.assertEqual(path.read_bytes(), original)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_text(self.directory / "nope.csv")


class TestWriteNormalisedCopy(TemporaryDirectoryTest):
    """The optional clean copy, and the guard that keeps it off the input."""

    def test_writes_clean_utf8(self) -> None:
        path = self.write("sheet.csv", "Acme Corp,x".encode("cp1252"))
        decoded = read_text(path)

        copy = write_normalised_copy(decoded, self.directory / "normalised" / "sheet.csv")

        self.assertTrue(copy.is_file())
        self.assertEqual(copy.read_bytes().decode("utf-8"), "Acme Corp,x")

    def test_refuses_to_overwrite_the_source(self) -> None:
        original = "Acme Corp,x".encode("cp1252")
        path = self.write("sheet.csv", original)
        decoded = read_text(path)

        with self.assertRaises(ValueError):
            write_normalised_copy(decoded, path)

        self.assertEqual(path.read_bytes(), original)

    def test_refuses_even_via_a_different_spelling_of_the_path(self) -> None:
        original = b"Acme,x"
        path = self.write("sheet.csv", original)
        decoded = read_text(path)
        indirect = self.directory / "sub" / ".." / "sheet.csv"

        with self.assertRaises(ValueError):
            write_normalised_copy(decoded, indirect)

        self.assertEqual(path.read_bytes(), original)

    def test_a_decode_with_no_source_can_be_written_anywhere(self) -> None:
        decoded = DecodedText(
            text="Acme,x", encoding="utf-8", how="ladder", was_utf8=True, substitutions=0
        )
        copy = write_normalised_copy(decoded, self.directory / "out.csv")
        self.assertEqual(copy.read_text(encoding="utf-8"), "Acme,x")


class TestReadCompaniesEncoding(TemporaryDirectoryTest):
    """The end of the story: the sheet that used to stop a run now loads."""

    def test_windows_1252_sheet_loads(self) -> None:
        body = HEADER + "Acme Corp,acme.com,https://acme.com/careers\n"
        path = self.write("companies.csv", body.encode("cp1252"))

        records = read_companies(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["company"], "Acme Corp")
        self.assertEqual(records[0]["career_url"], "https://acme.com/careers")

    def test_utf8_sheet_still_loads_identically(self) -> None:
        body = HEADER + "Café Müller,cafe.example,https://cafe.example/jobs\n"
        path = self.write("companies.csv", body.encode("utf-8"))

        records = read_companies(path)

        self.assertEqual(records[0]["company"], "Café Müller")

    def test_excel_byte_order_mark_does_not_hide_the_first_column(self) -> None:
        body = HEADER + "Acme,acme.com,https://acme.com/careers\n"
        path = self.write("companies.csv", codecs.BOM_UTF8 + body.encode("utf-8"))

        records = read_companies(path)

        self.assertEqual(records[0]["company"], "Acme")

    def test_utf16_sheet_loads(self) -> None:
        body = HEADER + "Acme,acme.com,https://acme.com/careers\n"
        path = self.write("companies.csv", body.encode("utf-16"))

        records = read_companies(path)

        self.assertEqual(records[0]["company"], "Acme")

    def test_the_detected_encoding_is_reported_to_the_caller(self) -> None:
        body = HEADER + "Acme Corp,acme.com,https://acme.com/careers\n"
        path = self.write("companies.csv", body.encode("cp1252"))

        records, decoded = read_companies_with_encoding(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(decoded.encoding, "cp1252")
        self.assertTrue(decoded.recovered)
        self.assertGreaterEqual(decoded.substitutions, 1)

    def test_reading_leaves_the_sheet_byte_identical(self) -> None:
        body = HEADER + "Acme Corp,acme.com,https://acme.com/careers\n"
        original = body.encode("cp1252")
        path = self.write("companies.csv", original)

        read_companies(path)

        self.assertEqual(path.read_bytes(), original)

    def test_missing_column_still_raises_a_clear_error(self) -> None:
        path = self.write("companies.csv", b"Company Name,Website\nAcme,acme.com\n")
        with self.assertRaises(ValueError) as caught:
            read_companies(path)
        self.assertIn("Career Page URL", str(caught.exception))

    def test_empty_file_still_raises(self) -> None:
        path = self.write("companies.csv", b"")
        with self.assertRaises(ValueError):
            read_companies(path)

    def test_missing_file_still_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_companies(self.directory / "nope.csv")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

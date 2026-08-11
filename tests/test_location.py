"""Unit tests for :mod:`utils.location`."""

from __future__ import annotations

import unittest

from utils.location import derive_country, split_locations


class TestDeriveCountry(unittest.TestCase):
    """Location strings map to a country, or to nothing when unsupported."""

    def test_us_state_codes(self) -> None:
        for location in ("Austin, TX", "Boston, MA", "New York, NY", "Austin, TX 78701"):
            with self.subTest(location=location):
                self.assertEqual(derive_country(location), "United States")

    def test_spelled_out_us_states(self) -> None:
        self.assertEqual(derive_country("Austin, Texas"), "United States")
        self.assertEqual(derive_country("Raleigh, North Carolina"), "United States")

    def test_named_countries(self) -> None:
        cases = {
            "London, United Kingdom": "United Kingdom",
            "London, UK": "United Kingdom",
            "Bengaluru, Karnataka, India": "India",
            "Toronto, ON, Canada": "Canada",
            "Munich, Germany": "Germany",
            "Sydney, NSW, Australia": "Australia",
            "United States of America": "United States",
        }
        for location, expected in cases.items():
            with self.subTest(location=location):
                self.assertEqual(derive_country(location), expected)

    def test_strips_work_arrangement_prefix(self) -> None:
        self.assertEqual(derive_country("Remote - Austin, TX"), "United States")
        self.assertEqual(derive_country("Hybrid: London, UK"), "United Kingdom")
        self.assertEqual(derive_country("Remote - India"), "India")

    def test_ignores_trailing_site_qualifier(self) -> None:
        """Workday tenants append building names: 'High Point, NC (EAS HQ)'."""
        self.assertEqual(derive_country("High Point, NC (EAS Premier)"), "United States")
        self.assertEqual(derive_country("London, UK (Head Office)"), "United Kingdom")

    def test_placeholders_yield_nothing(self) -> None:
        for location in ("", "Remote", "3 Locations", "Multiple Locations", "Various", "N/A"):
            with self.subTest(location=location):
                self.assertEqual(derive_country(location), "")

    def test_bare_city_is_not_guessed(self) -> None:
        """A city alone must not be assumed to be in the United States."""
        self.assertEqual(derive_country("Austin"), "")
        self.assertEqual(derive_country("London"), "")

    def test_ca_resolves_to_california_not_canada(self) -> None:
        """'City, CA' is the US postal form; Canada names itself or uses BC/ON."""
        self.assertEqual(derive_country("San Jose, CA"), "United States")
        self.assertEqual(derive_country("Vancouver, BC"), "Canada")
        self.assertEqual(derive_country("Montreal, QC, Canada"), "Canada")

    def test_several_locations_in_one_country(self) -> None:
        self.assertEqual(derive_country("Austin, TX | Boston, MA"), "United States")

    def test_several_countries_yield_nothing(self) -> None:
        self.assertEqual(derive_country("Austin, TX; London, UK"), "")


class TestSplitLocations(unittest.TestCase):
    """Only real separators split; commas stay inside one location."""

    def test_splits_on_separators(self) -> None:
        self.assertEqual(split_locations("Austin, TX | Boston, MA"), ["Austin, TX", "Boston, MA"])
        self.assertEqual(split_locations("London; Paris"), ["London", "Paris"])

    def test_comma_is_not_a_separator(self) -> None:
        self.assertEqual(split_locations("Austin, TX"), ["Austin, TX"])

    def test_empty_input(self) -> None:
        self.assertEqual(split_locations(""), [])


if __name__ == "__main__":
    unittest.main()

"""A device must not be storable twice under a brand-prefixed and a bare name.

source_products is UNIQUE on (manufacturer, normalized_name) and carries the
manufacturer in its own column, so repeating the brand inside the name made the same
device insertable twice. _norm stripped a hardcoded "xiaomi " prefix and nothing else:
'Xiaomi 12' normalised to '12', while 'TECNO POVA Neo' stayed 'tecno pova neo' and the
same device arriving as 'POVA Neo' produced 'pova neo'. Two keys, so the ON CONFLICT
upsert never fired. Seven TECNO devices were stored twice, all review_state approved.

These were written as bare module-level functions, which `python -m unittest
discover` -- the runner README.md tells you to use, and the only one an air-gapped
box has -- does not collect. They ran under pytest and nowhere else, so the suite
reported 210 tests where 223 existed and these six guarded nothing on the target
machine. They are a TestCase now so both runners see them.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mobile_observatory.identity_bridge import _norm  # noqa: E402


class BrandPrefixIdentityTest(unittest.TestCase):
    def test_brand_prefixed_and_bare_name_agree(self) -> None:
        """The bug, in one assertion."""
        self.assertEqual(_norm("TECNO POVA Neo", "TECNO"), _norm("POVA Neo", "TECNO"))

    def test_every_vendor_not_just_xiaomi(self) -> None:
        """The original stripped one hardcoded brand. Assert the rule, not the instance."""
        for maker, prefixed, bare in [
            ("TECNO", "TECNO Spark 20", "Spark 20"),
            ("Xiaomi", "Xiaomi 14T", "14T"),
            ("Samsung", "Samsung Galaxy S24", "Galaxy S24"),
            ("realme", "realme GT 7", "GT 7"),
        ]:
            with self.subTest(manufacturer=maker):
                self.assertEqual(_norm(prefixed, maker), _norm(bare, maker))

    def test_sub_brands_stay_distinct(self) -> None:
        """The over-merge direction. Redmi is a Xiaomi sub-brand but NOT the manufacturer
        token, so 'Redmi 12' must not collapse into 'Xiaomi 12' -- they are different
        phones, and merging them is the worse bug."""
        self.assertNotEqual(_norm("Redmi 12", "Xiaomi"), _norm("Xiaomi 12", "Xiaomi"))
        self.assertNotEqual(_norm("POCO F6", "Xiaomi"), _norm("Xiaomi F6", "Xiaomi"))

    def test_distinguishing_characters_survive(self) -> None:
        """Firmware Atlas stripped '+' and merged Galaxy S25+ into S25, giving a Plus
        handset another phone's silicon and patch level. Nothing here may do that."""
        self.assertNotEqual(_norm("Redmi Note 14 Pro+", "Xiaomi"),
                            _norm("Redmi Note 14 Pro", "Xiaomi"))
        self.assertNotEqual(_norm("Galaxy A06 5G", "Samsung"), _norm("Galaxy A06", "Samsung"))

    def test_name_equal_to_the_brand_is_not_emptied(self) -> None:
        """Stripping must never produce an empty key, which would collide with every
        other empty key."""
        self.assertEqual("tecno", _norm("TECNO", "TECNO"))
        self.assertEqual("xiaomi", _norm("Xiaomi", "Xiaomi"))

    def test_only_a_leading_occurrence_is_removed(self) -> None:
        """'Galaxy Tecno Edition' is not a brand prefix in the middle of a name."""
        self.assertEqual("spark tecno edition", _norm("Spark TECNO Edition", "TECNO"))


if __name__ == "__main__":
    unittest.main()

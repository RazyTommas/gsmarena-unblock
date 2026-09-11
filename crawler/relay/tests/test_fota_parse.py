#!/usr/bin/env python3
"""Fixture test for the fota-cloud manifest parser.

Every network we have tried is blocked by Akamai, so this parser has never seen a
real response. That is a standing risk, not a retired one: a parser validated only
against a sample its own author invented can agree with itself and still be wrong.

This test exists because collector@field pointed that out, and it immediately found
a real bug -- `<value rcount="1" fwsize="...">` carries attributes and the original
regex required a bare `<value>`, so the whole upgrade history parsed as nothing.

WHEN A REAL RESPONSE IS FINALLY CAPTURED: paste it in as REAL_SAMPLE and flip
REAL_SAMPLE_SEEN to True. Until then this file asserts shape, not truth.
"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collect_samsung import parse, split_ver

REAL_SAMPLE_SEEN = False        # flip when a live response has been captured

FIXTURE = '''<?xml version="1.0" encoding="UTF-8"?>
<versioninfo>
 <url>http://fota-cloud-dn.ospserver.net/firmware/ILO/SM-A055F/</url>
 <firmware>
  <version>
   <latest o="14">A055FXXU5CYA1/A055FOXM5CYA1/A055FXXU5CYA1/A055FXXU5CYA1</latest>
   <upgrade>
    <value rcount="1" fwsize="123456">A055FXXU4CXL2/A055FOXM4CXL2/A055FXXU4CXL2</value>
    <value rcount="2" fwsize="123456">A055FXXU3CXG1/A055FOXM3CXG1/A055FXXU3CXG1</value>
   </upgrade>
  </version>
 </firmware>
</versioninfo>'''

NO_UPGRADE = '''<?xml version="1.0"?><versioninfo><firmware><version>
 <latest o="15">A166BXXU2BYH3/A166BOXM2BYH3/A166BXXU2BYH3</latest>
 <upgrade></upgrade></version></firmware></versioninfo>'''


class TestFotaParse(unittest.TestCase):
    def test_latest_with_attributes(self):
        rows = parse(FIXTURE)
        self.assertTrue(any(k == "latest" for k, _ in rows),
                        "the <latest o=\"14\"> element must parse despite its attribute")

    def test_upgrade_history_is_not_dropped(self):
        """The bug this file was written to catch."""
        rows = parse(FIXTURE)
        ups = [v for k, v in rows if k == "upgrade"]
        self.assertEqual(len(ups), 2,
                         f"expected 2 upgrade entries, got {len(ups)} -- attributes on "
                         f"<value> silently dropped the whole history")

    def test_version_triplet_splits(self):
        rows = parse(FIXTURE)
        pda, csc, phone = split_ver(rows[0][1])
        self.assertEqual(pda, "A055FXXU5CYA1")
        self.assertEqual(csc, "A055FOXM5CYA1")
        self.assertEqual(phone, "A055FXXU5CYA1")

    def test_no_duplicates(self):
        rows = parse(FIXTURE)
        vals = [v for _, v in rows]
        self.assertEqual(len(vals), len(set(vals)), "the same build must not repeat")

    def test_empty_upgrade_block(self):
        rows = parse(NO_UPGRADE)
        self.assertEqual(len([1 for k, _ in rows if k == "latest"]), 1)
        self.assertEqual(len([1 for k, _ in rows if k == "upgrade"]), 0)

    def test_garbage_returns_nothing_rather_than_guessing(self):
        self.assertEqual(parse("<html>Access Denied</html>"), [],
                         "a refusal page must parse as zero builds, never as one")

    @unittest.skipUnless(REAL_SAMPLE_SEEN, "no live response captured yet -- Akamai "
                                           "blocks every network tried so far")
    def test_against_real_response(self):
        self.fail("paste the captured response in and assert on it")


if __name__ == "__main__":
    unittest.main(verbosity=2)

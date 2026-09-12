#!/usr/bin/env python3
"""Fixture tests for the gsmarena index parsers.

Both regexes here were written against markup nobody had looked at. The brand one
was wrong -- it expected a trailing <br> that does not exist -- so makers.php3
parsed as zero brands and a remote box burned a round-trip to report 'empty' from a
page carrying over a hundred of them.

The control saved the diagnosis: because a known-good brand page had already
returned 47 device links, 'empty' could only mean the parser, never the network.
These fixtures are the cheaper version of that lesson.

Markup below is copied verbatim from live responses on 2026-09-11.
"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collect_gsm_slugs import BRAND_RE, devices_on

# BOTH markups, exactly as the live page carries them. The nav entries exist to prove
# the parser ignores them: matching the menu returns a plausible count and silently
# drops 90 brands, which is worse than returning zero.
MAKERS_NAV = ('<li><a href="samsung-phones-9.php">Samsung</a></li>'
              '<li><a href="apple-phones-48.php">Apple</a></li>')
MAKERS_TABLE = ('<td><a href=acer-phones-59.php>Acer<br><span>117 devices</span></a></td>'
                '<td><a href=alcatel-phones-5.php>alcatel<br><span>424 devices</span></a></td>'
                '<td><a href=samsung-phones-9.php>Samsung<br><span>1465 devices</span></a></td>')
MAKERS = MAKERS_NAV + MAKERS_TABLE

BRAND_PAGE = ('<div class="makers"><ul>'
              '<li><a href="samsung_galaxy_a07-13698.php">'
              '<img src="x.jpg"><strong><span>Galaxy A07</span></strong></a></li>'
              '<li><a href="samsung_galaxy_s25-13610.php">'
              '<img src="y.jpg"><strong><span>Galaxy S25</span></strong></a></li>'
              '</ul></div>')


class TestGsmParse(unittest.TestCase):
    def test_parses_the_table_not_the_nav(self):
        """The second bug, and the worse one: the nav menu yields a plausible count."""
        b = BRAND_RE.findall(MAKERS)
        self.assertEqual(len(b), 3, f"expected the 3 TABLE entries, got {len(b)}")
        self.assertEqual(b[0], ("acer-phones-59.php", "Acer", "117"))

    def test_declared_device_count_is_captured(self):
        """gsmarena prints each brand's device count. It is the completeness oracle
        that would have caught the 55% truncation, so losing it is a real defect --
        and it was lost once already to '<br' matching only three characters and
        leaving the '>' before <span>."""
        b = BRAND_RE.findall(MAKERS)
        self.assertEqual([x[2] for x in b], ["117", "424", "1465"])
        self.assertTrue(all(x[2].isdigit() for x in b))

    def test_nav_alone_yields_nothing(self):
        """A regex that drifts onto the dropdown must return zero, not 36."""
        self.assertEqual(BRAND_RE.findall(MAKERS_NAV), [],
                         "the quoted nav dropdown is not the catalogue")

    def test_devices_parse(self):
        d = devices_on(BRAND_PAGE)
        self.assertEqual(len(d), 2)
        self.assertEqual(d[0], ("Galaxy A07", "samsung_galaxy_a07-13698.php"))

    def test_challenge_page_yields_nothing(self):
        """A Turnstile page is 200 with real HTML. It must parse as zero devices,
        never as one malformed one."""
        self.assertEqual(devices_on('<html><title>Just a moment...</title></html>'), [])

    def test_brand_regex_does_not_match_device_links(self):
        self.assertEqual(BRAND_RE.findall(BRAND_PAGE), [],
                         "device links must not be mistaken for brand links")


if __name__ == "__main__":
    unittest.main(verbosity=2)

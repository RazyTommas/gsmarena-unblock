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

MAKERS = ('<a href="samsung-phones-9.php">Samsung</a></li>'
          '<li><a href="apple-phones-48.php">Apple</a></li>'
          '<li><a href="xiaomi-phones-80.php">Xiaomi</a></li>'
          '<li><a href="google-phones-107.php">Google</a></li>')

BRAND_PAGE = ('<div class="makers"><ul>'
              '<li><a href="samsung_galaxy_a07-13698.php">'
              '<img src="x.jpg"><strong><span>Galaxy A07</span></strong></a></li>'
              '<li><a href="samsung_galaxy_s25-13610.php">'
              '<img src="y.jpg"><strong><span>Galaxy S25</span></strong></a></li>'
              '</ul></div>')


class TestGsmParse(unittest.TestCase):
    def test_brands_parse(self):
        """The bug this file exists for."""
        b = BRAND_RE.findall(MAKERS)
        self.assertEqual(len(b), 4, f"expected 4 brands, got {len(b)} -- a regex written "
                                    f"against imagined markup returns zero, and zero "
                                    f"reads as 'the catalogue is empty'")
        self.assertEqual(b[0], ("samsung-phones-9.php", "Samsung"))

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

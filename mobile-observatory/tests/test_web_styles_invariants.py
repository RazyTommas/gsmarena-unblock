"""Source-level invariants for apps/web/styles.css.

tools/ui_layout_audit.py measures the rendered page and is the real guard, but
it needs Chromium and a populated corpus, so it cannot run in this suite. These
checks are the cheap half: they hold the three rules the stylesheet's header
comment states, at the source level, in both test runners, with no browser.

Each one exists because its absence shipped a measured defect:

* a bare `1fr` track (min-width:auto) inflated to a table's min-content width
  and scrolled the body 776px sideways at 800x600, and squeezed the Admin
  offline-bundle panel to 36px at 1366. Same omission, two rules, one of five
  places it appeared.
* `overflow:hidden` on `.data-card` made 416px of Explore > Source records
  permanently unreachable, with `overflow:auto` granted only below 700px.
* three colours (later measured as five) had their only definition in an
  unthemed rule and so stayed light in dark mode, down to 1.12:1.
* the `system` theme matched no rule at all: there was no prefers-color-scheme
  block in the file, so OS dark silently rendered light.

The dark palette is necessarily written twice — plain CSS cannot put one
declaration behind both a media query and an attribute selector, and
light-dark() is too new for an offline tool — so the duplication is asserted
identical rather than trusted.
"""

import re
import unittest
from pathlib import Path

CSS_PATH = Path(__file__).resolve().parents[1] / "apps" / "web" / "styles.css"

# `overflow:hidden` is legitimate where it clips a decorative pseudo-element and
# no text. Every entry here is a promise that ui_layout_audit.py's "nothing with
# overflow-x:hidden may have content past its edge" assertion still passes.
OVERFLOW_HIDDEN_ALLOWED = {
    ".security-hero",       # clips the ◇ glyph downwards; it is pinned right:20px
}


def _strip_comments(text):
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def _balanced_spans(value, func):
    """Yield (start, end) of each `func(...)` call in value, paren-balanced."""
    for m in re.finditer(re.escape(func) + r"\s*\(", value):
        depth, i = 0, m.end() - 1
        while i < len(value):
            if value[i] == "(":
                depth += 1
            elif value[i] == ")":
                depth -= 1
                if depth == 0:
                    yield (m.start(), i + 1)
                    break
            i += 1


def _rules(css):
    """Very small flat rule scanner: yields (selector, [(prop, value), ...]).

    At-rule blocks are descended into, so a rule inside @media is reported with
    its own selector. Good enough for assertions about declarations; it is not a
    general CSS parser and does not need to be.
    """
    css = _strip_comments(css)
    out = []
    i = 0
    stack = []
    sel_start = 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            head = css[sel_start:i].strip()
            if head.startswith("@"):
                stack.append(head)
                sel_start = i + 1
            else:
                depth, j = 1, i + 1
                while j < len(css) and depth:
                    if css[j] == "{":
                        depth += 1
                    elif css[j] == "}":
                        depth -= 1
                    j += 1
                body = css[i + 1:j - 1]
                decls = []
                for part in body.split(";"):
                    if ":" in part:
                        prop, _, val = part.partition(":")
                        decls.append((prop.strip().lower(), val.strip()))
                out.append((head, decls, tuple(stack)))
                i = j
                sel_start = i
                continue
        elif ch == "}":
            if stack:
                stack.pop()
            sel_start = i + 1
        i += 1
    return out


class StylesheetExists(unittest.TestCase):
    def test_present(self):
        self.assertTrue(CSS_PATH.is_file(), f"{CSS_PATH} is missing")


class NoBareFrTrack(unittest.TestCase):
    """Rule 1: every flexible track is the max half of an explicit minmax()."""

    def test_no_bare_fr_track(self):
        offenders = []
        for sel, decls, at in _rules(CSS_PATH.read_text()):
            for prop, value in decls:
                if not prop.startswith("grid-template") and prop != "grid":
                    continue
                stripped = value
                # remove every minmax(...) after checking its own min half
                spans = list(_balanced_spans(value, "minmax"))
                for start, end in reversed(spans):
                    inner = value[start + len("minmax("):end - 1]
                    depth, split = 0, None
                    for k, c in enumerate(inner):
                        if c == "(":
                            depth += 1
                        elif c == ")":
                            depth -= 1
                        elif c == "," and depth == 0:
                            split = k
                            break
                    low = inner[:split].strip().lower() if split is not None else ""
                    if low in ("auto", "min-content", "max-content", ""):
                        offenders.append(
                            f"{at}{sel} {{{prop}: {value}}} — minmax() min half is {low!r}, "
                            f"which is the same min-width:auto trap as a bare fr")
                    stripped = stripped[:start] + " " + stripped[end:]
                if re.search(r"[\d.]+fr", stripped):
                    offenders.append(
                        f"{at}{sel} {{{prop}: {value}}} — bare fr track; "
                        f"a bare fr has min-width:auto and inflates to min-content. "
                        f"Use minmax(0,<n>fr).")
        self.assertEqual([], offenders, "bare fr tracks found:\n  " + "\n  ".join(offenders))


class WideContainersScroll(unittest.TestCase):
    """Rule 2: a container that can hold a too-wide table must scroll, always."""

    def test_data_card_scrolls_and_never_hides(self):
        css = CSS_PATH.read_text()
        found_scroll = False
        for sel, decls, at in _rules(css):
            if "data-card" not in sel:
                continue
            for prop, value in decls:
                v = value.lower()
                if prop in ("overflow", "overflow-x") and "hidden" in v:
                    self.fail(f"{at}{sel} sets {prop}:{value} — that is the defect that made "
                              f"416px of Explore > Source records unreachable")
                if prop == "overflow-x" and ("auto" in v or "scroll" in v):
                    found_scroll = True
        self.assertTrue(found_scroll, ".data-card must declare overflow-x:auto unconditionally, "
                                      "not only inside a max-width media query")

    def test_overflow_hidden_only_where_allowed(self):
        offenders = []
        for sel, decls, at in _rules(CSS_PATH.read_text()):
            for prop, value in decls:
                if prop in ("overflow", "overflow-x") and "hidden" in value.lower():
                    if sel.strip() not in OVERFLOW_HIDDEN_ALLOWED:
                        offenders.append(f"{at}{sel} {{{prop}: {value}}}")
        self.assertEqual([], offenders,
                         "overflow:hidden outside the allowlist — every one of these clips "
                         "content with no scroll route:\n  " + "\n  ".join(offenders))

    def test_no_max_width_ch_clipping_on_table_cells(self):
        """The 28ch/34ch/22ch caps were fitted to one box's font metrics."""
        for sel, decls, at in _rules(CSS_PATH.read_text()):
            if "data-table" not in sel:
                continue
            for prop, value in decls:
                if prop == "max-width" and "ch" in value:
                    self.fail(f"{at}{sel} caps at {value}; a ch cap tuned on a box without "
                              f"Inter installed is not a fix. Let the cell wrap instead.")
                if prop == "text-overflow" and "ellipsis" in value:
                    self.fail(f"{at}{sel} ellipsises data; a title attribute is a hover "
                              f"tooltip, not a route to the value")


class EveryColourIsAToken(unittest.TestCase):
    """Rule 3: a colour defined outside the theme blocks cannot be themed."""

    # Deliberate self-contained foreground+background pairs: both halves are
    # stated in the same rule, so they carry their own contrast in every theme.
    SELF_CONTAINED = ("badge", "demo-flag", "chip-error", "chip-warning",
                      "integrity-ok", "integrity-warn", "integrity-bad",
                      "scrim", "update-card:hover", "detail-panel")

    def test_chrome_backgrounds_are_tokens(self):
        offenders = []
        for sel, decls, at in _rules(CSS_PATH.read_text()):
            if sel.startswith(":root") or any(k in sel for k in self.SELF_CONTAINED):
                continue
            for prop, value in decls:
                if prop not in ("background", "background-color", "color"):
                    continue
                if re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", value) and "var(--" not in value:
                    offenders.append(f"{at}{sel} {{{prop}: {value}}}")
        self.assertEqual([], offenders,
                         "hardcoded colour outside the theme blocks — this is exactly how "
                         ".avatar, .global-search kbd, .flow-step i, .mobile-menu and .skip "
                         "stayed light in dark mode:\n  " + "\n  ".join(offenders))

    def test_every_referenced_token_is_defined_on_root(self):
        css = _strip_comments(CSS_PATH.read_text())
        root = next((decls for sel, decls, at in _rules(CSS_PATH.read_text())
                     if sel.strip() == ":root" and not at), None)
        self.assertIsNotNone(root, "the light palette must live on bare :root")
        defined = {p for p, _ in root if p.startswith("--")}
        used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", css))
        missing = sorted(used - defined)
        self.assertEqual([], missing,
                         "token used but not defined on :root, so it has no value in the "
                         f"light theme: {missing}")

    def test_no_fallback_values_hide_a_missing_token(self):
        """`var(--muted,#6b7580)` silently paints an off-palette grey if the
        token is ever renamed, which is a defect that looks like a design."""
        css = _strip_comments(CSS_PATH.read_text())
        bad = re.findall(r"var\(\s*--[a-z0-9-]+\s*,\s*[^)]+\)", css)
        self.assertEqual([], bad,
                         "var() fallbacks found; every token is defined on :root, so a "
                         f"fallback can only mask a rename: {bad}")


class SystemThemeFollowsTheOS(unittest.TestCase):
    def test_prefers_color_scheme_block_exists(self):
        css = _strip_comments(CSS_PATH.read_text())
        self.assertIn("prefers-color-scheme", css,
                      "without a prefers-color-scheme block the 'System' theme matches no "
                      "rule and a user on OS dark silently gets the light palette")

    def test_media_guard_matches_an_empty_data_theme(self):
        css = _strip_comments(CSS_PATH.read_text())
        m = re.search(r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)\s*\{\s*([^{]+)\{", css)
        self.assertIsNotNone(m, "expected one @media (prefers-color-scheme:dark) block")
        guard = m.group(1)
        # app.js writes data-theme="" for 'system', so a :not([data-theme]) guard
        # would never match. The guard must exclude the explicit values instead.
        self.assertNotIn(':not([data-theme])', guard,
                         'the system option writes data-theme="", so :not([data-theme]) '
                         'never matches it')
        for explicit in ("light", "dark", "contrast"):
            self.assertIn(f':not([data-theme="{explicit}"])', guard,
                          f'an explicit {explicit} choice must win over the OS preference')

    def test_the_two_dark_palettes_are_identical(self):
        """One list under @media, one under [data-theme="dark"]. Same rule in two
        places is how the first three hardcoded colours survived a fix; here the
        duplication is unavoidable, so it is asserted instead of trusted."""
        rules = _rules(CSS_PATH.read_text())
        media = [decls for sel, decls, at in rules
                 if at and any("prefers-color-scheme" in a for a in at)]
        attr = [decls for sel, decls, at in rules
                if not at and sel.strip().startswith(':root[data-theme="dark"]')]
        self.assertEqual(1, len(media), "expected exactly one rule inside the dark media query")
        self.assertEqual(1, len(attr), 'expected exactly one :root[data-theme="dark"] rule')
        a = {p: v for p, v in media[0]}
        b = {p: v for p, v in attr[0]}
        self.assertEqual(a, b,
                         "the two dark palettes have drifted apart; a token that differs is a "
                         "colour that changes when the user picks Dark explicitly vs inherits "
                         f"it from the OS.\n  only in media: {sorted(set(a) - set(b))}\n"
                         f"  only in attr:  {sorted(set(b) - set(a))}\n"
                         f"  differing:     {sorted(k for k in set(a) & set(b) if a[k] != b[k])}")

    def test_contrast_theme_reaches_the_rail_and_the_hero(self):
        """It used to redefine only the core palette, leaving the rail and the
        security hero on hardcoded dark greens — a third of the chrome."""
        rules = _rules(CSS_PATH.read_text())
        contrast = next((decls for sel, decls, at in rules
                         if not at and sel.strip().startswith(':root[data-theme="contrast"]')), None)
        self.assertIsNotNone(contrast, "expected a :root[data-theme=contrast] palette")
        names = {p for p, _ in contrast}
        for token in ("--rail-bg", "--rail-fg", "--rail-muted", "--rail-active-bg",
                      "--hero-bg", "--hero-fg", "--hero-muted"):
            self.assertIn(token, names, f"the high-contrast theme does not redefine {token}")


if __name__ == "__main__":
    unittest.main()

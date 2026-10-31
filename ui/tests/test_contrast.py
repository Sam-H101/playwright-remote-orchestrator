"""WCAG 2.2 AA contrast for the colour tokens in grid.css, in both themes.

Each token is defined once as light-dark(<light>, <dark>); state tints are the hue mixed at --tint
over the surface, exactly as color-mix() computes them in sRGB.
"""
import os
import re

import pytest

CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "grid.css")


def _tokens():
    text = open(CSS, encoding="utf-8").read()
    pairs = dict(re.findall(r"--([\w-]+):\s*light-dark\((#[0-9a-fA-F]{6}),\s*#[0-9a-fA-F]{6}\)", text))
    dark = dict((k, v) for k, v in re.findall(r"--([\w-]+):\s*light-dark\(#[0-9a-fA-F]{6},\s*(#[0-9a-fA-F]{6})\)", text))
    tint = int(re.search(r"--tint:\s*(\d+)%", text).group(1)) / 100
    return {"light": pairs, "dark": dark}, tint, text


def _rgb(hex_):
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, share):
    return "#" + "".join("{:02x}".format(round(x * share + y * (1 - share))) for x, y in zip(_rgb(a), _rgb(b)))


def _lum(hex_):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TOKENS, TINT, TEXT = _tokens()


def test_tokens_are_defined_once():
    names = re.findall(r"^\s*--([\w-]+):\s*light-dark\(", TEXT, re.M)
    assert len(names) == len(set(names)) and len(names) >= 15
    assert "prefers-color-scheme: dark" not in TEXT  # one definition, switched by color-scheme


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_is_at_least_4_5_to_1(scheme):
    t = TOKENS[scheme]
    tints = {h: _mix(t[h], t["surface"], TINT) for h in ("ok", "warn", "bad", "accent", "off")}
    pairs = []
    for fg in ("text", "text-2", "text-3"):  # body text, meta, placeholders and counts
        for bg in ("bg", "surface", "raised"):
            pairs.append((fg, bg, t[fg], t[bg]))
    for hue in ("ok", "warn", "bad", "accent", "off"):  # state text, and pills on their own tint
        pairs.append((hue, "surface", t[hue], t["surface"]))
        pairs.append((hue, hue + "-tint", t[hue], tints[hue]))
    pairs.append(("text", "warn-tint", t["text"], tints["warn"]))  # the database error and banner
    pairs.append(("text", "bad-tint", t["text"], tints["bad"]))
    pairs.append(("accent-ink", "accent", t["accent-ink"], t["accent"]))  # primary buttons, busy glyph
    pairs.append(("bad-ink", "bad", t["bad-ink"], t["bad"]))  # failure badges, confirm buttons
    pairs.append(("bg", "text", t["bg"], t["text"]))  # the toast is inverted
    low = [(f, b, round(ratio(x, y), 2)) for f, b, x, y in pairs if ratio(x, y) < 4.5]
    assert not low, low


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_boundaries_are_at_least_3_to_1(scheme):
    t = TOKENS[scheme]
    pairs = [("line-strong", bg) for bg in ("bg", "surface", "raised")]  # inputs, cells, meters
    pairs += [(h, "surface") for h in ("ok", "warn", "bad", "accent", "off")]  # slot cell outlines
    pairs += [("accent", "bg"), ("accent", "raised")]  # the focus ring
    low = [(f, b, round(ratio(t[f], t[b]), 2)) for f, b in pairs if ratio(t[f], t[b]) < 3]
    assert not low, low

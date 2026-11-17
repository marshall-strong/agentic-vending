"""Honcho, the mascot: our head agent (full name Rocco Kirk Honcho) drawn as a small vending
machine in a captain's cap, saluting. One name from each of us: Rocco (Kevin, and the ROC),
Kirk (Sean, a captain) and Honcho (Marshall, the head honcho); the naming proposal is #76.

`svg(mood)` returns inline SVG with its own colours, so it reads on light and dark pages. It is
decoration: hidden from screen readers, since the verdict pill beside it already says the same,
with a hover tooltip for everyone else.
The mood follows the dashboard's verdict: "ok" smiles, "check" raises an eyebrow, and
"problem" frowns and sweats.
"""
from __future__ import annotations

import math

NAME = "Rocco Kirk Honcho"
SHORT = "Honcho"
MOODS = ("ok", "check", "problem")
_SAYS = {"ok": "all good", "check": "something to check", "problem": "a problem needs a person"}


def _bottle_cap(cx: float, cy: float, outer: float, inner: float, teeth: int = 21) -> str:
    """Points of a crimped bottle cap: the badge on Honcho's cap."""
    points = []
    for i in range(teeth * 2):
        r = outer if i % 2 == 0 else inner
        a = math.pi * i / teeth - math.pi / 2
        points.append(f"{cx + r * math.cos(a):.2f},{cy + r * math.sin(a):.2f}")
    return " ".join(points)


_ROWS = (("#f2b134", "#3fa7d6", "#59c27a", "#e8604c"),
         ("#3fa7d6", "#e8604c", "#f2b134", "#59c27a"),
         ("#59c27a", "#f2b134", "#e8604c", "#3fa7d6"))

_MOUTH = {"ok": "M37 61 Q44 68 51 61",
          "check": "M38 64 Q44 62.5 50 63.5",
          "problem": "M37.5 66 Q44 60 50.5 66"}


def svg(mood: str = "ok", size: int = 44) -> str:
    """Honcho as inline SVG, `size` pixels tall. An unknown mood draws "check"."""
    mood = mood if mood in MOODS else "check"
    label = f"{SHORT}, our head agent: {_SAYS[mood]}"
    products = "".join(f'<rect x="{27 + 9 * i}" y="{73 + 9 * r}" width="7" height="6" rx="1.5" fill="{c}"/>'
                       for r, row in enumerate(_ROWS) for i, c in enumerate(row))
    brow = ('<path d="M47.5 44.5 L56 42" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>'
            if mood == "check" else
            '<path d="M31 45 L39 46.5 M49 46.5 L57 45" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>'
            if mood == "problem" else "")
    sweat = ('<path d="M12 40 q-3.5 5.5 0 7.5 q3.5 -2 0 -7.5 Z" fill="#7cc3f0" stroke="#3b8fc4" stroke-width=".6"/>'
             if mood == "problem" else "")
    return (
        f'<svg class="mascot {mood}" aria-hidden="true" focusable="false" viewBox="0 0 100 120" '
        f'width="{size * 100 // 120}" height="{size}" style="flex:none" xmlns="http://www.w3.org/2000/svg">'
        f'<title>{label}</title>'
        # feet and left arm, behind the cabinet
        '<rect x="25" y="108" width="13" height="7" rx="2.5" fill="#2a2f38"/>'
        '<rect x="62" y="108" width="13" height="7" rx="2.5" fill="#2a2f38"/>'
        '<path d="M19 60 Q8 70 13 80" fill="none" stroke="#8f1d23" stroke-width="5" stroke-linecap="round"/>'
        '<circle cx="13.5" cy="81" r="4.2" fill="#f7f7f7" stroke="#b9bec7" stroke-width="1"/>'
        # cabinet, glass, keypad, pickup bin
        '<rect x="18" y="30" width="64" height="80" rx="8" fill="#c8323a" stroke="#8f1d23" stroke-width="2"/>'
        '<rect x="24" y="37" width="41" height="64" rx="4" fill="#1d2430"/>'
        '<rect x="68.5" y="40" width="9" height="30" rx="2" fill="#ece7df"/>'
        '<circle cx="73" cy="45.5" r="1.6" fill="#8a8f98"/><circle cx="73" cy="51" r="1.6" fill="#8a8f98"/>'
        '<circle cx="73" cy="56.5" r="1.6" fill="#8a8f98"/>'
        '<rect x="70.5" y="62.5" width="5" height="1.8" rx=".9" fill="#3a3f48"/>'
        '<rect x="27" y="103" width="35" height="4.5" rx="1.5" fill="#11151c"/>'
        # face on the glass, and the products below it
        '<circle cx="36" cy="52" r="5.2" fill="#fff"/><circle cx="52" cy="52" r="5.2" fill="#fff"/>'
        '<circle cx="37" cy="53" r="2.5" fill="#11151c"/><circle cx="53" cy="53" r="2.5" fill="#11151c"/>'
        f'{brow}<path d="{_MOUTH[mood]}" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/>'
        f'{products}{sweat}'
        # captain's cap: crown, band with braid, brim, bottle-cap badge
        '<path d="M25 23 L11 11 Q50 -2 89 11 L75 23 Z" fill="#2b4d8a" stroke="#13233f" stroke-width="1.2" '
        'stroke-linejoin="round"/>'
        '<path d="M14 11.5 Q50 1 86 11.5" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="1.4"/>'
        '<rect x="24" y="22" width="52" height="8.5" rx="1" fill="#1c3563" stroke="#13233f" stroke-width=".8"/>'
        '<path d="M26 26.5 H74" stroke="#e3b341" stroke-width="1.4"/>'
        '<path d="M20 30 Q50 37 80 30 L82 33 Q50 43.5 18 33 Z" fill="#0f1a2e"/>'
        f'<polygon points="{_bottle_cap(50, 16, 6.4, 5.3)}" fill="#e3b341" stroke="#a97f1e" stroke-width=".5"/>'
        '<circle cx="50" cy="16" r="3.7" fill="#c8323a"/>'
        '<text x="50" y="18.1" text-anchor="middle" font-family="Arial, sans-serif" font-size="5.6" '
        'font-weight="700" fill="#fff">H</text>'
        # right arm, saluting: the hand at the brim's edge
        '<path d="M81 60 Q97 45 84 30" fill="none" stroke="#8f1d23" stroke-width="5" stroke-linecap="round"/>'
        '<circle cx="82.5" cy="28.5" r="4.4" fill="#f7f7f7" stroke="#b9bec7" stroke-width="1"/>'
        '</svg>')

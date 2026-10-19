"""
Site_Theme.py
-------------
AI Site Themes: turn a light site dark while keeping its colour identity.

Division of labour:
  - theme_engine.js measures the rendered page locally and sends only a
    compact palette summary: clustered colours, how much background
    area / text / border each covers, surface depth, and which text
    colours sit on which backgrounds. No page content leaves the browser.
  - The smart model (navigator_smart_llm) designs the new palette:
    roles, mood, brand feel.
  - validate_design() then enforces the hard rules (mode sanity, text
    contrast, surface separation) so a bad model call can never make a
    site unreadable. If the model call fails entirely, a role-aware
    OKLCH remap stands in, so the user still gets a usable theme.
  - The result is stored per hostname in ThemeStore (SQLite).
"""

import json
import math
import re
import time
import uuid
from typing import Literal, Optional

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

import configuration
from Navigator.Task_Files.ThemeStore import ThemeStore

log = structlog.get_logger()
store = ThemeStore()

Mode = Literal["light", "dark"]
Role = Literal["bg", "text", "border"]
ROLES: tuple[str, ...] = ("bg", "text", "border")


# ── API MODELS ──────────────────────────────────────────────────────────

class PaletteColor(BaseModel):
    id: str
    hex: str
    bg_share: float = 0.0
    text_share: float = 0.0
    border_share: float = 0.0
    depth: int = 0


class ColorPair(BaseModel):
    fg: str
    bg: str
    share: float = 0.0


class GenerateThemeRequest(BaseModel):
    host: str
    url: str = ""
    title: str = ""
    detected_mode: Mode
    palette: list[PaletteColor]
    pairs: list[ColorPair] = []
    fingerprint: str = ""


class ThemeAnchor(BaseModel):
    role: Role
    src: str
    dst: str
    share: float = 0.0


class ThemeRecord(BaseModel):
    host: str
    enabled: bool
    original_mode: Mode
    target_mode: Mode
    anchors: list[ThemeAnchor]
    fingerprint: str = ""
    notes: str = ""
    source: Literal["ai", "fallback"] = "ai"
    updated_at: float


class ListThemesResponse(BaseModel):
    themes: list[ThemeRecord]


# ── LLM OUTPUT SCHEMA ───────────────────────────────────────────────────
# Every field required-but-nullable so it stays valid under strict
# structured output.

class ColorAssignment(BaseModel):
    id: str = Field(description="Palette colour id, exactly as given")
    as_background: Optional[str] = Field(description="New #rrggbb when this colour is used as a background, else null")
    as_text: Optional[str] = Field(description="New #rrggbb when this colour is used as text, else null")
    as_border: Optional[str] = Field(description="New #rrggbb when this colour is used as a border, else null")


class ThemeDesign(BaseModel):
    assignments: list[ColorAssignment]
    notes: str = Field(description="One or two sentences on the main design choices")


# ── COLOUR MATHS (sRGB <-> OKLab / OKLCH, WCAG contrast) ────────────────

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def normalize_hex(value) -> str | None:
    if not isinstance(value, str):
        return None
    m = _HEX_RE.match(value.strip())
    if not m:
        return None
    h = m.group(1).lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h[:6]


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = normalize_hex(h) or "#000000"
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{round(min(1.0, max(0.0, c)) * 255):02x}" for c in rgb)


def _to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _from_lin(c: float) -> float:
    c = max(0.0, c)
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _cbrt(v: float) -> float:
    return math.copysign(abs(v) ** (1 / 3), v)


def rgb_to_oklab(rgb) -> tuple[float, float, float]:
    r, g, b = (_to_lin(c) for c in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def oklab_to_linear(L: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def hex_to_oklch(h: str) -> tuple[float, float, float]:
    L, a, b = rgb_to_oklab(hex_to_rgb(h))
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def oklch_to_hex(L: float, C: float, H: float) -> str:
    """OKLCH -> hex, reducing chroma (never lightness or hue) to fit sRGB."""
    L = min(1.0, max(0.0, L))
    C = max(0.0, C)
    hr = math.radians(H)

    def lin(c):
        return oklab_to_linear(L, c * math.cos(hr), c * math.sin(hr))

    def fits(v):
        return all(-1e-4 <= x <= 1 + 1e-4 for x in v)

    if not fits(lin(C)):
        lo, hi = 0.0, C
        for _ in range(22):
            mid = (lo + hi) / 2
            if fits(lin(mid)):
                lo = mid
            else:
                hi = mid
        C = lo
    return rgb_to_hex(tuple(_from_lin(x) for x in lin(C)))


def ok_distance(h1: str, h2: str) -> float:
    a, b = rgb_to_oklab(hex_to_rgb(h1)), rgb_to_oklab(hex_to_rgb(h2))
    return math.dist(a, b)


def _luminance(h: str) -> float:
    r, g, b = (_to_lin(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(h1: str, h2: str) -> float:
    a, b = _luminance(h1), _luminance(h2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ── ALGORITHMIC REMAP ───────────────────────────────────────────────────
# Mirrors algorithmic() in theme_engine.js — keep the two in sync. Used to
# fill anything the model left out, to replace obviously wrong values,
# and as the whole palette if the model call fails.

def algorithmic_map(hex_value: str, role: str) -> str:
    """Role-aware light -> dark remap that keeps hue."""
    L, C, H = hex_to_oklch(hex_value)
    accent = C > 0.08
    if role == "bg":
        if accent:
            return oklch_to_hex(min(L, 0.62), C * 0.9, H)
        if L >= 0.5:
            return oklch_to_hex(0.16 + (1 - L) * 0.22, min(C, 0.025), H)
        return oklch_to_hex(L, C, H)
    if role == "text":
        if accent:
            return oklch_to_hex(max(L, 0.72), C, H)
        if L < 0.6:
            return oklch_to_hex(0.95 - L * 0.45, min(C, 0.03), H)
        return oklch_to_hex(L, C, H)
    if accent:
        return oklch_to_hex(max(L, 0.6), C, H)
    if L >= 0.5:
        return oklch_to_hex(0.30 + (1 - L) * 0.25, min(C, 0.03), H)
    return oklch_to_hex(L, C, H)


# ── PROMPT ──────────────────────────────────────────────────────────────

_DARK_RULES = """\
- Large surfaces (high bg_share) become calm, deep, low-chroma tones, OKLCH L ≈ 0.14–0.22. Never pure #000000. A faint tint (chroma ≤ 0.02) of the site's brand hue is welcome when it suits the site.
- Preserve elevation: surfaces at greater depth (cards, popovers, inputs) are slightly LIGHTER than the surface beneath them (ΔL ≈ 0.03–0.06) — that is how good dark UIs show elevation.
- Body text becomes light but not pure white (L ≈ 0.88–0.94). Secondary / muted text stays visibly secondary (L ≈ 0.68–0.78). Keep the original hierarchy between text colours.
- Accent and brand colours (chroma above ~0.08) keep their hue. Change lightness only as much as needed: accent text and links usually brighten a little (L ≈ 0.70–0.80) to read on dark surfaces; accent backgrounds (buttons, badges, highlights) keep roughly their lightness so the text on them still works.
- Neutral borders and dividers become subtle — a little lighter than the surface they sit on (L ≈ 0.28–0.36).
- Status colours (red, green, amber, blue info) stay recognisable."""

def _system_prompt() -> str:
    rules = _DARK_RULES
    return f"""You are a senior UI colour designer. You convert a website's colour palette from light mode to dark mode while keeping the site's identity — its brand hues, its accent colours, its sense of hierarchy — so it still clearly feels like the same website, just in dark mode.

You receive the site's measured palette. Each colour has an id, its hex, its OKLCH values [L, C, H°], and how it is used:
- bg_share: fraction of the painted background area it covers
- text_share: fraction of the page's text (weighted by length × font size) drawn in it
- border_share: fraction of border length drawn in it
- depth: surface nesting level (0 = page, 1 = a section or card on the page, 2 = something on a card, ...)
You also get pairs: which text colour sits on which background colour, and how much of the page's text that pair accounts for.

For every colour, return a new #rrggbb for each way it is used: as_background if bg_share > 0, as_text if text_share > 0, as_border if border_share > 0, and null otherwise. One original colour can need different values per use — white as the page background becomes a deep surface, while white text on a blue button stays near-white.

Design rules for dark mode:
{rules}

General rules:
- Every text/background pair must stay comfortably readable: WCAG contrast ≥ 4.5:1 everywhere, ≥ 7:1 for the dominant body-text pair.
- Colours that were clearly distinguishable must stay distinguishable.
- Keep the mood. A warm, cream-toned site should feel warm in dark mode; a cool corporate blue site should feel cool.
- Hex only, format #rrggbb. Include every id exactly once."""


def _human_prompt(req: GenerateThemeRequest) -> str:
    palette = []
    for p in req.palette:
        L, C, H = hex_to_oklch(p.hex)
        palette.append({
            "id": p.id,
            "hex": normalize_hex(p.hex),
            "oklch": [round(L, 3), round(C, 3), round(H)],
            "bg_share": round(p.bg_share, 4),
            "text_share": round(p.text_share, 4),
            "border_share": round(p.border_share, 4),
            "depth": p.depth,
        })
    pairs = [{"text": x.fg, "on": x.bg, "share": round(x.share, 4)} for x in req.pairs]
    payload = {
        "site": req.host,
        "title": req.title[:120],
        "palette": palette,
        "pairs": pairs,
    }
    return json.dumps(payload, ensure_ascii=False)


# ── VALIDATION ──────────────────────────────────────────────────────────

def _share(p: PaletteColor, role: str) -> float:
    return getattr(p, f"{role}_share")


def validate_design(
    req: GenerateThemeRequest, design: ThemeDesign | None
) -> tuple[dict[tuple[str, str], str], int, int]:
    """
    Turns the model's proposal into a safe (palette_id, role) -> hex map.
    Returns (mapping, fixes_applied, values_filled_algorithmically).
    """
    proposed = {}
    if design:
        for a in design.assignments:
            proposed[a.id] = a

    mapping: dict[tuple[str, str], str] = {}
    filled = 0
    fields = {"bg": "as_background", "text": "as_text", "border": "as_border"}

    for p in req.palette:
        a = proposed.get(p.id)
        for role in ROLES:
            val = normalize_hex(getattr(a, fields[role])) if a else None
            if _share(p, role) <= 0 and val is None:
                continue
            if val is None:
                val = algorithmic_map(p.hex, role)
                filled += 1
            mapping[(p.id, role)] = val

    fixes = 0

    # 1. Mode sanity: big surfaces must actually be dark.
    for p in req.palette:
        key = (p.id, "bg")
        if p.bg_share >= 0.05 and key in mapping:
            L = hex_to_oklch(mapping[key])[0]
            if L > 0.42:
                mapping[key] = algorithmic_map(p.hex, "bg")
                fixes += 1

    # 2. Surfaces that were distinct stay distinct; deeper ones lighter.
    bgs = [p for p in req.palette if (p.id, "bg") in mapping and p.bg_share > 0]
    if bgs:
        base = max(bgs, key=lambda p: p.bg_share)
        base_new = mapping[(base.id, "bg")]
        base_L = hex_to_oklch(base_new)[0]
        for p in bgs:
            if p.id == base.id or p.bg_share < 0.005:
                continue
            # Light UIs separate surfaces by tiny steps (#fff page vs
            # #fafafa / #f6f9fc cards ≈ 0.015–0.02 apart), so only treat
            # colours under 0.008 apart as "the same surface".
            if ok_distance(p.hex, base.hex) < 0.008:
                continue
            new = mapping[(p.id, "bg")]
            if ok_distance(new, base_new) >= 0.02:
                continue
            L, C, H = hex_to_oklch(new)
            step = 0.035 if p.depth > base.depth else -0.03
            mapping[(p.id, "bg")] = oklch_to_hex(base_L + step, C, H)
            fixes += 1

    # 3. Contrast for every measured text/background pair.
    pairs = sorted(req.pairs, key=lambda x: -x.share)
    body = pairs[0] if pairs else None
    for _ in range(3):
        changed = False
        for pr in pairs:
            fk, bk = (pr.fg, "text"), (pr.bg, "bg")
            if fk not in mapping or bk not in mapping:
                continue
            need = 7.0 if pr is body else 4.5
            fg, bg = mapping[fk], mapping[bk]
            if contrast(fg, bg) >= need:
                continue
            L, C, H = hex_to_oklch(fg)
            step = 0.02 if hex_to_oklch(bg)[0] < 0.5 else -0.02
            new = fg
            for _ in range(50):
                L += step
                if L < 0 or L > 1:
                    break
                new = oklch_to_hex(L, C, H)
                if contrast(new, bg) >= need:
                    break
            if new != fg:
                mapping[fk] = new
                fixes += 1
                changed = True
        if not changed:
            break

    return mapping, fixes, filled


def _anchors(req: GenerateThemeRequest, mapping: dict) -> list[ThemeAnchor]:
    anchors = []
    for p in req.palette:
        src = normalize_hex(p.hex)
        if not src:
            continue
        for role in ROLES:
            if (p.id, role) in mapping:
                anchors.append(ThemeAnchor(
                    role=role, src=src, dst=mapping[(p.id, role)],
                    share=round(_share(p, role), 4),
                ))
    return anchors


def _record_usage(raw, host: str) -> None:
    try:
        from usage_tracker import record_usage
        if raw is not None and getattr(raw, "usage_metadata", None):
            usage = raw.usage_metadata
            record_usage(
                dimension="navigator",
                session_id=f"theme_{uuid.uuid4().hex[:8]}",
                model_name=raw.response_metadata.get("model_name", "unknown"),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0),
                message_id=getattr(raw, "id", None),
            )
    except Exception as rec_err:
        log.warning("record_usage failed for site theme", host=host, error=str(rec_err))


# ── PROCESS FUNCTIONS (called from navigator_bridge.py) ─────────────────

async def process_generate_theme(req: GenerateThemeRequest) -> ThemeRecord:
    host = req.host.strip().lower()
    if not host:
        raise ValueError("Missing host")
    if not req.palette:
        raise ValueError("Empty palette — nothing to theme")

    if req.detected_mode == "dark":
        raise ValueError("This site is already dark")

    design: ThemeDesign | None = None
    source = "ai"
    notes = ""
    started = time.time()

    try:
        llm = configuration.navigator_smart_llm().with_structured_output(ThemeDesign, include_raw=True)
        result = await llm.ainvoke([
            SystemMessage(content=_system_prompt()),
            HumanMessage(content=_human_prompt(req)),
        ])
        _record_usage(result.get("raw"), host)
        design = result.get("parsed")
        if design is None:
            raise ValueError(f"Unparseable design: {result.get('parsing_error')}")
        notes = design.notes.strip()
    except Exception as e:
        log.warning("Site theme fell back to algorithmic palette", host=host, error=str(e))
        design = None
        source = "fallback"
        notes = "Generated without AI because the model call failed."

    mapping, fixes, filled = validate_design(req, design)

    record = ThemeRecord(
        host=host,
        enabled=True,
        original_mode="light",
        target_mode="dark",
        anchors=_anchors(req, mapping),
        fingerprint=req.fingerprint,
        notes=notes,
        source=source,
        updated_at=time.time(),
    )
    store.upsert(record.model_dump())

    log.info(
        "Site theme generated", host=host,
        source=source, palette_size=len(req.palette), pairs=len(req.pairs),
        fixes=fixes, filled=filled, seconds=round(time.time() - started, 1),
    )
    return record


async def process_get_theme(host: str) -> ThemeRecord | None:
    row = store.get(host.strip().lower())
    return ThemeRecord(**row) if row else None


async def process_list_themes() -> ListThemesResponse:
    return ListThemesResponse(themes=[ThemeRecord(**r) for r in store.list_all()])


async def process_delete_theme(host: str) -> dict:
    existed = store.delete(host.strip().lower())
    return {"status": "deleted", "existed": existed}
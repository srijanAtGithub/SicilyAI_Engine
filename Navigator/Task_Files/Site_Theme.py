"""
Site_Theme.py
-------------
AI dark themes for light websites.

Design: colour fundamentals in code, AI only for taste.
  - theme_engine.js measures the rendered page locally and sends a compact
    palette summary (clustered colours, their background / text / border
    share, surface depth, text-on-background pairs). No page content.
  - A fixed dark-mode ladder (the same structure standard design systems
    use) maps every colour by its role and lightness: page and raised
    surfaces, primary / secondary / disabled text, subtle borders, and
    accents that keep their hue. This guarantees depth and contrast on
    every site.
  - The smart model makes only the taste decisions: which brand hue (if
    any) faintly tints the dark surfaces, how strongly, and how deep the
    page sits within a safe range. ~20 output tokens, low reasoning effort.
  - validate() then enforces text contrast and surface separation.
  - Results are stored per hostname in ThemeStore (SQLite).
"""

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


class ThemeParams(BaseModel):
    """The model's whole answer — the three taste decisions."""
    hue: Optional[float] = Field(description="OKLCH hue (0-360) of the brand colour that faintly tints dark surfaces, or null for neutral grey")
    tint: float = Field(description="Tint chroma, 0 to 0.02")
    page: float = Field(description="Page surface OKLCH lightness, 0.12 to 0.18")


class ThemeRecord(BaseModel):
    host: str
    enabled: bool
    original_mode: Mode
    target_mode: Mode
    anchors: list[ThemeAnchor]
    params: dict = {}
    fingerprint: str = ""
    notes: str = ""
    source: Literal["ai", "fallback"] = "ai"
    updated_at: float


class ListThemesResponse(BaseModel):
    themes: list[ThemeRecord]


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

# ── THE DARK LADDER ─────────────────────────────────────────────────────
# Mirrors ladder() in theme_engine.js — keep the two in sync.

DEFAULT_PARAMS = {"hue": None, "tint": 0.0, "page": 0.16}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def clean_params(p: ThemeParams | dict | None) -> dict:
    """Clamp whatever the model said into the safe ranges."""
    if p is None:
        return dict(DEFAULT_PARAMS)
    d = p.model_dump() if isinstance(p, BaseModel) else dict(p)
    hue = d.get("hue")
    return {
        "hue": None if hue is None else float(hue) % 360,
        "tint": round(_clamp(float(d.get("tint") or 0), 0.0, 0.02), 4),
        "page": round(_clamp(float(d.get("page") or DEFAULT_PARAMS["page"]), 0.12, 0.18), 4),
    }


def ladder(hex_value: str, role: str, p: dict) -> str:
    """
    Light-theme colour -> dark-theme colour, by role and lightness.
      surfaces: white -> page; light greys -> raised steps above the page
      text:     near-black -> primary (L ~.9); greys -> secondary; light stays light
      borders:  light lines -> subtle lines just above the page
      accents:  keep hue and chroma; lightness clamped to readable ranges
    Neutrals take the brand tint; softly tinted colours keep their own hue.
    """
    L, C, H = hex_to_oklch(hex_value)
    page, tint, th = p["page"], p["tint"], p["hue"]
    accent = C > 0.08
    tinted = 0.02 < C <= 0.08

    def tone(lo: float, own_c: float) -> str:
        if tinted:
            return oklch_to_hex(lo, own_c, H)
        if th is None or tint <= 0:
            return oklch_to_hex(lo, 0, H)
        return oklch_to_hex(lo, tint, th)

    if role == "bg":
        if accent:
            return oklch_to_hex(_clamp(L, 0.42, 0.58), C * 0.9, H)
        if L >= 0.75:
            lo = page + (1 - L) / 0.25 * 0.12
        elif L >= 0.5:
            lo = page + 0.12 + (0.75 - L) * 0.4
        else:
            lo = _clamp(L, page - 0.03, page + 0.06)
        return tone(lo, min(C * 0.6, 0.04))

    if role == "text":
        if accent:
            return oklch_to_hex(_clamp(L, 0.72, 0.85), C, H)
        lo = 0.94 - ((L / 0.65) ** 1.5) * 0.26 if L < 0.65 else L
        return tone(min(lo, 0.96), min(C, 0.04))

    if accent:
        return oklch_to_hex(_clamp(L, 0.55, 0.7), C, H)
    lo = page + 0.10 + (1 - L) * 0.3 if L >= 0.6 else 0.42
    return tone(lo, min(C, 0.04))


# ── PROMPT ──────────────────────────────────────────────────────────────

_SYSTEM = """You choose dark-theme parameters for a website from its colours. Reply with JSON only.
hue: OKLCH hue (0-360) of the site's brand colour, used to faintly tint dark surfaces; null for plain grey (monochrome sites, or red/orange/yellow brands, where tinted dark surfaces look muddy).
tint: tint strength 0-0.02 (0.006-0.012 typical; 0 if hue is null).
page: page surface lightness 0.12-0.18 (0.13 crisp and high-contrast for tools and apps; 0.16-0.18 softer for reading sites)."""


def _user_prompt(req: GenerateThemeRequest) -> str:
    rows = []
    for p in sorted(req.palette, key=lambda c: -(c.bg_share + c.text_share + c.border_share))[:10]:
        use = max(("bg", p.bg_share), ("text", p.text_share), ("border", p.border_share), key=lambda x: x[1])
        L, C, H = hex_to_oklch(p.hex)
        rows.append(f"{normalize_hex(p.hex)} {use[0]} {use[1]:.2f} h{round(H)} c{C:.2f}")
    return f"{req.host}\n" + "\n".join(rows)


# ── VALIDATION ──────────────────────────────────────────────────────────

def _share(p: PaletteColor, role: str) -> float:
    return getattr(p, f"{role}_share")


def validate(req: GenerateThemeRequest, params: dict) -> tuple[dict[tuple[str, str], str], int]:
    """
    Ladder-maps every measured colour, then enforces the hard rules:
    distinct surfaces stay distinct (deeper = lighter) and every measured
    text/background pair stays readable. Returns (mapping, fixes).
    """
    mapping: dict[tuple[str, str], str] = {}
    for p in req.palette:
        for role in ROLES:
            if _share(p, role) > 0:
                mapping[(p.id, role)] = ladder(p.hex, role, params)

    fixes = 0

    # 1. Surfaces that were distinct stay distinct; deeper ones lighter.
    bgs = [p for p in req.palette if (p.id, "bg") in mapping]
    if bgs:
        base = max(bgs, key=lambda p: p.bg_share)
        base_new = mapping[(base.id, "bg")]
        base_L = hex_to_oklch(base_new)[0]
        for p in bgs:
            if p.id == base.id or p.bg_share < 0.005:
                continue
            # Light UIs separate surfaces by tiny steps (#fff vs #fafafa),
            # so only colours under 0.008 apart count as the same surface.
            if ok_distance(p.hex, base.hex) < 0.008:
                continue
            new = mapping[(p.id, "bg")]
            if ok_distance(new, base_new) >= 0.02:
                continue
            L, C, H = hex_to_oklch(new)
            step = 0.035 if p.depth > base.depth else -0.03
            mapping[(p.id, "bg")] = oklch_to_hex(base_L + step, C, H)
            fixes += 1

    # 2. Contrast for every measured text/background pair. Text moves away
    #    from its background on its own side (light text stays light); if
    #    it runs out of room, the background moves the other way instead.
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
            new_fg, new_bg = _separate(fg, bg, need)
            if new_fg != fg:
                mapping[fk] = new_fg
            if new_bg != bg:
                mapping[bk] = new_bg
            fixes += 1
            changed = True
        if not changed:
            break

    return mapping, fixes


def _separate(fg: str, bg: str, need: float) -> tuple[str, str]:
    """Push fg away from bg (fg keeps its side); then bg, if fg hit the limit."""
    fL, fC, fH = hex_to_oklch(fg)
    bL, bC, bH = hex_to_oklch(bg)
    up = fL >= bL
    for _ in range(50):
        if contrast(fg, bg) >= need:
            return fg, bg
        if (up and fL >= 1) or (not up and fL <= 0):
            break
        fL = _clamp(fL + (0.02 if up else -0.02), 0, 1)
        fg = oklch_to_hex(fL, fC, fH)
    for _ in range(50):
        if contrast(fg, bg) >= need:
            break
        bL = _clamp(bL + (-0.02 if up else 0.02), 0, 1)
        bg = oklch_to_hex(bL, bC, bH)
        if bL in (0, 1):
            break
    return fg, bg


def _anchors(req: GenerateThemeRequest, mapping: dict) -> list[ThemeAnchor]:
    anchors = []
    for p in req.palette:
        src = normalize_hex(p.hex)
        if not src:
            continue
        for role in ROLES:
            if (p.id, role) in mapping:
                anchors.append(ThemeAnchor(role=role, src=src, dst=mapping[(p.id, role)], share=round(_share(p, role), 4)))
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

    started = time.time()
    source = "ai"
    params = None
    try:
        llm = configuration.navigator_smart_llm(reasoning_effort="low").with_structured_output(ThemeParams, include_raw=True)
        result = await llm.ainvoke([SystemMessage(content=_SYSTEM), HumanMessage(content=_user_prompt(req))])
        _record_usage(result.get("raw"), host)
        params = result.get("parsed")
        if params is None:
            raise ValueError(f"Unparseable params: {result.get('parsing_error')}")
    except Exception as e:
        log.warning("Site theme fell back to default parameters", host=host, error=str(e))
        source = "fallback"

    params = clean_params(params)
    mapping, fixes = validate(req, params)

    record = ThemeRecord(
        host=host,
        enabled=True,
        original_mode="light",
        target_mode="dark",
        anchors=_anchors(req, mapping),
        params=params,
        fingerprint=req.fingerprint,
        source=source,
        updated_at=time.time(),
    )
    store.upsert(record.model_dump())

    log.info("Site theme generated", host=host, source=source, params=params,
             palette_size=len(req.palette), fixes=fixes, seconds=round(time.time() - started, 1))
    return record


async def process_get_theme(host: str) -> ThemeRecord | None:
    row = store.get(host.strip().lower())
    return ThemeRecord(**row) if row else None


async def process_list_themes() -> ListThemesResponse:
    return ListThemesResponse(themes=[ThemeRecord(**r) for r in store.list_all()])


async def process_delete_theme(host: str) -> dict:
    existed = store.delete(host.strip().lower())
    return {"status": "deleted", "existed": existed}
/**
 * theme_engine.js
 * ----------------
 * Page side of the Dark Theme feature. Runs at document_start on every page,
 * completely separate from content_script.js (the writing tools).
 *
 *   1. APPLY — if chrome.storage.local holds an enabled theme for this
 *      hostname (a mirror of the backend's SQLite ThemeStore, kept in
 *      sync by background.js and the side panel), recolour the page as
 *      early as possible.
 *   2. MEASURE — when the side panel asks, measure the ORIGINAL page:
 *      area per background colour, text weight per text colour, border
 *      usage, surface depth, and which text sits on which background.
 *      Only that compact summary is ever sent to the backend.
 *
 * How colours are rewritten:
 *   - Every stylesheet is walked (cross-origin ones are fetched through
 *     background.js). Each rule that sets a colour gets a twin rule with
 *     the same selector and the remapped colour, all in one <style> kept
 *     as the last child of <html>. Same selector + later position = it
 *     wins, and :hover / :focus / @media variants keep working because
 *     they get twins of their own.
 *   - CSS custom properties are remapped the same way, so design-token
 *     sites change at the source.
 *   - Inline style="" colours are remapped per element and restored
 *     exactly when the theme is switched off.
 *   - Mapping a colour: find the nearest colour the AI designed for the
 *     same role (surface / text / border) and inherit that anchor's
 *     change, preserving small differences between shades. Colours far
 *     from every anchor fall back to a role-aware OKLCH remap that keeps
 *     hue. Images, video and canvas are never touched.
 */

(() => {
  // Dev reloads / re-injection: tear the previous instance down first so
  // two engines never fight over the same page.
  const previous = window.__sicilyThemeEngine;
  if (previous && typeof previous.teardown === "function") {
    try { previous.teardown(); } catch (_) { /* old extension context may be gone */ }
  }

  const HOST = location.hostname;
  if (!HOST) return;

  const STORAGE_KEY = `sicily-theme:${HOST}`;
  const STYLE_ID = "sicily-theme-style";
  const EARLY_STYLE_ID = "sicily-theme-early";

  // ══════════════════════════════════════════════════════════════════
  // 1. Colour parsing & OKLab maths
  // ══════════════════════════════════════════════════════════════════

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const NON_COLORS = new Set([
    "", "none", "transparent", "currentcolor", "inherit", "initial",
    "unset", "revert", "revert-layer", "auto",
  ]);

  const toLin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const fromLin = (c) => {
    c = Math.max(0, c);
    return (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055) * 255;
  };

  function rgbToOklab({ r, g, b }) {
    const lr = toLin(r), lg = toLin(g), lb = toLin(b);
    const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
    const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
    const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
    return {
      L: 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
      a: 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
      b: 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    };
  }

  function oklabToLinear({ L, a, b }) {
    const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
    const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
    const s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
    return [
      4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
      -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
      -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    ];
  }

  function linearToRgb([r, g, b]) {
    return { r: Math.round(clamp(fromLin(r), 0, 255)), g: Math.round(clamp(fromLin(g), 0, 255)), b: Math.round(clamp(fromLin(b), 0, 255)) };
  }

  function labToLch({ L, a, b }) {
    return { L, C: Math.hypot(a, b), H: ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360 };
  }

  const toOklch = (rgb) => labToLch(rgbToOklab(rgb));

  // OKLCH -> sRGB, shrinking chroma (never lightness or hue) to fit gamut.
  function oklchToRgb({ L, C, H }) {
    L = clamp(L, 0, 1);
    C = Math.max(0, C || 0);
    const hr = ((H || 0) * Math.PI) / 180;
    const cos = Math.cos(hr), sin = Math.sin(hr);
    const lin = (c) => oklabToLinear({ L, a: c * cos, b: c * sin });
    const fits = (v) => v.every((x) => x >= -0.0005 && x <= 1.0005);
    if (!fits(lin(C))) {
      let lo = 0, hi = C;
      for (let i = 0; i < 18; i++) {
        const mid = (lo + hi) / 2;
        if (fits(lin(mid))) lo = mid; else hi = mid;
      }
      C = lo;
    }
    return linearToRgb(lin(C));
  }

  const okDist = (p, q) => Math.hypot(p.L - q.L, p.a - q.a, p.b - q.b);

  const hex2 = (n) => n.toString(16).padStart(2, "0");
  const toHex = ({ r, g, b }) => `#${hex2(r)}${hex2(g)}${hex2(b)}`;
  const fmt = (c) => (c.a == null || c.a >= 0.999 ? toHex(c) : `rgba(${c.r}, ${c.g}, ${c.b}, ${+c.a.toFixed(3)})`);

  function parseDirect(s) {
    let m;
    if (s[0] === "#") {
      let h = s.slice(1);
      if (!/^[0-9a-f]+$/.test(h)) return null;
      if (h.length === 3 || h.length === 4) h = [...h].map((c) => c + c).join("");
      if (h.length !== 6 && h.length !== 8) return null;
      return {
        r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16),
        a: h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1,
      };
    }
    if ((m = s.match(/^rgba?\(([^)]*)\)$/))) {
      const p = m[1].replace(/[,/]/g, " ").split(/\s+/).filter(Boolean);
      if (p.length < 3) return null;
      const ch = (v) => (v.endsWith("%") ? parseFloat(v) * 2.55 : parseFloat(v));
      const a = p[3] != null ? (p[3].endsWith("%") ? parseFloat(p[3]) / 100 : parseFloat(p[3])) : 1;
      const out = { r: ch(p[0]), g: ch(p[1]), b: ch(p[2]), a };
      if ([out.r, out.g, out.b, out.a].some(Number.isNaN)) return null;
      return { r: Math.round(clamp(out.r, 0, 255)), g: Math.round(clamp(out.g, 0, 255)), b: Math.round(clamp(out.b, 0, 255)), a: clamp(out.a, 0, 1) };
    }
    if ((m = s.match(/^okl(ch|ab)\(([^)]*)\)$/))) {
      const p = m[2].replace(/\//g, " ").split(/\s+/).filter(Boolean);
      if (p.length < 3) return null;
      const num = (v, pctScale) => (v.endsWith("%") ? (parseFloat(v) / 100) * pctScale : parseFloat(v));
      const L = num(p[0], 1);
      if (Number.isNaN(L)) return null;
      const a = p[3] != null ? (p[3].endsWith("%") ? parseFloat(p[3]) / 100 : parseFloat(p[3])) : 1;
      let lab;
      if (m[1] === "ch") {
        const C = num(p[1], 0.4) || 0;
        const H = ((parseFloat(p[2]) || 0) * Math.PI) / 180;
        lab = { L, a: C * Math.cos(H), b: C * Math.sin(H) };
      } else {
        lab = { L, a: num(p[1], 0.4) || 0, b: num(p[2], 0.4) || 0 };
      }
      return { ...linearToRgb(oklabToLinear(lab)), a: Number.isNaN(a) ? 1 : clamp(a, 0, 1) };
    }
    if ((m = s.match(/^color\(srgb\s+([^)]*)\)$/))) {
      const p = m[1].replace(/\//g, " ").split(/\s+/).filter(Boolean);
      if (p.length < 3) return null;
      const c = p.slice(0, 3).map((v) => Math.round(clamp(parseFloat(v) * 255, 0, 255)));
      if (c.some(Number.isNaN)) return null;
      return { r: c[0], g: c[1], b: c[2], a: p[3] != null ? clamp(parseFloat(p[3]), 0, 1) : 1 };
    }
    return null;
  }

  // Named colours, hsl(), hwb() etc. are normalised by the browser itself.
  let probeCtx = null;
  function probeParse(s) {
    try {
      if (!probeCtx) {
        const c = document.createElement("canvas");
        c.width = c.height = 1;
        probeCtx = c.getContext("2d");
      }
      probeCtx.fillStyle = "#010203";
      probeCtx.fillStyle = s;
      const norm = String(probeCtx.fillStyle).toLowerCase();
      if (norm === "#010203") return null;
      return parseDirect(norm);
    } catch (_) {
      return null;
    }
  }

  const parseCache = new Map();
  function parseColor(input) {
    if (input == null) return null;
    const s = String(input).trim().toLowerCase();
    if (NON_COLORS.has(s) || s.includes("var(")) return null;
    if (parseCache.has(s)) return parseCache.get(s);
    const out = parseDirect(s) || probeParse(s);
    if (parseCache.size > 8000) parseCache.clear();
    parseCache.set(s, out);
    return out;
  }

  // Composite a (possibly translucent) colour over an opaque one.
  function blend(fg, bg) {
    const a = fg.a == null ? 1 : fg.a;
    return {
      r: Math.round(fg.r * a + bg.r * (1 - a)),
      g: Math.round(fg.g * a + bg.g * (1 - a)),
      b: Math.round(fg.b * a + bg.b * (1 - a)),
      a: 1,
    };
  }

  // ══════════════════════════════════════════════════════════════════
  // 2. The mapper: (colour, role) -> themed colour
  // ══════════════════════════════════════════════════════════════════

  // Mirrors algorithmic_map() in Site_Theme.py — keep the two in sync.
  // Role-aware light -> dark remap that keeps hue. Mirrors
  // algorithmic_map() in Site_Theme.py — keep the two in sync.
  function algorithmic({ L, C, H }, role) {
    const accent = C > 0.08;
    if (role === "bg") {
      if (accent) return { L: Math.min(L, 0.62), C: C * 0.9, H };
      if (L >= 0.5) return { L: 0.16 + (1 - L) * 0.22, C: Math.min(C, 0.025), H };
      return { L, C, H };
    }
    if (role === "text") {
      if (accent) return { L: Math.max(L, 0.72), C, H };
      if (L < 0.6) return { L: 0.95 - L * 0.45, C: Math.min(C, 0.03), H };
      return { L, C, H };
    }
    if (accent) return { L: Math.max(L, 0.6), C, H };
    if (L >= 0.5) return { L: 0.30 + (1 - L) * 0.25, C: Math.min(C, 0.03), H };
    return { L, C, H };
  }

  const hueDelta = (a, b) => ((a - b + 540) % 360) - 180;

  // Carry a nearby colour along with its anchor, keeping the small
  // difference between them (direction flipped if the anchor inverted).
  function fromAnchor(lch, anchor) {
    const { src, dst } = anchor;
    const inverted = (src.L - 0.5) * (dst.L - 0.5) < 0;
    const L = dst.L + (lch.L - src.L) * (inverted ? -1 : 1);
    const C = Math.max(0, dst.C + (lch.C - src.C));
    let H;
    if (src.C < 0.02 || dst.C < 0.02) H = dst.C < 0.02 ? lch.H : dst.H;
    else H = (dst.H + hueDelta(lch.H, src.H) + 360) % 360;
    return { L, C, H };
  }

  function mixLch(a, b, t) {
    let H;
    if (a.C < 0.02) H = b.H;
    else if (b.C < 0.02) H = a.H;
    else H = (a.H + hueDelta(b.H, a.H) * t + 360) % 360;
    return { L: a.L + (b.L - a.L) * t, C: a.C + (b.C - a.C) * t, H };
  }

  const PROP_ROLES = [
    ["color", "text"], ["-webkit-text-fill-color", "text"], ["caret-color", "text"],
    ["text-decoration-color", "text"], ["text-emphasis-color", "text"], ["text-shadow", "text"],
    ["fill", "text"], ["stroke", "text"],
    ["background-color", "bg"], ["background-image", "bg"],
    ["border-top-color", "border"], ["border-right-color", "border"],
    ["border-bottom-color", "border"], ["border-left-color", "border"],
    ["outline-color", "border"], ["column-rule-color", "border"], ["box-shadow", "border"],
  ];

  const COLOR_TOKEN = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|oklab|oklch|color)\([^()]*\)|\b(?:white|black|silver|gray|grey|whitesmoke|gainsboro|lightgray|lightgrey|darkgray|darkgrey|dimgray|dimgrey|snow|ivory|beige|linen|aliceblue|ghostwhite|azure|mintcream|honeydew|seashell|floralwhite|oldlace|lavender|red|blue|green|navy|teal|orange|purple)\b/g;
  const TRIPLET = /^\s*(\d{1,3})(\s*,\s*|\s+)(\d{1,3})\2(\d{1,3})\s*$/;

  function makeMapper(record) {
    const target = "dark";
    const anchors = { bg: [], text: [], border: [] };
    let pageBg = null, pageBgShare = -1;
    let pageText = null, pageTextShare = -1;

    for (const a of record.anchors || []) {
      const s = parseColor(a.src), d = parseColor(a.dst);
      if (!s || !d || !anchors[a.role]) continue;
      anchors[a.role].push({ srcLab: rgbToOklab(s), src: toOklch(s), dst: toOklch(d) });
      if (a.role === "bg" && (a.share || 0) > pageBgShare) {
        pageBgShare = a.share || 0;
        pageBg = toHex(d);
      }
      if (a.role === "text" && (a.share || 0) > pageTextShare) {
        pageTextShare = a.share || 0;
        pageText = toHex(d);
      }
    }
    if (!pageBg) pageBg = "#16181d";
    if (!pageText) pageText = "#e6e8ee";

    const NEAR = 0.05, FAR = 0.14;
    const cache = new Map();

    function mapRgb(rgb, role) {
      const lab = rgbToOklab(rgb);
      const lch = labToLch(lab);
      const algo = algorithmic(lch, role);
      let best = null, bestD = Infinity;
      for (const a of anchors[role] || []) {
        const d = okDist(lab, a.srcLab);
        if (d < bestD) { bestD = d; best = a; }
      }
      let out;
      if (!best || bestD >= FAR) out = algo;
      else if (bestD <= NEAR) out = fromAnchor(lch, best);
      else out = mixLch(fromAnchor(lch, best), algo, (bestD - NEAR) / (FAR - NEAR));
      return oklchToRgb(out);
    }

    function mapColorString(str, role) {
      const key = role + "|" + str;
      if (cache.has(key)) return cache.get(key);
      const c = parseColor(str);
      const out = c ? fmt({ ...mapRgb(c, role), a: c.a }) : null;
      if (cache.size > 20000) cache.clear();
      cache.set(key, out);
      return out;
    }

    // Remap every colour inside a CSS value (gradients, shadows…), never
    // touching anything inside url(...). Returns null when nothing changed.
    function mapValue(value, role) {
      if (!value) return null;
      let changed = false;
      const parts = value.split(/(url\([^)]*\))/i);
      const out = parts.map((part, i) => {
        if (i % 2 === 1) return part;
        return part.replace(COLOR_TOKEN, (tok) => {
          const m = mapColorString(tok, role);
          if (m && m.toLowerCase() !== tok.toLowerCase()) { changed = true; return m; }
          return tok;
        });
      }).join("");
      return changed ? out : null;
    }

    // Bootstrap-style "255, 255, 255" channel triplets used as rgb(var(--x)).
    function mapTriplet(value, role) {
      const m = value.match(TRIPLET);
      if (!m) return null;
      const rgb = { r: +m[1], g: +m[3], b: +m[4] };
      if (rgb.r > 255 || rgb.g > 255 || rgb.b > 255) return null;
      const o = mapRgb(rgb, role);
      return [o.r, o.g, o.b].join(m[2]);
    }

    function sampleColor(value) {
      const t = value.match(TRIPLET);
      if (t) return { r: +t[1], g: +t[3], b: +t[4], a: 1 };
      const tok = value.match(COLOR_TOKEN);
      return tok ? parseColor(tok[0]) : null;
    }

    // Guess what a custom property is FOR from its name, then its value.
    function roleForCustomProp(name, value) {
      const sample = sampleColor(value);
      if (!sample) return null;
      const n = name.toLowerCase();
      if (/(border|outline|divider|separator|stroke|ring|rule|hairline)/.test(n)) return "border";
      if (/(^|-)(bg|background|backdrop)(-|$)/.test(n)) return "bg";
      if (/(^|-)(text|fg|foreground|ink|font|heading|link|placeholder|caption|muted-foreground|on)(-|$)/.test(n)) return "text";
      if (/(surface|canvas|card|panel|paper|layer|overlay|sheet|page|well|popover|modal|elevation|base|body-bg|sidebar|navbar|header-bg|menu)/.test(n)) return "bg";
      const lch = toOklch(sample);
      if (/-color$/.test(n) && lch.C <= 0.08) return "text";
      if (lch.C > 0.08) return "bg"; // ambiguous accent: smallest change
      return lch.L >= 0.5 ? "bg" : "text";
    }

    return { target, pageBg, pageText, mapRgb, mapValue, mapTriplet, roleForCustomProp };
  }

  // ══════════════════════════════════════════════════════════════════
  // 2b. Site adapters — for apps that paint content where CSS can't reach
  // ══════════════════════════════════════════════════════════════════

  // Google Docs draws the document page onto <canvas> tiles, so no colour
  // rule can touch the text. Those tiles get a filter instead: invert turns
  // the white page dark and black text light, and hue-rotate(180deg) swings
  // coloured text, links and highlights back to roughly their own hues.
  // (Images inside the document are drawn on the same canvas, so they are
  // inverted too — the same trade-off every Docs dark mode makes.)
  // The text caret is a DOM element whose colour Docs sets to black; it is
  // repainted in the theme's text colour so it stays visible.
  const SITE_ADAPTERS = [
    {
      matches: () => location.hostname === "docs.google.com" && location.pathname.startsWith("/document/"),
      css: (m) =>
        `canvas.kix-canvas-tile-content{filter:invert(0.9) hue-rotate(180deg)!important}` +
        `.kix-cursor-caret{border-color:${m.pageText}!important}`,
    },
  ];

  function adapterCss() {
    if (!mapper) return "";
    let css = "";
    for (const a of SITE_ADAPTERS) {
      try { if (a.matches()) css += a.css(mapper); } catch (_) { /* never break theming */ }
    }
    return css;
  }

  // ══════════════════════════════════════════════════════════════════
  // 3. Stylesheet rewriting
  // ══════════════════════════════════════════════════════════════════

  let active = false;
  let mapper = null;
  let mapperId = 0;
  let pendingRemote = 0;

  const sheetCache = new WeakMap();   // CSSStyleSheet -> { sig, mapperId, css }
  const remoteSheets = new Map();     // href -> { status, sheet, css, mapperId }

  // Global design tokens (custom properties declared on :root / html).
  // A token like --brand is often used both as text (a logo, links) and
  // as a background (buttons). Remapping the token alone can only give it
  // ONE role, so a property that is exactly `var(--token)` is also
  // resolved and remapped for its own role at the point of use.
  let rootVars = new Map();
  let rootVarsSig = "";
  const ROOT_SELECTOR = /^\s*(:root|html|:root\s*,\s*html|html\s*,\s*:root)\s*$/i;
  const SOLE_VAR = /^\s*var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)\s*$/;

  function collectRootVars() {
    const vars = new Map();
    const scan = (rules) => {
      for (const r of rules) {
        if (r instanceof CSSStyleRule && ROOT_SELECTOR.test(r.selectorText)) {
          for (let i = 0; i < r.style.length; i++) {
            const n = r.style[i];
            if (n.startsWith("--")) vars.set(n, r.style.getPropertyValue(n).trim());
          }
        }
      }
    };
    for (const sheet of allSheets()) {
      if (isOwnSheet(sheet)) continue;
      try { scan(sheet.cssRules); } catch (_) {
        const e = sheet.href && remoteSheets.get(sheet.href);
        if (e && e.sheet) scan(e.sheet.cssRules);
      }
    }
    rootVars = vars;
    rootVarsSig = [...vars].map(([k, v]) => k + ":" + v).join(";");
  }

  function resolveSoleVar(value, depth = 0) {
    const m = value.match(SOLE_VAR);
    if (!m || depth > 5) return null;
    const v = rootVars.get(m[1]) ?? (m[2] != null ? m[2].trim() : null);
    if (v == null) return null;
    return SOLE_VAR.test(v) ? resolveSoleVar(v, depth + 1) : v;
  }

  function declsFor(style) {
    const parts = [];
    for (const [prop, role] of PROP_ROLES) {
      const v = style.getPropertyValue(prop);
      if (!v) continue;
      let m = mapper.mapValue(v, role);
      if (!m && SOLE_VAR.test(v)) {
        const resolved = resolveSoleVar(v);
        if (resolved) m = mapper.mapValue(resolved, role);
      }
      if (m) parts.push(`${prop}:${m}${style.getPropertyPriority(prop) ? " !important" : ""}`);
    }
    for (let i = 0; i < style.length; i++) {
      const name = style[i];
      if (!name || !name.startsWith("--")) continue;
      const v = style.getPropertyValue(name);
      if (!v) continue;
      const role = mapper.roleForCustomProp(name, v);
      if (!role) continue;
      const m = mapper.mapTriplet(v, role) ?? mapper.mapValue(v, role);
      if (m && m !== v) parts.push(`${name}:${m}${style.getPropertyPriority(name) ? " !important" : ""}`);
    }
    return parts.join(";");
  }

  function rulesToCss(rules, out) {
    for (const rule of rules) {
      try {
        if (rule instanceof CSSStyleRule) {
          const decls = declsFor(rule.style);
          if (decls) out.push(`${rule.selectorText}{${decls}}`);
        } else if (rule instanceof CSSMediaRule) {
          const inner = [];
          rulesToCss(rule.cssRules, inner);
          if (inner.length) out.push(`@media ${rule.conditionText || rule.media.mediaText}{${inner.join("")}}`);
        } else if (rule instanceof CSSSupportsRule) {
          const inner = [];
          rulesToCss(rule.cssRules, inner);
          if (inner.length) out.push(`@supports ${rule.conditionText}{${inner.join("")}}`);
        } else if (typeof CSSContainerRule !== "undefined" && rule instanceof CSSContainerRule) {
          const inner = [];
          rulesToCss(rule.cssRules, inner);
          if (inner.length) out.push(`@container ${rule.conditionText}{${inner.join("")}}`);
        } else if (typeof CSSLayerBlockRule !== "undefined" && rule instanceof CSSLayerBlockRule) {
          // Emitted unlayered on purpose: unlayered beats layered.
          rulesToCss(rule.cssRules, out);
        } else if (rule instanceof CSSImportRule && rule.styleSheet) {
          try { rulesToCss(rule.styleSheet.cssRules, out); } catch (_) { /* cross-origin import */ }
        }
      } catch (_) {
        // One odd rule never breaks the whole sheet.
      }
    }
  }

  function isOwnSheet(sheet) {
    const id = sheet.ownerNode && sheet.ownerNode.id;
    return id === STYLE_ID || id === EARLY_STYLE_ID;
  }

  function remoteSheetCss(href) {
    if (!href) return "";
    let entry = remoteSheets.get(href);
    if (!entry) {
      entry = { status: "pending", sheet: null, css: "", mapperId: -1 };
      remoteSheets.set(href, entry);
      pendingRemote++;
      try {
        chrome.runtime.sendMessage({ type: "sicily-theme-fetch-css", url: href }, (resp) => {
          pendingRemote = Math.max(0, pendingRemote - 1);
          void chrome.runtime.lastError;
          if (resp && resp.ok && typeof resp.text === "string") {
            try {
              const s = new CSSStyleSheet();
              s.replaceSync(resp.text);
              entry.sheet = s;
              entry.status = "done";
            } catch (_) {
              entry.status = "failed";
            }
          } else {
            entry.status = "failed";
          }
          scheduleRebuild();
        });
      } catch (_) {
        pendingRemote = Math.max(0, pendingRemote - 1);
        entry.status = "failed";
      }
      return "";
    }
    if (entry.status !== "done") return "";
    const stamp = mapperId + "|" + rootVarsSig;
    if (entry.mapperId !== stamp) {
      const out = [];
      rulesToCss(entry.sheet.cssRules, out);
      entry.css = out.join("\n");
      entry.mapperId = stamp;
    }
    return entry.css;
  }

  function sheetCss(sheet) {
    if (!sheet || isOwnSheet(sheet) || sheet.disabled) return "";
    let rules;
    try {
      rules = sheet.cssRules;
    } catch (_) {
      return remoteSheetCss(sheet.href);
    }
    if (!rules) return "";
    const sig = rules.length;
    const stamp = mapperId + "|" + rootVarsSig;
    const cached = sheetCache.get(sheet);
    if (cached && cached.sig === sig && cached.mapperId === stamp) return cached.css;
    const out = [];
    rulesToCss(rules, out);
    let css = out.join("\n");
    const media = sheet.media && sheet.media.mediaText;
    if (css && media && media !== "all") css = `@media ${media}{${css}}`;
    sheetCache.set(sheet, { sig, mapperId: stamp, css });
    return css;
  }

  function allSheets() {
    const list = [];
    try { for (const s of document.styleSheets) list.push(s); } catch (_) {}
    try { for (const s of document.adoptedStyleSheets || []) list.push(s); } catch (_) {}
    return list;
  }

  function buildCss() {
    collectRootVars();
    const chunks = [
      `:root{color-scheme:${mapper.target}}`,
      `html{background-color:${mapper.pageBg}}`,
      adapterCss(),
    ];
    for (const sheet of allSheets()) {
      const css = sheetCss(sheet);
      if (css) chunks.push(css);
    }
    return chunks.join("\n");
  }

  function keepLast(el) {
    const root = document.documentElement;
    if (!root) return;
    if (el.parentNode !== root || root.lastElementChild !== el) root.appendChild(el);
  }

  function writeStyle(css) {
    let el = document.getElementById(STYLE_ID);
    if (!el) {
      el = document.createElement("style");
      el.id = STYLE_ID;
    }
    if (el.textContent !== css) el.textContent = css;
    keepLast(el);
  }

  // At document_start <html> may not exist yet. Never append to `document`
  // itself (that would make our <style> the root element and break the
  // page) — wait for the root instead.
  function whenRoot(fn) {
    if (document.documentElement) { fn(); return; }
    const mo = new MutationObserver(() => {
      if (document.documentElement) { mo.disconnect(); fn(); }
    });
    mo.observe(document, { childList: true });
  }

  // Before stylesheets are processed, paint the page background in the
  // themed colour so there's no white flash during load.
  function insertEarlyStyle() {
    whenRoot(() => {
      if (!active || !mapper || document.getElementById(EARLY_STYLE_ID)) return;
      const el = document.createElement("style");
      el.id = EARLY_STYLE_ID;
      el.textContent =
        `html{background-color:${mapper.pageBg}!important;color-scheme:${mapper.target}}` +
        `body{background-color:${mapper.pageBg}!important}` +
        adapterCss();
      document.documentElement.appendChild(el);
    });
  }

  function maybeDropEarlyStyle() {
    const el = document.getElementById(EARLY_STYLE_ID);
    if (el && document.readyState !== "loading" && pendingRemote === 0) el.remove();
  }

  let rebuildQueued = false;
  function scheduleRebuild() {
    if (!active || rebuildQueued) return;
    rebuildQueued = true;
    setTimeout(runRebuild, document.readyState === "loading" ? 0 : 40);
  }

  function runRebuild() {
    rebuildQueued = false;
    if (!active || !mapper) return;
    if (!document.documentElement) { whenRoot(scheduleRebuild); return; }
    try {
      writeStyle(buildCss());
    } catch (err) {
      console.warn("[Sicily Theme] rebuild failed", err);
    }
    maybeDropEarlyStyle();
    queueFix(null);
  }

  // ══════════════════════════════════════════════════════════════════
  // 4. Inline style="" colours
  // ══════════════════════════════════════════════════════════════════

  const inlineState = new WeakMap();  // el -> Map(prop -> { orig, mapped, prio, mprio })
  const touchedInline = new Set();
  const inlineQueue = new Set();
  let inlineQueued = false;

  function queueInline(root) {
    if (!active || !root || root.nodeType !== 1) return;
    if (root.hasAttribute && root.hasAttribute("style")) inlineQueue.add(root);
    if (root.querySelectorAll) root.querySelectorAll("[style]").forEach((el) => inlineQueue.add(el));
    if (!inlineQueued && inlineQueue.size) {
      inlineQueued = true;
      setTimeout(flushInline, document.readyState === "loading" ? 0 : 16);
    }
  }

  function flushInline() {
    inlineQueued = false;
    if (!active) { inlineQueue.clear(); return; }
    const batch = [...inlineQueue];
    inlineQueue.clear();
    pauseObserver();
    try { for (const el of batch) processInline(el); } finally { resumeObserver(); }
  }

  function processInline(el) {
    const st = el.style;
    if (!st || !st.length) return;
    let state = inlineState.get(el);
    const props = [];
    for (const [prop, role] of PROP_ROLES) props.push([prop, role]);
    for (let i = 0; i < st.length; i++) if (st[i].startsWith("--")) props.push([st[i], null]);

    for (const [prop, fixedRole] of props) {
      const cur = st.getPropertyValue(prop);
      if (!cur) continue;
      const prev = state && state.get(prop);
      if (prev && prev.mapped === cur) continue; // already ours
      const role = fixedRole || mapper.roleForCustomProp(prop, cur);
      if (!role) continue;
      const m = fixedRole ? mapper.mapValue(cur, role) : (mapper.mapTriplet(cur, role) ?? mapper.mapValue(cur, role));
      if (!m) continue;
      const prio = st.getPropertyPriority(prop);
      st.setProperty(prop, m, prio);
      if (!state) { state = new Map(); inlineState.set(el, state); }
      state.set(prop, { orig: cur, mapped: st.getPropertyValue(prop), prio, mprio: prio });
      touchedInline.add(el);
    }
  }

  function restoreInline() {
    for (const el of touchedInline) {
      const state = inlineState.get(el);
      if (!state) continue;
      for (const [prop, rec] of state) {
        if (el.style.getPropertyValue(prop) === rec.mapped) el.style.setProperty(prop, rec.orig, rec.prio);
      }
      inlineState.delete(el);
    }
    touchedInline.clear();
  }

  function reapplyInline() {
    for (const el of touchedInline) {
      const state = inlineState.get(el);
      if (!state) continue;
      for (const [prop, rec] of state) {
        if (el.style.getPropertyValue(prop) === rec.orig) {
          el.style.setProperty(prop, rec.mapped, rec.mprio);
          rec.mapped = el.style.getPropertyValue(prop);
        }
      }
    }
  }


  // ══════════════════════════════════════════════════════════════════
  // 4b. Safety net: fix what is actually on screen
  // ══════════════════════════════════════════════════════════════════
  // The stylesheet pass can only recolour what it can read. Big apps load
  // styles it can't always reach (cross-origin sheets that fail to fetch,
  // styles set from script, …), and whatever it misses stays light. So
  // after each pass, the rendered page is checked: any element still
  // painted with a light background is darkened, and any text or icon
  // without enough contrast against what is really behind it is fixed.
  // Overrides are inline !important and restored exactly on removal.

  const NET_SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "IMG", "VIDEO", "CANVAS", "IFRAME", "PICTURE", "OBJECT", "EMBED", "AUDIO", "BR", "HEAD", "META", "LINK"]);
  const SVG_PARTS = "path,circle,rect,polygon,polyline,ellipse,line,use,text";
  const NET_MAX = 15000;

  function luminance({ r, g, b }) {
    return 0.2126 * toLin(r) + 0.7152 * toLin(g) + 0.0722 * toLin(b);
  }

  function contrastRatio(a, b) {
    const la = luminance(a), lb = luminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }

  // Move a colour's lightness (hue and chroma kept) away from the
  // background until it reaches the needed contrast.
  function withContrast(rgb, bg, need) {
    const lch = toOklch(rgb);
    const up = toOklch(bg).L < 0.5;
    let out = rgb;
    for (let i = 0; i < 40 && contrastRatio(out, bg) < need; i++) {
      lch.L = clamp(lch.L + (up ? 0.03 : -0.03), 0, 1);
      out = oklchToRgb(lch);
      if (lch.L === 0 || lch.L === 1) break;
    }
    return out;
  }

  function setForced(el, prop, value) {
    const st = el.style;
    if (!st) return;
    let state = inlineState.get(el);
    const prev = state && state.get(prop);
    const orig = prev ? prev.orig : st.getPropertyValue(prop);
    const prio = prev ? prev.prio : st.getPropertyPriority(prop);
    st.setProperty(prop, value, "important");
    if (!state) { state = new Map(); inlineState.set(el, state); }
    state.set(prop, { orig, mapped: st.getPropertyValue(prop), prio, mprio: "important" });
    touchedInline.add(el);
  }

  function hasOwnText(el) {
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return true;
    for (const n of el.childNodes) if (n.nodeType === 3 && n.data.trim()) return true;
    return false;
  }

  // What is actually painted behind an element.
  function backdropOf(el) {
    const chain = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const c = parseColor(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.05) { chain.push(c); if (c.a >= 0.99) break; }
    }
    let bg = parseColor(mapper.pageBg) || { r: 22, g: 24, b: 29, a: 1 };
    for (let i = chain.length - 1; i >= 0; i--) bg = blend(chain[i], bg);
    return bg;
  }

  // A colour that fails on its new background: first take the theme's own
  // designed colour for it (keeps hierarchy: body text bright, muted text
  // muted, links brand-coloured), and only nudge further if still needed.
  function readableOn(c, bg, need) {
    const designed = { ...mapper.mapRgb(c, "text"), a: c.a };
    const solid = blend(designed, bg);
    if (contrastRatio(solid, bg) >= need) return designed;
    return { ...withContrast(solid, bg, need), a: 1 };
  }

  function fixSvg(svg, bg) {
    const parts = [svg, ...svg.querySelectorAll(SVG_PARTS)].slice(0, 300);
    for (const node of parts) {
      const cs = getComputedStyle(node);
      for (const prop of ["fill", "stroke"]) {
        const c = parseColor(cs[prop]);
        if (!c || c.a < 0.05) continue;
        if (contrastRatio(blend(c, bg), bg) >= 3) continue;
        setForced(node, prop, fmt(readableOn(c, bg, 3)));
      }
    }
  }

  function fixTree(root, parentBg) {
    let count = 0;
    const visit = (el, under) => {
      if (count++ > NET_MAX || NET_SKIP.has(el.tagName) || el.id === STYLE_ID) return;
      const cs = getComputedStyle(el);
      if (cs.display === "none") return;

      let bg = under;
      const own = parseColor(cs.backgroundColor);
      if (own && own.a > 0.05) {
        let eff = blend(own, under);
        const lch = toOklch(eff);
        const tooLight = lch.C > 0.08 ? lch.L > 0.8 : lch.L > 0.6;
        if (tooLight && own.a > 0.3) {
          const dark = { ...mapper.mapRgb(own, "bg"), a: own.a };
          setForced(el, "background-color", fmt(dark));
          eff = blend(dark, under);
        }
        bg = eff;
      }

      // Light borders and outline-style shadows left from the original look.
      for (const side of ["top", "right", "bottom", "left"]) {
        if (!(parseFloat(cs[`border-${side}-width`]) > 0)) continue;
        const c = parseColor(cs[`border-${side}-color`]);
        if (c && c.a > 0.05 && toOklch(c).L > 0.6) setForced(el, `border-${side}-color`, fmt({ ...mapper.mapRgb(c, "border"), a: c.a }));
      }
      if (cs.boxShadow && cs.boxShadow !== "none") {
        const lightShadow = (cs.boxShadow.match(COLOR_TOKEN) || []).some((t) => { const c = parseColor(t); return c && c.a > 0.05 && toOklch(c).L > 0.6; });
        if (lightShadow) { const m = mapper.mapValue(cs.boxShadow, "border"); if (m) setForced(el, "box-shadow", m); }
      }

      if (hasOwnText(el)) {
        const fg = parseColor(cs.color);
        if (fg && fg.a > 0.05) {
          // Dimmed / disabled text keeps a lower bar so it still reads as dimmed.
          const need = fg.a < 0.7 ? 3 : 4.5;
          if (contrastRatio(blend(fg, bg), bg) < need) setForced(el, "color", fmt(readableOn(fg, bg, need)));
        }
      }

      if (el.tagName.toLowerCase() === "svg") { fixSvg(el, bg); return; }
      for (const child of el.children) visit(child, bg);
    };
    visit(root, parentBg);
  }

  const fixRoots = new Set();
  let fixAll = false;
  let fixQueued = false;

  // null = whole page; an element = just that subtree.
  function queueFix(root) {
    if (!active) return;
    if (root) { if (root.nodeType === 1) fixRoots.add(root); } else fixAll = true;
    if (!fixQueued) {
      fixQueued = true;
      setTimeout(runFix, fixAll ? 200 : 60);
    }
  }

  function runFix() {
    fixQueued = false;
    if (!active || !mapper || !document.body) { fixRoots.clear(); return; }
    const roots = fixAll ? [document.body] : [...fixRoots].filter((r) => r.isConnected).slice(0, 200);
    fixAll = false;
    fixRoots.clear();
    pauseObserver();
    try {
      for (const r of roots) fixTree(r, r === document.body ? backdropOf(document.documentElement) : backdropOf(r.parentElement || r));
    } catch (err) {
      console.warn("[Sicily Theme] safety net failed", err);
    } finally {
      resumeObserver();
    }
  }

  // ══════════════════════════════════════════════════════════════════
  // 5. Watching the page
  // ══════════════════════════════════════════════════════════════════

  let observer = null;
  let pollTimer = null;
  let lastSig = "";

  const isSheetNode = (n) => n.nodeType === 1 && (n.tagName === "STYLE" || (n.tagName === "LINK" && /stylesheet/i.test(n.rel || "")));

  function onMutations(muts) {
    let sheetsChanged = false;
    for (const m of muts) {
      if (m.type === "childList") {
        if (m.target === document.documentElement) {
          const own = document.getElementById(STYLE_ID);
          if (own && document.documentElement.lastElementChild !== own) sheetsChanged = true;
        }
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1 || n.id === STYLE_ID || n.id === EARLY_STYLE_ID) continue;
          if (isSheetNode(n) || (n.tagName === "STYLE" || n.querySelector?.("style,link[rel~=stylesheet]"))) sheetsChanged = true;
          queueInline(n);
          queueFix(n);
        }
        for (const n of m.removedNodes) if (isSheetNode(n)) sheetsChanged = true;
        if (m.target && m.target.tagName === "STYLE" && m.target.id !== STYLE_ID && m.target.id !== EARLY_STYLE_ID) sheetsChanged = true;
      } else if (m.type === "attributes") {
        if (m.attributeName === "style") queueInline(m.target);
        else if (m.attributeName === "class") queueFix(m.target);
        else if (isSheetNode(m.target)) sheetsChanged = true;
      }
    }
    if (sheetsChanged) scheduleRebuild();
  }

  function resumeObserver() {
    if (!active || !observer || !document.documentElement) return;
    observer.observe(document.documentElement, {
      childList: true, subtree: true, attributes: true,
      attributeFilter: ["style", "class", "media", "disabled", "href", "rel"],
    });
  }

  function pauseObserver() {
    if (!observer) return;
    const pending = observer.takeRecords();
    observer.disconnect();
    if (pending.length) onMutations(pending.filter((m) => !(m.type === "attributes" && (m.attributeName === "style" || m.attributeName === "class"))));
  }

  const onLinkLoad = (e) => { if (e.target && e.target.tagName === "LINK") scheduleRebuild(); };
  const onReady = () => { scheduleRebuild(); queueInline(document.documentElement); setTimeout(() => document.getElementById(EARLY_STYLE_ID)?.remove(), 4000); };

  // CSS-in-JS libraries add rules with insertRule(), which fires no DOM
  // mutation. A cheap rule-count signature catches those.
  function sheetSignature() {
    let sig = 0, n = 0;
    for (const s of allSheets()) {
      n++;
      try { sig += s.cssRules ? s.cssRules.length : 0; } catch (_) {}
    }
    return n + ":" + sig;
  }

  function startWatching() {
    observer = observer || new MutationObserver(onMutations);
    whenRoot(resumeObserver);
    document.addEventListener("load", onLinkLoad, true);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", onReady, { once: true });
    lastSig = sheetSignature();
    pollTimer = setInterval(() => {
      if (!active || document.hidden) return;
      const sig = sheetSignature();
      if (sig !== lastSig) { lastSig = sig; scheduleRebuild(); }
    }, 1500);
  }

  function stopWatching() {
    if (observer) observer.disconnect();
    document.removeEventListener("load", onLinkLoad, true);
    document.removeEventListener("DOMContentLoaded", onReady);
    clearInterval(pollTimer);
    pollTimer = null;
  }

  // ══════════════════════════════════════════════════════════════════
  // 6. Enable / disable
  // ══════════════════════════════════════════════════════════════════

  function enable(record) {
    const changedMapping = active;
    mapper = makeMapper(record);
    mapperId++;
    if (!active) {
      active = true;
      if (document.readyState === "loading") insertEarlyStyle();
      startWatching();
    }
    if (changedMapping) {
      // Regenerated theme: re-map inline styles from their originals.
      pauseObserver();
      try { restoreInline(); } finally { resumeObserver(); }
    }
    queueInline(document.documentElement);
    scheduleRebuild();
  }

  function disable() {
    if (!active) return;
    active = false;
    stopWatching();
    document.getElementById(STYLE_ID)?.remove();
    document.getElementById(EARLY_STYLE_ID)?.remove();
    restoreInline();
    inlineQueue.clear();
    fixRoots.clear();
    mapper = null;
  }

  // ══════════════════════════════════════════════════════════════════
  // 7. Measurement (always of the ORIGINAL page)
  // ══════════════════════════════════════════════════════════════════

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "IMG", "VIDEO", "CANVAS", "IFRAME", "PICTURE", "SVG", "svg", "OBJECT", "EMBED", "AUDIO", "BR", "HEAD", "META", "LINK"]);
  const MAX_ELEMENTS = 9000;

  function withThemeSuspended(fn) {
    if (!active) return fn();
    pauseObserver();
    const own = document.getElementById(STYLE_ID);
    const early = document.getElementById(EARLY_STYLE_ID);
    own?.remove();
    early?.remove();
    restoreInlineTemporarily();
    try {
      return fn();
    } finally {
      reapplyInline();
      if (own) keepLast(own);
      if (early && document.documentElement) document.documentElement.appendChild(early);
      resumeObserver();
    }
  }

  function restoreInlineTemporarily() {
    for (const el of touchedInline) {
      const state = inlineState.get(el);
      if (!state) continue;
      for (const [prop, rec] of state) {
        if (el.style.getPropertyValue(prop) === rec.mapped) el.style.setProperty(prop, rec.orig, rec.prio);
      }
    }
  }

  function addStat(map, key, amount, depth) {
    let s = map.get(key);
    if (!s) { s = { v: 0, depth: depth ?? 99 }; map.set(key, s); }
    s.v += amount;
    if (depth != null && depth < s.depth) s.depth = depth;
  }

  function doMeasure() {
    const root = document.documentElement;
    const body = document.body;
    if (!root || !body) return null;

    const rootCs = getComputedStyle(root);
    const schemeDark = /dark/.test(rootCs.colorScheme || "") && !/light/.test(rootCs.colorScheme || "");
    const canvas = schemeDark ? { r: 18, g: 18, b: 18, a: 1 } : { r: 255, g: 255, b: 255, a: 1 };

    const docW = Math.max(root.scrollWidth, innerWidth);
    const docH = Math.max(root.scrollHeight, innerHeight);

    const bgStats = new Map(), textStats = new Map(), borderStats = new Map(), pairStats = new Map();

    // Page base: html background, or body's (CSS propagates body bg to the
    // canvas when html is transparent), or the UA canvas.
    const htmlBg = parseColor(rootCs.backgroundColor);
    const bodyBg = parseColor(getComputedStyle(body).backgroundColor);
    let base = canvas;
    if (htmlBg && htmlBg.a > 0.05) base = blend(htmlBg, canvas);
    else if (bodyBg && bodyBg.a > 0.05) base = blend(bodyBg, canvas);
    const baseKey = toHex(base);
    addStat(bgStats, baseKey, docW * docH, 0);

    let visited = 0;

    function visit(el, parentBg, parentKey, depth) {
      if (visited++ > MAX_ELEMENTS) return;
      if (SKIP_TAGS.has(el.tagName) || el.id === STYLE_ID) return;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return;

      const rect = el.getBoundingClientRect();
      const w = Math.max(0, Math.min(rect.width, docW));
      const h = Math.max(0, Math.min(rect.height, docH));
      const area = w * h;

      let bg = parentBg, bgKey = parentKey, d = depth;
      const own = parseColor(cs.backgroundColor);
      if (own && own.a > 0.05 && area > 0) {
        const eff = blend(own, parentBg);
        const k = toHex(eff);
        if (k !== parentKey) {
          addStat(bgStats, k, area, depth + 1);
          const parentStat = bgStats.get(parentKey);
          if (parentStat) parentStat.v = Math.max(0, parentStat.v - area);
          bg = eff; bgKey = k; d = depth + 1;
        }
      }

      if (area > 0) {
        let len = 0;
        for (const n of el.childNodes) if (n.nodeType === 3) len += n.data.trim().length;
        if (!len && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) len = (el.value || el.placeholder || "").length;
        if (len) {
          const fg = parseColor(cs.color);
          if (fg) {
            const fk = toHex(blend(fg, bg));
            const weight = len * (parseFloat(cs.fontSize) || 16);
            addStat(textStats, fk, weight);
            addStat(pairStats, fk + "|" + bgKey, weight);
          }
        }
        for (const side of ["Top", "Right", "Bottom", "Left"]) {
          const bw = parseFloat(cs[`border${side}Width`]);
          if (!(bw > 0) || cs[`border${side}Style`] === "none") continue;
          const c = parseColor(cs[`border${side}Color`]);
          if (c && c.a > 0.05) addStat(borderStats, toHex(blend(c, bg)), side === "Top" || side === "Bottom" ? w : h);
        }
      }

      for (const child of el.children) visit(child, bg, bgKey, d);
    }

    visit(body, base, baseKey, 0);

    // ── Cluster near-identical colours ──
    const sum = (m) => { let t = 0; for (const s of m.values()) t += s.v; return t || 1; };
    const totBg = sum(bgStats), totText = sum(textStats), totBorder = sum(borderStats);

    const entries = new Map();
    const entry = (hex) => {
      let e = entries.get(hex);
      if (!e) {
        const rgb = parseDirect(hex);
        e = { hex, lab: rgbToOklab(rgb), bg: 0, text: 0, border: 0, depth: 99 };
        entries.set(hex, e);
      }
      return e;
    };
    for (const [k, s] of bgStats) { const e = entry(k); e.bg += s.v / totBg; e.depth = Math.min(e.depth, s.depth); }
    for (const [k, s] of textStats) entry(k).text += s.v / totText;
    for (const [k, s] of borderStats) entry(k).border += s.v / totBorder;

    const sorted = [...entries.values()].sort((x, y) => (y.bg + y.text + y.border) - (x.bg + x.text + x.border));
    const clusters = [];
    const clusterOf = new Map();
    for (const e of sorted) {
      let target = null;
      // 0.008 merges rendering noise (#fefefe vs #fff) but keeps the
      // subtle surface steps light UIs rely on (#fafafa cards on #fff).
      for (const c of clusters) if (okDist(c.lab, e.lab) < 0.008) { target = c; break; }
      if (!target) {
        target = { hex: e.hex, lab: e.lab, bg: 0, text: 0, border: 0, depth: 99, members: [] };
        clusters.push(target);
      }
      target.bg += e.bg; target.text += e.text; target.border += e.border;
      target.depth = Math.min(target.depth, e.depth);
      target.members.push(e.hex);
      clusterOf.set(e.hex, target);
    }

    const kept = clusters
      .filter((c) => c.bg >= 0.002 || c.text >= 0.01 || c.border >= 0.03)
      .slice(0, 28);
    kept.forEach((c, i) => { c.id = `c${i + 1}`; });

    const r4 = (v) => Math.round(v * 10000) / 10000;
    const palette = kept.map((c) => ({
      id: c.id, hex: c.hex,
      bg_share: r4(c.bg), text_share: r4(c.text), border_share: r4(c.border),
      depth: c.depth === 99 ? 0 : c.depth,
    }));

    const pairAgg = new Map();
    for (const [k, s] of pairStats) {
      const [fk, bk] = k.split("|");
      const fc = clusterOf.get(fk), bc = clusterOf.get(bk);
      if (!fc?.id || !bc?.id) continue;
      const key = fc.id + "|" + bc.id;
      pairAgg.set(key, (pairAgg.get(key) || 0) + s.v / totText);
    }
    const pairs = [...pairAgg.entries()]
      .map(([k, share]) => { const [fg, bg] = k.split("|"); return { fg, bg, share: r4(share) }; })
      .filter((p) => p.share >= 0.003)
      .sort((a, b) => b.share - a.share)
      .slice(0, 24);

    // ── Light or dark? Area-weighted background lightness, text as a tiebreak ──
    let bgL = 0, bgW = 0, txL = 0, txW = 0;
    for (const c of kept) {
      const L = labToLch(c.lab).L;
      bgL += L * c.bg; bgW += c.bg;
      txL += L * c.text; txW += c.text;
    }
    const avgBg = bgW ? bgL / bgW : labToLch(rgbToOklab(base)).L;
    const avgText = txW ? txL / txW : 0.3;
    let detected = avgBg >= 0.5 ? "light" : "dark";
    if (avgBg > 0.4 && avgBg < 0.6) detected = avgText > 0.6 ? "dark" : "light";

    const pageBgCluster = [...kept].sort((a, b) => b.bg - a.bg)[0];
    const fingerprint = kept.slice(0, 8)
      .map((c) => c.hex.slice(1).replace(/(.)./g, "$1"))
      .join("");

    return {
      host: HOST,
      url: location.href,
      title: document.title || "",
      detected_mode: detected,
      page_bg: pageBgCluster ? pageBgCluster.hex : toHex(base),
      palette,
      pairs,
      fingerprint,
      measured_elements: Math.min(visited, MAX_ELEMENTS),
    };
  }

  function measure() {
    return withThemeSuspended(doMeasure);
  }

  // ══════════════════════════════════════════════════════════════════
  // 8. Wiring
  // ══════════════════════════════════════════════════════════════════

  const onStorageChanged = (changes, area) => {
    if (area !== "local" || !(STORAGE_KEY in changes)) return;
    const rec = changes[STORAGE_KEY].newValue;
    if (rec && rec.enabled) enable(rec);
    else disable();
  };

  const onMessage = (msg, _sender, sendResponse) => {
    if (!msg || typeof msg.type !== "string" || !msg.type.startsWith("sicily-theme-")) return false;
    if (msg.type === "sicily-theme-fetch-css") return false; // background.js handles this one
    try {
      if (msg.type === "sicily-theme-ping") {
        sendResponse({ ok: true, host: HOST, active });
      } else if (msg.type === "sicily-theme-measure") {
        sendResponse({ ok: true, summary: measure() });
      } else if (msg.type === "sicily-theme-status") {
        const s = measure();
        sendResponse({
          ok: true, active,
          detected_mode: s ? s.detected_mode : null,
          page_bg: s ? s.page_bg : null,
        });
      }
    } catch (err) {
      sendResponse({ ok: false, error: String(err && err.message || err) });
    }
    return false;
  };

  function teardown() {
    try { chrome.storage.onChanged.removeListener(onStorageChanged); } catch (_) {}
    try { chrome.runtime.onMessage.removeListener(onMessage); } catch (_) {}
    disable();
  }

  try {
    chrome.storage.onChanged.addListener(onStorageChanged);
    chrome.runtime.onMessage.addListener(onMessage);
    chrome.storage.local.get(STORAGE_KEY, (res) => {
      void chrome.runtime.lastError;
      const rec = res && res[STORAGE_KEY];
      if (rec && rec.enabled) enable(rec);
    });
  } catch (err) {
    console.warn("[Sicily Theme] couldn't start", err);
  }

  window.__sicilyThemeEngine = { teardown };
})();
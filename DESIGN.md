---
name: TANDEM BTCQuant Dashboard
description: Read-only paper-trading cockpit for scanning portfolio state, performance, risk, and operational trust.
colors:
  carbon-black: "#090f12"
  surface-deep: "#111b1f"
  surface-raised: "#152126"
  surface-inset: "#1a292e"
  datum-white: "#eef4f1"
  datum-muted: "#a6b7b5"
  datum-grid: "#263a3f"
  datum-axis: "#40555a"
  signal-teal: "#4cc2d3"
  signal-mint: "#45c49d"
  tension-red: "#f16b76"
  caution-amber: "#e6ac57"
  chart-dark: "#0d171b"
  paper-field: "#e9ecea"
  paper-surface: "#f7f8f6"
  paper-solid: "#fbfcfa"
  paper-ink: "#172124"
  paper-muted: "#566468"
  paper-grid: "#d5dddb"
  paper-axis: "#aebbb9"
  paper-teal: "#0b6f86"
  paper-mint: "#168267"
  paper-red: "#bd3542"
  paper-amber: "#8b5608"
  chart-paper: "#f0f4f2"
typography:
  display:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "clamp(40px, 4vw, 64px)"
    fontWeight: 700
    lineHeight: 1.05
    letterSpacing: "-0.055em"
    fontFeature: "'tnum'"
  body:
    fontFamily: "Trebuchet MS, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0.005em"
  title:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "0.09em"
  label:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "10px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.13em"
rounded:
  xs: "3px"
  sm: "4px"
  md: "5px"
  lg: "7px"
  xl: "18px"
  pill: "99px"
spacing:
  xs: "6px"
  sm: "9px"
  md: "10px"
  lg: "12px"
  xl: "18px"
  section: "22px"
components:
  button-chip:
    backgroundColor: "{colors.surface-inset}"
    textColor: "{colors.datum-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "5px 10px"
  button-chip-active:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.datum-white}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "5px 10px"
  button-icon:
    backgroundColor: "{colors.surface-inset}"
    textColor: "{colors.datum-muted}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "7px 11px"
  button-refresh:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.datum-muted}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "6px 11px"
  card:
    backgroundColor: "{colors.surface-deep}"
    textColor: "{colors.datum-white}"
    rounded: "{rounded.lg}"
    padding: "18px 19px"
  truth-node:
    backgroundColor: "{colors.surface-deep}"
    textColor: "{colors.datum-white}"
    rounded: "{rounded.lg}"
    padding: "12px 13px 11px 18px"
  field:
    backgroundColor: "{colors.surface-inset}"
    textColor: "{colors.datum-white}"
    rounded: "{rounded.md}"
    padding: "6px 9px"
---

# Design System: TANDEM BTCQuant Dashboard

## Overview

**Creative North Star: "The Tensegrity Control Room"**

TANDEM is a dense, read-only operator cockpit: a system of forces held in visible balance. The visual world is code-led and structural, with carbon-black planes, teal signal surfaces, pale datum rails, and red tension accents. The first scan is intentionally operational: PAPER truth, read-only provenance, and blocked external authorization sit on a four-node rail before the dashboard asks the operator to interpret performance.

The dark expression is the signature control-room mode (`#090f12` field with layered teal-black surfaces). A deliberately pale-paper counterpart is available through the existing light theme for bright environments; it keeps the same geometry, rails, labels, and semantic accents. Surfaces are opaque and quiet rather than glassy. Depth comes from inset rules, one-pixel datum lines, and restrained state color. SVG controls and charts are part of the instrument panel, not decorative chrome. The direction contract is the assignment 4 / seed `e7f8d8f0` tensegrity world recorded in the first body child of `dashboard/index.html`.

**Key Characteristics:**
- Code-led operational labels: compact monospace uppercase labels, tabular numerals, and visible `//` eyebrow markers.
- Tensegrity rails: pale datum lines and small anchor dots connect the truth rail and semantic state changes.
- Flat/inset depth: opaque planes, thin borders, and inset top rules carry hierarchy; shadows are reserved for overlays.
- Explicit trust states: PAPER, LECTURE SEULE, and TESTNET BLOQUÉ remain distinct and visible.
- Restrained motion: local control transitions and reduced-motion fallbacks support scanning without spectacle.

## Colors

The palette is an instrument panel rather than a brand gradient: teal carries signal and navigation, mint carries healthy/active state, amber marks qualification or uncertainty, and red marks tension, blocking, or critical risk. The pale theme is a tonal translation of the same roles, not a separate identity.

### Primary

- **Signal Teal** (`#4cc2d3` dark / `#0b6f86` light): the main datum and chart accent; use for active view rails, chart series, focus rings, and the neutral truth-node wire.

### Secondary

- **Signal Mint** (`#45c49d` dark / `#168267` light): healthy state, active engine, long/position state, and the cadence anchor in the truth rail.

### Tertiary

- **Tension Red** (`#f16b76` dark / `#bd3542` light): blocked authorization, critical risk, short state, error copy, and the red tension segment in the header rail.
- **Caution Amber** (`#e6ac57` dark / `#8b5608` light): PAPER qualification, unknown/stale state, waiting thresholds, and warnings. It is never a substitute for the red blocked state.

### Neutral

- **Carbon Black** (`#090f12`): signature dark field behind the cockpit.
- **Deep Teal Surface** (`#111b1f`): default dark card and truth-node plane.
- **Raised Teal Surface** (`#152126`): solid controls, selected chips, and overlay canvas.
- **Inset Teal Surface** (`#1a292e`): compact metrics, fields, unselected controls, and chart-adjacent panels.
- **Datum White** (`#eef4f1`): primary dark-theme text and high-salience numbers.
- **Datum Muted** (`#a6b7b5`): secondary copy, labels, supporting values, and disabled/unknown context.
- **Datum Grid** (`#263a3f`) and **Datum Axis** (`#40555a`): pale rails, table rules, chart axes, and separators.
- **Chart Black** (`#0d171b`): dark plot field, kept slightly distinct from the page field.
- **Paper Field** (`#e9ecea`), **Paper Surface** (`#f7f8f6`), and **Paper Solid** (`#fbfcfa`): light-theme field and opaque planes.
- **Paper Ink** (`#172124`) and **Paper Muted** (`#566468`): light-theme type hierarchy.
- **Paper Grid** (`#d5dddb`) and **Paper Axis** (`#aebbb9`): light-theme datum rails and dividers; **Chart Paper** (`#f0f4f2`) is the light plot field.

### Named Rules

**The Trust-First Rule.** The four truth nodes precede interpretation: keep PAPER, read-only, testnet-blocked, and causal cadence visible in the first scan.

**The Signal-Rarity Rule.** Teal, mint, amber, and red are semantic wires. Keep their large fills rare; let thin rules, anchor dots, badges, and state copy carry most of the signal.

## Typography

**Display Font:** ui-monospace, SFMono-Regular, Menlo, Consolas, monospace

**Body Font:** Trebuchet MS, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif

**Label/Mono Font:** ui-monospace, SFMono-Regular, Menlo, Consolas, monospace

**Character:** Body copy stays human and readable while all operational chrome, key metrics, and trust states snap to a compact terminal register. Tabular numerals (`tnum`) keep a changing cockpit legible during refresh.

### Hierarchy

- **Display** (700, `clamp(40px, 4vw, 64px)`, 1.05): total equity hero value; high-salience, tabular, and intentionally dominant.
- **Headline** (700, 19px, 1.2): TANDEM masthead in uppercase with `.12em` tracking; it is a system name, not a marketing headline.
- **Title** (700, 12px, 1.3): card headings in uppercase with `.09em` tracking; section names should scan as instrument labels.
- **Body** (400, 14px, 1.5): explanatory copy and supporting state descriptions; keep longer notes comfortable and concise.
- **Label** (700, 9–10px, 1.2, uppercase): eyebrow markers, truth labels, table heads, and semantic metadata; use the `//` prefix on `.eyebrow` labels.

### Named Rules

**The Terminal-Label Rule.** Labels name the instrument; values carry the reading. Use compact uppercase mono for operational metadata and reserve body type for explanation.

## Layout

The page uses a full-width cockpit with inline padding `clamp(14px, 3vw, 42px)` and a 56px bottom breathing space. The sticky masthead establishes identity and the current BTC/funding context, followed by the view/status control bar and the four-node truth rail. The hero tile row uses a `2fr + 1fr + 1fr + 1fr` rhythm, then the active view grid uses a `1.35fr + 1fr` column split with named areas so Monitor, Performance, and Risk can reorder the same factual modules without changing their semantics.

The primary rhythm is compact: 9–10px gaps inside rails and tile groups, 10px grid gaps, 18–19px card padding, and 22px footer/section breathing space. Charts are bounded in their own fields (`clamp(240px, 26vw, 350px)` equity chart and `clamp(220px, 28vw, 390px)` price chart in the final layer) so dense data does not bleed into adjacent modules.

At `1120px` and below, named areas collapse to one column and the truth rail becomes a 2×2 grid. At `780px`, the header becomes non-sticky and tiles become a 2-column arrangement with the hero spanning both columns. At `520px`, page padding reduces to 10px, header facts distribute across the row, labels and headings tighten, and charts/technical details stay within the viewport. Position rows become cards below `899px`; metric and exposure groups reduce to two columns and then one where needed.

**The View-Order Rule.** Reordering a view changes scan priority, not product truth: Monitor foregrounds pulse and engine state, Performance foregrounds realized results and history, and Risk foregrounds radar, exposure, and authorization context.

## Elevation & Depth

The system is flat and inset by default. Cards, tiles, the cockpit bar, and truth nodes use opaque surfaces, one-pixel borders, and an inset top rule rather than glass blur or floating drop shadows. The final shared elevation token is a single inset highlight based on the current ink color; hover adds a restrained inset teal highlight. Depth is structural: a rail, a border, a top signal rule, or a distinct surface plane tells the operator where one instrument ends and another begins.

### Shadow Vocabulary

- **Resting instrument plane** (`inset 0 1px 0 color-mix(in srgb, var(--ink) 5%, transparent)`): default cards, tiles, truth nodes, and cockpit bar.
- **Active instrument plane** (`inset 0 1px 0 color-mix(in srgb, var(--s1) 16%, transparent)`): hover/focus response on cards and active surfaces.
- **Drawer separation** (`-20px 0 60px -20px rgba(0,0,0,.5)`): structural separation for the settings drawer only.
- **Modal separation** (`0 30px 80px -20px rgba(0,0,0,.6)`): structural separation for drill-down content only.

### Named Rules

**The Flat-by-Default Rule.** Do not make an instrument card float. Use opaque tonal layers, datum lines, and inset rules; reserve ambient shadow for drawer and modal boundaries.

**The Rail-Over-Glow Rule.** A state must remain legible when decorative effects are disabled. The WebGL field, if enabled, sits behind the UI and never carries the sole meaning of a state.

## Shapes

Geometry is clipped, squared, and engineered rather than soft or card-wall friendly. The recurring instrument radius is 7px for cards, tiles, truth nodes, and the cockpit bar; compact controls use 3–5px corners; badges use 4px; switches and delta/status pills use the existing pill radius. Borders are one pixel and low-contrast, with semantic top rules or bottom rails providing the stronger edge. Charts, tables, and technical details are contained in their own rectangular fields. The settings drawer is flush to the viewport edge; the modal is the one deliberately larger 18px radius.

## Components

### Buttons

- **Character:** small, tactile control surfaces with SVG instrumentation and a clear active rail.
- **Shape:** chip controls are tight 3px corners; icon and refresh controls are 5–6px; active/status pills are reserved for state.
- **Primary / Active:** selected chips use the raised solid surface with datum-white text; refresh uses the raised surface and muted datum text until hover.
- **Hover / Focus:** color and border shift toward signal teal within 120–180ms; `:focus-visible` uses a 2px teal ring with a 2px offset. Keep the hit area native and avoid ornamental scale.
- **Secondary / Ghost:** unselected chips remain transparent over the inset chip group; icon controls use the inset surface with a one-pixel datum border.

### Chips

- **Style:** the group is an inset surface with a 5px outer corner and 1px ring; each chip uses a 3px corner, compact mono label, and 5px × 10px padding.
- **State:** selected view/range/unit chips use the solid raised surface and white/primary ink; unselected chips use muted datum text; the view indicator slides locally inside the view group.

### Cards / Containers

- **Corner Style:** 7px for standard cards and tiles; signature focus cards retain the same silhouette and gain an inset 2px semantic top rule.
- **Background:** dark mode uses deep, raised, and inset teal planes; light mode translates them to paper field/surface/solid planes.
- **Shadow Strategy:** flat/inset at rest; only drawer and modal boundaries receive ambient separation.
- **Border:** 1px low-contrast ring; charts and technical fields add their own 1px boundary.
- **Internal Padding:** 18px × 19px for grid cards, 17px × 18px for top tiles, 12–13px for compact metric cells.

### Inputs / Fields

- **Style:** select, date, and number fields use the inset surface, 1px ring, 4–8px corners depending on the host control, 6px × 9px padding, and readable body/mono values.
- **Focus:** the shared 2px teal `:focus-visible` ring with offset; never rely on a color-only change.
- **Error / Disabled:** disabled/unknown state uses muted datum text; validation and blocked conditions use semantic amber/red backgrounds and copy, not disabled-looking decoration.

### Navigation

- **Style:** the view control is a compact chip rail inside the cockpit bar. It uses three tabs—Surveillance, Performance, Risque—with role/tab semantics and a local sliding indicator.
- **Default / Active / Hover:** default tabs are muted on inset; active is raised and datum-white; hover promotes text to ink. The surrounding status signal and freshness readout stay visible beside navigation.
- **Mobile:** the control expands to the full row below 520px, while the header itself becomes a compact, non-sticky strip.

### Truth Rail

The signature four-node tensegrity rail is the trust contract, not decoration. Its nodes state `PAPER`, `LECTURE SEULE`, `TESTNET BLOQUÉ`, and `4 H · CAUSAL`; anchor dots and a thin datum wire connect the system on wide screens, then become independent cards in the 2×2 mobile layout. PAPER is amber, testnet blocking is red, and cadence is mint; each node retains explanatory microcopy below the strong state.

### Charts & State Badges

SVG charts inherit the active signal palette and live in bounded chart fields. Donchian thresholds distinguish active solid lines, inactive dashed lines, and a waiting zone. Engine badges use mint/green for long or protected, red for short/unsafe, muted for flat, and amber for unknown. Charts and badges must preserve labels and provenance when data is unavailable.

### Overlays

The settings drawer slides from the right on a solid surface and the strategy drill-down modal opens over a blurred backdrop. Both are local, reversible reading aids: they do not alter the PAPER/read-only contract or imply execution authority. Their external shadows are the only broad depth cues in the system.

## Do's and Don'ts

### Do:

- **Do** show the PAPER, read-only, and testnet-blocked contract before performance interpretation.
- **Do** use the dark carbon-black/teal expression as the signature world and preserve the same roles in the pale-paper theme.
- **Do** use compact monospace labels, tabular numerals, semantic top rules, and one-pixel datum rails to make dense state scannable.
- **Do** keep red for tension/blocking/critical risk and amber for warning/qualification/unknown; make the distinction explicit in copy.
- **Do** preserve non-color meaning through text, badges, symbols, and SVG legend treatments.
- **Do** honor `prefers-reduced-motion`: the canvas, tilt, pulses, and local transitions must have a quiet fallback.
- **Do** keep chart, table, drawer, and modal content bounded on mobile; the dashboard is an always-on cockpit that must remain useful at phone widths.

### Don't:

- **Don't** imply live authorization, venue execution, wallets, trading actions, or testnet permission anywhere in the visual system.
- **Don't** turn the dashboard into a generic rounded glass card wall, soft gradient brand page, or decorative hero.
- **Don't** use glow, motion, or a background canvas as the only indicator of health, risk, freshness, or authorization.
- **Don't** flatten PAPER, stale, unknown, unavailable, realized, modeled, and backtest states into one generic status color.
- **Don't** spend the red accent on ordinary decoration; it marks tension that can break the balance.
- **Don't** introduce large corner radii, heavy floating shadows, or broad gradients into standard instrument cards.
- **Don't** replace the causal 4h decision cadence or provenance notes with a more convenient but less truthful summary.

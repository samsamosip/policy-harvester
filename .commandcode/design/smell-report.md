# Smell report — Policy Harvester admin

Score: 8/10 — FAINT. Date: 2026-10-10. Surface: `src/policy_harvester/templates/` (24 Jinja pages, server-rendered, no JS) + `src/policy_harvester/static/admin.css`.

TL;DR: product-instrument register, mostly authored. Two faint tells only. Fix with `deslop` (login backdrop) and `surface` (dashboard priority).

## Heuristics (10 rows, 1 = absent, 0 = detected)

| # | Odor | Score | Key finding |
|---|---|---|---|
| 1 | Tech gradient | 0 | `login.html:5-6` radial navy glow behind auth card |
| 2 | Generic tech hue | 1 | Brand `#0f6c8c` teal is domain-neutral but committed, not blue-violet |
| 3 | Feature tile grid | 1 | No marketing tiles; dashboard cards are operational with links + hints |
| 4 | Accent rail | 1 | `.notice` left border encodes ok/warn/bad + text, not decoration |
| 5 | Unearned blur | 1 | No blur anywhere |
| 6 | Stat monument | 0 | `dashboard.html:24-37` equal-weight number clusters, weak priority signal |
| 7 | Icon topper | 1 | No decorative section icons; `PH` mark is brand, inline |
| 8 | Bounce everywhere | 1 | Only `.12s` sharp transitions + 1px press; no elastic |
| 9 | Default type | 1 | Pretendard system stack is a deliberate Korean product-UI choice with scale |
| 10 | Center stack | 1 | Left-aligned shell, work-pattern grids (monitor/compare/configure) |

## Findings (ordered by severity, then leverage)

| # | Severity | Discipline | Location | Before | After | Why |
|---|---|---|---|---|---|---|
| 1 | LOW | Surface | `src/policy_harvester/templates/dashboard.html:24-37` | todo 3 cards + funnel 5 cells equal weight; only failure count turns red | Keep numbers (they link to filtered lists = proof-adjacent) but give the surface a priority lane: failed-jobs card gets urgent frame when > 0, funnel cells get freshness context already present in `<small>`; add `aria-label` counts | Equal clusters read as monument when nothing signals what needs attention first |
| 2 | LOW | Color | `src/policy_harvester/templates/login.html:5-6` | `radial-gradient(1200px 600px at 50% -10%,#1d4a66 0%,var(--nav) 60%)` glow | Flat `var(--nav)` + 3px brand hairline on the card top tied to admin identity | Glow is the dark-auth domain default; flat + hairline is authored and cheaper to paint |

## Considered but rejected

| Location | Candidate | Rejected because |
|---|---|---|
| `static/admin.css` `.notice` left border | Remove as accent rail | Border carries status with adjacent text label; removing it harms scanning, not a smell |
| `static/admin.css` system font stack | Switch to a display face | Product register: operators read dense tables daily; Pretendard stack is the deliberate voice |
| `dashboard.html` funnel | Convert to prose/case language | Monitor pattern: counts with links + queue context are the evidence, not decoration |
| `base.html` navy sidebar | Recolor away from navy | Navy + teal is the committed identity across shell, topbar, login; no cheaper change buys distinction |

## Verification

- Read `base.html`, `dashboard.html`, `login.html`, all 24 templates, `static/admin.css` (309 lines): no purple/cyan gradient, no frosted blur, no elastic easing, no icon toppers. Checked via grep for `gradient|blur|backdrop|bounce|elastic`.
- Rendered-surface check: not run (no live server in this pass); findings are source-grounded, marked accordingly.
- Not verified: colorblind simulation of funnel/todo emphasis (no browser here).

## Verdict

Needs changes (no HIGH; 2 LOW outstanding).

# Review report — Policy Harvester admin

Score: 35/50. Date: 2026-10-10. Register: product (admin instrument, not brand).

TL;DR: confident operator surface with best-in-class Korean guidance copy. Hierarchy and interaction carry it; list width + bulk ops + detail anchoring are the three moves that raise it.

## Lens scores (/10 each)

| Lens | Score | Key finding |
|---|---|---|
| First impression | 7 | Navy shell + teal brand + dense tables reads as owned ops tool, not template |
| Hierarchy | 7 | Page-head → tabs → filters → table → pager is stable; detail pages lose it to length |
| Color voice | 7 | Chips carry state with text + hue; warn/ok/bad roles consistent |
| Type voice | 7 | Pretendard stack + tabular numerals + mono tokens fit the register |
| Interaction feel | 7 | Full POST coverage, zero JS, confirm-delete pattern; bulk missing |

## Findings

| # | Severity | Discipline | Location | Before | After | Why |
|---|---|---|---|---|---|---|
| 1 | HIGH | Surface | `src/policy_harvester/api.py` + `base.html:7` | Stylesheet 404 (same as checkup #1) | Mount `/static` (done in tree) + verify 200 with cache headers | No hierarchy/color/type claim survives unstyled HTML |
| 2 | MEDIUM | Layout | list pages (`opportunities.html`, `notices.html`, `table.html`, `jobs.html`, `audit.html`) | 1480px cap + tight column max-widths squeeze titles before scroll helps | `main.wide` 1920px cap on list pages; `td.main` 22rem min, `td.ref` 28rem max, `td.prose` 56rem max; keep `.table-wrap` scroll + sticky thead | The explicit user pain: width limit + need scroll |
| 3 | MEDIUM | Layout | `notice.html`, `opportunity.html` | No anchors; right-rail actions fall below content < 1100px | Sticky sub-nav (`상태|장학|문서|추출|LLM|작업`) + sticky top action bar on opportunity | Longest pages need orientation + reachable actions |
| 4 | MEDIUM | Interaction | `jobs.html`, `reviews.html`, `notices.html` | Single-item actions only | Bulk retry (failed), bulk resolve (kind), bulk re-extract (selected notices) | Operator throughput on 300-row surfaces |
| 5 | MEDIUM | Interaction | `revision.html:76-80` | Hand-edited `patches_json` textarea | Per-patch rows (path readonly + value/quote inputs + remove) + block links | Fragile JSON editing is the highest-error interaction |
| 6 | LOW | Surface | `identity.html` | Side-by-side cards, no field delta | Field-by-field delta (title/provider/year/windows/amount) + `%` similarity | Merge decisions need highlighted difference, not parallel prose |
| 7 | LOW | Interaction | `opportunities.html:2` | `qs()` trailing-`&`/missing-separator on empty filters | `merged.items() \| reject \| join("&")` | Broken pagination links on filtered lists |
| 8 | LOW | Surface | `search_debug.html:7` | Hard-coded `0.45/0.55` text; no query highlight | Weights from settings + server `<mark>` highlight + profile/dims shown | Opaque scoring erodes trust in search tuning |

## Considered but rejected

| Location | Candidate | Rejected because |
|---|---|---|
| Whole-shell redesign | New palette/composition | Current composition serves monitor/compare/configure patterns; redesign buys novelty, not throughput |
| Card-ify all tables | Uniform card layout | Tables are the right compare pattern; cards only where scanning benefits (already on lists at 700px) |
| Client-side search/filter | Add JS filtering | CSP no-script is a deliberate constraint; server `GET ?q&kind` preserves it |
| Global search `GET /admin/search` | Build now | Valuable but out of deslop scope; needs backend design beyond visual treatment |

## Verification

- Walked (source): dashboard → opportunities → notice detail → opportunity detail → reviews → revision → identity → jobs → settings → users/api-keys. All flows render server-side with POST coverage.
- Checked: chips carry text labels (not color alone), focus-visible present, `prefers-reduced-motion` disables transitions, `details` used for nav/delete/disclosure (no-JS honored).
- Not verified: live render, keyboard-only full pass, screen-reader names on badges.

## Verdict

Block (1 HIGH: stylesheet mount; fix in tree, needs runtime proof). After that: Needs changes (6 MEDIUM/LOW).

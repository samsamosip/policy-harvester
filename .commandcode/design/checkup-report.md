# Checkup report — Policy Harvester admin

Score: 40/60. Date: 2026-10-10. Surface: admin shell + 24 templates, `static/admin.css`.

TL;DR: usable product surface with real copy and task links. Blocked from healthy by one Critical: the CSS split shipped without a `/static` mount, so every page renders unstyled. Fix mount first, then the three Watch items below.

## Heuristic scores (/10 each)

| # | Vital | Score | Key finding |
|---|---|---|---|
| 1 | Intentionality | 10 | Work-pattern composition (monitor/compare/configure) + authored Korean microcopy |
| 2 | Readability | 5 | Dense tables readable on desktop; broken while CSS 404s; long pages need anchors |
| 3 | Usability | 5 | Core flows complete but single-item POSTs; no bulk retry/resolve |
| 4 | Responsiveness | 5 | 900/700px breakpoints + cards for lists; remaining tables rely on horizontal scroll |
| 5 | Speed | 10 | No JS, no webfonts, one CSS file once mount is fixed; inline per-page `<style>` small |
| 6 | Accessibility | 5 | Focus ring, skip-free shell, chips carry text; badges/`×` need names, tables need captions |

## Findings

| # | Severity | Discipline | Location | Before | After | Why |
|---|---|---|---|---|---|---|
| 1 | HIGH | Surface | `src/policy_harvester/api.py:51` + `src/policy_harvester/templates/base.html:7` | `admin.css` exists on disk but no `StaticFiles` mount; `base.html` links `/static/admin.css` → 404, all pages unstyled | Mount verified in this pass (`app.mount("/static", ...)`); keep + add no-store-safe caching for CSS | Nothing renders as designed until the stylesheet loads; blocks every other vital |
| 2 | MEDIUM | Layout | `static/admin.css:70` | `main { max-width: 1480px }` left-aligned, no inline margin | `main { ...; margin-inline: auto; width: 100% }` for list pages; keep detail pages capped | Wide screens waste the freed width the list request is about |
| 3 | MEDIUM | Layout | list tables | `td.main min 16rem / td.ref 14-22rem / td.prose up to 44rem` inside a 1480px cap; `.table-wrap` scrolls but columns stay squeezed | Raise cap for list pages (`main.wide { max-width: 1920px }`), lift `td.ref/prose` max-widths, keep `.table-wrap` scroll + sticky `thead` | The reported pain: width limit squeezes titles/refs before scroll ever helps |
| 4 | MEDIUM | Interaction | `templates/jobs.html`, `templates/reviews.html` | One POST per row retry/resolve | Bulk `POST /admin/jobs/retry` (failed set) + bulk `POST /admin/reviews/resolve` (kind set) with count confirm | 300-row failure lists cost one click per row today |
| 5 | MEDIUM | Accessibility | `templates/base.html:13-52`, `templates/table.html`, `templates/users.html:30` | Badges without names; tables without captions; `×` relies on title/aria-label only at small size | `aria-label` on badges, `<caption class=sr-only>` + `scope=col`, 36px delete target | Screen-reader and touch users pay for density without names |
| 6 | LOW | Interaction | `templates/opportunities.html:2`, `notices.html`, `table.html` | `←/→` pager only; `qs()` separator bug on filtered empties | Numbered pager + `page` input + filter-preserving links; fix `qs()` with `reject`+`join` | Deep lists need jumping, not stepping |
| 7 | LOW | Surface | `templates/notice.html`, `templates/opportunity.html` | Endless detail scroll; jobs 60 / exchanges 100 unbounded | Sticky sub-nav anchors + `<details open>` collapse + LIMIT 20 + 더보기 | Detail pages are the longest surfaces; scanning costs grow with rows |

## Considered but rejected

| Location | Candidate | Rejected because |
|---|---|---|
| Per-page `<style>` blocks | Inline all into admin.css | Small layout rules co-located with their page are cheaper to maintain than one 40KB file |
| `login.html` standalone CSS | Merge into admin.css | Anonymous surface intentionally shares no shell; duplication is ~1KB and avoids leaking authed CSS |
| CSP `style-src 'unsafe-inline'` | Drop to `'self'` only | Required while per-page `<style>` + inline `style=` attributes exist; tightening needs the CSS-block refactor first |
| Dark mode | Add `prefers-color-scheme: dark` | Product register + operator daylight use; no evidence of demand, high verification cost |

## Verification

- Ran: `ls src/policy_harvester/static/` (admin.css 24KB present), grep for mount (absent before fix, present after), read `base.html` (stylesheet link), read all list/detail templates.
- Passed: composition, copy quality, no-JS constraint honored (`details` for nav/delete/disclosure).
- Not verified: rendered pixels (no live server), iOS 16px input zoom audit, 200% zoom reflow.

## Verdict

Block (1 HIGH outstanding until mount is deployed; code change is in tree, needs runtime verification).

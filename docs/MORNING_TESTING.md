# Morning manual pass — 2026-08-10

Fifteen minutes, three personas, one server. Every item is one line:
**click → what you should see.** A mismatch is a finding — the
re-verify loop after any fix is `python .scratch\net\run_net.py`
(~18s), then re-check the item by hand.

**The server:** `http://127.0.0.1:8791` — seeded demonstration estate
(products **Atlas** and **Beacon**; Atlas builds
`feature/checkout-rewrite` (cadenced, 5 covered passes),
`feat/payment-retry-backoff` (one-off), and the RC pair
`2026.9.0` → `2026.9.1`; `corvus-main` is the old-client, no-product,
no-streams environment; `linux-sim`/`linux-uat-sim`/`win-sim` are the
bulk mainline estate). Fresh perf log at the scratchpad's
`morning-perf.log`. Code is `streams-upgrade` tip `df7c1d1`.

**Start URLs:** manager `http://127.0.0.1:8791/watch.html` ·
delver `http://127.0.0.1:8791/index.html` ·
RC owner `http://127.0.0.1:8791/index.html` (arrive bare, like a
first-timer — the route IS part of the test).

**The two known cosmetic caveats** (no browser has rendered ANY of
this before you; these two are where trouble is most likely):
1. **Watch cards** — the fail/stale accent borders, the
   "Unassigned failing" stat-as-link, and four cards side by side have
   never been seen at a real screen width; crowding/legibility is
   unjudged.
2. **The build delta header stack** — band + framing + verdict line +
   five tiles + agree/coverage/drift lines all sit above the table
   now; whether that column of lines crowds one screen, and whether
   Open Actions' two-chip result strip fits its column, is unjudged.

---

## Manager (Watch, from bare)

- [ ] Open `watch.html` bare → empty state TELLS you what to do (add a
      card above, save default, copy link) — never a blank page.
- [ ] Kind dropdown reads **Environment / Product / Build** — the word
      "Branch" appears nowhere on this page.
- [ ] Add four cards: Product `Atlas`, Product `Beacon`, Build
      `Atlas · build:2026.9.1`, Build
      `Atlas · build:feature/checkout-rewrite` → grid shows 4 cards.
- [ ] "Save as my default" → status says
      "Saved as this browser's default."
- [ ] Reload `watch.html` bare → the same 4 cards return (the saved
      default survives a bare visit).
- [ ] "Copy link" → paste into a NEW tab → identical grid (the URL is
      the whole configuration).
- [ ] The Atlas card (113 failing / 113 unassigned) draws the eye
      first — caveat 1: judge the accents at your screen size.
- [ ] Atlas card "Open in dashboard →" → lands on `?product=Atlas`,
      dashboard numbers match the card's.
- [ ] Header nav "Watch" from there → your saved default renders
      again (that IS the way back — there is no per-page back
      control; on the decision list if it bothers you).

## Delver (plain mainline triage — must feel UNCHANGED)

- [ ] Bare `index.html` → status line, queue tabs, browse table. NO
      band, NO tabs, NO delta anything, NO Build picker. (The product
      switcher and Product column ARE expected — products are
      declared in this estate; that is the drop, not leakage.)
- [ ] "Still failing" queue → any row → "Review" → output renders
      inline, assign to a user, post a comment → button flips to
      "Posted".
- [ ] Same panel "Open full test page →" → test page renders
      title/identity/latest run.
- [ ] **NEW tonight:** beside "Latest run:" there is a
      "View in timeline →" link → lands on that run's night, the run
      visible in its block. (This link did not exist before —
      the journey dead-ended here.)
- [ ] History table + analytics (failing-since, flakiness, duration)
      all render; nothing on this page mentions streams beyond the
      (empty-of-builds) "Every build" disclosure — decision-list item
      if it reads as clutter.

## RC owner (verify build 2026.9.1, from bare)

- [ ] Bare `index.html` → no Build picker visible → product switcher →
      `Atlas` → **Build picker appears** → pick `build:2026.9.1` →
      lands on `?product=Atlas&stream=5`. (Switcher-first is the
      intended route; whether it needs an on-screen hint is on the
      decision list.)
- [ ] Band reads "Viewing build 2026.9.1 — compared against mainline";
      the verdict line reads
      "vs build 2026.9.0: 2 new failures · 7 fixed — vs mainline: 7
      new failures" — **visible without scrolling?** (caveat 2).
- [ ] Tiles: 7 new failures / 22 new passes / 0 both failing; delta
      table columns read "Mainline" / "This build" — never "branch".
- [ ] New-failures row `test_expire_session_23` → its test page →
      compare strip "mainline PASS · build:2026.9.1 FAIL"; the stream
      switcher dropdown lists Mainline / 2026.9.1 / 2026.9.0 each
      with its result.
- [ ] **Assign from THIS page's own assignee dropdown** → "Saved." →
      open Open Actions → the row shows an origin tag naming
      2026.9.1, and the origin filter chips
      ("Build-originated" / "Mainline") now appear — clicking
      Build-originated filters to that row. (Fixed tonight: this page's
      assign used to silently lose the origin.)
- [ ] Back on the build dashboard: Compare-to → `2026.9.0` → the
      comparison re-scopes, and the verdict line now names mainline
      as the OTHER side.
- [ ] Build picker → `build:feature/checkout-rewrite` → TWO tabs
      appear, "Its own results" is the default, caption states the
      real count ("5 passes … 2 or more"); its Time/Timeline quick
      links keep `stream=` in the URL.
- [ ] Honest empty: `time.html?stream=5&environment=atlas-lab-alpha` →
      not a bare blank — the empty state names **atlas-lab-bravo** as
      where 2026.9.1's data is, as a link that switches only the
      environment.
- [ ] Old-client sanity: filter the dashboard to `corvus-main` →
      ordinary mainline rows, zero stream UI anywhere.

---

**When you are done:** fix-or-decide per finding (net re-run after any
fix), then the ship path in `SESSION_HANDOVER.md` — `wp-18-timeline` →
`master` first, `streams-upgrade` → `master` second, re-date the drop
note + `whatsnew.html` to the real ship day, deploy per
`docs/drops/2026-08-14.md`.

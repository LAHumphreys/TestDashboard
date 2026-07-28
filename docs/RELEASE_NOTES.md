# What's new since yesterday's build

28 July 2026. All of this is live now — reload the page.

## Open actions — most of the change is here

- **Triage without leaving the list.** Open a row to read the output, add a
  comment, assign it, or retire it. The list keeps its place and the row updates
  where it sits, so working down a queue no longer means losing it.
- **Every failing row says when it last passed** — and whether it broke or is
  just unreliable: *"failed every one of the last 20 runs"* versus *"flaky —
  flips about 1 run in 3"*. Those need opposite responses and used to look
  identical. The open panel shows the last 20 runs as a strip.
- **Columns sort.** Sorting covers the whole queue, not just the rows on screen,
  so it returns you to the first page.

## Dashboard

- **New failures and Fixed now read `PASS → FAIL`** in one column: the current
  result solid, the superseded one ghosted. Previously the *previous* result was
  the loudest thing in the row, so a page of new failures read as a page of
  passes.
- **"Not run recently" now follows the suite's own rhythm** instead of a fixed
  36 hours. Monday mornings, and the environments that run first, no longer flag
  the whole estate as missing. Retirement offers use the same rule, so they stop
  appearing on healthy tests.
- **Sorting "All tests" is fast again** — roughly 3 ms a page, down from ~200 ms.

## Time — new tab

Where the suite's runtime actually goes: environments, then the scripts in one,
then the tests in one script. Bars plus the numbers, sortable.

## Users

A user can be **deactivated**, which takes a duplicate or departed account out of
the assignee pickers. History and existing assignments are untouched, and it is
reversible.

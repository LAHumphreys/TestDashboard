"""API-based sanity-net checks: classes 3 (unscoped catalogs), 4
(dishonest empties), 5 (unexercised UI), 6 (contract honesty), plus the
standing NUL-byte check and one extra "leftover kind" check the net
found while orienting (see LeftoverKindCheck below).

Pure stdlib, talks to the live scratch server on NET_PORT (set by
run_net.py; default 8931). Each check
function returns a list of failure dicts: {"check": name, "detail": str}.
Called from run_net.py; nothing here boots a server or seeds data.
"""
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PORT = "8931"


def base_url() -> str:
    """The net server's origin; read per call, since run_net.py sets
    NET_PORT after this module is imported."""
    return "http://127.0.0.1:%s" % os.environ.get("NET_PORT", DEFAULT_PORT)

#: The checkout this file lives in (tools/dev/net/ -> root).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _call(path: str, data: Any = None, method: Optional[str] = None
          ) -> Tuple[int, Any]:
    """(status, decoded JSON body); Any is the JSON boundary."""
    req = urllib.request.Request(
        base_url() + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"error": body}


def fail(check: str, detail: str) -> Dict[str, str]:
    return {"check": check, "detail": detail}


# ---------------------------------------------------------------------
# Class 5: unexercised UI -- the seed IS part of the net.
# ---------------------------------------------------------------------
def class5_seed_coverage() -> List[Dict[str, str]]:
    failures = []  # type: List[Dict[str, str]]

    # All five compare categories non-empty SOMEWHERE across the estate's
    # streams (checked against every declared product's streams).
    products = ["Atlas", "Beacon", "Corvus"]
    category_totals = {
        "new_failures": 0, "new_passes": 0, "both_failing": 0,
        "new_tests": 0, "no_result": 0,
    }
    category_fail_rows = {"new_tests": None}
    all_streams = []  # type: List[Any]
    for product in products:
        status, body = _call("/api/streams?product=%s" % product)
        if status != 200:
            failures.append(fail(
                "class5.streams_list",
                "/api/streams?product=%s -> %s %s" % (product, status, body)))
            continue
        all_streams.extend(body["streams"])
    if not all_streams:
        failures.append(fail("class5.no_streams",
                              "no non-mainline streams found in any product "
                              "-- cannot check compare categories at all"))
    for stream in all_streams:
        status, body = _call("/api/compare?stream=%d" % stream["id"])
        if status != 200:
            failures.append(fail(
                "class5.compare",
                "/api/compare?stream=%d -> %s %s"
                % (stream["id"], status, body)))
            continue
        counts = body["counts"]
        for key in category_totals:
            category_totals[key] += counts.get(key, 0)
        if counts.get("new_tests", 0) > 0 and category_fail_rows["new_tests"] is None:
            # confirm at least one new_tests row is a FAIL (the
            # "new-tests-with-a-FAIL" shape class 5 names explicitly).
            st, rows_body = _call(
                "/api/compare?stream=%d&category=new_tests&limit=50"
                % stream["id"])
            if st == 200:
                fails = [r for r in rows_body["tests"]
                         if r.get("stream_result") == "FAIL"]
                category_fail_rows["new_tests"] = bool(fails)

    for category, total in category_totals.items():
        if total == 0:
            failures.append(fail(
                "class5.compare_category_empty",
                "compare category '%s' is 0 across every stream in the "
                "estate (checked %d streams) -- the seed does not exercise "
                "it" % (category, len(all_streams))))
    if category_fail_rows.get("new_tests") is False:
        failures.append(fail(
            "class5.new_tests_no_fail",
            "new_tests category has rows somewhere but none is a FAIL -- "
            "the 'new-tests-with-a-FAIL' shape class 5 asks for by name "
            "is not exercised"))

    # Both watch highlight kinds: unassigned_failing > 0 somewhere, and
    # both stale=true and stale=false achievable via the @ suffix.
    status, body = _call(
        "/api/watch?c=e:atlas-lab-alpha@1h&c=e:atlas-lab-alpha@30d")
    if status != 200:
        failures.append(fail("class5.watch", "%s %s" % (status, body)))
    else:
        cards = body["cards"]
        stale_states = set(c.get("stale") for c in cards if c.get("ok"))
        if True not in stale_states:
            failures.append(fail(
                "class5.watch_no_stale_true",
                "no watch card shows stale:true with a 1h declared "
                "expectation against the seeded (day 7-9 Aug) data: %s"
                % cards))
        if False not in stale_states:
            failures.append(fail(
                "class5.watch_no_stale_false",
                "no watch card shows stale:false with a 30d declared "
                "expectation: %s" % cards))
        unassigned_any = any(
            c.get("unassigned_failing", 0) > 0 for c in cards if c.get("ok"))
        if not unassigned_any:
            failures.append(fail(
                "class5.watch_no_unassigned_highlight",
                "no watch card shows unassigned_failing > 0: %s" % cards))

    # An RC pair, a cadenced stream: named streams must exist and have
    # the right shape.
    status, body = _call("/api/streams?product=Atlas")
    names = {s["name"]: s for s in body.get("streams", [])} if status == 200 else {}
    for expected in ("2026.9.0", "2026.9.1", "feature/checkout-rewrite",
                      "feat/payment-retry-backoff"):
        if expected not in names:
            failures.append(fail(
                "class5.missing_named_stream",
                "expected seeded stream '%s' not found under Atlas" % expected))

    # Two-tab, both tabs, on a stream meeting the covered-pass threshold:
    # verified via the DOM-shim walk (drive_branch_tabs pattern), not
    # here -- record as a cross-reference so a reader of this module
    # knows where that assertion lives.

    # Corvus: mainline-only, zero streams.
    status, body = _call("/api/streams?product=Corvus")
    if status != 200:
        failures.append(fail("class5.corvus_streams", "%s %s" % (status, body)))
    elif body.get("streams"):
        failures.append(fail(
            "class5.corvus_not_zero_stream",
            "Corvus has streams (expected zero, mainline-only): %s"
            % body["streams"]))

    return failures


# ---------------------------------------------------------------------
# Class 6: contract honesty.
# ---------------------------------------------------------------------
def class6_contract_honesty() -> List[Dict[str, str]]:
    failures = []  # type: List[Dict[str, str]]

    # branch: rejected loudly per-record; valid records in the same
    # batch still import.
    batch = {
        "runs": [
            {
                "environment": "atlas-lab-alpha", "script": "net/contract.py",
                "test_name": "test_good_1", "result": "PASS",
                "start_time": "2026-08-09T23:00:00.000000",
                "end_time": "2026-08-09T23:00:01.000000",
                "output": "",
            },
            {
                "environment": "atlas-lab-alpha", "script": "net/contract.py",
                "test_name": "test_bad_branch", "result": "PASS",
                "start_time": "2026-08-09T23:00:02.000000",
                "end_time": "2026-08-09T23:00:03.000000",
                "output": "", "branch": "feat/should-be-rejected",
            },
            {
                "environment": "atlas-lab-alpha", "script": "net/contract.py",
                "test_name": "test_good_2", "result": "PASS",
                "start_time": "2026-08-09T23:00:04.000000",
                "end_time": "2026-08-09T23:00:05.000000",
                "output": "",
            },
        ]
    }
    status, body = _call("/api/import", batch)
    if status != 200:
        failures.append(fail("class6.branch_reject_envelope",
                              "%s %s" % (status, body)))
    else:
        if body.get("inserted", 0) + body.get("updated", 0) != 2:
            failures.append(fail(
                "class6.branch_reject_valid_still_imported",
                "expected 2 valid records accepted alongside the rejected "
                "one, got inserted=%s updated=%s unchanged=%s"
                % (body.get("inserted"), body.get("updated"),
                   body.get("unchanged"))))
        if body.get("rejected") != 1:
            failures.append(fail(
                "class6.branch_reject_count",
                "expected exactly 1 rejection, got %s (errors=%s)"
                % (body.get("rejected"), body.get("errors"))))
        errors = body.get("errors", [])
        matching = [e for e in errors if e.get("test_name") == "test_bad_branch"]
        expected_text = ("branch: removed before this contract ever "
                          "shipped — use build:")
        if not matching:
            failures.append(fail(
                "class6.branch_reject_missing_error",
                "no error entry found for test_bad_branch: %s" % errors))
        elif matching[0].get("error") != expected_text:
            failures.append(fail(
                "class6.branch_reject_wrong_text",
                "expected exact text %r, got %r"
                % (expected_text, matching[0].get("error"))))

    # streams_seen acknowledged for build:.
    stream_name = "net-check-streams-seen"
    batch2 = {
        "runs": [{
            "environment": "atlas-lab-alpha", "script": "net/contract.py",
            "test_name": "test_streams_seen", "result": "PASS",
            "start_time": "2026-08-09T23:00:06.000000",
            "end_time": "2026-08-09T23:00:07.000000",
            "output": "", "build": stream_name,
        }]
    }
    status, body = _call("/api/import", batch2)
    if status != 200:
        failures.append(fail("class6.streams_seen_envelope", "%s %s" % (status, body)))
    elif "streams_seen" not in body:
        failures.append(fail(
            "class6.streams_seen_missing_key",
            "response has no streams_seen key at all -- a --build feeder "
            "would treat this as a hard failure (talking to an old "
            "server): %s" % body))
    elif body["streams_seen"] != ["build:" + stream_name]:
        failures.append(fail(
            "class6.streams_seen_wrong_value",
            "expected streams_seen == ['build:%s'], got %s"
            % (stream_name, body["streams_seen"])))

    # Byte-identical re-push writes nothing.
    status, body1 = _call("/api/import", batch2)
    status2, body2 = _call("/api/import", batch2)
    if status != 200 or status2 != 200:
        failures.append(fail("class6.repush_envelope",
                              "%s / %s" % (body1, body2)))
    else:
        if body2.get("inserted", 0) != 0:
            failures.append(fail(
                "class6.repush_inserted_nonzero",
                "byte-identical re-push inserted %s new rows (expected 0): %s"
                % (body2.get("inserted"), body2)))
        if body2.get("unchanged", 0) != 1:
            failures.append(fail(
                "class6.repush_unchanged_count",
                "byte-identical re-push: expected unchanged=1, got %s: %s"
                % (body2.get("unchanged"), body2)))
        if body2.get("updated", 0) < body2.get("unchanged", 0):
            failures.append(fail(
                "class6.repush_updated_undercounts_unchanged",
                "'updated' must SUM unchanged (deployed feeders add them); "
                "got updated=%s unchanged=%s"
                % (body2.get("updated"), body2.get("unchanged"))))

    # Old-client nine-field import lands mainline and changes nothing
    # visible: Corvus was seeded exactly this way -- confirm it shows up
    # as ordinary mainline data with zero stream UI (also re-checked in
    # class5, from a different angle here: does the OLD envelope itself
    # still work standalone, right now, unprompted by the seed order).
    status, body = _call("/api/import", {
        "runs": [{
            "environment": "corvus-main", "script": "net/oldclient.py",
            "test_name": "test_nine_field", "result": "PASS",
            "start_time": "2026-08-09T23:00:08.000000",
            "end_time": "2026-08-09T23:00:09.000000",
            "output": "", "source_link": "", "known_failure_reason": None,
        }]
    })
    if status != 200:
        failures.append(fail("class6.old_client_envelope", "%s %s" % (status, body)))
    elif body.get("streams_seen") != []:
        failures.append(fail(
            "class6.old_client_stream_leak",
            "a nine-field old-client record produced a non-empty "
            "streams_seen (should land mainline, silently): %s"
            % body.get("streams_seen")))

    return failures


# ---------------------------------------------------------------------
# Class 3: unscoped catalogs -- per product, every dropdown/list value
# belongs ONLY to that product.
# ---------------------------------------------------------------------
def class3_unscoped_catalogs() -> List[Dict[str, str]]:
    failures = []  # type: List[Dict[str, str]]
    status, body = _call("/api/environments")
    if status != 200:
        failures.append(fail("class3.environments", "%s %s" % (status, body)))
        return failures
    env_product = {e["environment"]: e["product"] for e in body["environments"]}

    declared_products = sorted(set(p for p in env_product.values() if p))
    for product in declared_products:
        own_envs = {e for e, p in env_product.items() if p == product}

        # Dashboard scoped to this product: every row's environment must
        # be one of the product's own.
        status, body = _call(
            "/api/dashboard?product=%s&limit=500" % product)
        if status != 200:
            failures.append(fail("class3.dashboard", "%s %s" % (status, body)))
        else:
            leaked = {t["environment"] for t in body["tests"]} - own_envs
            if leaked:
                failures.append(fail(
                    "class3.dashboard_leak",
                    "product=%s dashboard rows include foreign "
                    "environments: %s" % (product, sorted(leaked))))
            leaked_products = {t.get("product") for t in body["tests"]} - {product}
            if leaked_products:
                failures.append(fail(
                    "class3.dashboard_product_field_leak",
                    "product=%s dashboard rows carry a different "
                    "product field: %s" % (product, leaked_products)))

        # Streams scoped to this product: every stream's product field
        # must equal the requested product (never another product's, and
        # never mainline masquerading in).
        status, body = _call("/api/streams?product=%s" % product)
        if status != 200:
            failures.append(fail("class3.streams", "%s %s" % (status, body)))
        else:
            wrong = [s for s in body["streams"] if s["product"] != product]
            if wrong:
                failures.append(fail(
                    "class3.streams_leak",
                    "product=%s streams list includes foreign-product "
                    "entries: %s" % (product, wrong)))

        # Environments list filtered/scoped: /api/watch p: card's laggard
        # must be one of the product's own environments too.
        status, body = _call("/api/watch?c=p:%s" % product)
        if status == 200 and body["cards"] and body["cards"][0].get("ok"):
            laggard = body["cards"][0].get("laggard")
            if laggard and laggard["environment"] not in own_envs:
                failures.append(fail(
                    "class3.watch_product_laggard_leak",
                    "p:%s card's laggard is a foreign environment: %s"
                    % (product, laggard)))

    # Cross-check: a product's own environments must never appear when
    # scoped to a DIFFERENT declared product.
    if len(declared_products) >= 2:
        a, b = declared_products[0], declared_products[1]
        envs_a = {e for e, p in env_product.items() if p == a}
        status, body = _call("/api/dashboard?product=%s&limit=500" % b)
        if status == 200:
            leaked = {t["environment"] for t in body["tests"]} & envs_a
            if leaked:
                failures.append(fail(
                    "class3.cross_product_leak",
                    "product=%s dashboard includes %s's own environments: %s"
                    % (b, a, sorted(leaked))))

    return failures


# ---------------------------------------------------------------------
# Class 4: dishonest empties.
# ---------------------------------------------------------------------
def class4_dishonest_empties() -> List[Dict[str, str]]:
    failures = []  # type: List[Dict[str, str]]

    status, body = _call("/api/streams?product=Atlas")
    streams = {s["name"]: s for s in body["streams"]} if status == 200 else {}
    cadenced = streams.get("feature/checkout-rewrite")
    if cadenced is None:
        failures.append(fail("class4.setup",
                              "feature/checkout-rewrite stream not found; "
                              "cannot run the stream-scoped-empty checks"))
        return failures

    # Timeline: stream scoped to an environment with NO rows must name
    # where the data IS.
    status, body = _call(
        "/api/timeline?environment=atlas-lab-bravo&stream=%d" % cadenced["id"])
    if status != 200:
        failures.append(fail("class4.timeline_empty_envelope",
                              "%s %s" % (status, body)))
    else:
        if body["blocks"] != [] and body["rows"] != []:
            failures.append(fail(
                "class4.timeline_not_actually_empty",
                "expected atlas-lab-bravo to have zero rows for this "
                "Atlas-lab-alpha-only stream, got blocks=%s rows=%s"
                % (body["blocks"], body["rows"])))
        if body.get("stream_environments") != ["atlas-lab-alpha"]:
            failures.append(fail(
                "class4.timeline_empty_no_honest_hint",
                "empty timeline response does not name the environments "
                "that DO have runs (stream_environments): got %s"
                % body.get("stream_environments")))

    # Time: same shape.
    status, body = _call(
        "/api/time?environment=atlas-lab-bravo&stream=%d" % cadenced["id"])
    if status != 200:
        failures.append(fail("class4.time_empty_envelope", "%s %s" % (status, body)))
    else:
        if body.get("stream_environments") != ["atlas-lab-alpha"]:
            failures.append(fail(
                "class4.time_empty_no_honest_hint",
                "empty /api/time response does not name the environments "
                "that DO have runs: got %s" % body.get("stream_environments")))

    # Watch error card: a bogus card kind/name must carry a SPECIFIC
    # error message, not a bare ok:false with nothing else.
    status, body = _call("/api/watch?c=bogus:xyz&c=s:999999&c=e:no-such-env")
    if status != 200:
        failures.append(fail("class4.watch_error_envelope", "%s %s" % (status, body)))
    else:
        for card in body["cards"]:
            if card.get("ok") is False:
                err = card.get("error", "")
                if not err or len(err) < 8:
                    failures.append(fail(
                        "class4.watch_error_card_blank",
                        "error card for spec=%r has no specific error "
                        "text: %r" % (card.get("spec"), err)))

    # Compare delta: a category with zero rows for a real stream still
    # answers with total=0 and an empty tests list -- the DISHONEST-EMPTY
    # assertion belongs to the DOM (does the page say "no tests in this
    # delta" rather than rendering a blank table), checked in the
    # DOM-shim walk; here we only confirm the API gives the DOM enough
    # to say something honest (category name + total present even at 0).
    status, body = _call(
        "/api/compare?stream=%d&category=both_failing" % cadenced["id"])
    if status == 200 and body["total"] == 0:
        if "category" not in body or body["category"] != "both_failing":
            failures.append(fail(
                "class4.compare_empty_no_category_echo",
                "an empty compare category response omits which category "
                "was asked for, which the empty-state text needs: %s"
                % body))

    # Corvus zero-stream UI: watch/streams endpoints must both agree
    # Corvus has nothing to pick from.
    status, body = _call("/api/streams?product=Corvus")
    if status == 200 and body.get("streams"):
        failures.append(fail(
            "class4.corvus_streams_not_empty",
            "Corvus (should be zero-stream) has streams: %s" % body["streams"]))

    return failures


# ---------------------------------------------------------------------
# Extra: findings the net turned up while orienting, that don't map
# cleanly onto one of the six named classes but are squarely inside
# ONE_KIND_PLAN's "done when" clause (no code path/UI string
# distinguishes branch from build).
# ---------------------------------------------------------------------
def extra_leftover_kind_checks() -> List[Dict[str, str]]:
    failures = []  # type: List[Dict[str, str]]

    # The Open Actions assignment-origin filter (api.py's `origin` query
    # param on /api/dashboard). Originally found (WP-25 tip, 96af20a)
    # still validating against 'branch'/'mainline' instead of
    # 'build'/'mainline', and actions.js's chip carrying the same
    # leftover wording -- fixed by the coordinator at 91fe0bd ("Open
    # Actions' origin filter learns the one-kind dialect"). Asserted
    # here as a positive regression guard, BOTH directions per the
    # coordinator's explicit request: origin=build must be accepted,
    # origin=branch must now be the one that is rejected.
    status, body = _call(
        "/api/dashboard?unassigned=1&result=FAIL&origin=build&limit=1")
    if status != 200:
        failures.append(fail(
            "extra.origin_build_rejected",
            "GET /api/dashboard?origin=build -> %s %s (expected 200 -- "
            "'build' is the only non-mainline kind since WP-25)"
            % (status, body)))
    status, body = _call(
        "/api/dashboard?unassigned=1&result=FAIL&origin=branch&limit=1")
    if status != 400 or "branch" not in json.dumps(body):
        failures.append(fail(
            "extra.origin_branch_not_rejected",
            "GET /api/dashboard?origin=branch -> %s %s (expected a 400 "
            "naming 'branch' as the rejected value -- the 'branch' kind "
            "died before it ever shipped, ONE_KIND_PLAN.md §1.2)"
            % (status, body)))
    path = os.path.join(REPO_ROOT, "static", "actions.js")
    try:
        with open(path, "rb") as fh:
            actions_js = fh.read().decode("utf-8")
        if 'value: "build"' not in actions_js or "Build-originated" not in actions_js:
            failures.append(fail(
                "extra.origin_chip_wording",
                "actions.js's origin-filter chip no longer reads "
                "value:\"build\" / \"Build-originated\" -- check it "
                "wasn't reverted or re-broken"))
        # The word "branch" legitimately still appears in a COMMENT
        # documenting the WP-25 rename (explicitly permitted,
        # ONE_KIND_PLAN.md's "done when": historical log excepted) -- so
        # this checks the specific CHIP CONSTRUCTION shape, not a bare
        # substring, to avoid flagging that comment as a false positive.
        if 'value: "branch"' in actions_js or "Branch-originated" in actions_js:
            failures.append(fail(
                "extra.origin_chip_branch_leftover",
                "actions.js's origin-filter chip construction still uses "
                "'branch' as a value or label"))
    except OSError as exc:
        failures.append(fail("extra.actions_js_read_error_origin", str(exc)))

    # actions.js NUL byte (standing check, NIGHT_RUN_2026-08-09.md §2
    # "plus the standing checks"): the UNASSIGNED sentinel must still be
    # present, byte for byte -- a prior round's edits could have
    # collapsed it without anyone noticing (it renders identically to a
    # normal empty string in every text view).
    path = os.path.join(REPO_ROOT, "static", "actions.js")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if b"\x00" not in data:
            failures.append(fail(
                "extra.actions_js_nul_byte_missing",
                "static/actions.js no longer contains the literal NUL "
                "byte in its UNASSIGNED sentinel (SCOPED_URLS_PLAN.md §7 "
                "standing check)"))
    except OSError as exc:
        failures.append(fail("extra.actions_js_read_error", str(exc)))

    return failures

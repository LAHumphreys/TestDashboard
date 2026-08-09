"""Unit tests for testboard.api.

Handlers are called directly through :func:`testboard.api.handle_api` with
fake :class:`testboard.api.Request` objects against a real
:class:`testboard.storage.Storage` on ``:memory:`` — no HTTP server.

Covers, per the build-spec checklist:

- every endpoint's happy path plus 400 / 404 / 405 (with Allow header)
  and import envelope errors;
- URL-encoded path segments: test names containing ``/`` (as ``%2F``),
  spaces and brackets round-trip through dashboard -> detail -> history,
  and ``+`` is NOT decoded to a space;
- mixed-batch partial import rejection with identity fields carried in
  each error object;
- implicit user creation via comments and via assignee changes;
- POST /api/users idempotency (201 then 200).
"""

import datetime
import json
import os
import re
import sqlite3
import unittest
import urllib.parse
from typing import Any, Dict, List, Optional, Union

from testboard import api
from testboard.model import format_iso
from testboard.storage import (
    COMPARE_CATEGORIES, DASHBOARD_SORTS, MAINLINE_STREAM_ID, QUEUE_KINDS,
    Storage,
)

#: Fixed clock injected into handle_api for deterministic timestamps.
NOW = datetime.datetime(2026, 7, 26, 12, 0, 0)

#: Expected key sets for the pinned JSON shapes.
RUN_OUT_KEYS = {
    "run_id",
    "result",
    "start_time",
    "end_time",
    "duration_seconds",
    "known_failure_reason",
    "source_link",
}
DASHBOARD_ROW_KEYS = {
    "environment",
    "product",
    "script",
    "test_name",
    "run_id",
    "result",
    "start_time",
    "end_time",
    "duration_seconds",
    "known_failure_reason",
    "source_link",
    "assignee",
    "assignment_stream_id",
    "retired_at",
    "retired_by",
}
IMPORT_ERROR_KEYS = {
    "index",
    "error",
    "environment",
    "script",
    "test_name",
    "start_time",
}


def fixed_now() -> datetime.datetime:
    """Deterministic clock passed to handle_api."""
    return NOW


def record(**overrides: Any) -> Dict[str, Any]:
    """Return a fresh valid transport dict, with *overrides* applied."""
    rec = {
        "environment": "linux-sim",
        "script": "suite/alpha.py",
        "test_name": "test_ok",
        "result": "PASS",
        "start_time": "2026-07-25T02:00:00.000000",
        "end_time": "2026-07-25T02:00:03.500000",
        "output": "all good\n",
        "source_link": "https://example.com/alpha.py#L1",
        "known_failure_reason": None,
    }  # type: Dict[str, Any]
    rec.update(overrides)
    return rec


def test_path(
    environment: str, script: str, test_name: str, suffix: str = ""
) -> str:
    """Build an /api/tests/... path with each segment percent-encoded."""
    return "/api/tests/{}/{}/{}{}".format(
        urllib.parse.quote(environment, safe=""),
        urllib.parse.quote(script, safe=""),
        urllib.parse.quote(test_name, safe=""),
        suffix,
    )


class ApiCase(unittest.TestCase):
    """Base case: a real Storage on :memory: plus request helpers."""

    def _make_storage(self) -> Storage:
        """The backend under test. tests/test_mariadb_backend.py
        overrides this to run the same tests against MariaDB."""
        return Storage(":memory:")

    def setUp(self) -> None:
        self.storage = self._make_storage()
        self.addCleanup(self.storage.close)

    def request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, List[str]]] = None,
        body: Optional[Union[bytes, Dict[str, Any], List[Any]]] = None,
    ) -> api.Response:
        """Call handle_api with a fake Request; assert JSON Content-Type."""
        if body is None:
            payload = b""
        elif isinstance(body, bytes):
            payload = body
        else:
            payload = json.dumps(body).encode("utf-8")
        response = api.handle_api(
            self.storage,
            api.Request(
                method=method, path=path, query=query or {}, body=payload
            ),
            now=fixed_now,
        )
        self.assertIn(
            ("Content-Type", "application/json; charset=utf-8"),
            response.headers,
        )
        return response

    def call(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, List[str]]] = None,
        body: Optional[Union[bytes, Dict[str, Any], List[Any]]] = None,
        expect: int = 200,
    ) -> Dict[str, Any]:
        """Call handle_api, assert *expect* status and return parsed JSON."""
        response = self.request(method, path, query=query, body=body)
        data = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            expect, response.status, msg="body: {!r}".format(data)
        )
        return data

    def assert_405(
        self,
        method: str,
        path: str,
        allow: str,
        body: Optional[Union[bytes, Dict[str, Any]]] = None,
    ) -> None:
        """Assert a 405 with the exact Allow header and a JSON error body."""
        response = self.request(method, path, body=body)
        self.assertEqual(405, response.status)
        allows = [v for (k, v) in response.headers if k == "Allow"]
        self.assertEqual([allow], allows)
        data = json.loads(response.body.decode("utf-8"))
        self.assertIn("error", data)

    def import_runs(
        self, records: List[Any], expect: int = 200
    ) -> Dict[str, Any]:
        """POST records to /api/import and return the parsed response."""
        return self.call(
            "POST", "/api/import", body={"runs": records}, expect=expect
        )


class TestImport(ApiCase):
    """POST /api/import: counts, partial rejection, envelope errors."""

    def test_insert_counts(self) -> None:
        data = self.import_runs(
            [record(test_name="test_a"), record(test_name="test_b")]
        )
        self.assertEqual(
            data,
            {
                "inserted": 2, "updated": 0, "unchanged": 0,
                "rejected": 0, "errors": [], "streams_seen": [],
            },
        )

    def test_reimport_updates_without_duplicates(self) -> None:
        batch = [record(test_name="test_a"), record(test_name="test_b")]
        self.import_runs(batch)
        data = self.import_runs(batch)
        self.assertEqual(data["inserted"], 0)
        # On the wire "updated" still counts every accepted, already-known
        # record — deployed feeders sum it — while "unchanged" refines it:
        # both records were byte-identical, so nothing was written.
        self.assertEqual(data["updated"], 2)
        self.assertEqual(data["unchanged"], 2)
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertEqual(len(rows), 2)

    def test_empty_runs_list_is_valid(self) -> None:
        data = self.import_runs([])
        self.assertEqual(
            data,
            {
                "inserted": 0, "updated": 0, "unchanged": 0,
                "rejected": 0, "errors": [], "streams_seen": [],
            },
        )

    def test_mixed_batch_partial_rejection_with_identity(self) -> None:
        good = record(test_name="test_good")
        bad_result = record(test_name="test_bad_result", result="BROKE")
        no_start = record(test_name="test_no_start")
        del no_start["start_time"]
        batch = [good, bad_result, no_start, "not-a-dict"]
        data = self.import_runs(batch)
        self.assertEqual(data["inserted"], 1)
        self.assertEqual(data["updated"], 0)
        self.assertEqual(data["rejected"], 3)
        self.assertEqual(len(data["errors"]), 3)

        err_result, err_start, err_dict = data["errors"]
        for err in data["errors"]:
            self.assertEqual(set(err.keys()), IMPORT_ERROR_KEYS)

        self.assertEqual(err_result["index"], 1)
        self.assertIn("result", err_result["error"])
        self.assertIn("BROKE", err_result["error"])
        self.assertEqual(err_result["environment"], "linux-sim")
        self.assertEqual(err_result["script"], "suite/alpha.py")
        self.assertEqual(err_result["test_name"], "test_bad_result")
        self.assertEqual(
            err_result["start_time"], "2026-07-25T02:00:00.000000"
        )

        self.assertEqual(err_start["index"], 2)
        self.assertIn("start_time", err_start["error"])
        self.assertEqual(err_start["test_name"], "test_no_start")
        self.assertIsNone(err_start["start_time"])

        self.assertEqual(err_dict["index"], 3)
        self.assertIsNone(err_dict["environment"])
        self.assertIsNone(err_dict["script"])
        self.assertIsNone(err_dict["test_name"])
        self.assertIsNone(err_dict["start_time"])

        # The valid record really was stored despite the rejects.
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertEqual([row["test_name"] for row in rows], ["test_good"])

    def test_identity_fields_null_when_not_strings(self) -> None:
        bad = record(environment=42, result="BROKE")
        data = self.import_runs([bad])
        self.assertEqual(data["rejected"], 1)
        err = data["errors"][0]
        self.assertIsNone(err["environment"])
        self.assertEqual(err["script"], "suite/alpha.py")

    def test_envelope_bad_json(self) -> None:
        data = self.call(
            "POST", "/api/import", body=b"{not json", expect=400
        )
        self.assertIn("error", data)

    def test_envelope_body_not_object(self) -> None:
        data = self.call("POST", "/api/import", body=[1, 2], expect=400)
        self.assertIn("error", data)

    def test_envelope_runs_missing(self) -> None:
        data = self.call(
            "POST", "/api/import", body={"nope": []}, expect=400
        )
        self.assertIn("runs", data["error"])

    def test_envelope_runs_not_list(self) -> None:
        data = self.call(
            "POST", "/api/import", body={"runs": {"a": 1}}, expect=400
        )
        self.assertIn("runs", data["error"])

    def test_wrong_method(self) -> None:
        self.assert_405("GET", "/api/import", "POST")


class TestDashboard(ApiCase):
    """GET /api/dashboard: latest-per-test rows, filters, validation."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs(
            [
                record(
                    environment="linux",
                    script="a.py",
                    test_name="t1",
                    result="PASS",
                    start_time="2026-07-24T02:00:00.000000",
                    end_time="2026-07-24T02:00:01.000000",
                ),
                record(
                    environment="linux",
                    script="a.py",
                    test_name="t1",
                    result="FAIL",
                    start_time="2026-07-25T02:00:00.000000",
                    end_time="2026-07-25T02:00:03.500000",
                ),
                record(
                    environment="linux",
                    script="a.py",
                    test_name="t2",
                    result="PASS",
                ),
                record(
                    environment="win",
                    script="b.py",
                    test_name="t3",
                    result="PASS",
                ),
            ]
        )

    def test_latest_per_test_and_row_shape(self) -> None:
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertEqual(len(rows), 3)
        by_name = {row["test_name"]: row for row in rows}
        t1 = by_name["t1"]
        self.assertEqual(set(t1.keys()), DASHBOARD_ROW_KEYS)
        self.assertEqual(t1["result"], "FAIL")
        self.assertEqual(t1["start_time"], "2026-07-25T02:00:00.000000")
        self.assertEqual(t1["duration_seconds"], 3.5)
        self.assertIsNone(t1["assignee"])
        self.assertIsNone(t1["known_failure_reason"])
        self.assertNotIn("output", t1)

    def test_ordering_by_environment_script_test(self) -> None:
        rows = self.call("GET", "/api/dashboard")["tests"]
        triples = [
            (row["environment"], row["script"], row["test_name"])
            for row in rows
        ]
        self.assertEqual(triples, sorted(triples))

    def test_filter_environment(self) -> None:
        rows = self.call(
            "GET", "/api/dashboard", query={"environment": ["win"]}
        )["tests"]
        self.assertEqual([row["test_name"] for row in rows], ["t3"])

    def test_filter_script(self) -> None:
        rows = self.call(
            "GET", "/api/dashboard", query={"script": ["a.py"]}
        )["tests"]
        self.assertEqual(
            [row["test_name"] for row in rows], ["t1", "t2"]
        )

    def test_filter_result_repeatable(self) -> None:
        rows = self.call(
            "GET",
            "/api/dashboard",
            query={"result": ["FAIL", "UNEXPECTED_PASS"]},
        )["tests"]
        self.assertEqual([row["test_name"] for row in rows], ["t1"])

    def test_filter_q_substring(self) -> None:
        rows = self.call("GET", "/api/dashboard", query={"q": ["t1"]})[
            "tests"
        ]
        self.assertEqual([row["test_name"] for row in rows], ["t1"])

    def test_invalid_result_400(self) -> None:
        data = self.call(
            "GET", "/api/dashboard", query={"result": ["BROKE"]}, expect=400
        )
        self.assertIn("result", data["error"])
        self.assertIn("BROKE", data["error"])

    def test_wrong_method(self) -> None:
        self.assert_405("POST", "/api/dashboard", "GET")


class TestDashboardAssignmentOrigin(ApiCase):
    """``/api/dashboard``'s ``origin=`` filter and ``streams`` map
    (WP-21, Open Actions §3.6)."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(test_name="test_a"),
            record(test_name="test_b"),
        ])
        self.import_runs([record(
            test_name="test_c", build="feat/x",
            start_time="2026-07-25T03:00:00.000000",
            end_time="2026-07-25T03:00:03.000000")])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]
        self.call(
            "PUT", test_path("linux-sim", "suite/alpha.py", "test_a",
                              "/assignee"),
            body={"username": "alice", "assigned_by": "bob",
                  "stream_id": self.stream_id})
        self.call(
            "PUT", test_path("linux-sim", "suite/alpha.py", "test_b",
                              "/assignee"),
            body={"username": "alice", "assigned_by": "bob"})

    def test_no_filter_returns_the_streams_map_for_the_page(self) -> None:
        data = self.call("GET", "/api/dashboard")
        self.assertEqual(
            data["streams"][str(self.stream_id)]["kind"], "build")
        self.assertEqual(
            data["streams"][str(self.stream_id)]["name"], "feat/x")

    def test_no_page_streams_no_map_entries(self) -> None:
        """A test with no non-mainline-originated assignment on the
        returned page must not spuriously carry a streams entry."""
        data = self.call(
            "GET", "/api/dashboard", query={"q": ["test_b"]})
        self.assertEqual(data["streams"], {})

    def test_origin_build_filters_to_the_build_made_assignment(
            self) -> None:
        rows = self.call(
            "GET", "/api/dashboard", query={"origin": ["build"]}
        )["tests"]
        self.assertEqual([r["test_name"] for r in rows], ["test_a"])

    def test_the_dead_branch_spelling_is_rejected(self) -> None:
        """WP-25 renamed the origin value branch->build before anything
        shipped; the old spelling must 400 like any other unknown value,
        not silently match nothing."""
        data = self.call(
            "GET", "/api/dashboard", query={"origin": ["branch"]},
            expect=400)
        self.assertIn("origin", data["error"])

    def test_origin_mainline_excludes_the_build_made_assignment(
            self) -> None:
        rows = self.call(
            "GET", "/api/dashboard", query={"origin": ["mainline"]}
        )["tests"]
        self.assertNotIn(
            "test_a", [r["test_name"] for r in rows])

    def test_an_invalid_origin_value_is_400(self) -> None:
        data = self.call(
            "GET", "/api/dashboard", query={"origin": ["nonsense"]},
            expect=400)
        self.assertIn("origin", data["error"])

    def test_row_carries_the_assignment_stream_id(self) -> None:
        rows = self.call(
            "GET", "/api/dashboard", query={"q": ["test_a"]}
        )["tests"]
        self.assertEqual(rows[0]["assignment_stream_id"], self.stream_id)


class TestDashboardPaging(ApiCase):
    """GET /api/dashboard paging, sorting and their validation."""

    NAMES = ["t{:02d}".format(i) for i in range(12)]

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(
                environment="linux", script="a.py", test_name=name,
                result="FAIL" if index % 4 == 0 else "PASS",
            )
            for index, name in enumerate(self.NAMES)
        ])

    def test_response_carries_the_window_and_the_exact_total(self) -> None:
        page = self.call(
            "GET", "/api/dashboard", query={"limit": ["5"]}
        )
        self.assertEqual(len(page["tests"]), 5)
        self.assertEqual(page["total"], 12)
        self.assertEqual(page["limit"], 5)
        self.assertEqual(page["offset"], 0)

    def test_paging_covers_every_row_exactly_once(self) -> None:
        seen = []  # type: List[str]
        for offset in (0, 5, 10):
            page = self.call(
                "GET", "/api/dashboard",
                query={"limit": ["5"], "offset": [str(offset)]},
            )
            seen.extend(row["test_name"] for row in page["tests"])
        self.assertEqual(sorted(seen), sorted(self.NAMES))

    def test_total_reflects_filters_not_the_page(self) -> None:
        page = self.call(
            "GET", "/api/dashboard",
            query={"result": ["FAIL"], "limit": ["1"]},
        )
        self.assertEqual(len(page["tests"]), 1)
        self.assertEqual(page["total"], 3)

    def test_sort_and_order(self) -> None:
        page = self.call(
            "GET", "/api/dashboard",
            query={"sort": ["test_name"], "order": ["desc"], "limit": ["2"]},
        )
        self.assertEqual(
            [row["test_name"] for row in page["tests"]], ["t11", "t10"]
        )

    def test_every_advertised_sort_key_is_accepted(self) -> None:
        for key in DASHBOARD_SORTS:
            page = self.call(
                "GET", "/api/dashboard", query={"sort": [key]}
            )
            self.assertEqual(page["total"], 12)

    def test_unknown_sort_400(self) -> None:
        data = self.call(
            "GET", "/api/dashboard", query={"sort": ["output"]}, expect=400
        )
        self.assertIn("sort", data["error"])

    def test_bad_order_400(self) -> None:
        data = self.call(
            "GET", "/api/dashboard", query={"order": ["sideways"]},
            expect=400,
        )
        self.assertIn("order", data["error"])

    def test_limit_out_of_range_400(self) -> None:
        for value in ("0", "100000", "abc"):
            data = self.call(
                "GET", "/api/dashboard", query={"limit": [value]},
                expect=400,
            )
            self.assertIn("limit", data["error"])

    def test_negative_offset_400(self) -> None:
        data = self.call(
            "GET", "/api/dashboard", query={"offset": ["-1"]}, expect=400
        )
        self.assertIn("offset", data["error"])

    def test_stale_filter_uses_the_recency_window(self) -> None:
        """stale=1 keeps only tests whose latest run predates the window."""
        self.import_runs([record(
            environment="linux", script="a.py", test_name="t_old",
            result="PASS",
            start_time="2026-07-20T02:00:00.000000",
            end_time="2026-07-20T02:00:01.000000",
        )])
        page = self.call(
            "GET", "/api/dashboard", query={"stale": ["1"]}
        )
        self.assertEqual(
            [row["test_name"] for row in page["tests"]], ["t_old"]
        )
        self.assertEqual(page["total"], 1)


class TestDetail(ApiCase):
    """GET /api/tests/{env}/{script}/{test}: detail plus analytics."""

    ENV = "linux"
    SCRIPT = "suite/alpha.py"
    NAME = "test_flow"

    def setUp(self) -> None:
        super().setUp()
        self.import_runs(
            [
                record(
                    environment=self.ENV,
                    script=self.SCRIPT,
                    test_name=self.NAME,
                    result="PASS",
                    start_time="2026-07-23T02:00:00.000000",
                    end_time="2026-07-23T02:00:01.000000",
                    source_link="https://example.com/old",
                ),
                record(
                    environment=self.ENV,
                    script=self.SCRIPT,
                    test_name=self.NAME,
                    result="PASS",
                    start_time="2026-07-24T02:00:00.000000",
                    end_time="2026-07-24T02:00:02.000000",
                    source_link="https://example.com/old",
                ),
                record(
                    environment=self.ENV,
                    script=self.SCRIPT,
                    test_name=self.NAME,
                    result="FAIL",
                    start_time="2026-07-25T02:00:00.000000",
                    end_time="2026-07-25T02:00:03.000000",
                    source_link="https://example.com/new",
                ),
            ]
        )
        self.path = test_path(self.ENV, self.SCRIPT, self.NAME)

    def test_detail_shape_and_analytics(self) -> None:
        data = self.call("GET", self.path)
        self.assertEqual(
            set(data.keys()),
            {
                "environment",
                "script",
                "test_name",
                "source_link",
                "assignee",
                "latest",
                "analytics",
                "stream",
                "stream_identity",
            },
        )
        self.assertEqual(data["environment"], self.ENV)
        self.assertEqual(data["script"], self.SCRIPT)
        self.assertEqual(data["test_name"], self.NAME)
        self.assertEqual(data["source_link"], "https://example.com/new")
        self.assertIsNone(data["assignee"])

        latest = data["latest"]
        self.assertEqual(set(latest.keys()), RUN_OUT_KEYS)
        self.assertEqual(latest["result"], "FAIL")
        self.assertEqual(
            latest["start_time"], "2026-07-25T02:00:00.000000"
        )
        self.assertEqual(latest["duration_seconds"], 3.0)

        analytics = data["analytics"]
        window = analytics["window"]
        self.assertEqual(window["run_count"], 3)
        self.assertEqual(window["max_days"], 90)
        self.assertEqual(window["max_runs"], 200)
        self.assertEqual(window["to"], format_iso(NOW))
        self.assertEqual(
            window["from"],
            format_iso(NOW - datetime.timedelta(days=90)),
        )
        self.assertEqual(
            analytics["failing_since"]["run_id"], latest["run_id"]
        )
        self.assertEqual(
            analytics["last_pass_before_failure"]["result"], "PASS"
        )
        self.assertEqual(len(analytics["day_of_week"]), 7)
        self.assertEqual(
            set(analytics["flakiness"].keys()),
            {"transitions", "score", "classification"},
        )
        self.assertEqual(
            set(analytics["duration_seconds"].keys()),
            {"min", "median", "max"},
        )

    def test_unknown_triple_404(self) -> None:
        data = self.call(
            "GET", test_path("linux", "a.py", "no_such"), expect=404
        )
        self.assertIn("error", data)

    def test_wrong_method(self) -> None:
        self.assert_405("POST", self.path, "GET")

    def test_assignee_reflected_after_put(self) -> None:
        self.call(
            "PUT",
            self.path + "/assignee",
            body={"username": "alice", "assigned_by": "bob"},
        )
        data = self.call("GET", self.path)
        self.assertEqual(data["assignee"], "alice")


class TestHistory(ApiCase):
    """GET .../history: pagination, validation, newest-first order."""

    ENV = "linux"
    SCRIPT = "suite/alpha.py"
    NAME = "test_flow"

    def setUp(self) -> None:
        super().setUp()
        self.import_runs(
            [
                record(
                    environment=self.ENV,
                    script=self.SCRIPT,
                    test_name=self.NAME,
                    start_time="2026-07-{:02d}T02:00:00.000000".format(day),
                    end_time="2026-07-{:02d}T02:00:01.000000".format(day),
                )
                for day in range(21, 26)
            ]
        )
        self.path = test_path(self.ENV, self.SCRIPT, self.NAME, "/history")

    def test_newest_first_without_output(self) -> None:
        runs = self.call("GET", self.path)["runs"]
        self.assertEqual(len(runs), 5)
        starts = [run["start_time"] for run in runs]
        self.assertEqual(starts, sorted(starts, reverse=True))
        for run in runs:
            self.assertEqual(set(run.keys()), RUN_OUT_KEYS)
            self.assertNotIn("output", run)

    def test_limit(self) -> None:
        runs = self.call("GET", self.path, query={"limit": ["2"]})["runs"]
        self.assertEqual(
            [run["start_time"] for run in runs],
            [
                "2026-07-25T02:00:00.000000",
                "2026-07-24T02:00:00.000000",
            ],
        )

    def test_limit_edge_values_accepted(self) -> None:
        runs = self.call("GET", self.path, query={"limit": ["1"]})["runs"]
        self.assertEqual(len(runs), 1)
        runs = self.call("GET", self.path, query={"limit": ["500"]})["runs"]
        self.assertEqual(len(runs), 5)

    def test_before_pagination(self) -> None:
        runs = self.call(
            "GET",
            self.path,
            query={"before": ["2026-07-23T02:00:00.000000"]},
        )["runs"]
        self.assertEqual(
            [run["start_time"] for run in runs],
            [
                "2026-07-22T02:00:00.000000",
                "2026-07-21T02:00:00.000000",
            ],
        )

    def test_bad_limit_values_400(self) -> None:
        for bad in ("abc", "0", "501", "1.5"):
            with self.subTest(limit=bad):
                data = self.call(
                    "GET", self.path, query={"limit": [bad]}, expect=400
                )
                self.assertIn("limit", data["error"])

    def test_bad_before_400(self) -> None:
        data = self.call(
            "GET", self.path, query={"before": ["yesterday"]}, expect=400
        )
        self.assertIn("before", data["error"])

    def test_unknown_triple_404(self) -> None:
        self.call(
            "GET",
            test_path("linux", "a.py", "no_such", "/history"),
            expect=404,
        )

    def test_wrong_method(self) -> None:
        self.assert_405("POST", self.path, "GET")


class TestBuildRebuildHistory(ApiCase):
    """WP-22 (docs/STREAMS_PLAN.md §4.1): a rebuild is just a second
    import under the same `build` name -- verifies that the WP-21
    history table (unchanged code) already reads a build's rebuild
    correctly rather than assuming it without checking (item 7 of the
    WP-22 work order)."""

    ENV = "linux-sim"
    SCRIPT = "suite/alpha.py"
    NAME = "test_flow"

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(environment=self.ENV, script=self.SCRIPT,
                   test_name=self.NAME, result="FAIL", build="1.0",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
        ])
        # The rebuild: same build name, a LATER run of the same test.
        self.import_runs([
            record(environment=self.ENV, script=self.SCRIPT,
                   test_name=self.NAME, result="PASS", build="1.0",
                   start_time="2026-07-25T05:00:00.000000",
                   end_time="2026-07-25T05:00:03.000000"),
        ])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]

    def test_the_rebuild_creates_no_second_stream(self) -> None:
        """Re-importing under the SAME name is the same stream, not a
        new one -- docs/STREAMS_PLAN.md §3.3's find-or-create rule."""
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["name"], "1.0")

    def test_the_newer_run_wins_as_latest(self) -> None:
        data = self.call(
            "GET", test_path(self.ENV, self.SCRIPT, self.NAME),
            query={"stream": [str(self.stream_id)]})
        self.assertEqual(data["latest"]["result"], "PASS")
        self.assertEqual(
            data["latest"]["start_time"], "2026-07-25T05:00:00.000000")

    def test_history_still_shows_both_runs_newest_first(self) -> None:
        """The older (superseded) run is not lost -- it is a real row
        in `runs`, just no longer the one `latest_runs` points to."""
        data = self.call(
            "GET",
            test_path(self.ENV, self.SCRIPT, self.NAME, "/history"),
            query={"stream": [str(self.stream_id)]})
        results = [run["result"] for run in data["runs"]]
        starts = [run["start_time"] for run in data["runs"]]
        self.assertEqual(results, ["PASS", "FAIL"])
        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_the_every_build_endpoint_also_reflects_the_newer_run(
            self) -> None:
        data = self.call(
            "GET",
            test_path(self.ENV, self.SCRIPT, self.NAME, "/streams"))
        (row,) = data["results"]
        self.assertEqual(row["result"], "PASS")
        self.assertEqual(row["start_time"], "2026-07-25T05:00:00.000000")


class TestStreamResults(ApiCase):
    """GET .../streams (WP-22, docs/STREAMS_PLAN.md §4.1): a triple's
    latest result on every stream that HAS one, newest first -- the test
    page's "Every build" table and its stream dropdown."""

    ENV = "linux-sim"
    SCRIPT = "suite/alpha.py"
    NAME = "test_flow"

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(environment=self.ENV, script=self.SCRIPT,
                   test_name=self.NAME, result="PASS"),
        ])
        self.call(
            "PUT", "/api/environments/{}/product".format(self.ENV),
            body={"product": "Atlas", "username": "amy"})
        self.import_runs([
            record(environment=self.ENV, script=self.SCRIPT,
                   test_name=self.NAME, result="FAIL", build="1.0",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
        ])
        self.import_runs([
            record(environment=self.ENV, script=self.SCRIPT,
                   test_name=self.NAME, result="FAIL", build="feat/x",
                   start_time="2026-07-25T04:00:00.000000",
                   end_time="2026-07-25T04:00:03.000000"),
        ])
        # A stream that never ran THIS test -- must never appear.
        self.import_runs([
            record(environment=self.ENV, script=self.SCRIPT,
                   test_name="test_other", build="9.9",
                   start_time="2026-07-25T02:30:00.000000",
                   end_time="2026-07-25T02:30:03.000000"),
        ])
        self.path = test_path(self.ENV, self.SCRIPT, self.NAME, "/streams")

    def test_newest_first_and_identity_shape(self) -> None:
        data = self.call("GET", self.path)
        self.assertEqual(data["environment"], self.ENV)
        self.assertEqual(data["script"], self.SCRIPT)
        self.assertEqual(data["test_name"], self.NAME)
        self.assertEqual(data["product"], "Atlas")
        kinds = [row["stream"]["kind"] for row in data["results"]]
        self.assertEqual(kinds, ["build", "build", "mainline"])
        self.assertEqual(data["results"][0]["result"], "FAIL")
        self.assertIn("run_id", data["results"][0])
        self.assertIn("start_time", data["results"][0])

    def test_product_is_empty_string_when_undeclared(self) -> None:
        """The mainline row's own `stream.product` is always "" (it is
        universal), which must never be mistaken for THIS environment's
        declared product -- exercised on an environment nobody has
        mapped, where both happen to agree by coincidence, and again
        implicitly by test_newest_first_and_identity_shape above where
        they must NOT agree."""
        self.import_runs([
            record(environment="unmapped-env", test_name="lonely",
                   result="PASS"),
        ])
        data = self.call(
            "GET",
            test_path("unmapped-env", self.SCRIPT, "lonely", "/streams"))
        self.assertEqual(data["product"], "")

    def test_a_stream_that_never_ran_this_test_is_absent(self) -> None:
        data = self.call("GET", self.path)
        names = {row["stream"]["name"] for row in data["results"]}
        self.assertNotIn("9.9", names)

    def test_unknown_triple_404(self) -> None:
        self.call(
            "GET",
            test_path(self.ENV, self.SCRIPT, "no_such", "/streams"),
            expect=404,
        )

    def test_wrong_method(self) -> None:
        self.assert_405("POST", self.path, "GET")


class TestRunEndpoint(ApiCase):
    """GET /api/runs/{run_id}: the only place output is returned."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs(
            [record(output="line one\nline two\n")]
        )
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.run_id = rows[0]["run_id"]

    def test_run_includes_output_and_identity(self) -> None:
        data = self.call("GET", "/api/runs/{}".format(self.run_id))
        self.assertEqual(
            set(data.keys()),
            RUN_OUT_KEYS
            | {"environment", "script", "test_name", "output"},
        )
        self.assertEqual(data["run_id"], self.run_id)
        self.assertEqual(data["environment"], "linux-sim")
        self.assertEqual(data["script"], "suite/alpha.py")
        self.assertEqual(data["test_name"], "test_ok")
        self.assertEqual(data["output"], "line one\nline two\n")
        self.assertEqual(data["duration_seconds"], 3.5)

    def test_unknown_id_404(self) -> None:
        data = self.call("GET", "/api/runs/999999", expect=404)
        self.assertIn("error", data)

    def test_non_integer_id_404(self) -> None:
        for bad in ("abc", "1.5", ""):
            path = "/api/runs/{}".format(bad)
            with self.subTest(path=path):
                # "" collapses to /api/runs -> also an unknown route.
                self.call("GET", path, expect=404)

    def test_wrong_method(self) -> None:
        self.assert_405(
            "DELETE", "/api/runs/{}".format(self.run_id), "GET"
        )


class TestComments(ApiCase):
    """GET/POST .../comments: thread, validation, implicit users."""

    ENV = "linux"
    SCRIPT = "suite/alpha.py"
    NAME = "test_flow"

    def setUp(self) -> None:
        super().setUp()
        self.import_runs(
            [
                record(
                    environment=self.ENV,
                    script=self.SCRIPT,
                    test_name=self.NAME,
                )
            ]
        )
        self.path = test_path(self.ENV, self.SCRIPT, self.NAME, "/comments")

    def test_empty_thread(self) -> None:
        data = self.call("GET", self.path)
        self.assertEqual(data, {"comments": [], "streams": {}})

    def test_post_and_get_oldest_first(self) -> None:
        first = self.call(
            "POST",
            self.path,
            body={"username": "alice", "text": "first comment"},
            expect=201,
        )["comment"]
        second = self.call(
            "POST",
            self.path,
            body={"username": "bob", "text": "second comment"},
            expect=201,
        )["comment"]
        self.assertEqual(
            set(first.keys()),
            {"id", "author", "created_at", "text", "stream_id"},
        )
        self.assertEqual(first["author"], "alice")
        self.assertEqual(first["text"], "first comment")
        self.assertEqual(first["created_at"], format_iso(NOW))
        self.assertLess(first["id"], second["id"])

        comments = self.call("GET", self.path)["comments"]
        self.assertEqual(
            [c["text"] for c in comments],
            ["first comment", "second comment"],
        )

    def test_implicit_user_creation_via_comment(self) -> None:
        self.call(
            "POST",
            self.path,
            body={"username": "alice", "text": "hi"},
            expect=201,
        )
        users = self.call("GET", "/api/users")["users"]
        self.assertIn("alice", [u["username"] for u in users])

    def test_username_stripped(self) -> None:
        data = self.call(
            "POST",
            self.path,
            body={"username": "  carol  ", "text": "hi"},
            expect=201,
        )
        self.assertEqual(data["comment"]["author"], "carol")

    def test_validation_errors_400(self) -> None:
        cases = [
            ("username missing", {"text": "hi"}, "username"),
            ("username empty", {"username": "  ", "text": "hi"}, "username"),
            (
                "username too long",
                {"username": "x" * 101, "text": "hi"},
                "username",
            ),
            (
                "username not str",
                {"username": 5, "text": "hi"},
                "username",
            ),
            ("text missing", {"username": "alice"}, "text"),
            ("text empty", {"username": "alice", "text": ""}, "text"),
            (
                "text too long",
                {"username": "alice", "text": "x" * 10001},
                "text",
            ),
            (
                "text not str",
                {"username": "alice", "text": 7},
                "text",
            ),
        ]
        for label, body, field in cases:
            with self.subTest(label=label):
                data = self.call("POST", self.path, body=body, expect=400)
                self.assertIn(field, data["error"])

    def test_body_not_object_400(self) -> None:
        self.call("POST", self.path, body=[1, 2], expect=400)

    def test_bad_json_400(self) -> None:
        self.call("POST", self.path, body=b"{oops", expect=400)

    def test_unknown_triple_404(self) -> None:
        missing = test_path("linux", "a.py", "no_such", "/comments")
        self.call("GET", missing, expect=404)
        self.call(
            "POST",
            missing,
            body={"username": "alice", "text": "hi"},
            expect=404,
        )

    def test_wrong_method(self) -> None:
        self.assert_405(
            "PUT",
            self.path,
            "GET, POST",
            body={"username": "alice", "text": "hi"},
        )


class TestAssignee(ApiCase):
    """PUT .../assignee: assign, clear, validation, implicit users."""

    ENV = "linux"
    SCRIPT = "suite/alpha.py"
    NAME = "test_flow"

    def setUp(self) -> None:
        super().setUp()
        self.import_runs(
            [
                record(
                    environment=self.ENV,
                    script=self.SCRIPT,
                    test_name=self.NAME,
                )
            ]
        )
        self.detail_path = test_path(self.ENV, self.SCRIPT, self.NAME)
        self.path = self.detail_path + "/assignee"

    def test_assign_then_clear(self) -> None:
        data = self.call(
            "PUT",
            self.path,
            body={"username": "alice", "assigned_by": "bob"},
        )
        self.assertEqual(data, {"assignee": "alice"})
        self.assertEqual(
            self.call("GET", self.detail_path)["assignee"], "alice"
        )

        data = self.call(
            "PUT",
            self.path,
            body={"username": None, "assigned_by": "bob"},
        )
        self.assertEqual(data, {"assignee": None})
        self.assertIsNone(self.call("GET", self.detail_path)["assignee"])

    def test_implicit_user_creation_for_both(self) -> None:
        self.call(
            "PUT",
            self.path,
            body={"username": "alice", "assigned_by": "bob"},
        )
        usernames = [
            u["username"] for u in self.call("GET", "/api/users")["users"]
        ]
        self.assertIn("alice", usernames)
        self.assertIn("bob", usernames)

    def test_assignee_visible_on_dashboard(self) -> None:
        self.call(
            "PUT",
            self.path,
            body={"username": "alice", "assigned_by": "bob"},
        )
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertEqual(rows[0]["assignee"], "alice")

    def test_username_key_required_400(self) -> None:
        data = self.call(
            "PUT", self.path, body={"assigned_by": "bob"}, expect=400
        )
        self.assertIn("username", data["error"])

    def test_assigned_by_required_and_non_empty_400(self) -> None:
        for body in (
            {"username": "alice"},
            {"username": "alice", "assigned_by": "   "},
            {"username": "alice", "assigned_by": 9},
        ):
            with self.subTest(body=body):
                data = self.call("PUT", self.path, body=body, expect=400)
                self.assertIn("assigned_by", data["error"])

    def test_username_wrong_type_400(self) -> None:
        data = self.call(
            "PUT",
            self.path,
            body={"username": 42, "assigned_by": "bob"},
            expect=400,
        )
        self.assertIn("username", data["error"])

    def test_unknown_triple_404(self) -> None:
        self.call(
            "PUT",
            test_path("linux", "a.py", "no_such", "/assignee"),
            body={"username": "alice", "assigned_by": "bob"},
            expect=404,
        )

    def test_wrong_method(self) -> None:
        self.assert_405("GET", self.path, "PUT")
        self.assert_405(
            "POST",
            self.path,
            "PUT",
            body={"username": "alice", "assigned_by": "bob"},
        )

    def test_a_deactivated_user_cannot_be_assigned_work(self) -> None:
        """The picker will not offer them, but the picker is not the
        boundary: a stale page or a script would still get through, and
        the resulting assignment is invisible to everyone."""
        self.call("POST", "/api/users", body={"username": "alice"},
                  expect=201)
        self.call("PUT", "/api/users/alice/active",
                  body={"active": False, "changed_by": "bob"})
        data = self.call(
            "PUT", self.path,
            body={"username": "alice", "assigned_by": "bob"}, expect=400)
        self.assertIn("deactivated", data["error"])
        self.assertIn("alice", data["error"])

    def test_clearing_an_assignment_is_never_blocked(self) -> None:
        """Unassigning must keep working whatever the account's state —
        it is the way out of the situation, not another instance of it."""
        self.call("PUT", self.path,
                  body={"username": "alice", "assigned_by": "bob"})
        self.call("PUT", "/api/users/alice/active",
                  body={"active": False, "changed_by": "bob"}, expect=409)
        self.call("PUT", self.path,
                  body={"username": None, "assigned_by": "bob"})
        self.call("PUT", "/api/users/alice/active",
                  body={"active": False, "changed_by": "bob"})

    def test_deactivating_an_owner_is_refused_and_says_what_they_hold(
        self
    ) -> None:
        """The invisible-queue guard.

        Deactivating someone who still owns live tests leaves that work
        assigned to a name no picker offers. Nothing would ever surface
        it again, so this is a hard refusal rather than a warning.
        """
        self.call("PUT", self.path,
                  body={"username": "alice", "assigned_by": "bob"})
        data = self.call("PUT", "/api/users/alice/active",
                         body={"active": False, "changed_by": "bob"},
                         expect=409)
        self.assertIn("alice", data["error"])
        self.assertIn(self.NAME, data["error"])
        self.assertIn("Reassign", data["error"])
        # And it really did not happen.
        listed = self.call("GET", "/api/users")["users"]
        self.assertIn("alice", [u["username"] for u in listed])

    def test_retiring_the_test_unblocks_deactivation(self) -> None:
        """A retired test is not open work, and retirement deliberately
        leaves the assignment in place — so it would block forever."""
        self.call("PUT", self.path,
                  body={"username": "alice", "assigned_by": "bob"})
        self.call("PUT", "/api/users/alice/active",
                  body={"active": False, "changed_by": "bob"}, expect=409)
        self.call(
            "PUT", self.detail_path + "/retired",
            body={"retired": True, "username": "bob",
                  "comment": "suite dropped it"})
        self.call("PUT", "/api/users/alice/active",
                  body={"active": False, "changed_by": "bob"})


class TestAssigneeStreamId(ApiCase):
    """PUT .../assignee's optional ``stream_id`` (WP-21, folded into
    migration 9): WHERE the assignment was made from — the frontend
    sends the page's current stream scope when set."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([record(
            environment="linux-sim", test_name="test_a")])
        self.import_runs([record(
            environment="linux-sim", test_name="test_a", build="feat/x",
            result="FAIL",
            start_time="2026-07-25T03:00:00.000000",
            end_time="2026-07-25T03:00:03.000000")])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]
        self.path = test_path(
            "linux-sim", "suite/alpha.py", "test_a", "/assignee")

    def test_defaults_to_null(self) -> None:
        self.call("PUT", self.path,
                   body={"username": "alice", "assigned_by": "bob"})
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertIsNone(rows[0]["assignment_stream_id"])

    def test_round_trips_when_given(self) -> None:
        self.call("PUT", self.path, body={
            "username": "alice", "assigned_by": "bob",
            "stream_id": self.stream_id,
        })
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertEqual(rows[0]["assignment_stream_id"], self.stream_id)
        self.assertEqual(rows[0]["assignee"], "alice")

    def test_unknown_stream_id_is_404(self) -> None:
        self.call("PUT", self.path, body={
            "username": "alice", "assigned_by": "bob",
            "stream_id": 999999,
        }, expect=404)

    def test_non_integer_stream_id_is_400(self) -> None:
        self.call("PUT", self.path, body={
            "username": "alice", "assigned_by": "bob",
            "stream_id": "nope",
        }, expect=400)


class TestOriginResultTruthfulDisplay(ApiCase):
    """ADDENDUM to the perf round: a row whose assignment origin is a
    non-mainline stream must not show only mainline's result -- that
    read as a contradiction on its face ("assigned from the RC" showing
    PASS while the RC failure it represents is live). ``origin_result``
    (docs/STREAMS_PLAN.md §5.4) is the batched fix.

    Fixture, deliberately the exact contradiction case: mainline's
    test_a PASSES, the branch feat/x's test_a FAILS -- the same shape
    TestAssigneeStreamId already seeds, reused here.
    """

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([record(
            environment="linux-sim", test_name="test_a", result="PASS")])
        self.import_runs([record(
            environment="linux-sim", test_name="test_a", build="feat/x",
            result="FAIL",
            start_time="2026-07-25T03:00:00.000000",
            end_time="2026-07-25T03:00:03.000000")])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]
        self.path = test_path(
            "linux-sim", "suite/alpha.py", "test_a", "/assignee")

    def test_non_mainline_origin_carries_its_own_result(self) -> None:
        self.call("PUT", self.path, body={
            "username": "alice", "assigned_by": "bob",
            "stream_id": self.stream_id,
        })
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertEqual(rows[0]["result"], "PASS")   # mainline, unchanged
        self.assertEqual(rows[0]["origin_result"], "FAIL")   # the branch

    def test_mainline_origin_has_no_origin_result_field_at_all(
        self
    ) -> None:
        """Not merely null -- ABSENT, so a mainline-origin row's payload
        is byte-identical to before this addendum (zero visible
        change)."""
        self.call("PUT", self.path,
                   body={"username": "alice", "assigned_by": "bob"})
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertNotIn("origin_result", rows[0])

    def test_the_origin_stream_never_ran_the_test_gives_none(self) -> None:
        """Never a fabricated result, never a colour for absence --
        None (rendered client-side as "no result")."""
        self.import_runs([record(
            environment="linux-sim", test_name="test_never_on_branch",
            result="PASS")])
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py",
                      "test_never_on_branch", "/assignee"),
            body={"username": "alice", "assigned_by": "bob",
                  "stream_id": self.stream_id})
        rows = {
            r["test_name"]: r
            for r in self.call("GET", "/api/dashboard")["tests"]
        }
        self.assertIsNone(rows["test_never_on_branch"]["origin_result"])

    def test_an_estate_with_no_stream_origin_assignments_costs_no_extra_query(
        self
    ) -> None:
        """The common case -- no row on the page has a non-mainline
        origin -- must not run the batched lookup at all."""
        self.call("PUT", self.path,
                   body={"username": "alice", "assigned_by": "bob"})
        seen = []  # type: List[str]
        conn = self.storage._conn()
        _trace_sql_into(conn, seen)
        try:
            self.call("GET", "/api/dashboard")
        finally:
            conn.set_trace_callback(None)
        origin_queries = [s for s in seen if "FROM latest_runs WHERE (" in s]
        self.assertEqual(origin_queries, [])

    def test_many_origin_rows_cost_exactly_one_extra_query(self) -> None:
        """Batched, not per-row -- several rows with a non-mainline
        origin on one page must still cost ONE extra query total."""
        names = ["test_a", "test_b", "test_c"]
        for name in names[1:]:
            self.import_runs([record(
                environment="linux-sim", test_name=name, result="PASS")])
            self.import_runs([record(
                environment="linux-sim", test_name=name, build="feat/x",
                result="FAIL",
                start_time="2026-07-25T03:00:00.000000",
                end_time="2026-07-25T03:00:03.000000")])
        for name in names:
            self.call(
                "PUT",
                test_path("linux-sim", "suite/alpha.py", name, "/assignee"),
                body={"username": "alice", "assigned_by": "bob",
                      "stream_id": self.stream_id})
        seen = []  # type: List[str]
        conn = self.storage._conn()
        _trace_sql_into(conn, seen)
        try:
            data = self.call("GET", "/api/dashboard")
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(len(data["tests"]), 3)
        origin_queries = [s for s in seen if "FROM latest_runs WHERE (" in s]
        self.assertEqual(len(origin_queries), 1, origin_queries)


class TestSortingIsStable(ApiCase):
    """Every sort key must page without repeating or skipping a row.

    This is the test that catches a missing primary-key tiebreak. A sort
    on a column with duplicate values — ``result`` has four possible
    values across the whole estate — leaves SQLite free to order the
    ties however it likes, and it need not choose the same order twice.
    Page 1 and page 2 then overlap: a row appears on both, another
    appears on neither, and nothing anywhere reports an error.
    """

    def setUp(self) -> None:
        super().setUp()
        rows = []
        for index in range(40):
            rows.append(record(
                environment="env%d" % (index % 3),
                script="s%d.py" % (index % 5),
                test_name="t%02d" % index,
                # Deliberately few distinct values: ties are the bug.
                result=["PASS", "FAIL"][index % 2],
                start_time=format_iso(
                    NOW - datetime.timedelta(minutes=index % 4)),
                end_time=format_iso(
                    NOW - datetime.timedelta(minutes=index % 4)
                    + datetime.timedelta(seconds=index % 3)),
            ))
        self.import_runs(rows)

    def _identities(self, data):
        return [
            (row["environment"], row["script"], row["test_name"])
            for row in data["tests"]
        ]

    def test_every_sort_key_pages_without_repeats(self) -> None:
        for key in sorted(api.DASHBOARD_SORTS):
            for order in ("asc", "desc"):
                seen = []
                for offset in (0, 15, 30):
                    page = self.call(
                        "GET", "/api/dashboard",
                        query={"sort": [key], "order": [order],
                               "limit": ["15"], "offset": [str(offset)]})
                    seen.extend(self._identities(page))
                self.assertEqual(
                    len(seen), 40, "%s/%s lost rows" % (key, order))
                self.assertEqual(
                    len(set(seen)), 40,
                    "%s/%s repeated a row across pages — the sort needs "
                    "the full primary key as a tiebreak" % (key, order))

    def test_descending_is_the_exact_reverse_for_a_unique_key(self) -> None:
        ascending = self._identities(self.call(
            "GET", "/api/dashboard",
            query={"sort": ["test_name"], "order": ["asc"],
                   "limit": ["40"]}))
        descending = self._identities(self.call(
            "GET", "/api/dashboard",
            query={"sort": ["test_name"], "order": ["desc"],
                   "limit": ["40"]}))
        self.assertEqual(ascending, list(reversed(descending)))

    def test_an_unknown_sort_is_refused(self) -> None:
        """ORDER BY takes no parameters, so the whitelist is the
        security boundary, not a convenience."""
        data = self.call(
            "GET", "/api/dashboard",
            query={"sort": ["duration; DROP TABLE runs"]}, expect=400)
        self.assertIn("sort", data["error"])

    def test_an_unknown_order_is_refused(self) -> None:
        self.call(
            "GET", "/api/dashboard",
            query={"order": ["sideways"]}, expect=400)


class TestTimeEndpoint(ApiCase):
    """GET /api/time — the "where is the time going" drill-down."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(environment="linux", script="a.py", test_name="one",
                   start_time=format_iso(NOW - datetime.timedelta(hours=1)),
                   end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                       + datetime.timedelta(seconds=10))),
            record(environment="linux", script="b.py", test_name="two",
                   start_time=format_iso(NOW - datetime.timedelta(hours=1)),
                   end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                       + datetime.timedelta(seconds=4))),
            record(environment="win", script="a.py", test_name="three",
                   start_time=format_iso(NOW - datetime.timedelta(hours=1)),
                   end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                       + datetime.timedelta(seconds=1))),
        ])

    def test_default_level_is_environment(self) -> None:
        data = self.call("GET", "/api/time")
        self.assertEqual(data["group_by"], "environment")
        self.assertEqual(
            [(i["key"], i["total_seconds"]) for i in data["items"]],
            [("linux", 14.0), ("win", 1.0)])
        self.assertEqual(data["total_seconds"], 15.0)
        self.assertEqual(data["test_count"], 3)

    def test_scoping_drills_down(self) -> None:
        scripts = self.call(
            "GET", "/api/time",
            query={"group_by": ["script"], "environment": ["linux"]})
        self.assertEqual(
            [i["key"] for i in scripts["items"]], ["a.py", "b.py"])
        tests = self.call(
            "GET", "/api/time",
            query={"group_by": ["test_name"], "environment": ["linux"],
                   "script": ["a.py"]})
        self.assertEqual([i["key"] for i in tests["items"]], ["one"])

    def test_an_unknown_group_by_is_400(self) -> None:
        data = self.call(
            "GET", "/api/time", query={"group_by": ["output"]}, expect=400)
        self.assertIn("group_by", data["error"])

    def _nightly_history(self) -> None:
        """A fortnight of nightly passes.

        The recency cutoff is derived from when the suite ACTUALLY ran
        (``api._recent_cutoff``), and the rule is "one whole pass of
        grace". A fixture with only two blocks of activity therefore
        reaches its grace all the way back to the first of them — a
        correct answer for that fixture and nothing like production.
        This gives the estate a realistic history, so "the previous
        pass" means yesterday.
        """
        rows = []
        for day in range(1, 15):
            when = NOW - datetime.timedelta(days=day)
            for index in range(3):
                rows.append(record(
                    environment="linux", script="a.py",
                    test_name="hist%d" % index,
                    start_time=format_iso(when),
                    end_time=format_iso(
                        when + datetime.timedelta(seconds=1))))
        self.import_runs(rows)

    def test_stale_tests_are_excluded_and_reported(self) -> None:
        self._nightly_history()
        self.import_runs([
            record(environment="linux", script="c.py", test_name="old",
                   start_time=format_iso(NOW - datetime.timedelta(days=10)),
                   end_time=format_iso(NOW - datetime.timedelta(days=10)
                                       + datetime.timedelta(seconds=500))),
        ])
        data = self.call("GET", "/api/time")
        self.assertEqual(data["excluded_tests"], 1)
        # 15s from the three tests this class seeds, plus 1s each from
        # the three that carry the nightly history above.
        self.assertEqual(data["total_seconds"], 18.0)
        self.assertFalse(data["include_stale"])

    def test_stale_tests_can_be_included_on_request(self) -> None:
        """Without this the page is blank after any quiet day."""
        self.import_runs([
            record(environment="linux", script="c.py", test_name="old",
                   start_time=format_iso(NOW - datetime.timedelta(days=10)),
                   end_time=format_iso(NOW - datetime.timedelta(days=10)
                                       + datetime.timedelta(seconds=500))),
        ])
        data = self.call(
            "GET", "/api/time", query={"include_stale": ["1"]})
        self.assertTrue(data["include_stale"])
        self.assertEqual(data["total_seconds"], 515.0)
        self.assertEqual(data["excluded_tests"], 0)

    def test_retired_tests_are_excluded(self) -> None:
        self.call(
            "PUT", test_path("win", "a.py", "three", "/retired"),
            body={"retired": True, "username": "bob", "comment": "gone"})
        data = self.call("GET", "/api/time")
        self.assertEqual([i["key"] for i in data["items"]], ["linux"])

    def test_wrong_method(self) -> None:
        self.assert_405("POST", "/api/time", "GET", body={})


class TimeStreamScopingTest(ApiCase):
    """WP-23 (docs/STREAMS_PLAN.md §5.2): ``/api/time`` accepts
    ``stream=`` (default mainline) so a branch's "own results" tab can
    read WHERE ITS OWN suite spent its time."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(environment="linux", script="a.py", test_name="one",
                   start_time=format_iso(NOW - datetime.timedelta(hours=1)),
                   end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                       + datetime.timedelta(seconds=10))),
        ])

    def test_a_branch_import_leaves_mainline_unaffected(self) -> None:
        before = self.call("GET", "/api/time")
        self.import_runs([
            record(environment="linux", script="a.py",
                   test_name="branch_only", build="feat/x",
                   start_time=format_iso(NOW - datetime.timedelta(hours=1)),
                   end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                       + datetime.timedelta(seconds=99))),
        ])
        after = self.call("GET", "/api/time")
        self.assertEqual(before, after)

    def test_stream_param_reads_the_branch_own_time(self) -> None:
        self.import_runs([
            record(environment="linux", script="a.py",
                   test_name="branch_only", build="feat/x",
                   start_time=format_iso(NOW - datetime.timedelta(hours=1)),
                   end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                       + datetime.timedelta(seconds=99))),
        ])
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        stream_id = streams["streams"][0]["id"]
        data = self.call(
            "GET", "/api/time", query={"stream": [str(stream_id)]})
        self.assertEqual(data["stream"], stream_id)
        self.assertEqual(data["test_count"], 1)
        self.assertEqual(data["total_seconds"], 99.0)

    def test_stream_param_echoes_the_streams_identity(self) -> None:
        """F7 (docs/STREAMS_PLAN.md §5.2 "as built"): the Time page
        needs a stream's kind/name to render the branch band, the same
        field test detail already echoes as "stream_identity"."""
        self.import_runs([
            record(environment="linux", script="a.py",
                   test_name="branch_only", build="feat/x",
                   start_time=format_iso(NOW - datetime.timedelta(hours=1)),
                   end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                       + datetime.timedelta(seconds=99))),
        ])
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        stream_id = streams["streams"][0]["id"]
        data = self.call(
            "GET", "/api/time", query={"stream": [str(stream_id)]})
        self.assertEqual(data["stream_identity"]["id"], stream_id)
        self.assertEqual(data["stream_identity"]["kind"], "build")
        self.assertEqual(data["stream_identity"]["name"], "feat/x")

    def test_mainline_has_no_stream_identity(self) -> None:
        data = self.call("GET", "/api/time")
        self.assertIsNone(data["stream_identity"])


class TimeStreamEnvironmentHintTest(ApiCase):
    """WP-25 (docs/ONE_KIND_PLAN.md §2b.1, user-reported 2026-08-09): a
    build that ran on one environment showed a bare empty page on every
    OTHER environment — the data was honest, the page was not.
    ``stream_environments`` names where the stream's data actually is."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(environment="atlas-lab-bravo", script="a.py",
                   test_name="t", build="2026.9.1",
                   start_time=format_iso(NOW - datetime.timedelta(hours=1)),
                   end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                       + datetime.timedelta(seconds=10))),
        ])
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        self.stream_id = streams["streams"][0]["id"]

    def test_empty_on_a_different_environment_names_the_real_one(
            self) -> None:
        data = self.call(
            "GET", "/api/time",
            query={"stream": [str(self.stream_id)],
                   "environment": ["atlas-lab-alpha"], "group_by": ["script"]})
        self.assertEqual(data["items"], [])
        self.assertEqual(data["stream_environments"], ["atlas-lab-bravo"])

    def test_present_and_populated_costs_nothing_extra(self) -> None:
        data = self.call(
            "GET", "/api/time",
            query={"stream": [str(self.stream_id)],
                   "environment": ["atlas-lab-bravo"], "group_by": ["script"]})
        self.assertNotEqual(data["items"], [])
        self.assertIsNone(data["stream_environments"])

    def test_mainline_never_carries_the_field(self) -> None:
        data = self.call(
            "GET", "/api/time", query={"environment": ["nope-at-all"],
                                        "group_by": ["script"]})
        self.assertEqual(data["items"], [])
        self.assertIsNone(data["stream_environments"])


class TestUsers(ApiCase):
    """GET/POST /api/users: listing, idempotent creation, validation."""

    def test_list_empty(self) -> None:
        self.assertEqual(
            self.call("GET", "/api/users"),
            {"users": [], "include_inactive": False},
        )

    def test_create_then_idempotent(self) -> None:
        created = self.call(
            "POST", "/api/users", body={"username": "alice"}, expect=201
        )
        self.assertEqual(
            created,
            {
                "user": {
                    "username": "alice",
                    "created_at": format_iso(NOW),
                    "active": True,
                    "deactivated_at": None,
                    "deactivated_by": None,
                },
                "created": True,
            },
        )
        again = self.call(
            "POST", "/api/users", body={"username": "alice"}, expect=200
        )
        self.assertEqual(again["created"], False)
        self.assertEqual(again["user"], created["user"])

    def test_username_stripped_on_create(self) -> None:
        data = self.call(
            "POST", "/api/users", body={"username": "  dave  "}, expect=201
        )
        self.assertEqual(data["user"]["username"], "dave")

    def test_deactivated_users_are_absent_from_the_default_listing(
        self
    ) -> None:
        """This is the whole feature.

        Every assignee picker in the frontend reads this endpoint with
        no parameters, so a deactivated user stops being offered without
        a single line of frontend change.
        """
        for name in ("alice", "bob"):
            self.call("POST", "/api/users", body={"username": name},
                      expect=201)
        self.call("PUT", "/api/users/bob/active",
                  body={"active": False, "changed_by": "alice"})
        listed = self.call("GET", "/api/users")["users"]
        self.assertEqual([u["username"] for u in listed], ["alice"])

    def test_the_full_roster_is_available_on_request(self) -> None:
        for name in ("alice", "bob"):
            self.call("POST", "/api/users", body={"username": name},
                      expect=201)
        self.call("PUT", "/api/users/bob/active",
                  body={"active": False, "changed_by": "alice"})
        data = self.call("GET", "/api/users",
                         query={"include_inactive": ["1"]})
        self.assertTrue(data["include_inactive"])
        self.assertEqual(
            [(u["username"], u["active"]) for u in data["users"]],
            [("alice", True), ("bob", False)],
        )
        bob = data["users"][1]
        self.assertEqual(bob["deactivated_at"], format_iso(NOW))
        self.assertEqual(bob["deactivated_by"], "alice")

    def test_reactivating_restores_the_user(self) -> None:
        self.call("POST", "/api/users", body={"username": "bob"},
                  expect=201)
        self.call("PUT", "/api/users/bob/active",
                  body={"active": False, "changed_by": "bob"})
        self.assertEqual(self.call("GET", "/api/users")["users"], [])
        data = self.call("PUT", "/api/users/bob/active",
                         body={"active": True, "changed_by": "bob"})
        self.assertTrue(data["user"]["active"])
        self.assertIsNone(data["user"]["deactivated_at"])
        self.assertEqual(
            [u["username"] for u in self.call("GET", "/api/users")["users"]],
            ["bob"])

    def test_deactivating_an_unknown_user_404s(self) -> None:
        self.call("PUT", "/api/users/ghost/active",
                  body={"active": False, "changed_by": "alice"},
                  expect=404)

    def test_active_field_is_validated(self) -> None:
        self.call("POST", "/api/users", body={"username": "bob"},
                  expect=201)
        missing = self.call("PUT", "/api/users/bob/active",
                            body={"changed_by": "bob"}, expect=400)
        self.assertIn("active", missing["error"])
        wrong = self.call("PUT", "/api/users/bob/active",
                          body={"active": "yes", "changed_by": "bob"},
                          expect=400)
        self.assertIn("true or false", wrong["error"])
        no_actor = self.call("PUT", "/api/users/bob/active",
                             body={"active": False}, expect=400)
        self.assertIn("changed_by", no_actor["error"])

    def test_wrong_method_on_active(self) -> None:
        self.assert_405("GET", "/api/users/bob/active", "PUT")

    def test_list_sorted_by_username(self) -> None:
        self.call("POST", "/api/users", body={"username": "bob"}, expect=201)
        self.call(
            "POST", "/api/users", body={"username": "alice"}, expect=201
        )
        users = self.call("GET", "/api/users")["users"]
        self.assertEqual(
            [u["username"] for u in users], ["alice", "bob"]
        )
        for user in users:
            self.assertEqual(
                set(user.keys()),
                {"username", "created_at", "active", "deactivated_at",
                 "deactivated_by"},
            )

    def test_validation_errors_400(self) -> None:
        for body in (
            {},
            {"username": ""},
            {"username": "   "},
            {"username": "x" * 101},
            {"username": 3},
        ):
            with self.subTest(body=body):
                data = self.call(
                    "POST", "/api/users", body=body, expect=400
                )
                self.assertIn("username", data["error"])

    def test_bad_json_400(self) -> None:
        self.call("POST", "/api/users", body=b"nope{", expect=400)

    def test_wrong_method(self) -> None:
        self.assert_405("PUT", "/api/users", "GET, POST")


class TestRoutingAndEncoding(ApiCase):
    """Route fall-through 404s and URL-encoded segment round-trips."""

    def test_api_root_404(self) -> None:
        for path in ("/api", "/api/"):
            with self.subTest(path=path):
                data = self.call("GET", path, expect=404)
                self.assertEqual(data, {"error": "not found"})

    def test_unknown_api_paths_404(self) -> None:
        for path in (
            "/api/nope",
            "/api/tests",
            "/api/tests/env/script",
            "/api/tests/env/script/test/unknown-action",
            "/api/runs",
            "/api/runs/1/extra",
        ):
            with self.subTest(path=path):
                data = self.call("GET", path, expect=404)
                self.assertEqual(data, {"error": "not found"})

    def test_non_api_path_404(self) -> None:
        data = self.call("GET", "/static/index.html", expect=404)
        self.assertEqual(data, {"error": "not found"})

    def test_encoded_segments_round_trip(self) -> None:
        environment = "linux prod"
        script = "regression/orders.py"
        test_name = "test_fill/replace [edge]"
        data = self.import_runs(
            [
                record(
                    environment=environment,
                    script=script,
                    test_name=test_name,
                )
            ]
        )
        self.assertEqual(data["inserted"], 1)

        # Dashboard returns the exact identity strings.
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertEqual(rows[0]["environment"], environment)
        self.assertEqual(rows[0]["script"], script)
        self.assertEqual(rows[0]["test_name"], test_name)

        # Detail -> history -> comments all resolve the encoded path.
        detail = self.call(
            "GET", test_path(environment, script, test_name)
        )
        self.assertEqual(detail["test_name"], test_name)
        self.assertEqual(detail["script"], script)
        runs = self.call(
            "GET", test_path(environment, script, test_name, "/history")
        )["runs"]
        self.assertEqual(len(runs), 1)
        comments = self.call(
            "GET", test_path(environment, script, test_name, "/comments")
        )["comments"]
        self.assertEqual(comments, [])

    def test_unencoded_slash_is_a_path_separator(self) -> None:
        self.import_runs(
            [record(environment="linux", script="s.py", test_name="x/y")]
        )
        # Correctly encoded -> found.
        self.call("GET", test_path("linux", "s.py", "x/y"))
        # A literal slash splits into an extra segment -> route miss.
        data = self.call("GET", "/api/tests/linux/s.py/x/y", expect=404)
        self.assertEqual(data, {"error": "not found"})

    def test_plus_is_not_decoded_to_space(self) -> None:
        self.import_runs(
            [
                record(
                    environment="linux",
                    script="s.py",
                    test_name="a+b test",
                )
            ]
        )
        # Raw '+' must stay '+'; only %20 becomes a space. If the router
        # used unquote_plus this would decode to "a b test" and 404.
        data = self.call("GET", "/api/tests/linux/s.py/a+b%20test")
        self.assertEqual(data["test_name"], "a+b test")

    def test_trailing_slash_tolerated(self) -> None:
        self.assertEqual(
            self.call("GET", "/api/users/"),
            {"users": [], "include_inactive": False},
        )


SUMMARY_QUEUE_ENTRY_KEYS = {
    "environment",
    "product",
    "script",
    "test_name",
    "run_id",
    "result",
    "prev_result",
    "start_time",
    "duration_seconds",
    "known_failure_reason",
    "assignee",
    "latest_comment",
    "failing_since",
    "last_pass_time",
}


class TestSummary(ApiCase):
    """GET /api/summary — the home-screen estate rollup."""

    def seed(self) -> None:
        """Seed a small estate around NOW (2026-07-26 12:00 UTC).

        - test_new_fail: PASS on the 25th, FAIL on the 26th.
        - test_still_fail: PASS 23rd, then FAIL 24th/25th/26th.
        - test_fixed: FAIL 25th, PASS 26th.
        - test_stale: PASS on the 20th, not run since (36h cutoff is
          the 25th 00:00, so it counts as not run).
        - test_note: UNEXPECTED_PASS on the 26th.
        - env2/test_first: FAIL on the 26th, first ever run.
        """
        def at(day: int, hour: int = 2) -> str:
            return "2026-07-{:02d}T{:02d}:00:00.000000".format(day, hour)

        self.import_runs([
            record(test_name="test_new_fail", result="PASS",
                   start_time=at(25), end_time=at(25, 3)),
            record(test_name="test_new_fail", result="FAIL",
                   start_time=at(26), end_time=at(26, 3)),
            record(test_name="test_still_fail", result="PASS",
                   start_time=at(23), end_time=at(23, 3)),
            record(test_name="test_still_fail", result="FAIL",
                   start_time=at(24), end_time=at(24, 3)),
            record(test_name="test_still_fail", result="FAIL",
                   start_time=at(25), end_time=at(25, 3)),
            record(test_name="test_still_fail", result="FAIL",
                   start_time=at(26), end_time=at(26, 3)),
            record(test_name="test_fixed", result="FAIL",
                   start_time=at(25), end_time=at(25, 3)),
            record(test_name="test_fixed", result="PASS",
                   start_time=at(26), end_time=at(26, 3)),
            record(test_name="test_stale", result="PASS",
                   start_time=at(20), end_time=at(20, 3)),
            record(test_name="test_note", result="UNEXPECTED_PASS",
                   start_time=at(26), end_time=at(26, 3),
                   known_failure_reason="JIRA-1: stale"),
            record(environment="env2", script="smoke.py",
                   test_name="test_first", result="FAIL",
                   start_time=at(26), end_time=at(26, 3)),
        ])

    def queue_names(
        self, data: Dict[str, Any], queue: str
    ) -> List[str]:
        """Test names in a queue, in response order."""
        return [
            entry["test_name"]
            for entry in data["queues"][queue]["tests"]
        ]

    def test_status_and_queues(self) -> None:
        self.seed()
        data = self.call("GET", "/api/summary")
        self.assertEqual(data["generated_at"], format_iso(NOW))
        self.assertEqual(data["environments"], ["env2", "linux-sim"])
        status = data["status"]
        self.assertEqual(status["total_tests"], 6)
        self.assertEqual(status["ran_recently"], 5)
        self.assertEqual(status["not_run"], 1)
        self.assertEqual(status["results"]["FAIL"], 3)
        self.assertEqual(status["results"]["PASS"], 2)
        self.assertEqual(status["results"]["UNEXPECTED_PASS"], 1)
        self.assertEqual(status["recent_results"]["PASS"], 1)
        self.assertEqual(status["new_failures"], 2)
        self.assertEqual(status["still_failing"], 1)
        self.assertEqual(status["fixed"], 1)
        self.assertEqual(status["assigned_open"], 0)

        self.assertEqual(
            self.queue_names(data, "new_failures"),
            ["test_first", "test_new_fail"],
        )
        self.assertEqual(
            self.queue_names(data, "still_failing"), ["test_still_fail"]
        )
        self.assertEqual(
            self.queue_names(data, "unexpected_passes"), ["test_note"]
        )
        self.assertEqual(self.queue_names(data, "fixed"), ["test_fixed"])
        self.assertEqual(data["queues"]["assigned"]["total"], 0)

    def test_queue_entry_shape_and_streak(self) -> None:
        self.seed()
        data = self.call("GET", "/api/summary")
        entry = data["queues"]["still_failing"]["tests"][0]
        self.assertEqual(set(entry.keys()), SUMMARY_QUEUE_ENTRY_KEYS)
        self.assertEqual(entry["result"], "FAIL")
        self.assertEqual(entry["prev_result"], "FAIL")
        self.assertEqual(
            entry["failing_since"], "2026-07-24T02:00:00.000000"
        )
        self.assertEqual(
            entry["last_pass_time"], "2026-07-23T02:00:00.000000"
        )
        # A fixed test carries no streak info (latest run is not FAIL).
        fixed = data["queues"]["fixed"]["tests"][0]
        self.assertIsNone(fixed["failing_since"])
        self.assertIsNone(fixed["last_pass_time"])

    def test_trend_zero_filled(self) -> None:
        self.seed()
        data = self.call(
            "GET", "/api/summary", query={"days": ["7"]}
        )
        trend = data["trend"]
        self.assertEqual(trend["days"], 7)
        self.assertEqual(trend["from"], "2026-07-20")
        self.assertEqual(trend["to"], "2026-07-26")
        self.assertEqual(len(trend["nights"]), 7)
        by_date = {n["date"]: n for n in trend["nights"]}
        # The 21st/22nd had no runs at all: present, zeroed.
        self.assertEqual(by_date["2026-07-21"]["total"], 0)
        self.assertEqual(by_date["2026-07-22"]["FAIL"], 0)
        self.assertEqual(by_date["2026-07-26"]["FAIL"], 3)
        self.assertEqual(by_date["2026-07-26"]["PASS"], 1)
        self.assertEqual(by_date["2026-07-26"]["UNEXPECTED_PASS"], 1)
        self.assertEqual(by_date["2026-07-26"]["total"], 5)

    def test_environment_filter_scopes_everything(self) -> None:
        self.seed()
        data = self.call(
            "GET", "/api/summary", query={"environment": ["env2"]}
        )
        self.assertEqual(data["environment"], "env2")
        self.assertEqual(data["status"]["total_tests"], 1)
        self.assertEqual(data["status"]["new_failures"], 1)
        self.assertEqual(len(data["by_environment"]), 1)
        self.assertEqual(
            data["by_environment"][0]["environment"], "env2"
        )
        self.assertEqual(
            data["top_failing_scripts"],
            [{"environment": "env2", "script": "smoke.py", "failing": 1}],
        )
        # The environments list stays unfiltered (it feeds the picker).
        self.assertEqual(data["environments"], ["env2", "linux-sim"])
        trend_total = sum(
            night["total"] for night in data["trend"]["nights"]
        )
        self.assertEqual(trend_total, 1)

    def test_by_environment_and_top_scripts(self) -> None:
        self.seed()
        data = self.call("GET", "/api/summary")
        rollups = {r["environment"]: r for r in data["by_environment"]}
        self.assertEqual(rollups["linux-sim"]["total_tests"], 5)
        self.assertEqual(rollups["linux-sim"]["failed"], 2)
        self.assertEqual(rollups["linux-sim"]["new_failures"], 1)
        self.assertEqual(rollups["linux-sim"]["not_run"], 1)
        self.assertEqual(rollups["env2"]["failed"], 1)
        self.assertEqual(
            data["top_failing_scripts"][0],
            {"environment": "linux-sim", "script": "suite/alpha.py",
             "failing": 2},
        )

    def test_assigned_queue_after_assignment(self) -> None:
        self.seed()
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", "test_still_fail",
                      "/assignee"),
            body={"username": "alice", "assigned_by": "bob"},
        )
        data = self.call("GET", "/api/summary")
        self.assertEqual(data["status"]["assigned_open"], 1)
        assigned = data["queues"]["assigned"]["tests"]
        self.assertEqual(
            [(e["test_name"], e["assignee"]) for e in assigned],
            [("test_still_fail", "alice")],
        )

    def test_days_validation(self) -> None:
        for bad in ("0", "91", "x"):
            data = self.call(
                "GET", "/api/summary", query={"days": [bad]}, expect=400
            )
            self.assertIn("days", data["error"])

    def test_empty_database(self) -> None:
        data = self.call("GET", "/api/summary")
        self.assertEqual(data["status"]["total_tests"], 0)
        self.assertEqual(data["environments"], [])
        self.assertEqual(data["scripts"], [])
        self.assertEqual(len(data["trend"]["nights"]), 14)
        self.assertEqual(data["queues"]["new_failures"]["total"], 0)
        self.assertEqual(data["queues"]["mine"], {"total": 0, "tests": []})

    def test_scripts_list_feeds_the_filter_and_follows_the_env_scope(
        self
    ) -> None:
        """The test-list filter can no longer derive scripts client-side."""
        self.seed()
        self.assertEqual(
            self.call("GET", "/api/summary")["scripts"],
            ["smoke.py", "suite/alpha.py"],
        )
        scoped = self.call(
            "GET", "/api/summary", query={"environment": ["env2"]}
        )
        self.assertEqual(scoped["scripts"], ["smoke.py"])

    def test_mine_queue_is_filtered_server_side(self) -> None:
        """"My actions" is its own query, not a slice of a capped queue."""
        self.seed()
        for name, who in (("test_still_fail", "alice"),
                          ("test_new_fail", "carol")):
            self.call(
                "PUT",
                test_path("linux-sim", "suite/alpha.py", name, "/assignee"),
                body={"username": who, "assigned_by": "bob"},
            )
        data = self.call(
            "GET", "/api/summary", query={"assignee": ["alice"]}
        )
        self.assertEqual(data["queues"]["mine"]["total"], 1)
        self.assertEqual(
            [e["test_name"] for e in data["queues"]["mine"]["tests"]],
            ["test_still_fail"],
        )
        # The estate-wide assigned queue still sees both.
        self.assertEqual(data["queues"]["assigned"]["total"], 2)

    def test_mine_queue_empty_for_unknown_user(self) -> None:
        self.seed()
        data = self.call(
            "GET", "/api/summary", query={"assignee": ["nobody"]}
        )
        self.assertEqual(data["queues"]["mine"], {"total": 0, "tests": []})

    def test_still_failing_is_ordered_oldest_regression_first(self) -> None:
        """The queue leads with the test that has been broken longest."""
        # Broken since the 25th, where test_still_fail broke on the 24th.
        self.import_runs([
            record(test_name="test_recent_break", result="PASS",
                   start_time="2026-07-24T02:00:00.000000",
                   end_time="2026-07-24T02:00:01.000000"),
            record(test_name="test_recent_break", result="FAIL",
                   start_time="2026-07-25T02:00:00.000000",
                   end_time="2026-07-25T02:00:01.000000"),
            record(test_name="test_recent_break", result="FAIL",
                   start_time="2026-07-26T02:00:00.000000",
                   end_time="2026-07-26T02:00:01.000000"),
        ])
        self.seed()
        entries = self.call("GET", "/api/summary")[
            "queues"]["still_failing"]["tests"]
        self.assertEqual(
            [e["test_name"] for e in entries],
            ["test_still_fail", "test_recent_break"],
        )

    def test_queue_entries_carry_the_latest_comment(self) -> None:
        """Triage must show what someone already worked out.

        Without it, triaging means opening each test to discover it was
        looked at yesterday.
        """
        self.seed()
        path = test_path(
            "linux-sim", "suite/alpha.py", "test_new_fail", "/comments")
        for text in ("first look", "root caused: clock skew"):
            self.call("POST", path,
                      body={"username": "alice", "text": text}, expect=201)

        entries = {
            entry["test_name"]: entry
            for entry in self.call(
                "GET", "/api/summary")["queues"]["new_failures"]["tests"]
        }
        comment = entries["test_new_fail"]["latest_comment"]
        self.assertEqual(comment["text"], "root caused: clock skew")
        self.assertEqual(comment["author"], "alice")
        # A test nobody has commented on says so explicitly.
        self.assertIsNone(entries["test_first"]["latest_comment"])

    def test_retiring_shows_its_reason_in_the_queue(self) -> None:
        """The retire note is a comment, so it surfaces like any other."""
        self.seed()
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", "test_stale",
                      "/retired"),
            body={"retired": False, "username": "bob",
                  "comment": "Checked with the team: still expected."},
        )
        entries = {
            entry["test_name"]: entry
            for entry in self.call(
                "GET", "/api/summary")["queues"]["not_run"]["tests"]
        }
        self.assertIn("still expected",
                      entries["test_stale"]["latest_comment"]["text"])

    def test_queue_cap_is_reported(self) -> None:
        data = self.call("GET", "/api/summary")
        self.assertEqual(data["queue_cap"], api._SUMMARY_QUEUE_CAP)

    def test_the_full_payload_counts_every_queue_in_one_grouped_query(
        self
    ) -> None:
        """WP-23 perf pass: the full payload used to run
        2*(len(QUEUE_KINDS)+1) separate status_queue_count-shaped
        queries (12 on this project's own 6-kind QUEUE_KINDS) --
        Storage.queue_counts's grouped SUM(CASE...) replaces every one
        of them with a SINGLE query, reused for both queue_totals and
        every queue's own "total" field. "SUM(CASE WHEN" is unique to
        that one query shape; nothing else in this request builds a
        statement that way."""
        self.seed()
        seen = []  # type: List[str]
        conn = self.storage._conn()
        _trace_sql_into(conn, seen)
        try:
            self.call("GET", "/api/summary")
        finally:
            conn.set_trace_callback(None)
        grouped = [s for s in seen if "SUM(CASE WHEN" in s]
        self.assertEqual(
            len(grouped), 1,
            "expected exactly 1 grouped queue-counts query, got "
            "{0}:\n{1}".format(len(grouped), "\n---\n".join(grouped)),
        )
        # _queue_clause (status_queue_count's WHERE builder) always
        # opens with "WHERE lr.stream_id = ..." -- distinct from
        # assigned_open_count's unrelated, pre-existing COUNT(*) query
        # (same join, same predicate, but stream_id is its LAST AND,
        # not its WHERE) that legitimately still runs once, for the
        # headline's status.assigned_open field, not a queue total.
        per_kind_counts = [
            s for s in seen
            if s.strip().upper().startswith("SELECT COUNT(*)")
            and "WHERE lr.stream_id = " in s
        ]
        self.assertEqual(
            per_kind_counts, [],
            "a per-kind COUNT(*) query survived batching: {0}".format(
                per_kind_counts),
        )

    def test_batched_streak_lookups_still_agree_with_the_single_row_form(
        self
    ) -> None:
        """WP-23 perf pass: still_failing's failing_since/last_pass_time
        now come from Storage.failure_streak_bounds_many rather than one
        call per row -- the existing test_status_and_queues-style
        assertions already cover the VALUES; this pins that the queue
        page's own rows are the source, not a coincidence, by cross-
        checking against the single-row method directly for the one
        FAIL row this fixture produces from a real streak (test_still_fail
        has been failing since day 24, per seed())."""
        self.seed()
        data = self.call("GET", "/api/summary")
        [row] = [
            entry for entry in data["queues"]["still_failing"]["tests"]
            if entry["test_name"] == "test_still_fail"
        ]
        expected = self.storage.failure_streak_bounds(
            "linux-sim", "suite/alpha.py", "test_still_fail",
            self.storage.latest_run(
                "linux-sim", "suite/alpha.py", "test_still_fail"
            ).start_time,
        )
        self.assertEqual(
            row["failing_since"], format_iso(expected.failing_since))
        self.assertEqual(
            row["last_pass_time"],
            None if expected.last_pass_before is None
            else format_iso(expected.last_pass_before),
        )

    def test_wrong_method_405(self) -> None:
        self.assert_405("POST", "/api/summary", "GET")


class TestRetired(ApiCase):
    """PUT .../retired — approving a test as no longer in the suite."""

    ENV = "linux-sim"
    SCRIPT = "suite/alpha.py"
    NAME = "test_gone"

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(test_name=self.NAME, result="FAIL",
                   start_time="2026-07-01T02:00:00.000000",
                   end_time="2026-07-01T02:00:01.000000"),
            record(test_name="test_live", result="PASS"),
        ])

    def path(self) -> str:
        return test_path(self.ENV, self.SCRIPT, self.NAME, "/retired")

    def retire(self, retired: bool = True, **overrides: Any) -> Any:
        body = {
            "retired": retired,
            "username": "alice",
            "comment": "Deleted in release 4.2.",
        }  # type: Dict[str, Any]
        body.update(overrides)
        return self.call("PUT", self.path(), body=body)

    def test_retiring_records_who_and_why(self) -> None:
        data = self.retire()
        self.assertTrue(data["retired"])
        self.assertEqual(data["retired_by"], "alice")
        self.assertEqual(data["comment"]["author"], "alice")
        self.assertIn("release 4.2", data["comment"]["text"])
        # The reason lands in the test's normal thread.
        comments = self.call(
            "GET", test_path(self.ENV, self.SCRIPT, self.NAME, "/comments")
        )["comments"]
        self.assertEqual(len(comments), 1)

    def test_retired_test_leaves_the_estate_views(self) -> None:
        self.retire()
        summary = self.call("GET", "/api/summary")
        self.assertEqual(summary["status"]["retired"], 1)
        self.assertEqual(summary["status"]["total_tests"], 1)
        self.assertEqual(summary["queues"]["new_failures"]["total"], 0)
        listing = self.call("GET", "/api/dashboard")
        self.assertEqual(
            [row["test_name"] for row in listing["tests"]], ["test_live"]
        )
        self.assertEqual(listing["total"], 1)

    def test_retired_rows_are_returned_when_asked_for(self) -> None:
        self.retire()
        listing = self.call(
            "GET", "/api/dashboard", query={"retired": ["1"]}
        )
        rows = {row["test_name"]: row for row in listing["tests"]}
        self.assertIn(self.NAME, rows)
        self.assertEqual(rows[self.NAME]["retired_by"], "alice")
        self.assertIsNotNone(rows[self.NAME]["retired_at"])
        self.assertIsNone(rows["test_live"]["retired_at"])

    def test_history_is_untouched(self) -> None:
        """Retirement is "not in the suite", not "never happened"."""
        self.retire()
        detail = self.call(
            "GET", test_path(self.ENV, self.SCRIPT, self.NAME))
        self.assertEqual(detail["latest"]["result"], "FAIL")
        history = self.call(
            "GET", test_path(self.ENV, self.SCRIPT, self.NAME, "/history")
        )
        self.assertEqual(len(history["runs"]), 1)

    def test_un_retiring(self) -> None:
        self.retire()
        data = self.retire(retired=False, comment="Back in the suite.")
        self.assertFalse(data["retired"])
        self.assertIsNone(data["retired_by"])
        self.assertEqual(
            self.call("GET", "/api/summary")["status"]["retired"], 0
        )

    def test_a_new_run_un_retires_automatically(self) -> None:
        self.retire()
        self.import_runs([record(
            test_name=self.NAME, result="PASS",
            start_time="2026-07-26T02:00:00.000000",
            end_time="2026-07-26T02:00:01.000000",
        )])
        self.assertEqual(
            self.call("GET", "/api/summary")["status"]["retired"], 0
        )
        texts = [
            c["text"] for c in self.call(
                "GET",
                test_path(self.ENV, self.SCRIPT, self.NAME, "/comments"),
            )["comments"]
        ]
        self.assertTrue(any("un-retired" in text for text in texts), texts)

    def test_comment_is_required(self) -> None:
        data = self.call(
            "PUT", self.path(),
            body={"retired": True, "username": "alice"}, expect=400,
        )
        self.assertIn("comment", data["error"])

    def test_blank_comment_is_rejected(self) -> None:
        data = self.call(
            "PUT", self.path(),
            body={"retired": True, "username": "alice", "comment": "   "},
            expect=400,
        )
        self.assertIn("comment", data["error"])

    def test_retired_flag_must_be_boolean(self) -> None:
        data = self.call(
            "PUT", self.path(),
            body={"retired": "yes", "username": "a", "comment": "c"},
            expect=400,
        )
        self.assertIn("retired", data["error"])

    def test_unknown_test_404(self) -> None:
        self.call(
            "PUT", test_path(self.ENV, self.SCRIPT, "nope", "/retired"),
            body={"retired": True, "username": "a", "comment": "c"},
            expect=404,
        )

    def test_wrong_method(self) -> None:
        self.assert_405("GET", self.path(), "PUT")


class TestActionsFilters(ApiCase):
    """The open-actions view: owner filters and the latest comment."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(test_name="test_a", result="FAIL"),
            record(test_name="test_b", result="FAIL"),
            record(test_name="test_c", result="UNEXPECTED_PASS"),
        ])
        for name, who in (("test_a", "alice"), ("test_b", "bob")):
            self.call(
                "PUT",
                test_path("linux-sim", "suite/alpha.py", name, "/assignee"),
                body={"username": who, "assigned_by": "carol"},
            )
        for text in ("first look", "root caused: timing"):
            self.call(
                "POST",
                test_path(
                    "linux-sim", "suite/alpha.py", "test_a", "/comments"),
                body={"username": "alice", "text": text},
                expect=201,
            )

    def names(self, **query: Any) -> List[str]:
        listing = self.call("GET", "/api/dashboard", query=query)
        return [row["test_name"] for row in listing["tests"]]

    def test_filter_by_one_assignee(self) -> None:
        self.assertEqual(self.names(assignee=["alice"]), ["test_a"])

    def test_filter_by_several_assignees(self) -> None:
        self.assertEqual(
            self.names(assignee=["alice", "bob"]), ["test_a", "test_b"]
        )

    def test_unassigned_only(self) -> None:
        self.assertEqual(self.names(unassigned=["1"]), ["test_c"])

    def test_assignees_or_unassigned(self) -> None:
        """"Alice's items plus anything nobody owns"."""
        self.assertEqual(
            self.names(assignee=["alice"], unassigned=["1"]),
            ["test_a", "test_c"],
        )

    def test_latest_comment_is_opt_in(self) -> None:
        rows = self.call("GET", "/api/dashboard")["tests"]
        self.assertNotIn("latest_comment", rows[0])

    def test_latest_comment_is_the_newest_one(self) -> None:
        rows = {
            row["test_name"]: row
            for row in self.call(
                "GET", "/api/dashboard", query={"with_comment": ["1"]}
            )["tests"]
        }
        self.assertEqual(
            rows["test_a"]["latest_comment"]["text"], "root caused: timing"
        )
        self.assertEqual(
            rows["test_a"]["latest_comment"]["author"], "alice"
        )
        self.assertNotIn("latest_comment", rows["test_b"])

    def test_summary_lists_current_assignees_for_the_filter(self) -> None:
        self.assertEqual(
            self.call("GET", "/api/summary")["assignees"], ["alice", "bob"]
        )


class TestSummaryAssignmentStreams(ApiCase):
    """``/api/summary``'s ``assignment_streams`` (WP-21, Open Actions'
    origin filter) — the same "available values, empty means nothing to
    filter" shape as ``assignees``."""

    def test_empty_with_no_build_originated_assignments(self) -> None:
        self.import_runs([record(test_name="test_a")])
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", "test_a",
                      "/assignee"),
            body={"username": "alice", "assigned_by": "bob"})
        self.assertEqual(
            self.call("GET", "/api/summary")["assignment_streams"], [])

    def test_lists_every_stream_with_a_current_branch_assignment(
            self) -> None:
        self.import_runs([record(test_name="test_a")])
        self.import_runs([record(
            test_name="test_a", build="feat/x",
            start_time="2026-07-25T03:00:00.000000",
            end_time="2026-07-25T03:00:03.000000")])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        stream_id = streams[0]["id"]
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", "test_a",
                      "/assignee"),
            body={"username": "alice", "assigned_by": "bob",
                  "stream_id": stream_id})
        result = self.call("GET", "/api/summary")["assignment_streams"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], stream_id)
        self.assertEqual(result[0]["kind"], "build")
        self.assertEqual(result[0]["name"], "feat/x")


class TestScriptExecutions(ApiCase):
    """GET /api/scripts/{env}/{script}/executions — history of a suite."""

    ENV = "linux-sim"
    SCRIPT = "regression/nightly.py"

    def seed(self) -> None:
        """Two executions on one day, plus one the day before.

        The point of the endpoint: a calendar-day chart shows two bars
        here, but the suite actually ran three times.
        """
        runs = []
        batches = [
            ("2026-07-24T02:00", ["PASS", "PASS", "FAIL"]),
            ("2026-07-25T02:00", ["PASS", "FAIL", "FAIL"]),
            ("2026-07-25T14:00", ["PASS", "PASS", "PASS"]),
        ]
        for stamp, results in batches:
            for index, result in enumerate(results):
                start = "{}:{:02d}.000000".format(stamp, index * 2)
                end = "{}:{:02d}.000000".format(stamp, index * 2 + 1)
                runs.append(record(
                    script=self.SCRIPT,
                    test_name="test_{}".format(index),
                    result=result, start_time=start, end_time=end,
                ))
        self.import_runs(runs)

    def path(self, script: Optional[str] = None) -> str:
        quoted = urllib.parse.quote(
            script if script is not None else self.SCRIPT, safe="")
        return "/api/scripts/{}/{}/executions".format(
            urllib.parse.quote(self.ENV, safe=""), quoted)

    def test_two_runs_on_one_day_are_two_executions(self) -> None:
        self.seed()
        data = self.call("GET", self.path(), query={"days": ["30"]})
        self.assertEqual(len(data["executions"]), 3)
        # Newest first.
        starts = [e["started"] for e in data["executions"]]
        self.assertEqual(starts, sorted(starts, reverse=True))
        self.assertEqual(starts[0], "2026-07-25T14:00:00.000000")
        self.assertEqual(starts[1], "2026-07-25T02:00:00.000000")

    def test_each_execution_reports_its_own_results(self) -> None:
        self.seed()
        executions = self.call(
            "GET", self.path(), query={"days": ["30"]})["executions"]
        self.assertEqual(executions[0]["failed"], 0)
        self.assertEqual(executions[0]["total"], 3)
        self.assertEqual(executions[1]["failed"], 2)
        self.assertEqual(executions[1]["results"]["PASS"], 1)

    def test_window_is_bounded_by_days(self) -> None:
        """NOW is 2026-07-26 12:00, so 2 days reaches back to the 24th noon."""
        self.seed()
        two_days = self.call("GET", self.path(), query={"days": ["2"]})
        self.assertEqual(len(two_days["executions"]), 2)
        one_day = self.call("GET", self.path(), query={"days": ["1"]})
        self.assertEqual(len(one_day["executions"]), 1)
        self.assertEqual(
            one_day["executions"][0]["started"],
            "2026-07-25T14:00:00.000000",
        )

    def test_days_validation(self) -> None:
        self.seed()
        for bad in ("0", "91", "x"):
            data = self.call(
                "GET", self.path(), query={"days": [bad]}, expect=400)
            self.assertIn("days", data["error"])

    def test_unknown_script_404(self) -> None:
        self.seed()
        self.call("GET", self.path(script="nope.py"), expect=404)

    def test_wrong_method(self) -> None:
        self.seed()
        self.assert_405("POST", self.path(), "GET")

    def test_mainline_has_no_stream_identity(self) -> None:
        self.seed()
        data = self.call("GET", self.path(), query={"days": ["30"]})
        self.assertEqual(data["stream"], 1)
        self.assertIsNone(data["stream_identity"])

    def test_stream_param_reads_the_branch_own_executions(self) -> None:
        """Script-page parity (FINAL ROUND, docs/STREAMS_PLAN.md §5.2
        "as built"): storage.script_runs() already carries a stream_id
        predicate for every caller (F7); this endpoint just did not
        pass one through yet. group_executions() aggregates away test
        names, so the proof here is EXECUTION SIZE: mainline gets one
        run, the branch gets two runs in the SAME window -- if either
        side leaked the other's data the totals would disagree with
        this."""
        base = "2026-07-25T02:00:00.000000"
        self.import_runs([record(
            script=self.SCRIPT, test_name="mainline_only",
            start_time=base, end_time=base,
        )])
        self.import_runs([
            record(script=self.SCRIPT, test_name="branch_a", build="feat/x",
                   start_time=base, end_time=base),
            record(script=self.SCRIPT, test_name="branch_b", build="feat/x",
                   start_time=base, end_time=base),
        ])
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        stream_id = streams["streams"][0]["id"]

        mainline = self.call(
            "GET", self.path(), query={"days": ["30"]})
        self.assertEqual(mainline["stream"], 1)
        self.assertIsNone(mainline["stream_identity"])
        self.assertEqual(len(mainline["executions"]), 1)
        self.assertEqual(mainline["executions"][0]["total"], 1)

        branch = self.call(
            "GET", self.path(),
            query={"days": ["30"], "stream": [str(stream_id)]})
        self.assertEqual(branch["stream"], stream_id)
        self.assertEqual(branch["stream_identity"]["kind"], "build")
        self.assertEqual(branch["stream_identity"]["name"], "feat/x")
        self.assertEqual(len(branch["executions"]), 1)
        self.assertEqual(branch["executions"][0]["total"], 2)


class TestFrontendSortContract(unittest.TestCase):
    """The UI's sortable columns must be keys the server actually serves.

    Sorting moved from the browser into SQL, which created a contract
    between the table headers in static/index.html and DASHBOARD_SORTS.
    A mismatch is invisible until someone clicks the column and gets an
    error banner, so it is pinned here.
    """

    def test_table_sort_keys_are_all_supported(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "static", "index.html"),
                  encoding="utf-8") as handle:
            markup = handle.read()
        keys = set(re.findall(r'class="sort-btn" data-key="([^"]+)"', markup))
        self.assertTrue(keys, "no sortable columns found in index.html")
        self.assertEqual(
            keys - set(DASHBOARD_SORTS), set(),
            "index.html offers sort keys the API rejects",
        )


if __name__ == "__main__":
    unittest.main()


class TestDashboardStreaks(ApiCase):
    """with_streak=1: last pass + the flaky-vs-broken signal (WP-8)."""

    def setUp(self) -> None:
        super().setUp()
        rows = []
        # A test that broke and stayed broken.
        for day in range(10):
            rows.append(record(
                environment="linux", script="s.py", test_name="broken",
                result="PASS" if day < 5 else "FAIL",
                start_time=format_iso(NOW - datetime.timedelta(days=9 - day)),
                end_time=format_iso(
                    NOW - datetime.timedelta(days=9 - day)
                    + datetime.timedelta(seconds=1))))
        # A test that fails every other night.
        for day in range(10):
            rows.append(record(
                environment="linux", script="s.py", test_name="flaky",
                result="FAIL" if day % 2 else "PASS",
                start_time=format_iso(NOW - datetime.timedelta(days=9 - day)),
                end_time=format_iso(
                    NOW - datetime.timedelta(days=9 - day)
                    + datetime.timedelta(seconds=1))))
        self.import_runs(rows)

    def test_streaks_are_off_by_default(self) -> None:
        data = self.call("GET", "/api/dashboard")
        self.assertFalse(data["with_streak"])
        for row in data["tests"]:
            self.assertNotIn("stability", row)

    def test_a_broken_test_reports_when_it_broke_and_last_passed(
        self
    ) -> None:
        data = self.call(
            "GET", "/api/dashboard",
            query={"with_streak": ["1"], "q": ["broken"]})
        row = data["tests"][0]
        self.assertIsNotNone(row["failing_since"])
        self.assertIsNotNone(row["last_pass_time"])
        self.assertEqual(row["stability"]["classification"], "stable-fail")

    def test_a_flaky_test_is_told_apart_from_a_broken_one(self) -> None:
        """The whole point of item 8: the last-pass DATE alone cannot."""
        data = self.call(
            "GET", "/api/dashboard",
            query={"with_streak": ["1"], "q": ["flaky"]})
        row = data["tests"][0]
        self.assertEqual(row["stability"]["classification"], "flaky")
        self.assertGreater(row["stability"]["transitions"], 1)

    def test_non_failing_rows_get_history_but_no_streak(self) -> None:
        self.import_runs([record(
            environment="linux", script="s.py", test_name="healthy",
            result="PASS",
            start_time=format_iso(NOW - datetime.timedelta(hours=1)),
            end_time=format_iso(NOW - datetime.timedelta(hours=1)
                                + datetime.timedelta(seconds=1)))])
        data = self.call(
            "GET", "/api/dashboard",
            query={"with_streak": ["1"], "q": ["healthy"]})
        row = data["tests"][0]
        self.assertIsNone(row["failing_since"])
        self.assertIsNone(row["last_pass_time"])
        self.assertEqual(row["stability"]["classification"], "stable-pass")

    def test_the_recent_results_are_capped_and_ordered(self) -> None:
        data = self.call(
            "GET", "/api/dashboard",
            query={"with_streak": ["1"], "q": ["broken"]})
        results = data["tests"][0]["stability"]["recent_results"]
        self.assertLessEqual(len(results), 20)
        self.assertEqual(results[-1], "FAIL")
        self.assertEqual(results[0], "PASS")

    def test_the_count_query_is_not_enriched(self) -> None:
        """Streaks are for the RETURNED PAGE only. The total must not
        pay for rows nobody asked to see."""
        page = self.call(
            "GET", "/api/dashboard",
            query={"with_streak": ["1"], "limit": ["1"]})
        self.assertEqual(len(page["tests"]), 1)
        self.assertGreater(page["total"], 1)
        self.assertIn("stability", page["tests"][0])


class TestEnvironments(ApiCase):
    """GET /api/environments and PUT .../expectation.

    The declared expected test count is the denominator of the coverage
    test in analytics.find_passes. Too high a value fails SILENTLY -
    nothing clears the bar, no pass counts, and the staleness line drops
    back to the 36-hour wall clock, which is the Monday-morning bug the
    derived cutoff exists to fix. The echo in the listing is how that
    becomes visible.
    """

    def _nightly(self, environment: str, tests: int, nights: int = 4,
                 hour: int = 2) -> None:
        """A few nights of full passes for *environment*."""
        rows = []
        for night in range(1, nights + 1):
            when = (NOW - datetime.timedelta(days=night)).replace(
                hour=hour, minute=0, second=0, microsecond=0)
            for index in range(tests):
                rows.append(record(
                    environment=environment, script="a.py",
                    test_name="t%d" % index,
                    start_time=format_iso(when),
                    end_time=format_iso(
                        when + datetime.timedelta(seconds=1))))
        self.import_runs(rows)

    def test_an_environment_with_no_declaration_reads_as_inferred(
        self
    ) -> None:
        self._nightly("linux-sim", 4)
        data = self.call("GET", "/api/environments")
        (item,) = data["environments"]
        self.assertEqual(item["environment"], "linux-sim")
        self.assertEqual(item["tests_seen"], 4)
        self.assertIsNone(item["expected_tests"])
        self.assertEqual(item["effective_expected"], 4)
        self.assertIsNone(item["updated_by"])

    def test_the_listing_echoes_whether_passes_actually_counted(
        self
    ) -> None:
        """A declaration you cannot check against reality is a form
        nobody knows how to fill in."""
        self._nightly("linux-sim", 4)
        data = self.call("GET", "/api/environments")
        (item,) = data["environments"]
        self.assertEqual(item["passes_total"], 4)
        self.assertEqual(item["passes_covered"], 4)
        self.assertTrue(item["latest_pass"]["covered"])
        self.assertEqual(item["latest_pass"]["runs"], 4)
        self.assertTrue(data["cutoff_from_passes"])

    def test_a_declaration_too_high_shows_as_nothing_counting(
        self
    ) -> None:
        """The silent failure, made visible. Same activity, declared
        against a number it cannot reach: every pass stops counting and
        the cutoff falls back to the wall clock."""
        self._nightly("linux-sim", 4)
        self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": 900, "changed_by": "amy"})
        data = self.call("GET", "/api/environments")
        (item,) = data["environments"]
        self.assertEqual(item["expected_tests"], 900)
        self.assertEqual(item["effective_expected"], 900)
        self.assertEqual(item["passes_covered"], 0)
        self.assertGreater(item["passes_total"], 0)
        self.assertFalse(data["cutoff_from_passes"])
        self.assertEqual(data["cutoff"], data["fallback"])

    def test_a_declaration_changes_the_estate_cutoff_not_just_the_page(
        self
    ) -> None:
        """One call path. If the admin page worked out its passes
        separately from the cutoff, a declaration could change what the
        page shows and not what the estate is judged by."""
        self._nightly("linux-sim", 4)
        before = self.call("GET", "/api/summary")["stale_before"]
        self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": 900, "changed_by": "amy"})
        after = self.call("GET", "/api/summary")["stale_before"]
        self.assertNotEqual(before, after)

    def test_the_cutoff_is_never_stricter_than_the_wall_clock(
        self
    ) -> None:
        """The clamp that bounds every way this can be got wrong -
        including a declared count and the retired-test exclusion. It can
        only ever flag FEWER tests than the old fixed window."""
        self._nightly("linux-sim", 4)
        fallback = format_iso(
            NOW - datetime.timedelta(hours=api._SUMMARY_RECENT_HOURS))
        for declared in (1, 4, 900):
            self.call(
                "PUT", "/api/environments/linux-sim/expectation",
                body={"expected_tests": declared, "changed_by": "amy"})
            data = self.call("GET", "/api/environments")
            self.assertLessEqual(data["cutoff"], fallback, str(declared))

    def test_declare_then_clear_returns_to_inference(self) -> None:
        self._nightly("linux-sim", 4)
        self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": 12, "changed_by": "amy"})
        cleared = self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": None, "changed_by": "amy"})
        self.assertTrue(cleared["cleared"])
        (item,) = self.call("GET", "/api/environments")["environments"]
        self.assertIsNone(item["expected_tests"])
        self.assertEqual(item["effective_expected"], 4)

    def test_clearing_what_was_never_declared_is_not_an_error(
        self
    ) -> None:
        self._nightly("linux-sim", 4)
        data = self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": None, "changed_by": "amy"})
        self.assertFalse(data["cleared"])

    def test_the_declaring_user_is_recorded(self) -> None:
        self._nightly("linux-sim", 4)
        self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": 12, "changed_by": "amy"})
        (item,) = self.call("GET", "/api/environments")["environments"]
        self.assertEqual(item["updated_by"], "amy")
        self.assertEqual(item["updated_at"], format_iso(NOW))
        names = [u["username"]
                 for u in self.call("GET", "/api/users")["users"]]
        self.assertIn("amy", names)

    def test_retired_tests_do_not_inflate_the_inferred_count(self) -> None:
        """A pass that does not run a retired test has missed nothing."""
        self._nightly("linux-sim", 4)
        self.call(
            "PUT", test_path("linux-sim", "a.py", "t0", "/retired"),
            body={"retired": True, "username": "amy", "comment": "gone"})
        (item,) = self.call("GET", "/api/environments")["environments"]
        self.assertEqual(item["tests_seen"], 3)

    def test_an_unknown_environment_is_404(self) -> None:
        self._nightly("linux-sim", 4)
        data = self.call(
            "PUT", "/api/environments/typo/expectation",
            body={"expected_tests": 5, "changed_by": "amy"}, expect=404)
        self.assertIn("typo", data["error"])

    def test_a_declaration_survives_its_environment_disappearing(
        self
    ) -> None:
        """Otherwise a renamed environment leaves a row nobody can
        clear."""
        self._nightly("linux-sim", 4)
        self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": 12, "changed_by": "amy"})
        self.storage._conn().execute("DELETE FROM latest_runs")
        keys = [item["environment"]
                for item in
                self.call("GET", "/api/environments")["environments"]]
        self.assertIn("linux-sim", keys)

    def test_zero_and_negative_are_rejected(self) -> None:
        self._nightly("linux-sim", 4)
        for bad in (0, -1):
            data = self.call(
                "PUT", "/api/environments/linux-sim/expectation",
                body={"expected_tests": bad, "changed_by": "amy"},
                expect=400)
            self.assertIn("expected_tests", data["error"])

    def test_a_boolean_is_not_a_count(self) -> None:
        """bool is an int in Python, so true would declare one test."""
        self._nightly("linux-sim", 4)
        data = self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": True, "changed_by": "amy"}, expect=400)
        self.assertIn("bool", data["error"])

    def test_a_float_is_rejected(self) -> None:
        self._nightly("linux-sim", 4)
        self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": 4.5, "changed_by": "amy"}, expect=400)

    def test_the_field_is_required(self) -> None:
        self._nightly("linux-sim", 4)
        data = self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"changed_by": "amy"}, expect=400)
        self.assertIn("required", data["error"])

    def test_the_changer_is_required(self) -> None:
        self._nightly("linux-sim", 4)
        self.call(
            "PUT", "/api/environments/linux-sim/expectation",
            body={"expected_tests": 5}, expect=400)

    def test_wrong_methods(self) -> None:
        self.assert_405("POST", "/api/environments", "GET", body={})
        self.assert_405(
            "GET", "/api/environments/linux-sim/expectation", "PUT")


class TestEnvironmentProduct(ApiCase):
    """PUT /api/environments/{env}/product, and the "product" field of
    GET /api/environments (WP-20, docs/STREAMS_PLAN.md §2.1/§2.2)."""

    def _nightly(self, environment: str, tests: int = 2) -> None:
        self.import_runs([
            record(environment=environment, test_name="t%d" % i)
            for i in range(tests)
        ])

    def test_an_environment_with_no_declaration_reads_as_the_implicit_product(
        self
    ) -> None:
        self._nightly("linux-sim")
        (item,) = self.call("GET", "/api/environments")["environments"]
        self.assertEqual(item["product"], "")

    def test_declare_then_read_back(self) -> None:
        self._nightly("linux-sim")
        data = self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"product": "Atlas", "username": "amy"})
        self.assertEqual(data["product"], "Atlas")
        self.assertFalse(data["cleared"])
        (item,) = self.call("GET", "/api/environments")["environments"]
        self.assertEqual(item["product"], "Atlas")

    def test_empty_string_clears_the_mapping(self) -> None:
        self._nightly("linux-sim")
        self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"product": "Atlas", "username": "amy"})
        data = self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"product": "", "username": "amy"})
        self.assertEqual(data["product"], "")
        self.assertTrue(data["cleared"])
        (item,) = self.call("GET", "/api/environments")["environments"]
        self.assertEqual(item["product"], "")

    def test_clearing_what_was_never_declared_is_not_an_error(self) -> None:
        self._nightly("linux-sim")
        data = self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"product": "", "username": "amy"})
        self.assertFalse(data["cleared"])

    def test_an_unknown_environment_is_404(self) -> None:
        self._nightly("linux-sim")
        data = self.call(
            "PUT", "/api/environments/typo/product",
            body={"product": "Atlas", "username": "amy"}, expect=404)
        self.assertIn("typo", data["error"])

    def test_the_product_field_is_required(self) -> None:
        self._nightly("linux-sim")
        data = self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"username": "amy"}, expect=400)
        self.assertIn("required", data["error"])

    def test_the_username_field_is_required(self) -> None:
        self._nightly("linux-sim")
        self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"product": "Atlas"}, expect=400)

    def test_a_non_string_product_is_rejected(self) -> None:
        self._nightly("linux-sim")
        data = self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"product": 5, "username": "amy"}, expect=400)
        self.assertIn("product", data["error"])

    def test_the_declaring_user_is_recorded(self) -> None:
        self._nightly("linux-sim")
        self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"product": "Atlas", "username": "amy"})
        names = [u["username"]
                 for u in self.call("GET", "/api/users")["users"]]
        self.assertIn("amy", names)

    def test_wrong_methods(self) -> None:
        self.assert_405(
            "GET", "/api/environments/linux-sim/product", "PUT")


class TestProductFiltering(ApiCase):
    """``product=`` on dashboard/summary/time/timeline, and the
    ``products[]`` breakdown on /api/summary (WP-20, §2.2)."""

    def _declare(self, environment: str, product: str) -> None:
        self.call(
            "PUT", "/api/environments/{}/product".format(environment),
            body={"product": product, "username": "amy"})

    def _seed(self) -> None:
        self.import_runs([
            record(environment="linux-sim", test_name="t1",
                   result="FAIL"),
            record(environment="win-sim", test_name="t2", result="FAIL"),
            record(environment="mac-sim", test_name="t3"),
        ])
        self._declare("linux-sim", "Atlas")
        self._declare("win-sim", "Atlas")
        self._declare("mac-sim", "Borealis")

    def test_products_empty_when_nothing_declared(self) -> None:
        self.import_runs([record(environment="linux-sim")])
        data = self.call("GET", "/api/summary")
        self.assertEqual(data["products"], [])

    def test_products_breakdown_groups_by_declared_product(self) -> None:
        self._seed()
        data = self.call("GET", "/api/summary")
        by_product = {p["product"]: p for p in data["products"]}
        self.assertEqual(sorted(by_product), ["Atlas", "Borealis"])
        self.assertEqual(by_product["Atlas"]["failing"], 2)
        self.assertEqual(by_product["Borealis"]["failing"], 0)

    def test_products_breakdown_is_estate_wide_regardless_of_scope(
        self
    ) -> None:
        """A request scoped to one product must still see the others, or
        the switcher has no way back to "All products"."""
        self._seed()
        scoped = self.call(
            "GET", "/api/summary", query={"product": ["Atlas"]})
        self.assertEqual(
            sorted(p["product"] for p in scoped["products"]),
            ["Atlas", "Borealis"])

    def test_dashboard_product_filter_is_an_environment_allow_list(
        self
    ) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/dashboard", query={"product": ["Atlas"]})
        environments = {t["environment"] for t in data["tests"]}
        self.assertEqual(environments, {"linux-sim", "win-sim"})
        self.assertEqual(data["product"], "Atlas")

    def test_dashboard_rows_carry_their_own_product(self) -> None:
        """A cheap per-row join, so the Product column can render without
        a second request for the environment -> product mapping."""
        self._seed()
        data = self.call("GET", "/api/dashboard")
        by_env = {t["environment"]: t["product"] for t in data["tests"]}
        self.assertEqual(by_env["linux-sim"], "Atlas")
        self.assertEqual(by_env["win-sim"], "Atlas")
        self.assertEqual(by_env["mac-sim"], "Borealis")

    def test_dashboard_unmapped_environment_has_the_implicit_product(
        self
    ) -> None:
        self.import_runs([record(environment="unmapped")])
        (row,) = self.call("GET", "/api/dashboard")["tests"]
        self.assertEqual(row["product"], "")

    def test_summary_queue_rows_carry_their_own_product(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/summary",
            query={"parts": ["queue"], "queue": ["new_failures"]})
        by_env = {
            t["environment"]: t["product"] for t in data["queue"]["tests"]
        }
        self.assertEqual(by_env["linux-sim"], "Atlas")
        self.assertEqual(by_env["win-sim"], "Atlas")

    def test_dashboard_unknown_product_is_empty_not_404(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/dashboard", query={"product": ["Nope"]})
        self.assertEqual(data["tests"], [])
        self.assertEqual(data["total"], 0)

    def test_summary_product_filter_scopes_the_headline(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/summary", query={"product": ["Atlas"]})
        self.assertEqual(data["status"]["total_tests"], 2)
        self.assertEqual(data["product"], "Atlas")

    def test_summary_product_filter_scopes_the_stale_before(self) -> None:
        """Each product's own window -- never one wall-clock phrase
        across products (docs/STREAMS_PLAN.md §2.3)."""
        self._seed()
        scoped = self.call(
            "GET", "/api/summary", query={"product": ["Atlas"]})
        unscoped = self.call("GET", "/api/summary")
        # Both are legitimate cutoffs; the point under test is that the
        # scoped call actually goes through the scoped code path and
        # both answer without error -- exact equality is not asserted
        # because with this little history both may legitimately fall
        # back to the same wall-clock default.
        self.assertIn("stale_before", scoped)
        self.assertIn("stale_before", unscoped)

    def test_summary_unknown_product_is_empty_not_404(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/summary", query={"product": ["Nope"]})
        self.assertEqual(data["status"]["total_tests"], 0)

    def test_summary_environments_are_scoped_to_the_product(self) -> None:
        """The bug found live: product=Atlas and product=Beacon returned
        the IDENTICAL environments list, offering each other's
        environments in the picker — environments()/scripts()/
        latest_run_time_by_environment() never looked at product scope
        at all."""
        self._seed()
        atlas = self.call(
            "GET", "/api/summary", query={"product": ["Atlas"]})
        borealis = self.call(
            "GET", "/api/summary", query={"product": ["Borealis"]})
        self.assertEqual(
            sorted(atlas["environments"]), ["linux-sim", "win-sim"])
        self.assertEqual(borealis["environments"], ["mac-sim"])

    def test_summary_environment_updated_is_scoped_to_the_product(
        self
    ) -> None:
        self._seed()
        atlas = self.call(
            "GET", "/api/summary", query={"product": ["Atlas"]})
        self.assertEqual(
            sorted(atlas["environment_updated"]), ["linux-sim", "win-sim"])
        self.assertNotIn("mac-sim", atlas["environment_updated"])

    def test_summary_scripts_are_scoped_to_the_product(self) -> None:
        self._seed()
        borealis = self.call(
            "GET", "/api/summary", query={"product": ["Borealis"]})
        # t3 is mac-sim's only script/test in _seed(); t1/t2 belong to
        # Atlas's environments and must not leak into Borealis's list.
        self.assertNotIn("t1", borealis["scripts"])
        self.assertNotIn("t2", borealis["scripts"])

    def test_summary_environments_unscoped_is_unchanged(self) -> None:
        """Zero visible change: no product= means every environment,
        exactly as before this fix."""
        self._seed()
        data = self.call("GET", "/api/summary")
        self.assertEqual(
            sorted(data["environments"]),
            ["linux-sim", "mac-sim", "win-sim"])

    def test_summary_environments_unknown_product_is_empty(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/summary", query={"product": ["Nope"]})
        self.assertEqual(data["environments"], [])
        self.assertEqual(data["environment_updated"], {})

    def test_time_unknown_product_is_empty_not_404(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/time",
            query={"product": ["Nope"], "include_stale": ["1"]})
        self.assertEqual(data["items"], [])

    def test_time_product_filter(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/time",
            query={"group_by": ["environment"], "product": ["Atlas"],
                   "include_stale": ["1"]})
        keys = {item["key"] for item in data["items"]}
        self.assertEqual(keys, {"linux-sim", "win-sim"})
        self.assertEqual(data["product"], "Atlas")

    def test_timeline_product_resolving_to_one_environment_is_used(
        self
    ) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/timeline", query={"product": ["Borealis"]})
        self.assertEqual(data["environment"], "mac-sim")

    def test_timeline_product_resolving_to_many_still_needs_environment(
        self
    ) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/timeline", query={"product": ["Atlas"]},
            expect=400)
        self.assertIn("environment", data["error"])

    def test_timeline_explicit_environment_wins_over_product(self) -> None:
        """No 400 for "environment does not belong to product" -- an
        explicit environment always wins (docs/STREAMS_PLAN.md §2.6's
        only stated product-error rule is unknown product = empty
        result, not this)."""
        self._seed()
        data = self.call(
            "GET", "/api/timeline",
            query={"environment": ["mac-sim"], "product": ["Atlas"]})
        self.assertEqual(data["environment"], "mac-sim")


def _trace_sql_into(conn: sqlite3.Connection, into: List[str]) -> None:
    """Register a trace callback that appends each statement to *into*.

    Not ``conn.set_trace_callback(into.append)``: 3.6's sqlite3 keeps
    registered callbacks in an internal dict, so the callable must be
    hashable, and a bound ``list.append`` hashes via the list -- a
    TypeError on the deployment interpreter. The lambda is the fix.
    Same helper as tests/test_storage.py's, kept local rather than
    imported so the two test modules stay independent.
    """
    conn.set_trace_callback(lambda statement: into.append(statement))


class ParseWatchSpecTest(unittest.TestCase):
    """:func:`api._parse_watch_spec` — the ``kind:name@expected``
    grammar, pure and offline (docs/STREAMS_PLAN.md §2.4)."""

    def test_no_colon_is_kind_with_empty_name_and_no_suffix(self) -> None:
        self.assertEqual(api._parse_watch_spec("garbage"),
                          ("garbage", "", None))

    def test_plain_kind_name_has_no_suffix(self) -> None:
        self.assertEqual(api._parse_watch_spec("e:linux-sim"),
                          ("e", "linux-sim", None))

    def test_day_suffix(self) -> None:
        self.assertEqual(api._parse_watch_spec("p:Atlas@7d"),
                          ("p", "Atlas", "7d"))

    def test_hour_suffix(self) -> None:
        self.assertEqual(api._parse_watch_spec("e:win-sim@36h"),
                          ("e", "win-sim", "36h"))

    def test_numeric_stream_id_with_suffix(self) -> None:
        self.assertEqual(api._parse_watch_spec("s:2@1d"),
                          ("s", "2", "1d"))

    def test_a_name_containing_at_with_no_valid_tail_is_not_split(
        self
    ) -> None:
        """A build/branch name is free text and may itself contain
        "@" — "release@2026" has no digit+unit tail, so it is not a
        suffix at all, and the WHOLE thing is the name."""
        self.assertEqual(api._parse_watch_spec("p:release@2026"),
                          ("p", "release@2026", None))

    def test_an_invalid_unit_is_part_of_the_name(self) -> None:
        self.assertEqual(api._parse_watch_spec("e:foo@3w"),
                          ("e", "foo@3w", None))

    def test_a_non_digit_count_is_part_of_the_name(self) -> None:
        self.assertEqual(api._parse_watch_spec("e:foo@d"),
                          ("e", "foo@d", None))

    def test_double_at_splits_at_the_last_one_only(self) -> None:
        """"release@2026@1d" -- the name legitimately contains an "@",
        and the suffix is still found, at the LAST "@"."""
        self.assertEqual(api._parse_watch_spec("p:release@2026@1d"),
                          ("p", "release@2026", "1d"))

    def test_at_with_empty_tail_is_part_of_the_name(self) -> None:
        self.assertEqual(api._parse_watch_spec("e:foo@"),
                          ("e", "foo@", None))


class ParseExpectedAgeTest(unittest.TestCase):
    """:func:`api._parse_expected_age` — the two units the grammar
    allows, days and hours."""

    def test_days(self) -> None:
        self.assertEqual(
            api._parse_expected_age("7d"), datetime.timedelta(days=7))

    def test_hours(self) -> None:
        self.assertEqual(
            api._parse_expected_age("36h"), datetime.timedelta(hours=36))


class ApplyStalenessTest(unittest.TestCase):
    """:func:`api._apply_staleness` — the pure comparison, offline."""

    NOW = datetime.datetime(2026, 8, 9, 12, 0, 0)

    def test_no_expected_adds_nothing_at_all(self) -> None:
        card = {}  # type: Dict[str, Any]
        api._apply_staleness(card, None, self.NOW, self.NOW)
        self.assertEqual(card, {})

    def test_never_reported_is_stale_regardless_of_age(self) -> None:
        card = {}  # type: Dict[str, Any]
        api._apply_staleness(card, "7d", None, self.NOW)
        self.assertEqual(card["expected"], "7d")
        self.assertTrue(card["stale"])

    def test_within_the_declared_age_is_not_stale(self) -> None:
        card = {}  # type: Dict[str, Any]
        recent = self.NOW - datetime.timedelta(hours=1)
        api._apply_staleness(card, "1d", recent, self.NOW)
        self.assertFalse(card["stale"])

    def test_older_than_the_declared_age_is_stale(self) -> None:
        card = {}  # type: Dict[str, Any]
        old = self.NOW - datetime.timedelta(days=3)
        api._apply_staleness(card, "1d", old, self.NOW)
        self.assertTrue(card["stale"])


class TestWatch(ApiCase):
    """GET /api/watch (WP-20, docs/STREAMS_PLAN.md §2.4): the whole
    Watchlist page in one request, cards in request order."""

    def _declare(self, environment: str, product: str) -> None:
        self.call(
            "PUT", "/api/environments/{}/product".format(environment),
            body={"product": product, "username": "amy"})

    def _seed(self) -> None:
        self.import_runs([
            record(environment="linux-sim", test_name="t1",
                   result="FAIL"),
            record(environment="win-sim", test_name="t2", result="FAIL"),
            record(environment="mac-sim", test_name="t3"),
        ])
        self._declare("linux-sim", "Atlas")
        self._declare("win-sim", "Atlas")
        self._declare("mac-sim", "Borealis")

    def test_cards_come_back_in_request_order(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/watch",
            query={"c": ["e:mac-sim", "p:Atlas", "e:linux-sim"]})
        self.assertEqual(
            [c["spec"] for c in data["cards"]],
            ["e:mac-sim", "p:Atlas", "e:linux-sim"])

    def test_an_environment_card(self) -> None:
        self._seed()
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["e:linux-sim"]}
        )["cards"]
        self.assertTrue(card["ok"])
        self.assertEqual(card["kind"], "environment")
        self.assertEqual(card["name"], "linux-sim")
        self.assertEqual(card["failing"], 1)
        self.assertEqual(card["new_failures"], 1)
        self.assertIsNotNone(card["last_reported"])
        self.assertIsNotNone(card["stale_before"])
        # WP-23: t1 fails and nobody owns it.
        self.assertEqual(card["unassigned_failing"], 1)
        # No "@" suffix in the spec -- no staleness judgment at all.
        self.assertNotIn("stale", card)
        self.assertNotIn("expected", card)
        # WP-23 bugfix: the card names its OWN product, so the frontend
        # link is scope-self-sufficient rather than trusting whatever
        # product this browser's switcher happens to have stored.
        self.assertEqual(card["product"], "Atlas")

    def test_an_environment_cards_product_is_empty_when_unmapped(
        self
    ) -> None:
        """The bug this pins: an unmapped environment's card must say
        "" (never omit the key, never guess), so the frontend's link
        can send an empty ?product= -- "All products" -- instead of
        silently inheriting whatever product this browser last had
        selected, which could resolve the environment filter to an
        empty allow-list under the WRONG product and render a blank
        page."""
        self.import_runs([record(environment="unmapped-env")])
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["e:unmapped-env"]}
        )["cards"]
        self.assertEqual(card["product"], "")

    def test_an_environment_cards_unassigned_failing_excludes_assigned(
        self
    ) -> None:
        self._seed()
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", "t1") + "/assignee",
            body={"username": "alice", "assigned_by": "bob"})
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["e:linux-sim"]}
        )["cards"]
        self.assertEqual(card["failing"], 1)  # still failing
        self.assertEqual(card["unassigned_failing"], 0)  # but owned

    def test_an_environment_card_declares_staleness_fresh(self) -> None:
        self._seed()
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["e:linux-sim@7d"]}
        )["cards"]
        self.assertEqual(card["expected"], "7d")
        self.assertFalse(card["stale"])

    def test_an_environment_card_declares_staleness_stale(self) -> None:
        self.import_runs([record(
            environment="linux-sim", test_name="t1", result="FAIL",
            start_time="2026-07-01T00:00:00.000000",
            end_time="2026-07-01T00:00:01.000000")])
        self._declare("linux-sim", "Atlas")
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["e:linux-sim@1h"]}
        )["cards"]
        self.assertEqual(card["expected"], "1h")
        self.assertTrue(card["stale"])

    def test_a_product_card(self) -> None:
        self._seed()
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["p:Atlas"]}
        )["cards"]
        self.assertTrue(card["ok"])
        self.assertEqual(card["kind"], "product")
        self.assertEqual(card["failing"], 2)
        self.assertIsNone(card["last_reported"])
        self.assertIsNotNone(card["stale_before"])
        # WP-23: t1 (linux-sim) and t2 (win-sim) both fail, unowned.
        self.assertEqual(card["unassigned_failing"], 2)
        self.assertNotIn("stale", card)
        self.assertNotIn("expected", card)

    def test_a_product_cards_unassigned_failing_sums_its_environments(
        self
    ) -> None:
        self._seed()
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", "t1") + "/assignee",
            body={"username": "alice", "assigned_by": "bob"})
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["p:Atlas"]}
        )["cards"]
        self.assertEqual(card["failing"], 2)
        self.assertEqual(card["unassigned_failing"], 1)  # only t2 now

    def test_a_product_cards_staleness_is_judged_by_its_laggard(
        self
    ) -> None:
        """docs/STREAMS_PLAN.md §2.4: the product's freshness timestamp
        is deliberately its OLDEST-reporting environment, not its
        newest -- "everything reported" is the bar, not "something
        did"."""
        self.import_runs([
            record(environment="linux-sim", test_name="t1",
                   start_time="2026-07-26T10:00:00.000000",
                   end_time="2026-07-26T10:00:01.000000"),
            record(environment="win-sim", test_name="t2",
                   start_time="2026-07-01T00:00:00.000000",
                   end_time="2026-07-01T00:00:01.000000"),
        ])
        self._declare("linux-sim", "Atlas")
        self._declare("win-sim", "Atlas")
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["p:Atlas@1d"]}
        )["cards"]
        # linux-sim reported 2h ago (fresh); win-sim reported 25 days
        # ago -- the laggard -- so the CARD is stale even though its
        # newest environment is not.
        self.assertEqual(card["laggard"]["environment"], "win-sim")
        self.assertTrue(card["stale"])

    def test_a_product_card_names_its_laggard_environment(self) -> None:
        """A product spans environments reporting hours apart, so its
        card carries no single timestamp (pinned above) — what it
        carries instead is the furthest-behind environment BY NAME,
        because "which one am I waiting on" is the morning question and
        a newest-of-several figure is exactly the trap the handover
        documents. Found in the 2026-08-09 manager-persona review."""
        self.import_runs([
            record(environment="linux-sim", test_name="t1",
                   start_time="2026-08-08T06:00:00.000000",
                   end_time="2026-08-08T06:00:01.000000"),
            record(environment="win-sim", test_name="t2",
                   start_time="2026-08-05T04:30:00.000000",
                   end_time="2026-08-05T04:30:01.000000"),
        ])
        self._declare("linux-sim", "Atlas")
        self._declare("win-sim", "Atlas")
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["p:Atlas"]}
        )["cards"]
        self.assertEqual(card["laggard"]["environment"], "win-sim")
        self.assertEqual(card["laggard"]["last_reported"],
                         "2026-08-05T04:30:00.000000")

    def test_a_silent_environment_is_the_worst_laggard(self) -> None:
        """An environment with NO recorded run outranks any old one —
        absence of data must never hide behind stale data. Unreachable
        through the API today (the product PUT refuses an unknown
        environment, and dropping an environment takes its mapping with
        it — environment_products is in _ENVIRONMENT_TABLES), so this
        pins the DEFENSIVE branch of the pure helper directly: if some
        future path ever leaves a mapping without data, the card must
        point at the silence, not average over it."""
        laggard = api._product_laggard(
            ["linux-sim", "ghost-sim"],
            {"linux-sim": datetime.datetime(2026, 8, 5, 4, 30)},
        )
        self.assertEqual(laggard["environment"], "ghost-sim")
        self.assertIsNone(laggard["last_reported"])

    def test_the_laggard_of_no_environments_is_none(self) -> None:
        self.assertIsNone(api._product_laggard([], {}))

    def test_an_unknown_environment_is_an_error_card_not_404(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/watch", query={"c": ["e:typo"]})
        (card,) = data["cards"]
        self.assertFalse(card["ok"])
        self.assertIn("error", card)

    def test_an_unknown_environment_page_is_still_200(self) -> None:
        self._seed()
        response = self.request(
            "GET", "/api/watch", query={"c": ["e:typo"]})
        self.assertEqual(response.status, 200)

    def test_an_unknown_product_is_an_error_card(self) -> None:
        self._seed()
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["p:Nope"]}
        )["cards"]
        self.assertFalse(card["ok"])

    def test_error_card_kind_matches_the_ok_cards_spelling(self) -> None:
        """A recognised kind's error card must say "environment"/
        "product", the same word an ok card of that kind uses -- so the
        frontend never has to special-case error cards to know what
        they were trying to be."""
        self._seed()
        data = self.call(
            "GET", "/api/watch",
            query={"c": ["e:typo", "p:Nope", "e:linux-sim"]})
        env_card, product_card, ok_card = data["cards"]
        self.assertEqual(env_card["kind"], "environment")
        self.assertEqual(product_card["kind"], "product")
        self.assertEqual(ok_card["kind"], "environment")

    def test_an_unknown_kind_is_an_error_card(self) -> None:
        self._seed()
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["x:whatever"]}
        )["cards"]
        self.assertFalse(card["ok"])
        self.assertIn("unknown", card["error"])

    def test_a_malformed_spec_with_no_colon_is_an_error_card(self) -> None:
        self._seed()
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["garbage"]}
        )["cards"]
        self.assertFalse(card["ok"])

    def test_a_mix_of_good_and_bad_cards_is_still_200(self) -> None:
        self._seed()
        data = self.call(
            "GET", "/api/watch",
            query={"c": ["e:linux-sim", "e:typo", "p:Atlas"]})
        oks = [c["ok"] for c in data["cards"]]
        self.assertEqual(oks, [True, False, True])

    def test_the_mainline_stream_id_is_never_a_valid_s_card(self) -> None:
        """Mainline is not a Build picker entry (list_streams excludes
        it too) -- a card comparing it to itself would be trivially
        all-zero, not a useful verdict."""
        self._seed()
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["s:1"]})["cards"]
        self.assertFalse(card["ok"])

    def test_an_s_card_mixed_with_good_cards_all_render(self) -> None:
        """Streams work like every other card kind -- a mix of good and
        (here, deliberately bad) cards is still one 200 page."""
        self._seed()
        data = self.call(
            "GET", "/api/watch",
            query={"c": ["p:Atlas", "e:linux-sim", "s:1"]})
        oks = [c["ok"] for c in data["cards"]]
        self.assertEqual(oks, [True, True, False])

    def test_the_cap_refuses_clearly(self) -> None:
        self._seed()
        specs = ["e:linux-sim"] * (api._WATCH_MAX_CARDS + 1)
        data = self.call(
            "GET", "/api/watch", query={"c": specs}, expect=413)
        self.assertIn(str(api._WATCH_MAX_CARDS), data["error"])

    def test_the_cap_boundary_is_accepted(self) -> None:
        self._seed()
        specs = ["e:linux-sim"] * api._WATCH_MAX_CARDS
        data = self.call("GET", "/api/watch", query={"c": specs})
        self.assertEqual(len(data["cards"]), api._WATCH_MAX_CARDS)
        self.assertEqual(data["cap"], api._WATCH_MAX_CARDS)

    def test_no_cards_is_an_empty_page_not_an_error(self) -> None:
        data = self.call("GET", "/api/watch")
        self.assertEqual(data["cards"], [])

    def test_query_count_does_not_grow_with_card_count(self) -> None:
        """§0.4: no new list query may cost more the more cards are
        asked for. One card and fifty must cost the SAME number of
        queries -- every card after the first fetch is a Python-side
        slice of data already in memory.

        WP-23 "ONE MORE PERF SLICE" widened this: each measured call is
        now preceded by an identical untraced warm-up call, so both
        measurements start from the SAME (warm) summary/watch memo
        state. Without it a bare comparison of a cold call against a
        warm one would fail for a reason unrelated to card count --
        exactly the false positive this guard must not produce. The
        original finding -- card count must not change query count --
        is unchanged and still enforced; only cache STATE is now held
        equal between the two sides of the comparison, matching the
        steady-state repeat-load traffic the memo targets."""
        self._seed()
        conn = self.storage._conn()

        def query_count(specs: List[str]) -> int:
            self.call("GET", "/api/watch", query={"c": specs})
            statements = []  # type: List[str]
            _trace_sql_into(conn, statements)
            try:
                self.call("GET", "/api/watch", query={"c": specs})
            finally:
                conn.set_trace_callback(None)
            return len(statements)

        one = query_count(["e:linux-sim"])
        fifty = query_count(["e:linux-sim"] * api._WATCH_MAX_CARDS)
        self.assertEqual(one, fifty)

    def test_wrong_methods(self) -> None:
        self.assert_405("POST", "/api/watch", "GET", body={})


class TestWatchStreamCards(ApiCase):
    """s: cards (WP-21, docs/STREAMS_PLAN.md §3.6): branch/build verdicts
    on the Watchlist, resolved through the same storage reads as
    /api/compare, still O(1) queries regardless of card count."""

    def _declare(self, environment: str, product: str) -> None:
        self.call(
            "PUT", "/api/environments/{}/product".format(environment),
            body={"product": product, "username": "amy"})

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(environment="linux-sim", test_name="test_a",
                   result="PASS"),
            record(environment="linux-sim", test_name="test_b",
                   result="FAIL"),
        ])
        self._declare("linux-sim", "Atlas")
        self.import_runs([
            record(environment="linux-sim", test_name="test_a",
                   result="FAIL", build="feat/x",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
        ])
        streams = self.call(
            "GET", "/api/streams", query={"product": ["Atlas"]})["streams"]
        self.stream_id = streams[0]["id"]

    def test_an_ok_stream_card(self) -> None:
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}".format(self.stream_id)]})["cards"]
        self.assertTrue(card["ok"])
        self.assertEqual(card["kind"], "stream")
        self.assertEqual(card["name"], "feat/x")
        self.assertEqual(card["stream_kind"], "build")
        self.assertEqual(card["product"], "Atlas")
        self.assertEqual(card["new_failures"], 1)   # test_a
        self.assertEqual(card["no_result"], 1)      # test_b
        self.assertEqual(card["agree"], 0)
        self.assertIsNotNone(card["last_seen"])
        self.assertIsNotNone(card["baseline_last_seen"])
        # WP-25 (docs/ONE_KIND_PLAN.md §1.4): one wording for every
        # stream -- the baseline is always mainline, named explicitly,
        # never assumed by the frontend from a hardcoded word.
        self.assertEqual(card["baseline_kind"], "mainline")
        self.assertEqual(card["baseline_name"], "")
        # WP-23: test_a fails on the branch and is unassigned.
        self.assertEqual(card["unassigned_failing"], 1)
        self.assertNotIn("stale", card)
        self.assertNotIn("expected", card)

    def test_a_stream_cards_unassigned_failing_excludes_assigned(
        self
    ) -> None:
        """Assignments are stream-agnostic (docs/STREAMS_PLAN.md §3.6):
        assigning test_a — from mainline, no stream_id at all — still
        clears it off the BRANCH card's unassigned-failing count,
        because there is only ever one owner for a triple."""
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", "test_a")
            + "/assignee",
            body={"username": "alice", "assigned_by": "bob"})
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}".format(self.stream_id)]})["cards"]
        self.assertEqual(card["unassigned_failing"], 0)

    def test_a_stream_cards_staleness_uses_last_seen(self) -> None:
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}@1d".format(self.stream_id)]})["cards"]
        self.assertEqual(card["expected"], "1d")
        # The branch's only run is 2026-07-25T03:00, NOW is
        # 2026-07-26T12:00 -- about 33h, older than the declared 1d.
        self.assertTrue(card["stale"])

    def test_a_stream_cards_staleness_when_fresh(self) -> None:
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}@7d".format(self.stream_id)]})["cards"]
        self.assertFalse(card["stale"])

    def test_both_unassigned_failing_and_stale_can_show_on_one_card(
        self
    ) -> None:
        """The two are independent facts about the same card — both
        keys are present together when both are true."""
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}@1h".format(self.stream_id)]})["cards"]
        self.assertEqual(card["unassigned_failing"], 1)
        self.assertTrue(card["stale"])

    def test_an_unknown_stream_id_is_an_error_card(self) -> None:
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["s:999999"]})["cards"]
        self.assertFalse(card["ok"])
        self.assertEqual(card["kind"], "stream")

    def test_a_non_integer_s_value_is_an_error_card(self) -> None:
        (card,) = self.call(
            "GET", "/api/watch", query={"c": ["s:not-a-number"]})["cards"]
        self.assertFalse(card["ok"])

    def test_the_click_through_id_is_present(self) -> None:
        """The frontend opens the branch-scoped dashboard from this."""
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}".format(self.stream_id)]})["cards"]
        self.assertEqual(card["id"], self.stream_id)

    def test_query_count_does_not_grow_with_s_card_count(self) -> None:
        """The same §0.4 flat-cost property TestWatch pins for e:/p:
        cards, extended here to s: cards -- a SEPARATE assertion rather
        than editing TestWatch's, since that one is specifically about
        the pre-existing card kinds and must keep passing unchanged.

        WP-23 "ONE MORE PERF SLICE": same warm-up-before-each-measurement
        widening as ``TestWatch.test_query_count_does_not_grow_with_card_
        count`` -- see its docstring for why."""
        conn = self.storage._conn()

        def query_count(specs: List[str]) -> int:
            self.call("GET", "/api/watch", query={"c": specs})
            statements = []  # type: List[str]
            _trace_sql_into(conn, statements)
            try:
                self.call("GET", "/api/watch", query={"c": specs})
            finally:
                conn.set_trace_callback(None)
            return len(statements)

        spec = "s:{}".format(self.stream_id)
        one = query_count([spec])
        many = query_count([spec] * api._WATCH_MAX_CARDS)
        self.assertEqual(one, many)


class TestWatchStreamCardImplicitProduct(ApiCase):
    """A stream whose product is "" (WP-21) -- the common case on a
    deployment that has never declared any products (WP-20's default).

    Regression: found by driving /api/watch against a real server with
    NO declared products at all (never exercised by TestWatchStreamCards
    above, which always calls `set_environment_product` first). Every
    s: card came back all-zero, silently wrong -- `_handle_watch` built
    the stream's environment scope from `product_to_envs.get("", [])`,
    which is ALWAYS empty (`environment_products_map()` only ever
    contains environments that HAVE a declared product), instead of
    resolving "" the way `Storage.environments_for_product("")` does
    (every KNOWN environment nobody has mapped). Fixed by special-casing
    "" in `_handle_watch` to that same set, computed once, still O(1).
    """

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(environment="linux-sim", test_name="test_a",
                   result="PASS"),
            record(environment="linux-sim", test_name="test_b",
                   result="FAIL"),
        ])
        # Deliberately NO set_environment_product call -- linux-sim stays
        # in the implicit "" product, same as a fresh install.
        self.import_runs([
            record(environment="linux-sim", test_name="test_a",
                   result="FAIL", build="feat/x",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
        ])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]

    def test_the_card_is_not_all_zero(self) -> None:
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}".format(self.stream_id)]})["cards"]
        self.assertTrue(card["ok"])
        self.assertEqual(card["product"], "")
        self.assertEqual(card["new_failures"], 1)   # test_a
        self.assertEqual(card["no_result"], 1)      # test_b
        self.assertEqual(
            card["new_failures"], self.call(
                "GET", "/api/compare",
                query={"stream": [str(self.stream_id)]}
            )["counts"]["new_failures"],
            "the card must agree with /api/compare for the same stream")


class TestWatchBuildStreamCards(ApiCase):
    """WP-25 (docs/ONE_KIND_PLAN.md §1.4, user decision, explicit): the
    s: card's verdict is ALWAYS against mainline -- one wording for every
    stream. WP-22 had a build-kind card default to its predecessor build
    when one existed (see git history for that behaviour); WP-25 removed
    it, the same "default baseline is mainline, always" decision applied
    to the dashboard's own delta view. A card reading "vs build 1.0"
    that clicked through to a dashboard reading "vs mainline" would be
    exactly the two-surfaces-disagree bug this decision avoids."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(environment="linux-sim", test_name="test_a",
                   result="PASS"),
        ])
        self.call(
            "PUT", "/api/environments/linux-sim/product",
            body={"product": "Atlas", "username": "amy"})
        self.import_runs([
            record(environment="linux-sim", test_name="test_a",
                   result="FAIL", build="1.0",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
        ])
        self.import_runs([
            record(environment="linux-sim", test_name="test_a",
                   result="FAIL", build="1.1",
                   start_time="2026-07-25T04:00:00.000000",
                   end_time="2026-07-25T04:00:03.000000"),
            record(environment="linux-sim", test_name="test_b",
                   result="PASS", build="1.1",
                   start_time="2026-07-25T04:00:00.000000",
                   end_time="2026-07-25T04:00:03.000000"),
        ])
        builds = {
            s["name"]: s["id"] for s in self.call(
                "GET", "/api/streams", query={"product": ["Atlas"]}
            )["streams"]
        }
        self.build_1_0 = builds["1.0"]
        self.build_1_1 = builds["1.1"]

    def test_a_later_builds_verdict_is_still_against_mainline(self) -> None:
        """Even though 1.0 (an earlier same-product build) exists, the
        card must NOT default to it -- mainline only."""
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}".format(self.build_1_1)]})["cards"]
        self.assertEqual(card["baseline_kind"], "mainline")
        self.assertEqual(card["baseline_name"], "")
        # mainline: test_a PASS only. 1.1: test_a FAIL, test_b PASS (new).
        self.assertEqual(card["new_failures"], 1)
        self.assertEqual(card["new_tests"], 1)
        self.assertEqual(card["both_failing"], 0)

    def test_the_oldest_builds_baseline_is_also_mainline(self) -> None:
        (card,) = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}".format(self.build_1_0)]})["cards"]
        self.assertEqual(card["baseline_kind"], "mainline")

    def test_agrees_with_compare_using_the_default_baseline(self) -> None:
        card = self.call(
            "GET", "/api/watch",
            query={"c": ["s:{}".format(self.build_1_1)]})["cards"][0]
        compared = self.call(
            "GET", "/api/compare",
            query={"stream": [str(self.build_1_1)]})
        self.assertEqual(card["both_failing"], compared["counts"]["both_failing"])
        self.assertEqual(card["new_tests"], compared["counts"]["new_tests"])


class TestEnvironmentUpdated(ApiCase):
    """/api/summary says when each environment last reported."""

    def _seed(self) -> None:
        self.import_runs([
            record(environment="linux-sim", test_name="a",
                   start_time=format_iso(
                       NOW - datetime.timedelta(hours=7)),
                   end_time=format_iso(
                       NOW - datetime.timedelta(hours=7)
                       + datetime.timedelta(seconds=1))),
            record(environment="win-sim", script="b.py", test_name="c",
                   start_time=format_iso(
                       NOW - datetime.timedelta(hours=2)),
                   end_time=format_iso(
                       NOW - datetime.timedelta(hours=2)
                       + datetime.timedelta(seconds=1))),
        ])

    def test_each_environment_appears_with_its_own_time(self) -> None:
        self._seed()
        updated = self.call("GET", "/api/summary")["environment_updated"]
        self.assertEqual(
            updated,
            {"linux-sim": format_iso(NOW - datetime.timedelta(hours=7)),
             "win-sim": format_iso(NOW - datetime.timedelta(hours=2))})

    def test_the_estate_figure_is_only_the_newest_of_them(self) -> None:
        """Which is exactly why the per-environment map exists: the
        headline reads healthy while an environment that has not run
        yet is invisible in it."""
        self._seed()
        data = self.call("GET", "/api/summary")
        self.assertEqual(
            data["latest_run_time"],
            format_iso(NOW - datetime.timedelta(hours=2)))
        self.assertNotEqual(
            data["environment_updated"]["linux-sim"],
            data["latest_run_time"])

    def test_an_empty_estate_reports_an_empty_map(self) -> None:
        self.assertEqual(
            self.call("GET", "/api/summary")["environment_updated"], {})

    def test_it_is_not_narrowed_by_the_environment_filter(self) -> None:
        """The map always carries every environment; the SCOPE is the
        client's to apply.

        The home screen shows only the selected one, so this is not
        "the page shows them all regardless" — verified by driving the
        filter through All -> each environment -> All. The server keeps
        the whole map because narrowing three entries buys nothing and
        because the scope is a presentation decision, not a data one.
        """
        self._seed()
        updated = self.call(
            "GET", "/api/summary",
            query={"environment": ["linux-sim"]})["environment_updated"]
        self.assertEqual(sorted(updated), ["linux-sim", "win-sim"])


class SummaryPartsTest(ApiCase):
    """The parts split must be a PARTITION of the full payload.

    Borrows TestSummary's seeded estate (the method, not the class —
    subclassing would re-run every TestSummary test under this name).
    Every assertion here compares a slice against the whole rather than
    against hand-written values: the contract being pinned is "the
    split cannot drift from the monolith", which hand-written
    expectations could not express.
    """

    seed = TestSummary.seed

    def _assign(self, name: str, who: str) -> None:
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", name, "/assignee"),
            body={"username": who, "assigned_by": "bob"},
        )

    def test_headline_is_the_full_payload_minus_queue_rows(self) -> None:
        self.seed()
        self._assign("test_still_fail", "alice")
        query = {"assignee": ["alice"]}
        full = self.call("GET", "/api/summary", query=query)
        headline = self.call(
            "GET", "/api/summary",
            query={"assignee": ["alice"], "parts": ["headline"]})
        expected = {
            key: value for key, value in full.items() if key != "queues"
        }
        self.assertEqual(headline, expected)
        self.assertNotIn("queues", headline)

    def test_queue_totals_agree_with_the_queues_themselves(self) -> None:
        self.seed()
        self._assign("test_still_fail", "alice")
        full = self.call(
            "GET", "/api/summary", query={"assignee": ["alice"]})
        self.assertEqual(
            full["queue_totals"],
            {
                kind: queue["total"]
                for kind, queue in full["queues"].items()
            },
        )

    def test_each_queue_part_matches_the_full_payload(self) -> None:
        self.seed()
        self._assign("test_still_fail", "alice")
        full = self.call(
            "GET", "/api/summary", query={"assignee": ["alice"]})
        for kind in list(QUEUE_KINDS) + ["mine"]:
            part = self.call(
                "GET", "/api/summary",
                query={
                    "assignee": ["alice"],
                    "parts": ["queue"],
                    "queue": [kind],
                })
            self.assertEqual(part["kind"], kind)
            self.assertEqual(part["queue"], full["queues"][kind], kind)
            self.assertEqual(part["stale_before"], full["stale_before"])
            self.assertEqual(part["queue_cap"], full["queue_cap"])

    def test_environment_scope_applies_to_a_queue_part(self) -> None:
        self.seed()
        part = self.call(
            "GET", "/api/summary",
            query={
                "parts": ["queue"], "queue": ["new_failures"],
                "environment": ["env2"],
            })
        self.assertEqual(
            [entry["test_name"] for entry in part["queue"]["tests"]],
            ["test_first"],
        )

    def test_unknown_parts_or_queue_is_a_400(self) -> None:
        self.seed()
        error = self.call(
            "GET", "/api/summary", query={"parts": ["everything"]},
            expect=400)
        self.assertIn("parts", error["error"])
        error = self.call(
            "GET", "/api/summary",
            query={"parts": ["queue"], "queue": ["bogus"]}, expect=400)
        self.assertIn("queue", error["error"])
        error = self.call(
            "GET", "/api/summary", query={"parts": ["queue"]}, expect=400)
        self.assertIn("queue", error["error"])


class SummaryStreamScopingTest(ApiCase):
    """WP-23 (docs/STREAMS_PLAN.md §5.2): ``/api/summary`` accepts an
    optional ``stream=`` so a long-running branch's "own results" tab
    reads its own headline/trend/queues from this same endpoint.

    The load-bearing guard here is the one the advisor flagged: a branch
    reporting into the SAME environment as mainline must leave every
    field of a MAINLINE (unscoped) ``/api/summary`` response
    byte-identical. This was NOT true of the code as found —
    ``test_counts_by_environment``/``daily_result_counts`` read
    ``latest_runs``/``activity_hours`` with no stream filter, so a
    branch import into ``linux-sim`` silently changed mainline's own
    coverage denominator and trend sums. Closed in the migration-10
    commit; this test is the record of it, written to fail against the
    pre-fix code (moving it here, after the fix already landed, still
    proves the invariant going forward).
    """

    seed = TestSummary.seed

    def test_a_branch_import_into_the_same_environment_leaves_mainline_unchanged(
            self) -> None:
        self.seed()
        before = self.call("GET", "/api/summary")
        self.import_runs([
            record(test_name="test_new_fail", build="feat/x",
                   result="FAIL",
                   start_time="2026-07-26T04:00:00.000000",
                   end_time="2026-07-26T04:00:03.000000"),
            record(test_name="test_branch_only", build="feat/x",
                   result="PASS",
                   start_time="2026-07-26T04:01:00.000000",
                   end_time="2026-07-26T04:01:03.000000"),
        ])
        after = self.call("GET", "/api/summary")
        self.assertEqual(before, after)

    def test_a_branch_import_leaves_the_mainline_trend_unchanged(
            self) -> None:
        self.seed()
        before = self.call(
            "GET", "/api/summary", query={"days": ["7"]})["trend"]
        self.import_runs([
            record(test_name="test_trend_branch", build="feat/y",
                   result="FAIL",
                   start_time="2026-07-26T05:00:00.000000",
                   end_time="2026-07-26T05:00:03.000000"),
        ])
        after = self.call(
            "GET", "/api/summary", query={"days": ["7"]})["trend"]
        self.assertEqual(before, after)

    def test_stream_param_scopes_status_to_the_branch_own_results(
            self) -> None:
        """The other half of the guarantee: the branch's OWN request
        (stream=<id>) reads ITS OWN numbers, not mainline's — this is
        what makes "own results" a real second dashboard rather than a
        copy of mainline's."""
        self.seed()
        self.import_runs([
            record(test_name="test_branch_only", build="feat/x",
                   result="FAIL",
                   start_time="2026-07-26T04:00:00.000000",
                   end_time="2026-07-26T04:00:03.000000"),
        ])
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        stream_id = streams["streams"][0]["id"]
        scoped = self.call(
            "GET", "/api/summary", query={"stream": [str(stream_id)]})
        self.assertEqual(scoped["stream"], stream_id)
        self.assertEqual(scoped["status"]["total_tests"], 1)
        self.assertEqual(scoped["status"]["results"]["FAIL"], 1)

    def test_the_36h_fallback_clamp_applies_to_a_sparse_branch_too(
            self) -> None:
        """The two clamps inside analytics.recent_cutoff (36h fallback
        floor, 14-day ceiling) are UNCHANGED -- CLAUDE.md is explicit
        that they must not be removed or loosened. A branch too sparse
        to have a single COVERED pass must fall back to the exact same
        36-hour wall-clock window mainline would use in the same spot,
        never something looser."""
        self.seed()
        self.import_runs([
            record(test_name="test_branch_only", build="feat/z",
                   result="PASS",
                   start_time="2026-07-26T04:00:00.000000",
                   end_time="2026-07-26T04:00:03.000000"),
        ])
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        stream_id = [s["id"] for s in streams["streams"]
                     if s["name"] == "feat/z"][0]
        scoped = self.call(
            "GET", "/api/summary", query={"stream": [str(stream_id)]})
        expected = format_iso(NOW - datetime.timedelta(hours=36))
        self.assertEqual(scoped["stale_before"], expected)
        # This is the number the branch dashboard's default-tab caption
        # is built from. A single run against a stream whose own
        # inferred test count is 1 clears the 50% coverage bar
        # trivially (find_passes' denominator is THIS stream's own
        # inferred count, not mainline's) -- one tiny, technically
        # "covered" pass, nowhere near the "regular cadence" the
        # caption's own >= 2 threshold is checking for.
        self.assertEqual(scoped["covered_passes"], 1)

    def test_unknown_stream_is_a_404(self) -> None:
        self.seed()
        error = self.call(
            "GET", "/api/summary", query={"stream": ["9999"]}, expect=404)
        self.assertIn("stream", error["error"])


class SummaryStreamProductEnvironmentsTest(ApiCase):
    """A stream scoped with no explicit ``product=`` must still offer
    only ITS OWN product's environments (WP-23 fix, found alongside the
    ``product=`` bug above) — the branch dashboard's own tab must not
    mix every product's environments into its pills either."""

    def _declare(self, environment: str, product: str) -> None:
        self.call(
            "PUT", "/api/environments/{}/product".format(environment),
            body={"product": product, "username": "amy"})

    def test_a_streams_own_environments_only(self) -> None:
        self.import_runs([
            record(environment="linux-sim", test_name="t1"),
            record(environment="win-sim", test_name="t2"),
            record(environment="mac-sim", test_name="t3"),
        ])
        self._declare("linux-sim", "Atlas")
        self._declare("win-sim", "Atlas")
        self._declare("mac-sim", "Borealis")
        # The stream's product is fixed at creation from the FIRST
        # record's environment (linux-sim -> Atlas), per WP-21.
        self.import_runs([record(
            environment="linux-sim", test_name="t1", build="feat/x",
            result="FAIL",
            start_time="2026-07-26T04:00:00.000000",
            end_time="2026-07-26T04:00:03.000000")])
        streams = self.call(
            "GET", "/api/streams", query={"product": ["Atlas"]})
        stream_id = streams["streams"][0]["id"]

        scoped = self.call(
            "GET", "/api/summary", query={"stream": [str(stream_id)]})
        self.assertEqual(
            sorted(scoped["environments"]), ["linux-sim", "win-sim"])
        self.assertNotIn("mac-sim", scoped["environments"])
        self.assertNotIn("mac-sim", scoped["environment_updated"])

    def test_an_explicit_product_still_wins_over_the_streams_own(
        self
    ) -> None:
        """product= is an explicit request; it must not be silently
        overridden by the stream's own declared product."""
        self.import_runs([
            record(environment="linux-sim", test_name="t1"),
            record(environment="mac-sim", test_name="t3"),
        ])
        self._declare("linux-sim", "Atlas")
        self._declare("mac-sim", "Borealis")
        self.import_runs([record(
            environment="linux-sim", test_name="t1", build="feat/x",
            result="FAIL",
            start_time="2026-07-26T04:00:00.000000",
            end_time="2026-07-26T04:00:03.000000")])
        streams = self.call(
            "GET", "/api/streams", query={"product": ["Atlas"]})
        stream_id = streams["streams"][0]["id"]

        data = self.call(
            "GET", "/api/summary",
            query={"stream": [str(stream_id)], "product": ["Borealis"]})
        self.assertEqual(data["environments"], ["mac-sim"])
        # Mainline's own count is untouched -- proof the two are not
        # secretly sharing a query.
        mainline = self.call("GET", "/api/summary")
        self.assertEqual(mainline["status"]["total_tests"], 2)


class TimelineTest(ApiCase):
    """GET /api/timeline: one environment's script running order."""

    def _night(
        self,
        days_ago: int,
        entries: List[Any],
        environment: str = "linux-sim",
    ) -> None:
        """One night's activity: (script, test, minute_offset[, result])."""
        base = (NOW - datetime.timedelta(days=days_ago)).replace(
            hour=2, minute=0, second=0, microsecond=0)
        rows = []
        for entry in entries:
            script, test_name, offset = entry[0], entry[1], entry[2]
            result = entry[3] if len(entry) > 3 else "PASS"
            when = base + datetime.timedelta(minutes=offset)
            rows.append(record(
                environment=environment, script=script,
                test_name=test_name, result=result,
                start_time=format_iso(when),
                end_time=format_iso(
                    when + datetime.timedelta(seconds=30)),
            ))
        self.import_runs(rows)

    def _standard_nights(self) -> None:
        """Three identical nights: a.py (two tests), then b.py (one).

        b.py starts at minute 70 — inside the block's FINAL hour
        bucket, so it also proves the window edge is hour-inclusive
        (trimmed against the exact bucket start, its row would vanish).
        """
        for day in (3, 2, 1):
            self._night(day, [
                ("a.py", "t0", 0),
                ("a.py", "t1", 5, "FAIL"),
                ("b.py", "t2", 70),
            ])

    def test_defaults_to_the_newest_block_in_running_order(self) -> None:
        self._standard_nights()
        data = self.call(
            "GET", "/api/timeline", query={"environment": ["linux-sim"]})
        self.assertEqual(len(data["blocks"]), 3)
        newest = data["blocks"][0]
        self.assertEqual(data["window"],
                         {"from": newest["started"],
                          "to": newest["ended"]})
        self.assertEqual(
            [(row["script"], row["total"]) for row in data["rows"]],
            [("a.py", 2), ("b.py", 1)])
        first = data["rows"][0]
        self.assertEqual(first["failed"], 1)
        self.assertEqual(first["results"]["FAIL"], 1)
        self.assertEqual(first["known_tests"], 2)
        self.assertEqual(data["rows"][1]["known_tests"], 1)
        self.assertEqual(data["gap_minutes"], 60)

    def test_a_rerun_after_a_gap_is_its_own_row(self) -> None:
        """A script that ran twice appears twice, in its real places."""
        self._night(1, [
            ("a.py", "t0", 0),
            ("b.py", "t1", 10),
            ("a.py", "t0", 150),
        ])
        data = self.call(
            "GET", "/api/timeline", query={"environment": ["linux-sim"]})
        self.assertEqual(
            [row["script"] for row in data["rows"]],
            ["a.py", "b.py", "a.py"])

    def test_a_partial_run_reads_short_against_known_tests(self) -> None:
        """The row says 1 of 2 rather than pretending 1 is everything."""
        self._night(3, [("a.py", "t0", 0), ("a.py", "t1", 1)])
        self._night(2, [("a.py", "t0", 0), ("a.py", "t1", 1)])
        self._night(1, [("a.py", "t0", 0)])
        data = self.call(
            "GET", "/api/timeline", query={"environment": ["linux-sim"]})
        (row,) = data["rows"]
        self.assertEqual(row["total"], 1)
        self.assertEqual(row["known_tests"], 2)

    def test_an_explicit_window_selects_an_older_block(self) -> None:
        """The picker echoes block edges back; they must round-trip."""
        self._standard_nights()
        data = self.call(
            "GET", "/api/timeline", query={"environment": ["linux-sim"]})
        previous = data["blocks"][1]
        chosen = self.call(
            "GET", "/api/timeline",
            query={
                "environment": ["linux-sim"],
                "from": [previous["started"]],
                "to": [previous["ended"]],
            })
        self.assertEqual(
            chosen["window"],
            {"from": previous["started"], "to": previous["ended"]})
        self.assertEqual(
            [(row["script"], row["total"]) for row in chosen["rows"]],
            [("a.py", 2), ("b.py", 1)])

    def test_no_activity_means_no_window_and_no_rows(self) -> None:
        """A long-quiet environment renders empty, not as an error."""
        old = (NOW - datetime.timedelta(days=60)).replace(
            hour=2, minute=0, second=0, microsecond=0)
        self.import_runs([record(
            environment="quiet-sim", script="a.py", test_name="t0",
            start_time=format_iso(old),
            end_time=format_iso(old + datetime.timedelta(seconds=1)))])
        data = self.call(
            "GET", "/api/timeline", query={"environment": ["quiet-sim"]})
        self.assertEqual(data["blocks"], [])
        self.assertIsNone(data["window"])
        self.assertEqual(data["rows"], [])

    def test_environment_is_required(self) -> None:
        error = self.call("GET", "/api/timeline", expect=400)
        self.assertIn("environment", error["error"])

    def test_an_unknown_environment_is_404(self) -> None:
        error = self.call(
            "GET", "/api/timeline", query={"environment": ["nowhere"]},
            expect=404)
        self.assertIn("nowhere", error["error"])

    def test_half_a_window_is_400(self) -> None:
        self._standard_nights()
        error = self.call(
            "GET", "/api/timeline",
            query={"environment": ["linux-sim"],
                   "from": ["2026-07-25T02:00:00.000000"]},
            expect=400)
        self.assertIn("from/to", error["error"])

    def test_a_backwards_window_is_400(self) -> None:
        self._standard_nights()
        error = self.call(
            "GET", "/api/timeline",
            query={"environment": ["linux-sim"],
                   "from": ["2026-07-25T02:00:00.000000"],
                   "to": ["2026-07-24T02:00:00.000000"]},
            expect=400)
        self.assertIn("window ends before", error["error"])

    def test_a_bad_timestamp_is_400(self) -> None:
        self._standard_nights()
        error = self.call(
            "GET", "/api/timeline",
            query={"environment": ["linux-sim"],
                   "from": ["yesterday"], "to": ["today"]},
            expect=400)
        self.assertIn("ISO-8601", error["error"])

    def test_days_reaches_retention_and_no_further(self) -> None:
        """The picker's "Earlier runs" promise: a year, exactly.

        365 is what lets "view any recorded run's night" be a true
        sentence, and the ceiling is what keeps the block list a read
        of activity_hours somebody asked for rather than an unbounded
        one.
        """
        self._standard_nights()
        data = self.call(
            "GET", "/api/timeline",
            query={"environment": ["linux-sim"], "days": ["365"]})
        self.assertEqual(data["days"], 365)
        error = self.call(
            "GET", "/api/timeline",
            query={"environment": ["linux-sim"], "days": ["366"]},
            expect=400)
        self.assertIn("365", error["error"])

    def test_wrong_method_is_405(self) -> None:
        self.assert_405("POST", "/api/timeline", "GET")


class TimelineStreamScopingTest(ApiCase):
    """WP-23 (docs/STREAMS_PLAN.md §5.2): ``/api/timeline`` accepts
    ``stream=`` (default mainline) so a long-running stream's running
    order reads from its OWN ``script_hours`` partition."""

    def _night(
        self, days_ago: int, environment: str = "linux-sim",
        build: Optional[str] = None,
    ) -> None:
        base = (NOW - datetime.timedelta(days=days_ago)).replace(
            hour=2, minute=0, second=0, microsecond=0)
        rec = record(
            environment=environment, script="a.py", test_name="t0",
            start_time=format_iso(base),
            end_time=format_iso(base + datetime.timedelta(seconds=30)),
        )
        if build:
            rec["build"] = build
        self.import_runs([rec])

    def test_a_build_import_leaves_the_mainline_timeline_unaffected(
            self) -> None:
        self._night(1)
        before = self.call(
            "GET", "/api/timeline", query={"environment": ["linux-sim"]})
        self._night(1, build="feat/x")
        after = self.call(
            "GET", "/api/timeline", query={"environment": ["linux-sim"]})
        self.assertEqual(before, after)

    def test_stream_param_reads_the_streams_own_running_order(self) -> None:
        self._night(1, build="feat/x")
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        stream_id = streams["streams"][0]["id"]
        data = self.call(
            "GET", "/api/timeline",
            query={"environment": ["linux-sim"],
                   "stream": [str(stream_id)]})
        self.assertEqual(data["stream"], stream_id)
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["script"], "a.py")
        # And the UNSCOPED (mainline) request sees no blocks at all --
        # proof the branch's activity never reached mainline's own
        # activity_hours/script_hours partition.
        mainline = self.call(
            "GET", "/api/timeline", query={"environment": ["linux-sim"]})
        self.assertEqual(mainline["blocks"], [])
        self.assertEqual(mainline["rows"], [])

    def test_stream_param_echoes_the_streams_identity(self) -> None:
        """F7 (docs/STREAMS_PLAN.md §5.2 "as built"): the Timeline page
        needs a stream's kind/name to render the branch band, the same
        field test detail already echoes as "stream_identity"."""
        self._night(1, build="feat/x")
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        stream_id = streams["streams"][0]["id"]
        data = self.call(
            "GET", "/api/timeline",
            query={"environment": ["linux-sim"],
                   "stream": [str(stream_id)]})
        self.assertEqual(data["stream_identity"]["id"], stream_id)
        self.assertEqual(data["stream_identity"]["kind"], "build")
        self.assertEqual(data["stream_identity"]["name"], "feat/x")

    def test_mainline_has_no_stream_identity(self) -> None:
        self._night(1)
        data = self.call(
            "GET", "/api/timeline", query={"environment": ["linux-sim"]})
        self.assertIsNone(data["stream_identity"])


class TimelineStreamEnvironmentHintTest(ApiCase):
    """WP-25 (docs/ONE_KIND_PLAN.md §2b.1, user-reported 2026-08-09): a
    build that ran on one environment showed a bare empty page on every
    OTHER environment — verified live: 2026.9.1 = 0 rows on
    atlas-lab-alpha, 69 on atlas-lab-bravo. ``stream_environments``
    names where the stream's data actually is."""

    def _night(
        self, days_ago: int, environment: str, build: Optional[str] = None,
    ) -> None:
        base = (NOW - datetime.timedelta(days=days_ago)).replace(
            hour=2, minute=0, second=0, microsecond=0)
        rec = record(
            environment=environment, script="a.py", test_name="t0",
            start_time=format_iso(base),
            end_time=format_iso(base + datetime.timedelta(seconds=30)),
        )
        if build:
            rec["build"] = build
        self.import_runs([rec])

    def setUp(self) -> None:
        super().setUp()
        # atlas-lab-alpha is a KNOWN environment (mainline only) so the
        # scoped request below 404s for the right reason (empty) rather
        # than the wrong one (unknown environment).
        self._night(1, "atlas-lab-alpha")
        self._night(1, "atlas-lab-bravo", build="2026.9.1")
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        self.stream_id = streams["streams"][0]["id"]

    def test_empty_on_a_different_environment_names_the_real_one(
            self) -> None:
        data = self.call(
            "GET", "/api/timeline",
            query={"environment": ["atlas-lab-alpha"],
                   "stream": [str(self.stream_id)]})
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["stream_environments"], ["atlas-lab-bravo"])

    def test_present_and_populated_costs_nothing_extra(self) -> None:
        data = self.call(
            "GET", "/api/timeline",
            query={"environment": ["atlas-lab-bravo"],
                   "stream": [str(self.stream_id)]})
        self.assertNotEqual(data["rows"], [])
        self.assertIsNone(data["stream_environments"])

    def test_mainline_never_carries_the_field(self) -> None:
        data = self.call(
            "GET", "/api/timeline", query={"environment": ["atlas-lab-alpha"]})
        self.assertIsNone(data["stream_environments"])


class ScriptWindowRunsTest(ApiCase):
    """GET /api/scripts/{env}/{script}/runs: the Timeline row expansion."""

    PATH = "/api/scripts/linux-sim/suite%2Falpha.py/runs"

    def _seed(self) -> None:
        base = datetime.datetime(2026, 7, 25, 2, 0, 0)
        self.import_runs([
            record(test_name="test_b", result="FAIL",
                   start_time=format_iso(
                       base + datetime.timedelta(minutes=5)),
                   end_time=format_iso(
                       base + datetime.timedelta(minutes=5, seconds=2))),
            record(test_name="test_a",
                   start_time=format_iso(base),
                   end_time=format_iso(
                       base + datetime.timedelta(seconds=3))),
            record(test_name="test_a",
                   start_time=format_iso(
                       base + datetime.timedelta(hours=5)),
                   end_time=format_iso(
                       base + datetime.timedelta(hours=5, seconds=3))),
        ])

    def test_returns_the_window_oldest_first(self) -> None:
        self._seed()
        data = self.call(
            "GET", self.PATH,
            query={"from": ["2026-07-25T02:00:00.000000"],
                   "to": ["2026-07-25T03:00:00.000000"]})
        self.assertEqual(
            [(run["test_name"], run["result"]) for run in data["runs"]],
            [("test_a", "PASS"), ("test_b", "FAIL")])
        first = data["runs"][0]
        self.assertEqual(first["start_time"], "2026-07-25T02:00:00.000000")
        self.assertEqual(first["duration_seconds"], 3.0)
        self.assertIn("run_id", first)
        self.assertFalse(data["truncated"])

    def test_an_unknown_script_is_404(self) -> None:
        self._seed()
        error = self.call(
            "GET", "/api/scripts/linux-sim/nope.py/runs",
            query={"from": ["2026-07-25T02:00:00.000000"],
                   "to": ["2026-07-25T03:00:00.000000"]},
            expect=404)
        self.assertIn("nope.py", error["error"])

    def test_the_window_is_required(self) -> None:
        self._seed()
        error = self.call("GET", self.PATH, expect=400)
        self.assertIn("from", error["error"])

    def test_a_backwards_window_is_400(self) -> None:
        self._seed()
        error = self.call(
            "GET", self.PATH,
            query={"from": ["2026-07-25T03:00:00.000000"],
                   "to": ["2026-07-25T02:00:00.000000"]},
            expect=400)
        self.assertIn("window ends before", error["error"])

    def test_wrong_method_is_405(self) -> None:
        self._seed()
        self.assert_405("PUT", self.PATH, "GET")

    def test_stream_param_reads_the_branch_own_runs(self) -> None:
        """F7 (docs/STREAMS_PLAN.md §5.2 "as built"): this row-expansion
        read had fallen behind /api/timeline's own stream-scoping —
        always mainline, regardless of which stream's block a caller
        expanded. Two DIFFERENT tests in the SAME window, one on
        mainline and one on a branch, prove `stream=` actually selects
        between them rather than merging or ignoring it."""
        base = datetime.datetime(2026, 7, 25, 2, 0, 0)
        self.import_runs([
            record(test_name="mainline_only",
                   start_time=format_iso(base),
                   end_time=format_iso(base + datetime.timedelta(seconds=3))),
        ])
        self.import_runs([
            record(test_name="branch_only", build="feat/x",
                   start_time=format_iso(base),
                   end_time=format_iso(base + datetime.timedelta(seconds=3))),
        ])
        streams = self.call("GET", "/api/streams", query={"product": [""]})
        stream_id = streams["streams"][0]["id"]

        mainline = self.call(
            "GET", self.PATH,
            query={"from": ["2026-07-25T02:00:00.000000"],
                   "to": ["2026-07-25T03:00:00.000000"]})
        self.assertEqual(mainline["stream"], 1)
        self.assertEqual(
            [run["test_name"] for run in mainline["runs"]],
            ["mainline_only"])

        branch = self.call(
            "GET", self.PATH,
            query={"from": ["2026-07-25T02:00:00.000000"],
                   "to": ["2026-07-25T03:00:00.000000"],
                   "stream": [str(stream_id)]})
        self.assertEqual(branch["stream"], stream_id)
        self.assertEqual(
            [run["test_name"] for run in branch["runs"]],
            ["branch_only"])


class TestImportStreamsContract(ApiCase):
    """WP-21 (docs/STREAMS_PLAN.md §3.3): build on /api/import. Narrowed
    to build-only by WP-25 (docs/ONE_KIND_PLAN.md §1.2): the `branch`
    kind died before it ever shipped, so a record carrying `branch` is
    now a loud per-record rejection, not a second stream kind — see
    TestImportBranchFieldRejected below for that half."""

    def _declare(self, environment: str, product: str) -> None:
        self.call(
            "PUT", "/api/environments/{}/product".format(environment),
            body={"product": product, "username": "amy"})

    def test_mainline_batch_has_an_empty_streams_seen(self) -> None:
        data = self.import_runs([record(test_name="t1")])
        self.assertEqual(data["streams_seen"], [])

    def test_a_build_record_is_named_in_streams_seen(self) -> None:
        data = self.import_runs(
            [record(test_name="t1", build="2026.9.1")])
        self.assertEqual(data["streams_seen"], ["build:2026.9.1"])

    def test_streams_seen_is_sorted_and_deduplicated(self) -> None:
        data = self.import_runs([
            record(test_name="t1", build="feat/y",
                   start_time="2026-07-25T02:00:00.000000",
                   end_time="2026-07-25T02:00:03.000000"),
            record(test_name="t2", build="feat/x",
                   start_time="2026-07-25T02:01:00.000000",
                   end_time="2026-07-25T02:01:03.000000"),
            record(test_name="t3", build="feat/x",
                   start_time="2026-07-25T02:02:00.000000",
                   end_time="2026-07-25T02:02:03.000000"),
        ])
        self.assertEqual(
            data["streams_seen"], ["build:feat/x", "build:feat/y"])

    def test_blank_build_is_mainline(self) -> None:
        data = self.import_runs(
            [record(test_name="t1", build="   ")])
        self.assertEqual(data["streams_seen"], [])
        self.assertEqual(data["inserted"], 1)

    def test_a_legacy_key_collision_is_rejected_and_names_both_streams(
            self) -> None:
        self.import_runs([record(test_name="t1")])
        data = self.import_runs(
            [record(test_name="t1", build="feat/x")])
        self.assertEqual(data["inserted"], 0)
        self.assertEqual(data["rejected"], 1)
        error = data["errors"][0]
        self.assertIn("mainline", error["error"])
        self.assertIn("build:feat/x", error["error"])
        self.assertEqual(error["test_name"], "t1")

    def test_the_rest_of_a_mixed_batch_still_imports(self) -> None:
        self.import_runs([record(test_name="t1")])
        data = self.import_runs([
            record(test_name="t1", build="feat/x"),  # collides
            record(test_name="t2", build="feat/x",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),  # fine
        ])
        self.assertEqual(data["inserted"], 1)
        self.assertEqual(data["rejected"], 1)


class TestImportBranchFieldRejected(ApiCase):
    """WP-25 (docs/ONE_KIND_PLAN.md §1.2): the `branch` kind died before
    it ever shipped anywhere -- a record carrying `branch` is REJECTED
    loudly, per-record, through the REAL import path (not just
    :func:`testboard.model.parse_run_record` in isolation), because
    unknown-key tolerance would otherwise file a stale script's runs
    into mainline silently the moment `branch`'s handling was removed --
    the same "old-server trap" class of danger §3.3 documents."""

    def test_branch_is_rejected_with_the_documented_message(self) -> None:
        data = self.import_runs(
            [record(test_name="t1", branch="feat/x")])
        self.assertEqual(data["inserted"], 0)
        self.assertEqual(data["rejected"], 1)
        self.assertEqual(
            data["errors"][0]["error"],
            "branch: removed before this contract ever shipped — use "
            "build:")
        self.assertEqual(data["streams_seen"], [])

    def test_rejected_by_presence_not_value(self) -> None:
        """A blank/null branch is still a `branch` key on the wire --
        rejected the same as any other value, never quietly read as
        mainline the way a blank `build` is."""
        data = self.import_runs(
            [record(test_name="t1", branch=None)])
        self.assertEqual(data["rejected"], 1)
        self.assertIn("branch:", data["errors"][0]["error"])

    def test_branch_and_build_together_is_still_rejected(self) -> None:
        """Both present used to be its own "mutually exclusive" error;
        now `branch`'s own rejection fires first regardless, since it is
        checked before `build` is even read."""
        data = self.import_runs([
            {**record(test_name="t1"), "branch": "x", "build": "y"},
        ])
        self.assertEqual(data["rejected"], 1)
        self.assertIn("branch:", data["errors"][0]["error"])
        self.assertEqual(data["streams_seen"], [])

    def test_the_rest_of_a_mixed_batch_still_imports(self) -> None:
        data = self.import_runs([
            record(test_name="t1", branch="feat/x"),   # rejected
            record(test_name="t2", build="feat/x"),    # fine
        ])
        self.assertEqual(data["inserted"], 1)
        self.assertEqual(data["rejected"], 1)
        self.assertEqual(data["streams_seen"], ["build:feat/x"])


class TestStreamsEndpoint(ApiCase):
    """GET /api/streams?product= — the Build picker's data."""

    def _declare(self, environment: str, product: str) -> None:
        self.call(
            "PUT", "/api/environments/{}/product".format(environment),
            body={"product": product, "username": "amy"})

    def test_empty_when_no_build_reported(self) -> None:
        self.import_runs([record()])
        data = self.call("GET", "/api/streams", query={"product": [""]})
        self.assertEqual(data["streams"], [])

    def test_mainline_is_never_listed(self) -> None:
        self.import_runs([record(build="feat/x")])
        data = self.call("GET", "/api/streams", query={"product": [""]})
        names = [(s["kind"], s["name"]) for s in data["streams"]]
        self.assertNotIn(("mainline", ""), names)
        self.assertIn(("build", "feat/x"), names)

    def test_streams_carry_id_timestamps_and_failing_count(self) -> None:
        self.import_runs([
            record(test_name="t1", result="FAIL", build="feat/x"),
            record(test_name="t2", result="PASS", build="feat/x",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
        ])
        [stream] = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.assertEqual(stream["kind"], "build")
        self.assertEqual(stream["name"], "feat/x")
        self.assertEqual(stream["failing"], 1)
        self.assertIn("first_seen", stream)
        self.assertIn("last_seen", stream)
        self.assertIsInstance(stream["id"], int)

    def test_scoped_to_the_declared_product(self) -> None:
        self.import_runs([record(environment="linux-sim")])
        self._declare("linux-sim", "Atlas")
        self.import_runs([record(
            environment="linux-sim", test_name="t2", build="feat/x",
            start_time="2026-07-25T03:00:00.000000",
            end_time="2026-07-25T03:00:03.000000")])
        self.assertEqual(
            len(self.call(
                "GET", "/api/streams", query={"product": ["Atlas"]}
            )["streams"]), 1)
        self.assertEqual(
            self.call(
                "GET", "/api/streams", query={"product": [""]}
            )["streams"], [])

    def test_an_unknown_product_is_empty_not_404(self) -> None:
        data = self.call(
            "GET", "/api/streams", query={"product": ["Nope"]})
        self.assertEqual(data["streams"], [])

    def test_missing_product_defaults_to_the_implicit_one(self) -> None:
        self.import_runs([record(build="feat/x")])
        without = self.call("GET", "/api/streams")["streams"]
        withempty = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.assertEqual(without, withempty)


class TestCompareEndpoint(ApiCase):
    """GET /api/compare — the six counts plus one paginated category."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([
            record(test_name="test_a", result="PASS"),
            record(test_name="test_b", result="FAIL"),
        ])
        self.import_runs([
            record(test_name="test_a", result="FAIL", build="feat/x",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
            record(test_name="test_c", result="PASS", build="feat/x",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
        ])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]

    def test_missing_stream_is_400(self) -> None:
        error = self.call("GET", "/api/compare", expect=400)
        self.assertIn("stream", error["error"])

    def test_unknown_stream_is_404(self) -> None:
        self.call(
            "GET", "/api/compare", query={"stream": ["999999"]},
            expect=404)

    def test_same_product_non_mainline_baseline_is_allowed(self) -> None:
        """WP-22 (docs/STREAMS_PLAN.md §4.1) lifts the WP-21-era
        restriction: baseline= now accepts any stream of the SAME
        product as stream=, not only mainline. Comparing a stream
        against itself is a degenerate case (every test agrees with
        itself) rather than an error -- the point of this test is that
        it is no longer REFUSED."""
        data = self.call(
            "GET", "/api/compare",
            query={"stream": [str(self.stream_id)],
                   "baseline": [str(self.stream_id)]})
        self.assertEqual(data["baseline"]["id"], self.stream_id)
        self.assertEqual(data["counts"]["new_failures"], 0)
        self.assertEqual(data["counts"]["new_tests"], 0)
        self.assertEqual(data["counts"]["no_result"], 0)

    def _make_other_product_stream(self) -> int:
        """A second stream, in a DIFFERENT product, product fixed at
        stream-creation time (declared before the BRANCH import,
        matching docs/STREAMS_PLAN.md §3.3). The environment has to
        exist (a mainline import) before /api/environments/{env}/product
        will accept a declaration for it -- same rule as every other
        expectation-style endpoint."""
        self.import_runs([
            record(environment="other-env", test_name="test_z",
                   result="PASS"),
        ])
        self.call(
            "PUT", "/api/environments/other-env/product",
            body={"product": "Borealis", "username": "amy"})
        self.import_runs([
            record(environment="other-env", test_name="test_z",
                   result="PASS", build="feat/y",
                   start_time="2026-07-25T04:00:00.000000",
                   end_time="2026-07-25T04:00:03.000000"),
        ])
        streams = self.call(
            "GET", "/api/streams", query={"product": ["Borealis"]}
        )["streams"]
        return int(streams[0]["id"])

    def test_cross_product_baseline_is_refused(self) -> None:
        """A baseline from a DIFFERENT product is refused with a clear
        400 naming both products -- the environments filter is
        resolved from stream='s own product alone, so a mismatched
        baseline would otherwise silently compare against the wrong
        environments instead of erroring."""
        other_stream_id = self._make_other_product_stream()
        error = self.call(
            "GET", "/api/compare",
            query={"stream": [str(self.stream_id)],
                   "baseline": [str(other_stream_id)]},
            expect=400)
        self.assertIn("Borealis", error["error"])

    def test_cross_product_refusal_is_symmetric(self) -> None:
        """The refusal fires however stream=/baseline= are assigned to
        the two streams, not only in one direction."""
        other_stream_id = self._make_other_product_stream()
        error = self.call(
            "GET", "/api/compare",
            query={"stream": [str(other_stream_id)],
                   "baseline": [str(self.stream_id)]},
            expect=400)
        self.assertIn("Borealis", error["error"])

    def test_mainline_baseline_never_triggers_the_product_check(
        self
    ) -> None:
        """Mainline is the one universal exception -- comparing any
        stream against mainline must keep working regardless of the
        stream's own product, exactly as it did before this drop."""
        other_stream_id = self._make_other_product_stream()
        data = self.call(
            "GET", "/api/compare", query={"stream": [str(other_stream_id)]})
        self.assertEqual(data["baseline"]["kind"], "mainline")

    def test_mainline_used_explicitly_as_stream_is_checked_too(self) -> None:
        """The product check is not one-sided: `stream=<mainline id>`
        paired with a REAL other product's baseline must be refused just
        like the reverse, not silently allowed through to compare
        against the wrong environments (mainline's own resolved
        environments are the IMPLICIT '' product's, which has nothing to
        do with the baseline's real product). No shipped frontend
        constructs this (getSelectedStreamId() is null for mainline,
        never the numeric id), but the endpoint is a documented contract
        regardless."""
        other_stream_id = self._make_other_product_stream()
        mainline_id = self.call(
            "GET", "/api/compare", query={"stream": [str(self.stream_id)]}
        )["baseline"]["id"]
        error = self.call(
            "GET", "/api/compare",
            query={"stream": [str(mainline_id)],
                   "baseline": [str(other_stream_id)]},
            expect=400)
        self.assertIn("Borealis", error["error"])

    def test_the_six_counts_and_both_sides_identity(self) -> None:
        data = self.call(
            "GET", "/api/compare", query={"stream": [str(self.stream_id)]})
        self.assertEqual(
            data["counts"],
            {
                "new_failures": 1,   # test_a
                "new_passes": 0,
                "both_failing": 0,
                "new_tests": 1,      # test_c
                "no_result": 1,      # test_b
                "agree": 0,          # no pair matches, not-FAIL, both sides
            },
        )
        self.assertEqual(data["stream"]["id"], self.stream_id)
        self.assertEqual(data["baseline"]["kind"], "mainline")
        self.assertIn("last_seen", data["stream"])
        self.assertIn("last_seen", data["baseline"])
        self.assertEqual(data["tests"], [])
        self.assertEqual(data["category"], None)

    def test_a_category_returns_a_paginated_list(self) -> None:
        data = self.call(
            "GET", "/api/compare",
            query={"stream": [str(self.stream_id)],
                   "category": ["new_failures"]})
        self.assertEqual(data["total"], 1)
        [row] = data["tests"]
        self.assertEqual(row["test_name"], "test_a")
        self.assertEqual(row["stream_result"], "FAIL")
        self.assertEqual(row["baseline_result"], "PASS")
        # WP-21: what the delta view's Review expander/assignee select
        # need — the branch's own run id, and the (unpartitioned)
        # current assignee.
        self.assertIsNotNone(row["stream_run_id"])
        self.assertIsNone(row["assignee"])

    def test_no_result_row_carries_no_run_to_review(self) -> None:
        data = self.call(
            "GET", "/api/compare",
            query={"stream": [str(self.stream_id)],
                   "category": ["no_result"]})
        [row] = data["tests"]
        self.assertIsNone(row["stream_run_id"])

    def test_the_row_shows_the_unpartitioned_current_assignee(self) -> None:
        self.call(
            "PUT",
            test_path("linux-sim", "suite/alpha.py", "test_a", "/assignee"),
            body={"username": "alice", "assigned_by": "bob"})
        data = self.call(
            "GET", "/api/compare",
            query={"stream": [str(self.stream_id)],
                   "category": ["new_failures"]})
        [row] = data["tests"]
        self.assertEqual(row["assignee"], "alice")

    def test_an_unknown_category_is_400(self) -> None:
        error = self.call(
            "GET", "/api/compare",
            query={"stream": [str(self.stream_id)],
                   "category": ["not_a_thing"]},
            expect=400)
        self.assertIn("category", error["error"])

    def test_a_category_request_runs_the_pairs_sql_twice_not_thrice(
        self
    ) -> None:
        """WP-23 perf pass: a category request used to run the expensive
        pairs SQL (_compare_pairs_sql) three times -- compare_counts,
        compare_category's page, and compare_category_count recomputing
        a total compare_counts already had. ``total`` is now
        getattr(counts, category) -- see _handle_compare's comment.
        "stream_result" is a column alias unique to the pairs SQL (every
        query built from it selects it, directly or through the
        categorized/counted wrapper), so counting its occurrences across
        the traced statements counts pairs-SQL executions specifically,
        not every query the request happens to run."""
        seen = []  # type: List[str]
        conn = self.storage._conn()
        _trace_sql_into(conn, seen)
        try:
            self.call(
                "GET", "/api/compare",
                query={"stream": [str(self.stream_id)],
                       "category": ["new_failures"]})
        finally:
            conn.set_trace_callback(None)
        pairs_runs = [s for s in seen if "stream_result" in s]
        self.assertEqual(
            len(pairs_runs), 2,
            "expected exactly 2 pairs-SQL executions (counts + page), "
            "got {0}:\n{1}".format(len(pairs_runs), "\n---\n".join(
                pairs_runs)),
        )

    def test_the_category_total_still_matches_the_headline_count(
        self
    ) -> None:
        """The value itself, not just the query count: total must be
        EXACTLY counts[category] for every category, the same agreement
        compare_category_count used to prove by independently
        recomputing it -- tests/test_storage.py's own tests are the
        oracle that compare_counts and compare_category_count still
        agree; this is the API-level half of that same guarantee."""
        counts = self.call(
            "GET", "/api/compare",
            query={"stream": [str(self.stream_id)]})["counts"]
        for category in COMPARE_CATEGORIES:
            data = self.call(
                "GET", "/api/compare",
                query={"stream": [str(self.stream_id)],
                       "category": [category]})
            self.assertEqual(
                data["total"], counts[category],
                "category {!r} total disagrees with the headline "
                "count".format(category),
            )


class TestDashboardStreamParam(ApiCase):
    """``stream=`` on /api/dashboard (WP-21, default mainline)."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([record(test_name="test_a")])
        self.import_runs([
            record(test_name="test_b", build="feat/x",
                   start_time="2026-07-25T03:00:00.000000",
                   end_time="2026-07-25T03:00:03.000000"),
        ])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]

    def test_default_is_mainline(self) -> None:
        data = self.call("GET", "/api/dashboard")
        self.assertEqual([t["test_name"] for t in data["tests"]],
                          ["test_a"])
        self.assertEqual(data["stream"], 1)

    def test_scoped_to_a_branch(self) -> None:
        data = self.call(
            "GET", "/api/dashboard",
            query={"stream": [str(self.stream_id)]})
        self.assertEqual([t["test_name"] for t in data["tests"]],
                          ["test_b"])
        self.assertEqual(data["stream"], self.stream_id)

    def test_unknown_stream_is_404(self) -> None:
        self.call(
            "GET", "/api/dashboard", query={"stream": ["999999"]},
            expect=404)

    def test_non_integer_stream_is_400(self) -> None:
        self.call(
            "GET", "/api/dashboard", query={"stream": ["nope"]},
            expect=400)


class TestDetailAndHistoryStreamParam(ApiCase):
    """``stream=`` on test detail and history (WP-21, default mainline)."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([record(
            environment="linux-sim", script="suite/alpha.py",
            test_name="shared_name", result="PASS")])
        self.import_runs([record(
            environment="linux-sim", script="suite/alpha.py",
            test_name="shared_name", result="FAIL", build="feat/x",
            start_time="2026-07-25T03:00:00.000000",
            end_time="2026-07-25T03:00:03.000000")])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]
        self.path = test_path(
            "linux-sim", "suite/alpha.py", "shared_name")

    def test_detail_default_is_mainline(self) -> None:
        data = self.call("GET", self.path)
        self.assertEqual(data["latest"]["result"], "PASS")
        self.assertEqual(data["stream"], 1)
        self.assertIsNone(data["stream_identity"])

    def test_detail_scoped_to_a_branch(self) -> None:
        data = self.call(
            "GET", self.path, query={"stream": [str(self.stream_id)]})
        self.assertEqual(data["latest"]["result"], "FAIL")
        self.assertEqual(data["stream"], self.stream_id)
        self.assertEqual(data["stream_identity"]["kind"], "build")
        self.assertEqual(data["stream_identity"]["name"], "feat/x")

    def test_history_default_is_mainline(self) -> None:
        data = self.call("GET", self.path + "/history")
        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(data["runs"][0]["result"], "PASS")

    def test_history_scoped_to_a_branch(self) -> None:
        data = self.call(
            "GET", self.path + "/history",
            query={"stream": [str(self.stream_id)]})
        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(data["runs"][0]["result"], "FAIL")


class TestCommentStreamId(ApiCase):
    """``stream_id`` on comment POST/GET (WP-21, "posted from")."""

    def setUp(self) -> None:
        super().setUp()
        self.import_runs([record(test_name="test_a")])
        self.import_runs([record(
            test_name="test_a", build="feat/x", result="FAIL",
            start_time="2026-07-25T03:00:00.000000",
            end_time="2026-07-25T03:00:03.000000")])
        streams = self.call(
            "GET", "/api/streams", query={"product": [""]})["streams"]
        self.stream_id = streams[0]["id"]
        self.path = test_path("linux-sim", "suite/alpha.py", "test_a")

    def test_defaults_to_null(self) -> None:
        data = self.call(
            "POST", self.path + "/comments",
            body={"username": "amy", "text": "hi"}, expect=201)
        self.assertIsNone(data["comment"]["stream_id"])

    def test_round_trips_when_given(self) -> None:
        self.call(
            "POST", self.path + "/comments",
            body={"username": "amy", "text": "from ci",
                  "stream_id": self.stream_id},
            expect=201)
        [comment] = self.call(
            "GET", self.path + "/comments")["comments"]
        self.assertEqual(comment["stream_id"], self.stream_id)

    def test_the_list_resolves_referenced_streams(self) -> None:
        """The "posted from" tag needs a name, not just an id — the list
        endpoint batch-resolves every distinct stream_id on the thread,
        mainline included, in the SAME response (no per-comment fetch)."""
        self.call(
            "POST", self.path + "/comments",
            body={"username": "amy", "text": "from mainline",
                  "stream_id": MAINLINE_STREAM_ID}, expect=201)
        self.call(
            "POST", self.path + "/comments",
            body={"username": "amy", "text": "from ci",
                  "stream_id": self.stream_id}, expect=201)
        self.call(
            "POST", self.path + "/comments",
            body={"username": "amy", "text": "no context at all"},
            expect=201)
        data = self.call("GET", self.path + "/comments")
        streams = data["streams"]
        self.assertEqual(
            sorted(streams), sorted([str(MAINLINE_STREAM_ID),
                                      str(self.stream_id)]))
        self.assertEqual(streams[str(MAINLINE_STREAM_ID)]["kind"], "mainline")
        self.assertEqual(streams[str(self.stream_id)]["kind"], "build")
        self.assertEqual(streams[str(self.stream_id)]["name"], "feat/x")

    def test_unknown_stream_id_is_404(self) -> None:
        self.call(
            "POST", self.path + "/comments",
            body={"username": "amy", "text": "hi", "stream_id": 999999},
            expect=404)

    def test_non_integer_stream_id_is_400(self) -> None:
        self.call(
            "POST", self.path + "/comments",
            body={"username": "amy", "text": "hi", "stream_id": "nope"},
            expect=400)

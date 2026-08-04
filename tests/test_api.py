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
import unittest
import urllib.parse
from typing import Any, Dict, List, Optional, Union

from testboard import api
from testboard.model import format_iso
from testboard.storage import DASHBOARD_SORTS, QUEUE_KINDS, Storage

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

    def setUp(self) -> None:
        self.storage = Storage(":memory:")
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
                "rejected": 0, "errors": [],
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
                "rejected": 0, "errors": [],
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
        self.assertEqual(data, {"comments": []})

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
            set(first.keys()), {"id", "author", "created_at", "text"}
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

    def test_wrong_method_is_405(self) -> None:
        self.assert_405("POST", "/api/timeline", "GET")


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

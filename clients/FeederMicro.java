/*
 * Sends one test run's results to a testboard dashboard. Single file,
 * JDK only (no external jars), invoked once per suite execution from
 * your test framework's cleanup step.
 *
 *   java FeederMicro --url http://dashboard-host:8000 \
 *       --environment NAME [--build NAME] [--dry-run] [this site's flags]
 *
 * --url and --environment are required. --build files the run under a
 * release/RC stream instead of mainline (pass your framework's branch
 * parameter here - there is no separate --branch). The engine stamps
 * environment/build onto every record; IMPLEMENT.readRecords() never
 * sets them.
 *
 * Exit codes:
 *   0  every batch was accepted (per-record rejections are logged, not
 *      fatal), or --dry-run finished cleanly
 *   1  a batch was not accepted (server unreachable or kept failing).
 *      Nothing is saved locally - re-invoking this feeder re-sends
 *      everything; the server skips records it already has.
 *   2  the invocation itself was wrong (bad args, or readRecords()
 *      threw) - nothing was sent.
 *
 * This is the Java sibling of clients/feeder_micro.py in the testboard
 * repository - same contract, same wire schema, same exit codes. See
 * docs/FEEDER_TEMPLATE.md there for the full schema and worked
 * examples (Python/Tcl shape; the fields translate directly).
 *
 * Edit only inside the IMPLEMENT THIS block below. Everything after it
 * is engine machinery - to take a newer release, replace that part
 * wholesale.
 */

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.util.*;

public class FeederMicro {

    // ====================================================================
    // IMPLEMENT THIS - the only part of this file you write.
    // ====================================================================

    /**
     * Produce one record per test run for THIS invocation. Each record
     * is a LinkedHashMap with String keys: script, test_name, result,
     * start_time, end_time, output (all required), and optionally
     * source_link / known_failure_reason (known_failure_reason may map
     * to null). Do NOT put "environment" or "build" - the engine
     * stamps those from the command line, overwriting anything set
     * here.
     *
     * {@code siteArgs} is whatever command-line tokens were not one of
     * the engine's own flags (--environment/--url/--build/--dry-run/
     * --http-timeout/--verbose/--help), in original order - parse your
     * own flags out of it exactly as you would out of a normal argv.
     *
     * Never let one bad input record abort the whole run: skip it and
     * log a warning (System.err), then keep going. If the results
     * source itself cannot be opened, log the problem and return an
     * empty list - that becomes an ordinary "nothing to send"
     * invocation. Only let this method throw when the invocation truly
     * cannot proceed; a throw here is fatal (exit 2, nothing sent).
     */
    static List<Map<String, String>> readRecords(List<String> siteArgs) throws Exception {
        log("WARN", "readRecords() has not been implemented for this site yet; nothing to read");
        return Collections.emptyList();
    }

    // ====================================================================
    // DO NOT EDIT BELOW THIS LINE - engine machinery.
    // ====================================================================

    static final String ENGINE_VERSION = "1.0.0";
    static final String CONTRACT_VERSION = "1";

    static double httpTimeoutSeconds = 15.0;
    static final int MAX_ATTEMPTS = 3;
    static final double RETRY_PAUSE_SECONDS = 2.0;
    static final int BATCH_SIZE = 500;
    static final int MAX_BATCH_BYTES = 8 * 1024 * 1024;
    static final int RECORD_OVERHEAD_BYTES = 400;
    static final int MAX_LOGGED_REJECTIONS = 5;
    static final int MAX_ERROR_TEXT_CHARS = 200;

    static final Set<String> RESULT_VALUES = new HashSet<>(
        Arrays.asList("PASS", "FAIL", "FAILED_AS_EXPECTED", "UNEXPECTED_PASS"));
    static final String[] REQUIRED_STRING_FIELDS =
        {"environment", "script", "test_name", "start_time", "end_time"};

    static boolean verbose = false;

    static void log(String level, String fmt, Object... args) {
        String ts = DateTimeFormatter.ISO_INSTANT.format(Instant.now());
        System.err.println(ts + " " + level + " testboard_feeder: " + String.format(fmt, args));
    }

    static class UsageError extends Exception {
        UsageError(String message) { super(message); }
    }

    // -- argument parsing (no library: engine flags only; the rest is handed to readRecords) --

    static class Args {
        String environment;
        String url;
        String build;
        boolean dryRun;
        List<String> siteArgs = new ArrayList<>();
    }

    static Args parseArgs(String[] argv) throws UsageError {
        Args a = new Args();
        String environment = null, url = null, build = null;
        for (int i = 0; i < argv.length; i++) {
            String tok = argv[i];
            switch (tok) {
                case "--environment": environment = need(argv, ++i, tok); break;
                case "--url": url = need(argv, ++i, tok); break;
                case "--build": build = need(argv, ++i, tok); break;
                case "--http-timeout":
                    try { httpTimeoutSeconds = Double.parseDouble(need(argv, ++i, tok)); }
                    catch (NumberFormatException e) { throw new UsageError("--http-timeout must be a number"); }
                    break;
                case "--dry-run": a.dryRun = true; break;
                case "--verbose": verbose = true; break;
                case "--help": case "-h":
                    System.out.println("usage: java FeederMicro --url URL --environment NAME "
                        + "[--build NAME] [--dry-run] [--http-timeout SECONDS] [--verbose] [site flags]");
                    System.exit(0);
                default: a.siteArgs.add(tok);
            }
        }
        if (environment == null || environment.trim().isEmpty())
            throw new UsageError("--environment is required and must not be blank");
        if (url == null || url.trim().isEmpty())
            throw new UsageError("--url is required and must not be blank");
        if (build != null && build.trim().isEmpty())
            throw new UsageError("--build must not be blank");
        a.environment = environment.trim();
        a.url = normalizeUrl(url.trim());
        a.build = build == null ? null : build.trim();
        return a;
    }

    static String need(String[] argv, int i, String flag) throws UsageError {
        if (i >= argv.length) throw new UsageError(flag + " requires a value");
        return argv[i];
    }

    static String normalizeUrl(String url) {
        String trimmed = url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
        return trimmed.endsWith("/api/import") ? trimmed : trimmed + "/api/import";
    }

    // -- record screening --

    static Map<String, String> stamped(Map<String, String> raw, Args a) {
        LinkedHashMap<String, String> record = new LinkedHashMap<>(raw);
        record.put("environment", a.environment);
        if (a.build != null) record.put("build", a.build);
        return record;
    }

    static String invalidReason(Map<String, String> record) {
        for (String field : REQUIRED_STRING_FIELDS) {
            String v = record.get(field);
            if (v == null || v.trim().isEmpty())
                return field + ": required and must be a non-empty string";
        }
        if (!RESULT_VALUES.contains(record.get("result")))
            return "result: unknown value " + record.get("result") + " (expected one of " + RESULT_VALUES + ")";
        if (!record.containsKey("output") || record.get("output") == null)
            return "output: required and must be a string";
        return null;
    }

    // -- JSON: just enough to write records and read the response --

    static String jsonEscape(String s) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.toString();
    }

    static String recordToJson(Map<String, String> record) {
        StringBuilder b = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, String> e : record.entrySet()) {
            if (!first) b.append(",");
            first = false;
            b.append("\"").append(jsonEscape(e.getKey())).append("\":");
            b.append(e.getValue() == null ? "null" : "\"" + jsonEscape(e.getValue()) + "\"");
        }
        return b.append("}").toString();
    }

    static byte[] batchBody(List<Map<String, String>> batch) {
        StringBuilder b = new StringBuilder("{\"runs\":[");
        for (int i = 0; i < batch.size(); i++) {
            if (i > 0) b.append(",");
            b.append(recordToJson(batch.get(i)));
        }
        b.append("]}");
        return b.toString().getBytes(StandardCharsets.UTF_8);
    }

    /** Minimal recursive-descent JSON reader - only what the response needs. */
    static class MiniJson {
        final String s; int i = 0;
        MiniJson(String s) { this.s = s; }
        static Object parse(String s) { MiniJson p = new MiniJson(s); p.ws(); return p.value(); }
        void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }
        Object value() {
            ws();
            char c = s.charAt(i);
            if (c == '{') return object();
            if (c == '[') return array();
            if (c == '"') return string();
            if (s.startsWith("true", i)) { i += 4; return Boolean.TRUE; }
            if (s.startsWith("false", i)) { i += 5; return Boolean.FALSE; }
            if (s.startsWith("null", i)) { i += 4; return null; }
            return number();
        }
        Map<String, Object> object() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++; ws();
            if (s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws(); String key = string(); ws(); i++; // ':'
                m.put(key, value()); ws();
                if (s.charAt(i) == ',') { i++; continue; }
                i++; break; // '}'
            }
            return m;
        }
        List<Object> array() {
            List<Object> l = new ArrayList<>();
            i++; ws();
            if (s.charAt(i) == ']') { i++; return l; }
            while (true) {
                l.add(value()); ws();
                if (s.charAt(i) == ',') { i++; continue; }
                i++; break; // ']'
            }
            return l;
        }
        String string() {
            i++; StringBuilder b = new StringBuilder();
            while (s.charAt(i) != '"') {
                char c = s.charAt(i);
                if (c == '\\') {
                    i++; char esc = s.charAt(i);
                    switch (esc) {
                        case 'n': b.append('\n'); break;
                        case 'r': b.append('\r'); break;
                        case 't': b.append('\t'); break;
                        case 'u': b.append((char) Integer.parseInt(s.substring(i + 1, i + 5), 16)); i += 4; break;
                        default: b.append(esc);
                    }
                } else b.append(c);
                i++;
            }
            i++;
            return b.toString();
        }
        Double number() {
            int start = i;
            while (i < s.length() && "-+.eE0123456789".indexOf(s.charAt(i)) >= 0) i++;
            return Double.parseDouble(s.substring(start, i));
        }
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> decodeResponse(String body, String label) {
        try {
            Object payload = MiniJson.parse(body);
            if (payload instanceof Map) return (Map<String, Object>) payload;
        } catch (Exception ignored) { /* fall through to warning below */ }
        log("WARN", "%s: server returned 200 but the response body was not a usable JSON object", label);
        return new LinkedHashMap<>();
    }

    static int intCount(Map<String, Object> payload, String key) {
        Object v = payload.get(key);
        return v instanceof Number ? ((Number) v).intValue() : 0;
    }

    // -- HTTP --

    static class HttpResult { int status; String body; }

    static HttpResult post(String url, byte[] body) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod("POST");
        conn.setDoOutput(true);
        conn.setConnectTimeout((int) (httpTimeoutSeconds * 1000));
        conn.setReadTimeout((int) (httpTimeoutSeconds * 1000));
        conn.setRequestProperty("Content-Type", "application/json");
        conn.setRequestProperty("User-Agent",
            "testboard-feeder-java-micro/" + ENGINE_VERSION + " (contract " + CONTRACT_VERSION + ")");
        conn.getOutputStream().write(body);
        conn.getOutputStream().close();

        HttpResult r = new HttpResult();
        r.status = conn.getResponseCode();
        InputStream in = r.status >= 400 ? conn.getErrorStream() : conn.getInputStream();
        r.body = in == null ? "" : readAll(in);
        return r;
    }

    static String readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) >= 0) out.write(buf, 0, n);
        return new String(out.toByteArray(), StandardCharsets.UTF_8);
    }

    /** One batch, riding out brief trouble; null once out of attempts. */
    static Map<String, Object> sendBatch(String url, byte[] body, String label) {
        for (int attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            try {
                HttpResult r = post(url, body);
                if (r.status == 200) return decodeResponse(r.body, label);
                boolean retryable = r.status >= 500;
                String text = r.body.length() > MAX_ERROR_TEXT_CHARS
                    ? r.body.substring(0, MAX_ERROR_TEXT_CHARS) + "...[truncated]" : r.body;
                String failure = "HTTP " + r.status + " from the server (response: " + text + ")";
                if (attempt >= MAX_ATTEMPTS || !retryable) {
                    log("ERROR", "%s: giving up (%s)", label, failure);
                    return null;
                }
                warnRetry(label, attempt, failure);
            } catch (IOException e) {
                String failure = describeConnectionError(url, e);
                if (attempt >= MAX_ATTEMPTS) {
                    log("ERROR", "%s: giving up (%s)", label, failure);
                    return null;
                }
                warnRetry(label, attempt, failure);
            }
        }
        return null;
    }

    static void warnRetry(String label, int attempt, String failure) {
        log("WARN", "%s: attempt %d of %d failed: %s - retrying in %.0fs",
            label, attempt, MAX_ATTEMPTS, failure, RETRY_PAUSE_SECONDS);
        try { Thread.sleep((long) (RETRY_PAUSE_SECONDS * 1000)); } catch (InterruptedException ignored) {}
    }

    static String describeConnectionError(String url, IOException e) {
        if (e instanceof SocketTimeoutException) return "request to " + url + " timed out";
        if (e instanceof UnknownHostException) return "DNS lookup failed for the host in " + url;
        return "cannot reach " + url + " (" + e.getClass().getSimpleName() + ": " + e.getMessage() + ")";
    }

    @SuppressWarnings("unchecked")
    static int[] reportPayload(Map<String, Object> payload, String label) {
        int inserted = intCount(payload, "inserted");
        int updated = intCount(payload, "updated");
        int rejected = intCount(payload, "rejected");

        Object errors = payload.get("errors");
        if (errors instanceof List) {
            List<Object> list = (List<Object>) errors;
            for (int i = 0; i < Math.min(list.size(), MAX_LOGGED_REJECTIONS); i++) {
                if (list.get(i) instanceof Map) {
                    Map<String, Object> err = (Map<String, Object>) list.get(i);
                    log("WARN", "%s: server rejected record index %s: %s",
                        label, err.get("index"), err.get("error"));
                }
            }
            int unlogged = list.size() - MAX_LOGGED_REJECTIONS;
            if (unlogged > 0)
                log("WARN", "%s: %d more rejected record(s) not shown individually", label, unlogged);
        }
        log("INFO", "%s: inserted=%d updated=%d rejected=%d", label, inserted, updated, rejected);
        return new int[]{inserted, updated, rejected};
    }

    static List<List<Map<String, String>>> batches(List<Map<String, String>> records) {
        List<List<Map<String, String>>> out = new ArrayList<>();
        List<Map<String, String>> batch = new ArrayList<>();
        int batchBytes = 0;
        for (Map<String, String> record : records) {
            batch.add(record);
            String output = record.get("output");
            batchBytes += (output == null ? 0 : output.length()) + RECORD_OVERHEAD_BYTES;
            if (batch.size() >= BATCH_SIZE || batchBytes >= MAX_BATCH_BYTES) {
                out.add(batch);
                batch = new ArrayList<>();
                batchBytes = 0;
            }
        }
        if (!batch.isEmpty()) out.add(batch);
        return out;
    }

    // -- one invocation, start to finish --

    public static void main(String[] argv) {
        Args a;
        try {
            a = parseArgs(argv);
        } catch (UsageError e) {
            log("ERROR", "%s", e.getMessage());
            System.exit(2); return;
        }

        int read = 0, skipped = 0;
        List<Map<String, String>> valid = new ArrayList<>();
        try {
            List<Map<String, String>> raw = readRecords(a.siteArgs);
            for (Map<String, String> r : raw) {
                read++;
                Map<String, String> record = stamped(r, a);
                String problem = invalidReason(record);
                if (problem != null) {
                    skipped++;
                    log("WARN", "skipping record %d: %s", read, problem);
                } else {
                    valid.add(record);
                }
            }
        } catch (Exception e) {
            if (verbose) e.printStackTrace();
            log("ERROR", "readRecords() crashed after producing %d record(s): %s: %s",
                read, e.getClass().getSimpleName(), e.getMessage());
            System.exit(2); return;
        }

        if (a.dryRun) {
            for (int i = 0; i < Math.min(3, valid.size()); i++) {
                System.out.println("\n--- record " + (i + 1) + " would be sent as ---");
                System.out.println(recordToJson(valid.get(i)));
            }
            log("INFO", "dry run: read=%d valid=%d skipped=%d - nothing was sent", read, valid.size(), skipped);
            System.exit(0); return;
        }

        int sent = 0, inserted = 0, updated = 0, rejected = 0, failedBatches = 0;
        for (List<Map<String, String>> batch : batches(valid)) {
            String label = "batch of " + batch.size() + " records";
            Map<String, Object> payload = sendBatch(a.url, batchBody(batch), label);
            if (payload == null) {
                log("ERROR", "%s was not accepted; nothing is saved locally - re-invoke this feeder "
                    + "to re-send it (safe: the server skips anything it already has)", label);
                failedBatches++;
            } else {
                int[] counts = reportPayload(payload, label);
                sent += batch.size();
                inserted += counts[0]; updated += counts[1]; rejected += counts[2];
            }
        }

        log("INFO", "feeder summary: read=%d valid=%d skipped=%d sent=%d inserted=%d updated=%d "
            + "rejected=%d failed_batches=%d", read, valid.size(), skipped, sent, inserted, updated,
            rejected, failedBatches);
        System.exit(failedBatches > 0 ? 1 : 0);
    }
}

#!/usr/bin/env tclsh
# testboard single-file feeder (Tcl).
#
# Copy this ONE file into your product's own repository. It is the
# whole client: nothing else from the testboard checkout is needed,
# and nothing beyond a bare tclsh is used - no tcllib, no tls, no
# ::json package. "package require http" is the ONE package this file
# needs, and it ships bundled with every stock Tcl install.
#
# Structure of this file
# -----------------------
# 1. IMPLEMENT THIS (below, between the two banners): a
#    ::DASHBOARD_URL variable, an ::EXTRA_FLAGS list (the site-specific
#    command-line flags read_records needs) and a read_records proc.
#    This is the only part you write. See docs/FEEDER_TEMPLATE.md in
#    the testboard repository for the full contract, two worked
#    examples and an acceptance checklist.
# 2. DO NOT EDIT BELOW THIS LINE: the engine - argument parsing, wire
#    validation, hand-built JSON encode/decode, batching, retries,
#    replay files, exit codes. Upgrading the engine later is: paste a
#    newer copy of everything from the banner onward over what you
#    have. Your IMPLEMENT THIS section is untouched by that, and
#    nothing about the wire contract requires you to re-paste at all -
#    old engines are tolerated forever.
#
# Invocation model
# -----------------
# This feeder is PUSHED, not polled: your test framework invokes it
# once, in its own CLEANUP phase, right after one suite execution
# finishes, passing --environment (required) and optionally --build
# (the one non-mainline stream kind - pass your framework's
# branch/release name here; there is no separate --branch, and a
# record carrying that key is rejected) plus whatever read_records
# needs to find this run's results (--results PATH is the worked
# convention below). There is no daily mode, no high-water mark, no
# scanning window: read_records is handed only the arguments for THIS
# invocation and produces records for THIS suite execution alone.
# Re-invoking is always safe - the server upserts on (environment,
# script, test_name, start_time) and skips a byte-identical re-send -
# so a framework that retries its cleanup step on failure costs
# nothing extra.
#
# --environment and --build are stamped onto every record by the
# engine (after read_records hands it to the "emit" callback); your
# reader does not set either field itself, and any value it does set
# there is overridden.
#
# Exit codes (a contract your framework's cleanup step can rely on):
#   0  every valid record was accepted (or --dry-run validated cleanly)
#   1  the server was unreachable/refused a batch, or an old server did
#      not acknowledge --build: the run's results are SAFE (written to
#      a replay file next to this script) and the NEXT invocation
#      resends them first, before its own batch. Treat this as
#      "deferred", not as a broken suite.
#   2  usage or validation error (bad arguments, unreadable results, no
#      dashboard URL configured, or read_records crashed outright):
#      NOTHING was sent. Fix the invocation or the reader.
#
# Transport: this feeder POSTs directly to the dashboard's backend
# port, plain HTTP. It never goes through nginx and never uses a URL
# prefix - see the testboard README's "Feeding in your own results"
# section.
#
# Time bound: every HTTP call carries a --http-timeout (default 15s)
# socket timeout. A --time-budget (default 100s) wall-clock deadline
# is checked before every attempt - resending a queued replay file, or
# sending this run's own batch - and once it is crossed, everything
# still pending is deferred to (or left in) a replay file without
# being attempted further. One attempt already in flight when the
# deadline is crossed is allowed to run to its own timeout, so the
# whole process finishes in at most (--time-budget + --http-timeout)
# seconds - about 115s with the defaults, comfortably inside the ~2
# minute ceiling a cleanup step needs.
#
# Header, not a wire field: this engine's version and the
# wire-contract version it speaks are sent as the User-Agent header on
# every import, never as a JSON field - the dashboard's contract makes
# an unrecognised field a loud per-record rejection, so a header is
# the only change-nothing way to say "here is which engine sent this".
# No site is ever required to update this file for the dashboard to
# keep accepting it.
#
# Vanilla Tcl 8.5: this file is written to 8.5 semantics deliberately
# (avoids try/finally, lmap, string cat, ::json, chan pipe, dict
# getwithdefault, dict map, throw, file tempfile, zlib, binary
# encode/decode, TclOO and coroutines - all 8.6+ only). A static guard
# test (tests/test_tcl_compat.py in the testboard repository) enforces
# that list on every push. This particular file could only be
# EXECUTED against tclsh 8.6 while it was written (that is the only
# interpreter available on the machine that wrote it) - it has never
# actually been run under a real 8.5 interpreter. Treat that as
# "written to spec, execution-verified on 8.6 only" until a site with
# real 8.5 proves otherwise.

if {[catch {package require Tcl 8.5}]} {
    puts stderr "this feeder requires Tcl 8.5+; you are running [info patchlevel]"
    exit 2
}

# ============================================================================
# IMPLEMENT THIS SECTION - the only part of this file you write.
# See docs/FEEDER_TEMPLATE.md (testboard repo) for the full contract,
# two worked examples, and the acceptance checklist.
# ============================================================================

# Your dashboard's backend host:port - the DIRECT port, never an nginx
# front door and never a URL prefix (feeders always speak bare paths).
# Override at invocation time with --url, mainly useful for testing
# against a scratch server without editing this constant.
set ::DASHBOARD_URL "http://localhost:8000"; # CHANGE ME

# Site-specific command-line flags read_records needs to find this
# run's results. Every flag listed here is REPEATABLE and collects a
# list of string values, reachable in read_records as
#   [tb::DGetList [dict get $opts site_args] --results]
# The worked convention is a single --results PATH; add more (e.g.
# --log-dir) if your source needs them.
set ::EXTRA_FLAGS {--results}

# Yield one raw record per test run for THIS invocation by calling
#   $emit $recordDict
# once per record (Tcl has no generators/yield without 8.6
# coroutines, so a callback takes their place - this is the one place
# that shape differs from the Python engine's read_records).
#
# $recordDict is a plain Tcl dict in the /api/import RunRecord schema:
# result, start_time, end_time, output, and optionally source_link /
# known_failure_reason. Do NOT set "environment" or "build" - the
# engine stamps --environment (and --build, if given) onto every
# record after this proc hands it to $emit, overriding anything set
# here.
#
# Must never raise an error because ONE record is bad: log a warning
# (tb::Log WARNING ...) and skip it - the engine validates each record
# independently anyway, so over-reporting (emitting something
# malformed) is fine and expected. What must never happen is this proc
# itself raising an uncaught error; if your source cannot be opened at
# all, log the problem and return without emitting anything.
#
# The shipped default below is the "results-file reader" worked
# example from docs/FEEDER_TEMPLATE.md: JSON-lines from --results,
# passed straight through with no site-specific mapping at all. A
# second worked example (scraping a plain-text test log) is in the
# template; replace this proc with whichever shape - or neither - fits
# your test system.
proc read_records {opts emit} {
    set siteArgs [dict get $opts site_args]
    set paths [tb::DGetList $siteArgs --results]
    if {[llength $paths] == 0} {
        tb::Log WARNING "no --results given and read_records was not customized; nothing to read"
        return
    }
    foreach path $paths {
        if {[catch {set fh [open $path r]} err]} {
            tb::Log WARNING "cannot open $path ($err); skipping it"
            continue
        }
        fconfigure $fh -encoding utf-8
        set lineNumber 0
        while {[gets $fh line] >= 0} {
            incr lineNumber
            set trimmed [string trim $line]
            if {$trimmed eq ""} {
                continue
            }
            if {[catch {set tagged [tb::json::parse $trimmed]} jerr]} {
                tb::Log WARNING "$path:$lineNumber: skipping malformed JSON line ($jerr)"
                continue
            }
            if {[lindex $tagged 0] ne "object"} {
                tb::Log WARNING "$path:$lineNumber: skipping non-object JSON line"
                continue
            }
            $emit [tb::json::to_raw $tagged]
        }
        close $fh
    }
}

# ============================================================================
# DO NOT EDIT BELOW THIS LINE - engine machinery.
# To pick up a new engine version, replace everything from here to the
# end of the file with the new release. Your IMPLEMENT THIS section
# above is untouched by that.
# ============================================================================

namespace eval tb {
    # This engine's own version and the /api/import wire-contract
    # version it was written against. Sent as the User-Agent header,
    # never as a JSON field - see the file header.
    variable ENGINE_VERSION "1.0.0"
    variable CONTRACT_VERSION "1"

    # Per-HTTP-call socket timeout, in seconds. Overridable with
    # --http-timeout (mainly for tests that want a fast black-hole
    # check).
    variable HTTP_TIMEOUT_SECONDS 15.0

    # Total attempts per batch/replay-file (the first try plus retries).
    variable MAX_ATTEMPTS 3

    # Exponential backoff base between attempts: 2s then 4s. With
    # HTTP_TIMEOUT_SECONDS=15 that is a worst case of 3*15 + (2+4) =
    # 51s for one batch/replay-file that never answers.
    variable BACKOFF_BASE_SECONDS 2.0

    # Wall-clock budget for the WHOLE invocation (draining replay
    # files plus sending this run's own batches), in seconds. Checked
    # before every attempt, not just once per unit, so the actual
    # worst case is this plus at most one HTTP_TIMEOUT_SECONDS (the
    # attempt already in flight when the deadline is crossed is
    # allowed to finish) - about 115s with the defaults. Overridable
    # with --time-budget.
    variable TIME_BUDGET_SECONDS 100.0

    # Records per POST batch, same default as the deployed feeder.
    variable BATCH_SIZE 500

    # Flush a batch early once its encoded size reaches this many
    # bytes, even short of BATCH_SIZE records - captured test output
    # varies by orders of magnitude, and there is no operator present
    # to react to a 413. A constant, not a flag: a site invoking this
    # once per suite execution does not need to tune it.
    variable MAX_BATCH_BYTES [expr {8 * 1024 * 1024}]

    # Assumed per-record overhead (identity fields, two timestamps)
    # used only to decide when to flush a batch.
    variable RECORD_OVERHEAD_BYTES 400

    variable REPLAY_PREFIX "testboard_feeder_replay_"
    variable REPLAY_SUFFIX ".json"
    variable CLAIM_SUFFIX ".sending"

    variable RESULT_VALUES {PASS FAIL FAILED_AS_EXPECTED UNEXPECTED_PASS}

    variable VERBOSE 0
    variable SelfTestFailures 0
}

# ----------------------------------------------------------------------
# Hand-built JSON: encode (for the request body) and a small
# recursive-descent parser (for reading --results lines and the
# server's response). No ::json package - tcllib is not assumed
# present, and this project never depends on anything beyond a bare
# tclsh (docs/FEEDER_TEMPLATE.md; the same rule the Python engine's
# header states for pip).
# ----------------------------------------------------------------------

namespace eval tb::json {
    # A JSON null is represented internally by this sentinel string
    # (distinct from Tcl's empty string, which is what an ordinary
    # empty JSON string "" decodes to) so callers can tell "absent /
    # explicitly null" apart from "present and empty".
    variable NULL "@@json-null@@"

    variable Text ""
    variable Pos 0
    variable Len 0
}

# -- encode ---------------------------------------------------------

proc tb::json::Escape {s} {
    set map [list \\ \\\\ \" \\\" \n \\n \r \\r \t \\t]
    set s [string map $map $s]
    set out ""
    set n [string length $s]
    for {set i 0} {$i < $n} {incr i} {
        set ch [string index $s $i]
        scan $ch %c code
        if {$code < 0x20} {
            append out [format {\u%04x} $code]
        } else {
            append out $ch
        }
    }
    return $out
}

proc tb::json::JString {s} {
    return "\"[tb::json::Escape $s]\""
}

proc tb::json::JNull {} {
    return "null"
}

# pairs: a flat key/value list where every VALUE is already valid JSON
# text (built with JString/JNull/JObject/JArray) - keeps escaping
# localized to one place per field, and matches the "flat records"
# shape the wire contract uses.
proc tb::json::JObject {pairs} {
    set parts [list]
    foreach {k v} $pairs {
        lappend parts "[tb::json::JString $k]:$v"
    }
    return "{[join $parts ,]}"
}

# items: a list of already-valid JSON text fragments.
proc tb::json::JArray {items} {
    return "\[[join $items ,]\]"
}

# -- decode: recursive-descent parser --------------------------------
#
# Every JSON value parses to a TAGGED 2-element list {type value} so
# "object" and "array" are never ambiguous with an ordinary Tcl list
# (Tcl has no distinct dict type at the value-representation level -
# a dict is just a list with an even element count, so without a tag
# a one-item array like {5} and a one-pair object shaped the same way
# would be indistinguishable). type is one of object/array/string/
# number/true/false/null; value is a dict of tagged values (object), a
# list of tagged values (array), or the raw text (everything else).
# tb::json::to_raw flattens a tagged value into plain Tcl dicts/lists
# with plain strings, using the NULL sentinel above for JSON null -
# that flattened shape is what the rest of the engine (and
# read_records) actually works with.

proc tb::json::parse {text} {
    variable Text
    variable Pos
    variable Len
    set Text $text
    set Pos 0
    set Len [string length $text]
    SkipWs
    set value [ParseValue]
    SkipWs
    if {$Pos < $Len} {
        error "trailing data after JSON value at position $Pos"
    }
    return $value
}

proc tb::json::SkipWs {} {
    variable Text
    variable Pos
    variable Len
    while {$Pos < $Len} {
        set c [string index $Text $Pos]
        if {$c eq " " || $c eq "\t" || $c eq "\n" || $c eq "\r"} {
            incr Pos
        } else {
            break
        }
    }
}

proc tb::json::Peek {} {
    variable Text
    variable Pos
    variable Len
    if {$Pos >= $Len} {
        error "unexpected end of JSON input"
    }
    return [string index $Text $Pos]
}

proc tb::json::Expect {ch} {
    variable Text
    variable Pos
    variable Len
    if {$Pos >= $Len || [string index $Text $Pos] ne $ch} {
        error "expected '$ch' at position $Pos"
    }
    incr Pos
}

proc tb::json::ParseValue {} {
    variable Pos
    SkipWs
    set c [Peek]
    if {$c eq "\{"} {
        return [list object [ParseObject]]
    } elseif {$c eq "\["} {
        return [list array [ParseArray]]
    } elseif {$c eq "\""} {
        return [list string [ParseString]]
    } elseif {$c eq "t" || $c eq "f"} {
        return [ParseBool]
    } elseif {$c eq "n"} {
        return [ParseNull]
    } elseif {$c eq "-" || [string is digit -strict $c]} {
        return [list number [ParseNumber]]
    }
    error "unexpected character '$c' in JSON at position $Pos"
}

proc tb::json::ParseObject {} {
    Expect "\{"
    SkipWs
    set result [dict create]
    if {[Peek] eq "\}"} {
        Expect "\}"
        return $result
    }
    while {1} {
        SkipWs
        set key [ParseString]
        SkipWs
        Expect ":"
        SkipWs
        set value [ParseValue]
        dict set result $key $value
        SkipWs
        set c [Peek]
        if {$c eq ","} {
            Expect ","
        } elseif {$c eq "\}"} {
            Expect "\}"
            break
        } else {
            variable Pos
            error "expected ',' or '\}' in object at position $Pos"
        }
    }
    return $result
}

proc tb::json::ParseArray {} {
    Expect "\["
    SkipWs
    set result [list]
    if {[Peek] eq "\]"} {
        Expect "\]"
        return $result
    }
    while {1} {
        SkipWs
        set value [ParseValue]
        lappend result $value
        SkipWs
        set c [Peek]
        if {$c eq ","} {
            Expect ","
        } elseif {$c eq "\]"} {
            Expect "\]"
            break
        } else {
            variable Pos
            error "expected ',' or '\]' in array at position $Pos"
        }
    }
    return $result
}

proc tb::json::ParseString {} {
    variable Text
    variable Pos
    variable Len
    Expect "\""
    set out ""
    while {1} {
        if {$Pos >= $Len} {
            error "unterminated string in JSON"
        }
        set c [string index $Text $Pos]
        if {$c eq "\""} {
            incr Pos
            break
        } elseif {$c eq "\\"} {
            incr Pos
            if {$Pos >= $Len} {
                error "unterminated escape in JSON string"
            }
            set esc [string index $Text $Pos]
            if {$esc eq "\""} {
                append out "\""
                incr Pos
            } elseif {$esc eq "\\"} {
                append out "\\"
                incr Pos
            } elseif {$esc eq "/"} {
                append out "/"
                incr Pos
            } elseif {$esc eq "b"} {
                append out "\b"
                incr Pos
            } elseif {$esc eq "f"} {
                append out "\f"
                incr Pos
            } elseif {$esc eq "n"} {
                append out "\n"
                incr Pos
            } elseif {$esc eq "r"} {
                append out "\r"
                incr Pos
            } elseif {$esc eq "t"} {
                append out "\t"
                incr Pos
            } elseif {$esc eq "u"} {
                incr Pos
                if {$Pos + 4 > $Len} {
                    error "truncated \\u escape in JSON string"
                }
                set hex [string range $Text $Pos [expr {$Pos + 3}]]
                if {![string is xdigit -strict $hex]} {
                    error "invalid \\u escape '$hex' in JSON string"
                }
                scan $hex %4x code
                append out [format %c $code]
                incr Pos 4
            } else {
                error "invalid escape '\\$esc' in JSON string"
            }
        } else {
            append out $c
            incr Pos
        }
    }
    return $out
}

proc tb::json::ParseBool {} {
    variable Text
    variable Pos
    variable Len
    if {$Pos + 4 <= $Len && [string range $Text $Pos [expr {$Pos + 3}]] eq "true"} {
        incr Pos 4
        return {true {}}
    }
    if {$Pos + 5 <= $Len && [string range $Text $Pos [expr {$Pos + 4}]] eq "false"} {
        incr Pos 5
        return {false {}}
    }
    error "invalid literal at position $Pos"
}

proc tb::json::ParseNull {} {
    variable Text
    variable Pos
    variable Len
    if {$Pos + 4 <= $Len && [string range $Text $Pos [expr {$Pos + 3}]] eq "null"} {
        incr Pos 4
        return {null {}}
    }
    error "invalid literal at position $Pos"
}

proc tb::json::ParseNumber {} {
    variable Text
    variable Pos
    variable Len
    set start $Pos
    if {[Peek] eq "-"} {
        incr Pos
    }
    while {$Pos < $Len && [string is digit -strict [string index $Text $Pos]]} {
        incr Pos
    }
    if {$Pos < $Len && [string index $Text $Pos] eq "."} {
        incr Pos
        while {$Pos < $Len && [string is digit -strict [string index $Text $Pos]]} {
            incr Pos
        }
    }
    if {$Pos < $Len && ([string index $Text $Pos] eq "e" || [string index $Text $Pos] eq "E")} {
        incr Pos
        if {$Pos < $Len && ([string index $Text $Pos] eq "+" || [string index $Text $Pos] eq "-")} {
            incr Pos
        }
        while {$Pos < $Len && [string is digit -strict [string index $Text $Pos]]} {
            incr Pos
        }
    }
    set numtext [string range $Text $start [expr {$Pos - 1}]]
    if {$numtext eq "" || $numtext eq "-"} {
        error "invalid number at position $start"
    }
    return $numtext
}

# Flatten a tagged parse result into plain Tcl values: object -> dict
# (of flattened values), array -> list (of flattened values), string/
# number -> the raw text, true/false -> 1/0, null -> the NULL
# sentinel.
proc tb::json::to_raw {tagged} {
    variable NULL
    set type [lindex $tagged 0]
    set val [lindex $tagged 1]
    if {$type eq "object"} {
        set out [dict create]
        dict for {k v} $val {
            dict set out $k [to_raw $v]
        }
        return $out
    } elseif {$type eq "array"} {
        set out [list]
        foreach item $val {
            lappend out [to_raw $item]
        }
        return $out
    } elseif {$type eq "string" || $type eq "number"} {
        return $val
    } elseif {$type eq "true"} {
        return 1
    } elseif {$type eq "false"} {
        return 0
    } elseif {$type eq "null"} {
        return $NULL
    }
    error "unknown JSON node type '$type'"
}

# ----------------------------------------------------------------------
# Small utilities shared by validation, transport and reporting.
# ----------------------------------------------------------------------

proc tb::Log {level msg} {
    variable ::tb::VERBOSE
    if {$level eq "DEBUG" && !$::tb::VERBOSE} {
        return
    }
    set ts [clock format [clock seconds] -format "%Y-%m-%d %H:%M:%S" -gmt 0]
    puts stderr "$ts $level testboard_feeder: $msg"
}

proc tb::Truncate {text {limit 200}} {
    if {[string length $text] <= $limit} {
        return $text
    }
    return "[string range $text 0 [expr {$limit - 1}]]...\[truncated\]"
}

# The 8.5-safe equivalent of Tcl 8.6's "dict getwithdefault" - never
# used here (see the compat guard test).
proc tb::DGet {d key default} {
    if {[dict exists $d $key]} {
        set v [dict get $d $key]
        if {$v eq $::tb::json::NULL} {
            return $default
        }
        return $v
    }
    return $default
}

proc tb::DGetList {d key} {
    if {[dict exists $d $key]} {
        return [dict get $d $key]
    }
    return {}
}

proc tb::Sanitize {text} {
    set out [regsub -all {[^A-Za-z0-9_.-]+} $text "-"]
    set out [string trim $out "-"]
    if {$out eq ""} {
        return "unnamed"
    }
    return $out
}

proc tb::IdentityOf {raw} {
    set parts [list]
    foreach field {environment script test_name} {
        set piece "?"
        if {[dict exists $raw $field]} {
            set v [dict get $raw $field]
            if {$v ne $::tb::json::NULL && [string trim $v] ne ""} {
                set piece $v
            }
        }
        lappend parts $piece
    }
    set identity [join $parts " / "]
    if {[dict exists $raw start_time]} {
        set st [dict get $raw start_time]
        if {$st ne $::tb::json::NULL && [string trim $st] ne ""} {
            append identity " @ $st"
        }
    }
    return $identity
}

# ----------------------------------------------------------------------
# Wire-schema validation - a standalone reimplementation of the same
# rules testboard.model.parse_run_record enforces server-side (this
# file cannot import that module: it has to run with nothing but a
# bare tclsh, from inside a completely different repository). Tcl's
# "everything is a string" nature means this cannot distinguish, say,
# a JSON number given where a string was expected the way the Python
# engine's type checks can - a real, documented, minor gap versus that
# engine. Semantic rules (required/non-empty, known result values,
# timestamp shape and ordering, the branch rejection) are enforced
# identically.
# ----------------------------------------------------------------------

proc tb::RequireStr {d field} {
    if {![dict exists $d $field]} {
        error "$field: required field is missing"
    }
    set v [dict get $d $field]
    if {$v eq $::tb::json::NULL} {
        error "$field: must be a string, got null"
    }
    if {[string trim $v] eq ""} {
        error "$field: must not be empty or whitespace-only"
    }
    return $v
}

# Validate one ISO-8601 timestamp field and return it NORMALIZED to a
# fixed 6-digit-fraction string - once every timestamp has that fixed
# width, plain Tcl string comparison is chronological comparison (the
# same "lexical ordering works" property the rest of the project
# relies on for these timestamps).
proc tb::ParseTimestamp {raw field} {
    if {![dict exists $raw $field]} {
        error "$field: required field is missing"
    }
    set value [dict get $raw $field]
    if {$value eq $::tb::json::NULL} {
        error "$field: expected an ISO-8601 timestamp string, got null"
    }
    set ok [regexp {^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$} \
                $value -> y mo d h mi s frac]
    if {!$ok} {
        error "$field: invalid timestamp '$value': expected 'YYYY-MM-DDTHH:MM:SS\[.ffffff\]' (naive UTC, no timezone suffix)"
    }
    set base "$y-$mo-$d $h:$mi:$s"
    if {[catch {clock scan $base -format "%Y-%m-%d %H:%M:%S" -gmt 1} epoch]} {
        error "$field: invalid timestamp '$value': not a real calendar date/time"
    }
    # clock scan silently rolls an out-of-range date/time over (e.g.
    # Feb 30 becomes Mar 2) rather than raising - reformatting and
    # comparing back catches that.
    set reformatted [clock format $epoch -format "%Y-%m-%d %H:%M:%S" -gmt 1]
    if {$reformatted ne $base} {
        error "$field: invalid timestamp '$value': not a real calendar date/time"
    }
    set micro [string range "${frac}000000" 0 5]
    return "$y-$mo-${d}T$h:$mi:$s.$micro"
}

proc tb::validate_record {raw} {
    set environment [tb::RequireStr $raw environment]
    set script [tb::RequireStr $raw script]
    set testName [tb::RequireStr $raw test_name]

    if {![dict exists $raw result]} {
        error "result: required field is missing"
    }
    set result [dict get $raw result]
    if {$result eq $::tb::json::NULL || [lsearch -exact $::tb::RESULT_VALUES $result] < 0} {
        error "result: unknown value '$result' (expected one of [join $::tb::RESULT_VALUES {, }])"
    }

    set startTime [tb::ParseTimestamp $raw start_time]
    set endTime [tb::ParseTimestamp $raw end_time]
    if {[string compare $endTime $startTime] < 0} {
        error "end_time: must be >= start_time ($endTime < $startTime)"
    }

    if {![dict exists $raw output]} {
        error "output: required field is missing"
    }
    set output [dict get $raw output]
    if {$output eq $::tb::json::NULL} {
        error "output: must be a string, got null"
    }

    set sourceLink ""
    if {[dict exists $raw source_link]} {
        set sourceLink [dict get $raw source_link]
        if {$sourceLink eq $::tb::json::NULL} {
            set sourceLink ""
        }
    }

    set kfr ""
    if {[dict exists $raw known_failure_reason]} {
        set rawKfr [dict get $raw known_failure_reason]
        if {$rawKfr ne $::tb::json::NULL && [string trim $rawKfr] ne ""} {
            set kfr $rawKfr
        }
    }

    # Checked by PRESENCE, not value - a null "branch" still carries
    # the key. Before "build" is even read, so the rejection reads the
    # same regardless of what else the record carries.
    if {[dict exists $raw branch]} {
        error "branch: removed before this contract ever shipped — use build:"
    }

    set build ""
    set hasBuild 0
    if {[dict exists $raw build]} {
        set rawBuild [dict get $raw build]
        if {$rawBuild ne $::tb::json::NULL} {
            set trimmedBuild [string trim $rawBuild]
            if {$trimmedBuild ne ""} {
                set build $trimmedBuild
                set hasBuild 1
            }
        }
    }

    set out [dict create \
        environment $environment script $script test_name $testName \
        result $result start_time $startTime end_time $endTime \
        output $output source_link $sourceLink known_failure_reason $kfr]
    if {$hasBuild} {
        dict set out build $build
    }
    return $out
}

proc tb::RecordToJson {rec} {
    set pairs [list]
    foreach key {environment script test_name result start_time end_time output source_link} {
        lappend pairs $key [tb::json::JString [dict get $rec $key]]
    }
    set kfr [dict get $rec known_failure_reason]
    if {$kfr eq ""} {
        lappend pairs known_failure_reason [tb::json::JNull]
    } else {
        lappend pairs known_failure_reason [tb::json::JString $kfr]
    }
    if {[dict exists $rec build]} {
        lappend pairs build [tb::json::JString [dict get $rec build]]
    }
    return [tb::json::JObject $pairs]
}

# ----------------------------------------------------------------------
# HTTP transport - package require http (bundled with every stock Tcl
# install), -timeout on every call.
# ----------------------------------------------------------------------

proc tb::NormalizeUrl {url} {
    set trimmed [string trimright $url "/"]
    if {![string match "*/api/import" $trimmed]} {
        append trimmed "/api/import"
    }
    return $trimmed
}

proc tb::HttpPost {url body timeoutSeconds} {
    package require http 2
    # ::http::config -useragent, not a "User-Agent" entry in -headers:
    # the http package ALWAYS sends its own default User-Agent unless
    # this is overridden here, and passing "User-Agent" via -headers
    # merely ADDS a second one - both were observed on the wire while
    # writing this file (see the commit message). This is the only
    # correct way to get exactly one.
    ::http::config -useragent "testboard-feeder-tcl/$::tb::ENGINE_VERSION (contract $::tb::CONTRACT_VERSION)"
    set token ""
    set timeoutMs [expr {int($timeoutSeconds * 1000.0)}]
    if {$timeoutMs < 1} {
        set timeoutMs 1
    }
    set bytes [encoding convertto utf-8 $body]
    set rc [catch {
        set token [::http::geturl $url -query $bytes \
                       -type "application/json; charset=utf-8" \
                       -timeout $timeoutMs]
    } err]
    if {$rc != 0} {
        catch {::http::cleanup $token}
        return [dict create ok 0 reason "cannot reach $url ($err)"]
    }
    set status [::http::status $token]
    if {$status eq "timeout"} {
        ::http::cleanup $token
        return [dict create ok 0 reason "request to $url timed out"]
    }
    if {$status ne "ok"} {
        set errInfo ""
        catch {set errInfo [::http::error $token]}
        ::http::cleanup $token
        return [dict create ok 0 reason "cannot reach $url (status=$status $errInfo)"]
    }
    set code [::http::ncode $token]
    set respBody [::http::data $token]
    ::http::cleanup $token
    return [dict create ok 1 code $code body $respBody]
}

# Returns dict: ok deferred reason payload streams_seen_present.
# deferred means the time budget ran out before any attempt was even
# made, as distinct from an attempt that was made and failed.
proc tb::SendWithRetry {url body deadline httpTimeout label} {
    set reason "unknown error"
    for {set attempt 1} {$attempt <= $::tb::MAX_ATTEMPTS} {incr attempt} {
        if {[clock milliseconds] >= $deadline} {
            return [dict create ok 0 deferred 1 \
                "reason" "time budget exhausted before attempt $attempt of $::tb::MAX_ATTEMPTS for $label" \
                payload {} streams_seen_present 0]
        }
        if {$attempt > 1} {
            set delay [expr {$::tb::BACKOFF_BASE_SECONDS * pow(2, $attempt - 2)}]
            tb::Log INFO "$label: retrying in [format %.1f $delay]s (attempt $attempt of $::tb::MAX_ATTEMPTS)"
            after [expr {int($delay * 1000)}]
        }
        set result [tb::HttpPost $url $body $httpTimeout]
        if {![dict get $result ok]} {
            set reason [dict get $result reason]
            tb::Log WARNING "$label: attempt $attempt of $::tb::MAX_ATTEMPTS failed: $reason"
            continue
        }
        set code [dict get $result code]
        set respBody [dict get $result body]
        if {$code == 200} {
            set payload {}
            set streamsSeenPresent 0
            if {![catch {set tagged [tb::json::parse $respBody]} jerr]} {
                if {[lindex $tagged 0] eq "object"} {
                    set payload [tb::json::to_raw $tagged]
                    set streamsSeenPresent [dict exists $payload streams_seen]
                }
            } else {
                tb::Log WARNING "$label: server returned 200 but the response body was not valid JSON ($jerr)"
            }
            return [dict create ok 1 deferred 0 reason "" payload $payload \
                streams_seen_present $streamsSeenPresent]
        }
        if {$code >= 500} {
            set reason "server error HTTP $code (response: [tb::Truncate $respBody])"
            tb::Log WARNING "$label: attempt $attempt of $::tb::MAX_ATTEMPTS failed: $reason"
            continue
        }
        set reason "HTTP $code from the server (response: [tb::Truncate $respBody])"
        tb::Log WARNING "$label: $reason - not retrying a client error"
        break
    }
    return [dict create ok 0 deferred 0 reason $reason payload {} streams_seen_present 0]
}

proc tb::ReportBatchPayload {payload label} {
    if {$payload eq {}} {
        tb::Log WARNING "$label: server returned 200 but the response body was not a usable JSON object"
        return {0 0 0}
    }
    set inserted [tb::DGet $payload inserted 0]
    set updated [tb::DGet $payload updated 0]
    set rejected [tb::DGet $payload rejected 0]
    set errors [tb::DGet $payload errors {}]
    set shown 0
    foreach e $errors {
        if {$shown >= 5} {
            break
        }
        set idx [tb::DGet $e index ""]
        set msg [tb::DGet $e error ""]
        tb::Log WARNING "$label: server rejected record index $idx: $msg"
        incr shown
    }
    if {[llength $errors] > 5} {
        tb::Log WARNING "$label: [expr {[llength $errors] - 5}] more rejected record(s) not shown individually"
    }
    tb::Log INFO "$label: inserted=$inserted updated=$updated rejected=$rejected"
    return [list $inserted $updated $rejected]
}

# ----------------------------------------------------------------------
# Replay files - the only persistence this feeder has. Names are
# per-invocation (pid + millisecond timestamp + random suffix) so
# concurrent cleanups from different environments on one host never
# collide, and a fresh name is reserved with {WRONLY CREAT EXCL} so
# two processes racing to allocate one can never both win. Claiming an
# existing replay file for resend uses "file rename" to the SAME fixed
# ".sending" suffix from every process: rename is atomic, so only one
# process's rename can ever see the source still present - the other
# fails with "no such file", which is the lost-the-race case, not an
# error.
# ----------------------------------------------------------------------

proc tb::NewReplayPath {replayDir environment} {
    set safeEnv [tb::Sanitize $environment]
    for {set attempt 0} {$attempt < 50} {incr attempt} {
        set token "[pid]-[clock milliseconds]-[expr {int(rand() * 10000)}]"
        set name "$::tb::REPLAY_PREFIX${safeEnv}_$token$::tb::REPLAY_SUFFIX"
        set path [file join $replayDir $name]
        if {[catch {set fd [open $path {WRONLY CREAT EXCL}]}]} {
            continue
        }
        close $fd
        return $path
    }
    error "could not allocate a unique replay file name in $replayDir"
}

proc tb::WriteReplay {path body} {
    set fh [open $path w]
    fconfigure $fh -encoding utf-8 -translation lf
    puts -nonewline $fh $body
    close $fh
}

proc tb::PendingReplayFiles {replayDir} {
    set pattern [file join $replayDir "$::tb::REPLAY_PREFIX*$::tb::REPLAY_SUFFIX"]
    return [lsort [glob -nocomplain -- $pattern]]
}

# Returns the claimed path, or "" if another invocation already
# claimed or removed the file first (a lost race, not an error).
proc tb::Claim {path} {
    set claimed "$path$::tb::CLAIM_SUFFIX"
    if {[catch {file rename -- $path $claimed}]} {
        return ""
    }
    return $claimed
}

# Give a claimed-but-still-failing replay file back its real name so a
# later invocation will pick it up again. NOTE (a documented, narrow
# limitation shared with the Python engine): a crash of THIS process
# between Claim and ReleaseClaim leaves the file orphaned under its
# ".sending" name - PendingReplayFiles' glob does not match that
# suffix, so it will not be retried automatically. This mirrors an
# ordinary process-killed-mid-flight risk in any at-least-once queue;
# recovering it is a manual "rename it back" on the host.
proc tb::ReleaseClaim {claimed original} {
    if {[catch {file rename -- $claimed $original} err]} {
        tb::Log WARNING "could not restore replay file $original for a later retry ($err); if this persists, check [file dirname $original] by hand"
    }
}

proc tb::BodyExpectsStreamsAck {body} {
    if {[catch {set tagged [tb::json::parse $body]}]} {
        return 0
    }
    if {[lindex $tagged 0] ne "object"} {
        return 0
    }
    set raw [tb::json::to_raw $tagged]
    if {![dict exists $raw runs]} {
        return 0
    }
    foreach r [dict get $raw runs] {
        if {[dict exists $r build]} {
            return 1
        }
    }
    return 0
}

# Resend every pending replay file; return the count still pending
# afterwards (failed again, or never attempted for lack of time).
proc tb::DrainReplayFiles {url replayDir deadline httpTimeout} {
    set stillPending 0
    foreach path [tb::PendingReplayFiles $replayDir] {
        if {[clock milliseconds] >= $deadline} {
            tb::Log WARNING "time budget exhausted; leaving $path (and any later replay files) for the next invocation"
            incr stillPending
            continue
        }
        set claimed [tb::Claim $path]
        if {$claimed eq ""} {
            continue
        }
        if {[catch {
            set fh [open $claimed r]
            fconfigure $fh -encoding utf-8 -translation lf
            set body [read $fh]
            close $fh
        } err]} {
            tb::Log ERROR "could not read replay file $claimed ($err); leaving it for a later retry"
            tb::ReleaseClaim $claimed $path
            incr stillPending
            continue
        }
        set expectAck [tb::BodyExpectsStreamsAck $body]
        set result [tb::SendWithRetry $url $body $deadline $httpTimeout \
            "replay file [file tail $path]"]
        if {[dict get $result ok]} {
            if {$expectAck && ![dict get $result streams_seen_present]} {
                tb::Log ERROR "replay file $path: this batch carries --build records but the server's response has no streams_seen key at all - an old server would have filed it into mainline. The batch WAS accepted (HTTP 200), so it is not resent - check the dashboard by hand."
            } else {
                tb::ReportBatchPayload [dict get $result payload] "replay file $path"
            }
            catch {file delete -- $claimed}
            tb::Log INFO "replay file $path: resent successfully"
        } else {
            tb::Log ERROR "replay file $path: still failing ([dict get $result reason])"
            tb::ReleaseClaim $claimed $path
            incr stillPending
        }
    }
    return $stillPending
}

# ----------------------------------------------------------------------
# Batching and sending this invocation's own records
# ----------------------------------------------------------------------

proc tb::Batches {records batchSize maxBytes} {
    set batches [list]
    set batch [list]
    set batchBytes 0
    foreach rec $records {
        lappend batch $rec
        incr batchBytes [expr {[string length [dict get $rec output]] + $::tb::RECORD_OVERHEAD_BYTES}]
        if {[llength $batch] >= $batchSize || $batchBytes >= $maxBytes} {
            lappend batches $batch
            set batch [list]
            set batchBytes 0
        }
    }
    if {[llength $batch] > 0} {
        lappend batches $batch
    }
    return $batches
}

# Returns {sent inserted updated rejected failedBatches}. Every failed
# batch is saved to a fresh replay file before this returns - nothing
# is ever only in memory.
proc tb::SendOwnRecords {records url environment build replayDir deadline httpTimeout} {
    set sent 0
    set inserted 0
    set updated 0
    set rejected 0
    set failedBatches 0
    foreach batch [tb::Batches $records $::tb::BATCH_SIZE $::tb::MAX_BATCH_BYTES] {
        set jsonRecords [list]
        set expectAck 0
        foreach rec $batch {
            lappend jsonRecords [tb::RecordToJson $rec]
            if {[dict exists $rec build]} {
                set expectAck 1
            }
        }
        set body [tb::json::JObject [list runs [tb::json::JArray $jsonRecords]]]
        if {[clock milliseconds] >= $deadline} {
            set path [tb::NewReplayPath $replayDir $environment]
            tb::WriteReplay $path $body
            tb::Log ERROR "time budget exhausted before this batch of [llength $batch] records could be sent; saved to $path for the next invocation"
            incr failedBatches
            continue
        }
        set result [tb::SendWithRetry $url $body $deadline $httpTimeout \
            "batch of [llength $batch] records"]
        if {[dict get $result ok] && $expectAck && ![dict get $result streams_seen_present]} {
            set path [tb::NewReplayPath $replayDir $environment]
            tb::WriteReplay $path $body
            tb::Log ERROR "this run used --build '$build' but the server's response has no streams_seen key at all - it does not understand builds and would have silently filed these into mainline. The batch WAS accepted server-side; its body is ALSO saved to $path so the mismatch can be investigated. Update the dashboard, or drop --build to import as mainline deliberately."
            incr failedBatches
            continue
        }
        if {[dict get $result ok]} {
            lassign [tb::ReportBatchPayload [dict get $result payload] "this run"] ins upd rej
            incr sent [llength $batch]
            incr inserted $ins
            incr updated $upd
            incr rejected $rej
            continue
        }
        set path [tb::NewReplayPath $replayDir $environment]
        tb::WriteReplay $path $body
        tb::Log ERROR "batch of [llength $batch] records failed ([dict get $result reason]); saved to $path - the NEXT invocation resends it before its own batch"
        incr failedBatches
    }
    return [list $sent $inserted $updated $rejected $failedBatches]
}

# ----------------------------------------------------------------------
# Per-invocation accumulator for read_records' callback shape (Tcl has
# no closures, so the callback shares state via namespace variables
# reset at the start of each Main() call rather than captured ones).
# ----------------------------------------------------------------------

namespace eval tb::acc {
    variable Read 0
    variable Valid 0
    variable Skipped 0
    variable Canonical {}
    variable Reasons {}
    variable Environment ""
    variable Build ""
    variable HasBuild 0
}

proc tb::acc::reset {environment build hasBuild} {
    variable Read
    variable Valid
    variable Skipped
    variable Canonical
    variable Reasons
    variable Environment
    variable Build
    variable HasBuild
    set Read 0
    set Valid 0
    set Skipped 0
    set Canonical {}
    set Reasons [dict create]
    set Environment $environment
    set Build $build
    set HasBuild $hasBuild
}

proc tb::acc::handle {raw} {
    variable Read
    variable Valid
    variable Skipped
    variable Canonical
    variable Reasons
    variable Environment
    variable Build
    variable HasBuild
    incr Read
    dict set raw environment $Environment
    if {$HasBuild} {
        dict set raw build $Build
    }
    if {[catch {set record [tb::validate_record $raw]} errMsg]} {
        incr Skipped
        set count 1
        if {[dict exists $Reasons $errMsg]} {
            set count [expr {[dict get $Reasons $errMsg] + 1}]
        }
        dict set Reasons $errMsg $count
        if {$count <= 5} {
            tb::Log WARNING "skipping invalid record \[[tb::IdentityOf $raw]\] $errMsg"
        } elseif {$count == 6} {
            tb::Log WARNING "\[$errMsg\] has now affected more than 5 records - further occurrences will be counted but not logged individually"
        }
        return
    }
    incr Valid
    lappend Canonical $record
}

# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

proc tb::PrintHelp {} {
    puts "usage: feeder.tcl --environment NAME \[--build NAME\] \[--dry-run\]"
    puts "                  \[--url URL\] \[--replay-dir DIR\]"
    puts "                  \[--http-timeout SECONDS\] \[--time-budget SECONDS\]"
    puts "                  \[--verbose\] \[site-specific flags...\]"
    puts ""
    puts "exit codes"
    puts "  0  every valid record was accepted (or --dry-run validated cleanly)"
    puts "  1  server unreachable/refused, or an old server did not ack --build -"
    puts "     results are saved to a replay file; the next invocation resends them"
    puts "  2  usage/validation error - nothing was sent"
    puts ""
    puts "See docs/FEEDER_TEMPLATE.md in the testboard repository for the wire"
    puts "schema, the invocation contract, and two worked reader examples."
}

proc tb::NeedValue {argv i n flag} {
    if {$i >= $n} {
        tb::Log ERROR "$flag requires a value"
        exit 2
    }
    return [lindex $argv $i]
}

proc tb::ParseArgs {argv} {
    set opts [dict create environment "" build "" dry_run 0 url "" \
        replay_dir "." http_timeout $::tb::HTTP_TIMEOUT_SECONDS \
        time_budget $::tb::TIME_BUDGET_SECONDS verbose 0]
    set siteArgs [dict create]
    foreach flag $::EXTRA_FLAGS {
        dict set siteArgs $flag [list]
    }
    set i 0
    set n [llength $argv]
    while {$i < $n} {
        set arg [lindex $argv $i]
        if {$arg eq "--environment"} {
            incr i
            dict set opts environment [tb::NeedValue $argv $i $n "--environment"]
        } elseif {$arg eq "--build"} {
            incr i
            dict set opts build [tb::NeedValue $argv $i $n "--build"]
        } elseif {$arg eq "--dry-run"} {
            dict set opts dry_run 1
        } elseif {$arg eq "--url"} {
            incr i
            dict set opts url [tb::NeedValue $argv $i $n "--url"]
        } elseif {$arg eq "--replay-dir"} {
            incr i
            dict set opts replay_dir [tb::NeedValue $argv $i $n "--replay-dir"]
        } elseif {$arg eq "--http-timeout"} {
            incr i
            dict set opts http_timeout [tb::NeedValue $argv $i $n "--http-timeout"]
        } elseif {$arg eq "--time-budget"} {
            incr i
            dict set opts time_budget [tb::NeedValue $argv $i $n "--time-budget"]
        } elseif {$arg eq "--verbose"} {
            dict set opts verbose 1
        } elseif {$arg eq "--help" || $arg eq "-h"} {
            tb::PrintHelp
            exit 0
        } elseif {[lsearch -exact $::EXTRA_FLAGS $arg] >= 0} {
            incr i
            dict lappend siteArgs $arg [tb::NeedValue $argv $i $n $arg]
        } else {
            tb::Log ERROR "unknown argument '$arg' (see --help)"
            exit 2
        }
        incr i
    }
    dict set opts site_args $siteArgs
    return $opts
}

proc tb::Main {argv} {
    set opts [tb::ParseArgs $argv]
    set ::tb::VERBOSE [dict get $opts verbose]

    set environment [string trim [dict get $opts environment]]
    if {$environment eq ""} {
        tb::Log ERROR "--environment is required and must not be empty or whitespace-only"
        return 2
    }

    set buildArg [dict get $opts build]
    set build ""
    set hasBuild 0
    if {$buildArg ne ""} {
        set build [string trim $buildArg]
        if {$build eq ""} {
            tb::Log ERROR "--build must not be empty or whitespace-only"
            return 2
        }
        set hasBuild 1
    }

    set rawUrl [string trim [dict get $opts url]]
    if {$rawUrl eq ""} {
        set rawUrl [string trim $::DASHBOARD_URL]
    }
    if {$rawUrl eq ""} {
        tb::Log ERROR "no dashboard URL: set DASHBOARD_URL at the top of this file, or pass --url"
        return 2
    }
    set url [tb::NormalizeUrl $rawUrl]

    set replayDir [dict get $opts replay_dir]
    set dryRun [dict get $opts dry_run]
    if {!$dryRun && ![file isdirectory $replayDir]} {
        tb::Log ERROR "--replay-dir $replayDir does not exist or is not a directory"
        return 2
    }

    set httpTimeout [dict get $opts http_timeout]
    set timeBudget [dict get $opts time_budget]
    set deadline [expr {[clock milliseconds] + int($timeBudget * 1000)}]

    set stillPending 0
    if {!$dryRun} {
        set stillPending [tb::DrainReplayFiles $url $replayDir $deadline $httpTimeout]
    }

    tb::acc::reset $environment $build $hasBuild
    if {[catch {read_records $opts tb::acc::handle} collectErr]} {
        tb::Log ERROR "read_records crashed after producing $::tb::acc::Read record(s): $collectErr"
        return 2
    }
    set readCount $::tb::acc::Read
    set validCount $::tb::acc::Valid
    set skippedCount $::tb::acc::Skipped
    set canonical $::tb::acc::Canonical

    if {$dryRun} {
        set shown 0
        foreach rec $canonical {
            if {$shown >= 3} {
                break
            }
            incr shown
            puts ""
            puts "--- record $shown would be sent as ---"
            puts [tb::RecordToJson $rec]
        }
        tb::Log INFO "dry run: read=$readCount valid=$validCount skipped=$skippedCount - nothing was sent"
        return 0
    }

    lassign [tb::SendOwnRecords $canonical $url $environment $build $replayDir $deadline $httpTimeout] \
        sent inserted updated rejected failedBatches

    tb::Log INFO "feeder summary: read=$readCount valid=$validCount skipped=$skippedCount sent=$sent inserted=$inserted updated=$updated rejected=$rejected failed_batches=$failedBatches replay_files_pending=$stillPending"

    if {$failedBatches > 0 || $stillPending > 0} {
        return 1
    }
    return 0
}

# ----------------------------------------------------------------------
# Self-test: exercises the hand-built JSON encoder/parser and the
# validation rules with no server and no arguments. This is how those
# two pieces are "unit-tested" in a file that may run nowhere a Tcl
# test framework is installed - invoke with:
#   tclsh clients/feeder.tcl --self-test
# Exits 0 if every check passes, 1 otherwise. The testboard
# conformance suite runs this on every push (gated on tclsh, like the
# rest of the Tcl variants) - but, like everything else in this file,
# only under tclsh 8.6; it has never executed on real 8.5.
# ----------------------------------------------------------------------

proc tb::SelfTestCheck {name cond} {
    variable SelfTestFailures
    if {$cond} {
        puts "ok - $name"
    } else {
        puts "FAIL - $name"
        incr SelfTestFailures
    }
}

proc tb::SelfTest {} {
    variable SelfTestFailures
    set SelfTestFailures 0

    # NOTE: every condition below is evaluated with [expr {...}] AT THE
    # CALL SITE, not passed as unevaluated {...} text. Tcl substitutes
    # $variables into a braced argument as inert text and does not
    # re-scan the result for further [...]/$... substitution, so
    # "tb::SelfTestCheck name {[foo] eq bar}" would hand SelfTestCheck
    # the literal, un-run string "[foo] eq bar" rather than a boolean -
    # this exact bug was caught and fixed while writing this file (see
    # the commit message).
    tb::SelfTestCheck "escape backslash and quote" \
        [expr {[tb::json::Escape "a\"b\\c"] eq {a\"b\\c}}]
    tb::SelfTestCheck "escape newline/tab/cr" \
        [expr {[tb::json::Escape "a\nb\tc\rd"] eq {a\nb\tc\rd}}]
    tb::SelfTestCheck "escape control character" \
        [expr {[tb::json::Escape "\x01"] eq [format {\u%04x} 1]}]
    tb::SelfTestCheck "JString wraps in quotes" \
        [expr {[tb::json::JString "hi"] eq "\"hi\""}]
    tb::SelfTestCheck "JNull" [expr {[tb::json::JNull] eq "null"}]

    set obj [tb::json::JObject [list a [tb::json::JString "x"] b [tb::json::JNull]]]
    tb::SelfTestCheck "JObject shape" [expr {$obj eq {{"a":"x","b":null}}}]

    set parsed [tb::json::parse {{"a": "x", "b": null, "c": [1, 2.5, true, false], "d": {"e": "nested"}}}]
    set raw [tb::json::to_raw $parsed]
    tb::SelfTestCheck "parse object string field" [expr {[dict get $raw a] eq "x"}]
    tb::SelfTestCheck "parse object null field is the NULL sentinel" \
        [expr {[dict get $raw b] eq $::tb::json::NULL}]
    set c [dict get $raw c]
    tb::SelfTestCheck "parse array contents (numbers + booleans)" \
        [expr {[lindex $c 0] eq "1" && [lindex $c 1] eq "2.5" && [lindex $c 2] == 1 && [lindex $c 3] == 0}]
    tb::SelfTestCheck "parse nested object" \
        [expr {[dict get [dict get $raw d] e] eq "nested"}]

    set escParsed [tb::json::to_raw [tb::json::parse "\"line1\\nline2\\t\\u0041end\""]]
    tb::SelfTestCheck "parse string escapes including \\u" \
        [expr {$escParsed eq "line1\nline2\tAend"}]

    tb::SelfTestCheck "parse rejects malformed JSON" \
        [expr {[catch {tb::json::parse "\{not json"}]}]

    set rec1 [dict create environment e script s test_name t result PASS \
        start_time 2026-01-01T00:00:00.000000 end_time 2026-01-01T00:00:01.000000 \
        output "" source_link "" known_failure_reason ""]
    set rt1 [tb::json::to_raw [tb::json::parse [tb::RecordToJson $rec1]]]
    tb::SelfTestCheck "record round-trip without build" \
        [expr {![dict exists $rt1 build] && [dict get $rt1 environment] eq "e"}]

    set rec2 [dict create environment e script s test_name t result PASS \
        start_time 2026-01-01T00:00:00.000000 end_time 2026-01-01T00:00:01.000000 \
        output "" source_link "" known_failure_reason "" build "rc1"]
    set rt2 [tb::json::to_raw [tb::json::parse [tb::RecordToJson $rec2]]]
    tb::SelfTestCheck "record round-trip with build" [expr {[dict get $rt2 build] eq "rc1"}]

    set good [dict create environment e script s test_name t result PASS \
        start_time 2026-01-01T00:00:00.000000 end_time 2026-01-01T00:00:01.000000 output ""]
    tb::SelfTestCheck "validate_record accepts a good record" \
        [expr {![catch {tb::validate_record $good}]}]

    set badResult [dict replace $good result NOPE]
    tb::SelfTestCheck "validate_record rejects an unknown result" \
        [expr {[catch {tb::validate_record $badResult}]}]

    set withBranch [dict replace $good branch "x"]
    tb::SelfTestCheck "validate_record rejects a branch key" \
        [expr {[catch {tb::validate_record $withBranch}]}]

    set backwards [dict replace $good start_time 2026-01-01T00:00:02.000000 \
        end_time 2026-01-01T00:00:01.000000]
    tb::SelfTestCheck "validate_record rejects end_time before start_time" \
        [expr {[catch {tb::validate_record $backwards}]}]

    set badTime [dict replace $good start_time "not-a-time"]
    tb::SelfTestCheck "validate_record rejects a malformed timestamp" \
        [expr {[catch {tb::validate_record $badTime}]}]

    set badDate [dict replace $good start_time 2026-02-30T00:00:00.000000]
    tb::SelfTestCheck "validate_record rejects an impossible calendar date" \
        [expr {[catch {tb::validate_record $badDate}]}]

    tb::SelfTestCheck "Sanitize collapses unsafe characters" \
        [expr {[tb::Sanitize "env/with spaces!"] eq "env-with-spaces"}]

    if {$SelfTestFailures == 0} {
        puts "self-test: all checks passed"
        return 0
    }
    puts "self-test: $SelfTestFailures check(s) failed"
    return 1
}

if {[llength $argv] >= 1 && [lindex $argv 0] eq "--self-test"} {
    exit [tb::SelfTest]
}
exit [tb::Main $argv]

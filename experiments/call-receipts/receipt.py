#!/usr/bin/env python3
"""Synthetic turn-receipt demonstration (disposable experiment).

NOT a production runtime, billing store or schema. Reads the synthetic JSON
fixtures in ./fixtures, aggregates per-HTTP-attempt usage into a turn
receipt, renders it compactly or expanded, and checks accounting invariants
against an independent recomputation from the fixture.

Field provenance markers used in output:
  [S] source-backed: a field chaz persists today (see README audit table)
  [H] hypothetical: not persisted by the current runtime
  [M] modeled assumption made by this experiment

Usage (from this directory):
  python3 receipt.py              # compact receipts for examples a, b, c
  python3 receipt.py --expanded   # per-attempt detail
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FIXTURES = sorted((HERE / "fixtures").glob("*.json"))

REPORTED = "reported"
UNAVAILABLE = "unavailable"
NOT_APPLICABLE = "not_applicable"

# [M] Modeled assumption: an HTTP 429/5xx rejection returns no usage and is
# treated as "no inference ran" (n/a), while a timeout or dropped connection
# may have been processed upstream, so its usage is unknown. Whether either
# is billable is provider-specific and an unresolved product decision.
FAILED_OUTCOME_STATUS = {
    "rate_limited": NOT_APPLICABLE,
    "server_error": NOT_APPLICABLE,
    "timeout": UNAVAILABLE,
    "network_error": UNAVAILABLE,
}

DIMENSIONS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)
OPTIONAL_DIMENSIONS = DIMENSIONS[2:]


# --- usage normalization (mirrors, read-only, the projections in
# crates/lib/src/openai.rs `Usage::into_token_usage` and
# crates/lib/src/anthropic.rs `AnthUsage::into_token_usage`) ---------------


def _dec(value):
    return None if value is None else Decimal(str(value))


def normalize_usage(http):
    """Return (usage dict or None, notes) for one HTTP attempt."""
    wire = http.get("wire", "normalized")
    notes = []
    if wire == "normalized":
        raw = http.get("usage")
        if raw is None:
            return None, notes
        usage = {d: raw.get(d) for d in DIMENSIONS}
        usage["total_tokens"] = raw.get("total_tokens")
        usage["cost_usd"] = _dec(raw.get("cost_usd"))
        return usage, notes
    raw = http.get("raw_usage")
    if raw is None:
        return None, notes
    if wire == "openrouter":
        details = raw.get("prompt_tokens_details") or {}
        reported_cached = details.get("cached_tokens", raw.get("cache_read_input_tokens"))
        write = details.get("cache_write_tokens")
        cached = reported_cached
        if reported_cached is not None and write is not None:
            cached = max(reported_cached - write, 0)
            notes.append(
                f"openrouter cached_tokens {reported_cached:,} includes {write:,} cache writes"
                f" -> {cached:,} cache reads (same back-out as openai.rs)"
            )
        return {
            "prompt_tokens": raw.get("prompt_tokens", 0),
            "completion_tokens": raw.get("completion_tokens", 0),
            "total_tokens": raw.get("total_tokens", 0),
            "cached_tokens": cached,
            "cache_creation_tokens": raw.get("cache_creation_input_tokens", write),
            "reasoning_tokens": (raw.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "cost_usd": _dec(raw.get("cost")),
        }, notes
    if wire == "anthropic":
        read = raw.get("cache_read_input_tokens")
        create = raw.get("cache_creation_input_tokens")
        prompt = raw.get("input_tokens", 0) + (read or 0) + (create or 0)
        notes.append(
            f"anthropic input_tokens {raw.get('input_tokens', 0):,} excludes cache;"
            f" folded to prompt {prompt:,} (same as anthropic.rs); no cost reported"
        )
        return {
            "prompt_tokens": prompt,
            "completion_tokens": raw.get("output_tokens", 0),
            "total_tokens": prompt + raw.get("output_tokens", 0),
            "cached_tokens": read,
            "cache_creation_tokens": create,
            "reasoning_tokens": None,
            "cost_usd": None,
        }, notes
    raise ValueError(f"unknown wire format {wire!r}")


def classify(http):
    """Usage status of one HTTP attempt: reported / unavailable / n/a."""
    if http["outcome"] == "ok":
        usage, _ = normalize_usage(http)
        return REPORTED if usage is not None else UNAVAILABLE
    return FAILED_OUTCOME_STATUS.get(http["outcome"], UNAVAILABLE)


def sig2(value):
    """Round a Decimal to two significant figures (estimates only)."""
    if value == 0:
        return Decimal(0)
    return value.quantize(Decimal(1).scaleb(value.adjusted() - 1), rounding=ROUND_HALF_UP)


def estimate_cost(usage, model, rate_card):
    rates = rate_card.get(model)
    if rates is None:
        return None
    read = usage.get("cached_tokens") or 0
    write = usage.get("cache_creation_tokens") or 0
    uncached = usage["prompt_tokens"] - read - write
    per = Decimal(1_000_000)
    return (
        uncached * Decimal(rates["input_uncached"])
        + read * Decimal(rates["cache_read"])
        + write * Decimal(rates["cache_write"])
        + usage["completion_tokens"] * Decimal(rates["output"])
    ) / per


def resolve_cost(http, usage, rate_card):
    """Return (cost, kind, original). kind: reported/corrected/estimated/unknown."""
    if usage is None:
        return None, "unknown", None
    reported = usage.get("cost_usd")
    correction = http.get("correction_hypothetical")
    if correction is not None:
        return Decimal(correction["cost_usd"]), "corrected", reported
    if reported is not None:
        return reported, "reported", None
    est = estimate_cost(usage, http.get("model"), rate_card)
    if est is not None:
        return est, "estimated", None
    return None, "unknown", None


# --- receipt model ----------------------------------------------------------


@dataclass
class Row:
    key: tuple
    generation: int
    attempt_id: str
    model_sequence: int
    n: int
    outcome: str
    status: str
    usage: dict = None
    cost: Decimal = None
    cost_kind: str = "n/a"
    cost_original: Decimal = None
    model: str = None
    provider: str = None
    response_id: str = None
    latency_ms: int = None
    backoff_ms: int = None
    notes: list = field(default_factory=list)


@dataclass
class Tally:
    value: int = 0
    known: int = 0
    missing: int = 0


@dataclass
class AttemptView:
    attempt_id: str
    generation: int
    status: str
    elapsed_s: float
    persisted: dict


@dataclass
class Receipt:
    example: str
    title: str
    request_id: str
    attempts: list
    rows: list
    counted: list
    totals: dict
    model_calls: int
    cost_reported: Decimal = Decimal(0)
    cost_reported_n: int = 0
    cost_corrected_n: int = 0
    cost_estimated: Decimal = Decimal(0)
    cost_estimated_n: int = 0
    cost_unknown_n: int = 0

    @property
    def unavailable(self):
        return [r for r in self.rows if r.status == UNAVAILABLE]

    @property
    def partial(self):
        return bool(self.unavailable)


def _elapsed(attempt):
    if not attempt.get("started_at") or not attempt.get("completed_at"):
        return None
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return (parse(attempt["completed_at"]) - parse(attempt["started_at"])).total_seconds()


class Aggregator:
    """Folds a fixture turn into a Receipt. Each HTTP attempt is counted at
    most once; cache/reasoning counts are subsets, never added to totals;
    persisted turn metadata is a cross-check only."""

    def resolve_usage(self, http):
        status = classify(http)
        usage, notes = normalize_usage(http) if status == REPORTED else (None, [])
        return status, usage, notes

    def input_tokens(self, usage):
        return usage["prompt_tokens"]

    def usage_sources(self, attempt, rows):
        return [r for r in rows if r.status == REPORTED]

    def aggregate(self, turn):
        rate_card = turn.get("rate_card_illustrative", {})
        rows, counted, attempts = [], [], []
        model_calls = 0
        for attempt in turn["turn_attempts"]:
            attempts.append(
                AttemptView(
                    attempt["attempt_id"],
                    attempt["generation"],
                    attempt["status"],
                    _elapsed(attempt),
                    attempt.get("persisted_turn_metadata"),
                )
            )
            attempt_rows = []
            for call in attempt["calls"]:
                model_calls += 1
                for http in call["http_attempts"]:
                    status, usage, notes = self.resolve_usage(http)
                    row = Row(
                        key=(attempt["attempt_id"], call["model_sequence"], http["n"]),
                        generation=attempt["generation"],
                        attempt_id=attempt["attempt_id"],
                        model_sequence=call["model_sequence"],
                        n=http["n"],
                        outcome=http["outcome"],
                        status=status,
                        usage=usage,
                        model=http.get("model"),
                        provider=http.get("provider"),
                        response_id=http.get("response_id"),
                        latency_ms=http.get("latency_ms"),
                        backoff_ms=http.get("backoff_ms"),
                        notes=list(notes),
                    )
                    if status == REPORTED:
                        row.cost, row.cost_kind, row.cost_original = resolve_cost(
                            http, usage, rate_card
                        )
                    elif status == UNAVAILABLE:
                        row.cost_kind = "unknown"
                    attempt_rows.append(row)
            rows.extend(attempt_rows)
            counted.extend(self.usage_sources(attempt, attempt_rows))

        totals = {d: Tally() for d in DIMENSIONS}
        receipt = Receipt(
            turn["example"], turn["title"], turn["request_id"], attempts, rows, counted,
            totals, model_calls,
        )
        unavailable = sum(1 for r in rows if r.status == UNAVAILABLE)
        for d in DIMENSIONS:
            totals[d].missing = unavailable
        for r in counted:
            for d in DIMENSIONS:
                v = self.input_tokens(r.usage) if d == "prompt_tokens" else r.usage.get(d)
                if v is None:
                    totals[d].missing += 1
                else:
                    totals[d].value += v
                    totals[d].known += 1
            if r.cost_kind in ("reported", "corrected"):
                receipt.cost_reported += r.cost
                receipt.cost_reported_n += 1
                receipt.cost_corrected_n += r.cost_kind == "corrected"
            elif r.cost_kind == "estimated":
                receipt.cost_estimated += r.cost
                receipt.cost_estimated_n += 1
            elif r.cost_kind == "unknown":
                receipt.cost_unknown_n += 1
        receipt.cost_unknown_n += unavailable
        return receipt


# --- rendering ----------------------------------------------------------------


def human_tokens(n):
    """Mirror of the TUI status bar's `human_tokens` (942, 12.3k, 1.5M)."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _dim(t, fmt, label=""):
    if t.known == 0:
        return None
    return (">=" if t.missing else "") + fmt(t.value) + label


def _money(d):
    return f"${d}"


def cost_line(r):
    parts = []
    if r.cost_reported_n:
        extra = f", {r.cost_corrected_n} corrected [H]" if r.cost_corrected_n else ""
        lower = ">=" if (r.cost_unknown_n or r.cost_estimated_n) else ""
        parts.append(
            f"{lower}{_money(r.cost_reported)} reported ({r.cost_reported_n} call(s){extra})"
        )
    if r.cost_estimated_n:
        parts.append(
            f"~{_money(sig2(r.cost_estimated))} est. ({r.cost_estimated_n} call(s),"
            " illustrative rate card [M])"
        )
    if r.cost_unknown_n:
        parts.append(f"unknown for {r.cost_unknown_n} attempt(s)")
    return " + ".join(parts) if parts else "not reported"


def token_line(r, fmt):
    t = r.totals
    inp = _dim(t["prompt_tokens"], fmt) or "unavailable"
    subs = [
        s
        for s in (
            _dim(t["cached_tokens"], fmt, " cache-read"),
            _dim(t["cache_creation_tokens"], fmt, " cache-write"),
        )
        if s and not s.lstrip(">=").startswith("0 ")
    ]
    out = _dim(t["completion_tokens"], fmt) or "unavailable"
    reas = _dim(t["reasoning_tokens"], fmt, " reasoning")
    line = f"in {inp}"
    if subs:
        line += f" (of which {', '.join(subs)})"
    line += f" | out {out}"
    if reas and not reas.lstrip(">=").startswith("0 "):
        line += f" (of which {reas})"
    return line


def today_line(r):
    """[S]-modeled: what /info and the TUI status bar would show for this
    turn today. They fold `SessionEntry.metadata`, which only a completed,
    non-silent turn's Message entry carries (one entry per turn)."""
    metas = [a.persisted for a in r.attempts if a.persisted]
    if not metas:
        return "nothing: no Message entry with metadata (failed/interrupted attempts write none)"
    p = sum(m["prompt_tokens"] for m in metas)
    c = sum(m["completion_tokens"] for m in metas)
    cached = sum(m.get("cached_tokens") or 0 for m in metas)
    costs = [Decimal(m["cost_usd"]) for m in metas if m.get("cost_usd") is not None]
    s = f"{len(metas)} call{'s' if len(metas) != 1 else ''} | {p} prompt + {c} completion"
    if cached:
        s += f" ({cached} cached)"
    if costs:
        s += f" | ${float(sum(costs)):.4f}"
    return s


def _elapsed_text(r):
    parts = []
    for a in r.attempts:
        if a.elapsed_s is None:
            parts.append(f"gen {a.generation} unavailable ({a.status})")
        else:
            parts.append(f"gen {a.generation} {a.elapsed_s:g}s ({a.status})")
    return "; ".join(parts) + " [S wall clock incl. tools/backoff]"


def render_compact(r):
    retried = sum(1 for row in r.rows if row.n > 1)
    head = (
        f"[{r.example}] {r.title}\n"
        f"    request {r.request_id} | {len(r.attempts)} turn attempt(s) | {r.model_calls}"
        f" model call(s) | {len(r.rows)} HTTP attempt(s), {retried} retried"
    )
    lines = [head, f"    tokens  {token_line(r, human_tokens)}"]
    if r.partial:
        lines.append(
            f"    PARTIAL: usage unavailable for {len(r.unavailable)} of {len(r.rows)}"
            " HTTP attempt(s); totals are lower bounds"
        )
    lines.append(f"    cost    {cost_line(r)}")
    lines.append(f"    elapsed {_elapsed_text(r)}")
    lines.append(f"    today   [S-modeled /info + TUI fold] {today_line(r)}")
    return "\n".join(lines)


def _cell(v, fmt="{:,}"):
    return "-" if v is None else fmt.format(v)


def row_tag(row):
    return f"#{row.generation}.{row.model_sequence}.{row.n}"


def render_expanded(r):
    out = [render_compact(r), ""]
    hdr = (
        f"      {'gen.seq.try':<12}{'outcome':<14}{'usage':<10}{'in':>8}{'c-read':>8}"
        f"{'c-write':>8}{'out':>6}{'reas':>6}  {'cost':<24}{'latency[H]':>11}"
    )
    for a in r.attempts:
        el = "unavailable" if a.elapsed_s is None else f"{a.elapsed_s:g}s"
        out.append(f"    turn attempt {a.attempt_id} gen {a.generation} [S]: {a.status}, elapsed {el}")
        out.append(hdr)
        for row in (x for x in r.rows if x.attempt_id == a.attempt_id):
            if row.status == REPORTED:
                u = row.usage
                cells = (
                    f"{_cell(u['prompt_tokens']):>8}{_cell(u.get('cached_tokens')):>8}"
                    f"{_cell(u.get('cache_creation_tokens')):>8}{_cell(u['completion_tokens']):>6}"
                    f"{_cell(u.get('reasoning_tokens')):>6}"
                )
                if row.cost_kind == "corrected":
                    cost = f"${row.cost} corr[H] was ${row.cost_original}"
                elif row.cost_kind == "estimated":
                    cost = f"~${sig2(row.cost)} est.[M]"
                elif row.cost_kind == "reported":
                    cost = f"${row.cost} reported"
                else:
                    cost = "unknown"
                usage = "reported"
            elif row.status == UNAVAILABLE:
                cells = f"{'unavail.':>8}{'?':>8}{'?':>8}{'?':>6}{'?':>6}"
                cost, usage = "unknown", "unavail."
            else:
                cells = f"{'n/a':>8}{'n/a':>8}{'n/a':>8}{'n/a':>6}{'n/a':>6}"
                cost, usage = "n/a [M]", "n/a [M]"
            lat = _cell(row.latency_ms, "{:,}ms")
            out.append(
                f"      {row_tag(row):<12}{row.outcome:<14}{usage:<10}{cells}  {cost:<24}{lat:>11}"
            )
            if row.backoff_ms:
                out.append(f"      {'':<12}then backoff {row.backoff_ms:,}ms [H: only in warn! log today]")
            for note in row.notes:
                out.append(f"      {'':<12}note: {note}")
            if row.status == UNAVAILABLE and row.outcome == "ok":
                out.append(
                    f"      {'':<12}today: response had no usage block; build_metadata stores"
                    " usage.unwrap_or_default() -> recorded as 0 tokens [S]"
                )
        if a.persisted:
            match = _persisted_matches(r, a)
            out.append(
                f"      persisted turn metadata [S] (SessionEntry.metadata):"
                f" in {a.persisted['prompt_tokens']:,} out {a.persisted['completion_tokens']:,}"
                f" cost ${a.persisted.get('cost_usd')} model {a.persisted['model']}"
                f" -> equals this attempt's per-call sum: {'yes' if match else 'NO'}"
                " (cross-check only, not added)"
            )
    out.append(
        "    legend: [S] persisted today  [H] hypothetical, not persisted  [M] modeled"
        " assumption; '?'/unavail. = unknown, never 0"
    )
    return "\n".join(out)


def _persisted_matches(r, attempt):
    rows = [x for x in r.rows if x.attempt_id == attempt.attempt_id and x.status == REPORTED]
    p = attempt.persisted
    ok = sum(x.usage["prompt_tokens"] for x in rows) == p["prompt_tokens"]
    ok &= sum(x.usage["completion_tokens"] for x in rows) == p["completion_tokens"]
    pre = [x.cost_original if x.cost_kind == "corrected" else x.usage.get("cost_usd") for x in rows]
    pre = [c for c in pre if c is not None]
    return ok and sum(pre, Decimal(0)) == Decimal(p.get("cost_usd") or 0)


# --- invariant checker (downstream of aggregate + render) --------------------


def oracle(turn):
    """Independent recomputation straight from the fixture."""
    rate_card = turn.get("rate_card_illustrative", {})
    o = {
        "keys": set(), "unavailable": set(), "attempts": 0, "calls": 0,
        "sums": {d: 0 for d in DIMENSIONS}, "reported": Decimal(0), "estimated_n": 0,
    }
    for attempt in turn["turn_attempts"]:
        for call in attempt["calls"]:
            o["calls"] += 1
            for http in call["http_attempts"]:
                o["attempts"] += 1
                key = (attempt["attempt_id"], call["model_sequence"], http["n"])
                status = classify(http)
                if status == UNAVAILABLE:
                    o["unavailable"].add(key)
                if status != REPORTED:
                    continue
                o["keys"].add(key)
                usage, _ = normalize_usage(http)
                for d in DIMENSIONS:
                    o["sums"][d] += usage.get(d) or 0
                cost, kind, _ = resolve_cost(http, usage, rate_card)
                if kind in ("reported", "corrected"):
                    o["reported"] += cost
                elif kind == "estimated":
                    o["estimated_n"] += 1
    return o


def check_receipt(turn, receipt, compact, expanded):
    """Return a list of invariant violations (empty = pass)."""
    o = oracle(turn)
    v = []
    keys = [r.key for r in receipt.counted]
    if len(keys) != len(set(keys)):
        v.append(f"double-count: {len(keys) - len(set(keys))} usage source(s) counted twice")
    if set(keys) != o["keys"]:
        v.append(
            f"counted sources {sorted(map(str, set(keys) ^ o['keys']))} differ from"
            " attempts that actually reported usage"
        )
    for d in DIMENSIONS:
        if receipt.totals[d].value != o["sums"][d]:
            v.append(f"{d}: receipt total {receipt.totals[d].value} != oracle {o['sums'][d]}")
    t = receipt.totals
    if t["cached_tokens"].value + t["cache_creation_tokens"].value > t["prompt_tokens"].value:
        v.append("cache subsets exceed prompt tokens")
    if t["reasoning_tokens"].value > t["completion_tokens"].value:
        v.append("reasoning subset exceeds completion tokens")
    if receipt.model_calls != o["calls"] or len(receipt.rows) != o["attempts"]:
        v.append("call/attempt counts differ from fixture")
    want_partial = bool(o["unavailable"])
    if receipt.partial != want_partial:
        v.append(f"partial flag {receipt.partial} but fixture has {len(o['unavailable'])} unknown-usage attempt(s)")
    if want_partial:
        if "PARTIAL" not in compact or "in >=" not in compact:
            v.append("unknown usage present but compact totals not marked partial/lower-bound")
        for key in o["unavailable"]:
            gen = next(a["generation"] for a in turn["turn_attempts"] if a["attempt_id"] == key[0])
            tag = f"#{gen}.{key[1]}.{key[2]}"
            line = next((ln for ln in expanded.splitlines() if ln.strip().startswith(tag + " ")), "")
            body = line.split(tag, 1)[-1]
            if "unavail" not in line or re.search(r"(?<![\d,.$])0(?![\d,.])", body):
                v.append(f"attempt {tag} has unknown usage but renders as {body.split()[:4]}")
    elif "PARTIAL" in compact:
        v.append("compact marked PARTIAL with no unknown usage")
    if receipt.cost_reported != o["reported"]:
        v.append(f"reported cost {receipt.cost_reported} != oracle {o['reported']}")
    if o["estimated_n"] and "est." not in compact:
        v.append("estimated cost present but not labelled est.")
    for a in receipt.attempts:
        if a.persisted and not _persisted_matches(receipt, a):
            v.append(f"fixture inconsistency: persisted metadata of {a.attempt_id} != per-call sum")
    return v


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build(turn, aggregator=None):
    receipt = (aggregator or Aggregator()).aggregate(turn)
    compact = render_compact(receipt)
    full = render_expanded(receipt)
    return receipt, compact, full


def main(argv=None):
    ap = argparse.ArgumentParser(description="Synthetic turn receipts (experiment).")
    ap.add_argument("--expanded", action="store_true", help="per-attempt detail")
    ap.add_argument("--fixture", action="append", type=Path, help="fixture path (repeatable)")
    args = ap.parse_args(argv)
    paths = args.fixture or DEFAULT_FIXTURES
    print("SYNTHETIC DATA ONLY. Money values are illustrative; this is not a billing ledger.\n")
    failures = 0
    for path in paths:
        turn = load(path)
        receipt, compact, full = build(turn)
        print(full if args.expanded else compact)
        violations = check_receipt(turn, receipt, compact, full)
        print(f"    check   {'OK' if not violations else 'FAILED: ' + '; '.join(violations)}\n")
        failures += bool(violations)
    print(f"{len(paths)} receipt(s), {failures} invariant failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for the synthetic receipt experiment.

Positive cases compare aggregated + rendered receipts against hand-computed
numbers in each fixture's `expected_hand_computed` block. Negative controls
plug deliberately broken aggregators into the same render + check pipeline
and assert that `check_receipt` rejects them.

These tests exercise a toy model over synthetic fixtures. They do not prove
the accounting correctness of the production Rust runtime.

Run from this directory: python3 -m unittest -v
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from decimal import Decimal

import receipt as rc

FIXTURES = {p.stem[0]: rc.load(p) for p in rc.DEFAULT_FIXTURES}
CONTROL_LOG = []


def diag(msg):
    print(f"\n    {msg}", file=sys.stderr)


# --- deliberately broken controls -----------------------------------------


class DoubleCountAggregator(rc.Aggregator):
    """BROKEN: counts the per-call transcript records AND the persisted
    turn-level Message metadata (which is already their sum)."""

    def usage_sources(self, attempt, rows):
        sources = super().usage_sources(attempt, rows)
        p = attempt.get("persisted_turn_metadata")
        if p:
            sources = sources + [
                rc.Row(
                    key=(attempt["attempt_id"], "persisted", 0),
                    generation=attempt["generation"], attempt_id=attempt["attempt_id"],
                    model_sequence=-1, n=0, outcome="ok", status=rc.REPORTED,
                    usage={d: p.get(d) for d in rc.DIMENSIONS}, cost=Decimal(p["cost_usd"]),
                    cost_kind="reported",
                )
            ]
        return sources


class UnknownAsZeroAggregator(rc.Aggregator):
    """BROKEN: treats unknown usage as zero, like `usage.unwrap_or_default()`."""

    def resolve_usage(self, http):
        status, usage, notes = super().resolve_usage(http)
        if status == rc.UNAVAILABLE:
            zero = {d: 0 for d in rc.DIMENSIONS}
            zero.update(total_tokens=0, cost_usd=Decimal(0))
            return rc.REPORTED, zero, notes
        return status, usage, notes


class CacheAddedToInputAggregator(rc.Aggregator):
    """BROKEN: adds cache-read tokens on top of prompt tokens (cache is a subset)."""

    def input_tokens(self, usage):
        return usage["prompt_tokens"] + (usage.get("cached_tokens") or 0)


def run(example, aggregator=None):
    turn = FIXTURES[example]
    receipt, compact, expanded = rc.build(turn, aggregator)
    return turn, receipt, compact, expanded, rc.check_receipt(turn, receipt, compact, expanded)


class PositiveCases(unittest.TestCase):
    def assert_expected(self, example):
        turn, r, compact, expanded, violations = run(example)
        exp = turn["expected_hand_computed"]
        self.assertEqual(violations, [], f"[{example}] correct aggregator violated invariants")
        self.assertEqual(r.model_calls, exp["model_calls"])
        self.assertEqual(len(r.rows), exp["http_attempts"])
        self.assertEqual(sum(1 for x in r.rows if x.n > 1), exp["retried_http_attempts"])
        self.assertEqual(len(r.unavailable), exp["unavailable"])
        self.assertEqual(sum(1 for x in r.rows if x.status == rc.NOT_APPLICABLE), exp["not_applicable"])
        self.assertEqual(r.partial, exp["partial"])
        for d in ("prompt_tokens", "completion_tokens", "cached_tokens",
                  "cache_creation_tokens", "reasoning_tokens"):
            if d not in exp:
                continue
            if exp[d] is None:
                self.assertEqual(r.totals[d].known, 0, f"{d} should be not-reported")
            else:
                self.assertEqual(r.totals[d].value, exp[d], d)
        self.assertEqual(r.cost_reported, Decimal(exp["cost_reported"]))
        if exp["cost_estimated"] is None:
            self.assertEqual(r.cost_estimated_n, 0)
        else:
            self.assertEqual(rc.sig2(r.cost_estimated), Decimal(exp["cost_estimated"]))
        diag(f"[{example}] calls={r.model_calls} http_attempts={len(r.rows)} "
             f"in={r.totals['prompt_tokens'].value} out={r.totals['completion_tokens'].value} "
             f"reported=${r.cost_reported} partial={r.partial}")
        return r, compact, expanded

    def test_a_completed_retries_not_double_counted(self):
        r, compact, expanded = self.assert_expected("a")
        # Persisted turn metadata equals the completed attempt's per-call sum,
        # and is not added on top of it.
        self.assertIn("equals this attempt's per-call sum: yes", expanded)
        self.assertIn("in 19.1k (of which 13.2k cache-read)", compact)
        self.assertNotIn("PARTIAL", compact)
        # Compact is one block; expanded adds one line per HTTP attempt.
        for tag in ("#1.0.1", "#2.0.1", "#2.0.2", "#2.1.1", "#2.2.1", "#2.2.2"):
            self.assertIn(tag, expanded)
            self.assertNotIn(tag, compact)
        # Interrupted attempt has no completion time: elapsed is unavailable, not 0s.
        self.assertIn("gen 1 unavailable (interrupted)", compact)

    def test_b_failed_unknown_is_not_zero_and_totals_partial(self):
        r, compact, expanded = self.assert_expected("b")
        self.assertIn("in >=3.0k | out >=150", compact)
        self.assertIn("PARTIAL: usage unavailable for 4 of 7", compact)
        self.assertIn("unknown for 4 attempt(s)", compact)
        self.assertIn("today   [S-modeled /info + TUI fold] nothing", compact)
        for tag in ("#1.1.1", "#1.1.2", "#1.2.2", "#1.2.3"):
            line = next(ln for ln in expanded.splitlines() if ln.strip().startswith(tag))
            self.assertIn("unavail.", line)
        self.assertIn("recorded as 0 tokens [S]", expanded)

    def test_c_reported_vs_estimated_never_merged(self):
        r, compact, expanded = self.assert_expected("c")
        self.assertIn("$0.00398 reported (1 call(s), 1 corrected [H])", compact)
        self.assertIn("~$0.0087 est.", compact)
        merged = Decimal("0.00398") + r.cost_estimated
        self.assertNotIn(f"${merged}", compact)
        self.assertNotIn(f"${rc.sig2(merged)}", compact)
        self.assertIn("corr[H] was $0.004215", expanded)
        self.assertIn("6,200 includes 1,500 cache writes -> 4,700 cache reads", expanded)
        self.assertIn("folded to prompt 8,300", expanded)
        # Estimate shown to two significant figures only.
        self.assertNotIn(str(r.cost_estimated), compact)

    def test_estimate_precision_is_two_significant_figures(self):
        self.assertEqual(rc.sig2(Decimal("0.00867")), Decimal("0.0087"))
        self.assertEqual(rc.sig2(Decimal("0.0321")), Decimal("0.032"))

    def test_cli_exit_status_zero(self):
        for argv in ([], ["--expanded"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(rc.main(argv), 0)
            self.assertIn("3 receipt(s), 0 invariant failure(s)", buf.getvalue())


class NegativeControls(unittest.TestCase):
    def assert_caught(self, name, aggregator, examples, expect_fragment):
        caught = 0
        for ex in examples:
            _, r, _, _, violations = run(ex, aggregator)
            if violations:
                caught += 1
                diag(f"NEGATIVE CONTROL {name} on [{ex}]: CAUGHT ({len(violations)} violation(s)); "
                     f"first: {violations[0]}")
                self.assertTrue(any(expect_fragment in v for v in violations),
                                f"{name}: expected a '{expect_fragment}' diagnostic, got {violations}")
            else:
                diag(f"NEGATIVE CONTROL {name} on [{ex}]: NOT CAUGHT")
        CONTROL_LOG.append((name, caught, len(examples)))
        self.assertEqual(caught, len(examples), f"{name} escaped the checker")

    def test_control_double_count_is_caught(self):
        self.assert_caught("double_count", DoubleCountAggregator(), ["a", "c"], "differ from")

    def test_control_unknown_as_zero_is_caught(self):
        self.assert_caught("unknown_as_zero", UnknownAsZeroAggregator(), ["b"], "partial flag")

    def test_control_cache_added_to_input_is_caught(self):
        self.assert_caught("cache_added_to_input", CacheAddedToInputAggregator(), ["a", "c"],
                           "prompt_tokens: receipt total")

    def test_unknown_as_zero_renders_zero_where_correct_renders_unknown(self):
        _, _, _, broken_expanded, _ = run("b", UnknownAsZeroAggregator())
        _, _, _, good_expanded, _ = run("b")
        tag = "#1.1.2"
        broken = next(ln for ln in broken_expanded.splitlines() if ln.strip().startswith(tag))
        good = next(ln for ln in good_expanded.splitlines() if ln.strip().startswith(tag))
        self.assertIn(" 0 ", broken)
        self.assertNotIn(" 0 ", good)


def tearDownModule():
    total = sum(n for _, _, n in CONTROL_LOG)
    caught = sum(c for _, c, _ in CONTROL_LOG)
    print(f"\nNEGATIVE CONTROL SUMMARY: {caught}/{total} broken runs caught across "
          f"{len(CONTROL_LOG)} controls", file=sys.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

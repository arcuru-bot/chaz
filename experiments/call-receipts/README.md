# Call receipts: per-call usage audit and synthetic turn-receipt demo

**Disposable experiment.** This is not a production feature, an accepted UI design, a billing ledger or a schema. All fixture data is synthetic. All money values are illustrative. No model or API calls are made, and no production code was changed.

- Repository: `arcuru-bot/chaz`
- Audited revision: `947cfdaee43b7343a06499cf0bdf363ff7fa6352` (branch `cloud/receipts-base-20261004`)
- Contents: this README (the audit and results), `receipt.py` (stdlib-only CLI), `test_receipt.py` (unittest), `fixtures/*.json` (synthetic), `samples/*.txt` (captured outputs)

## Commands

Run these from `experiments/call-receipts/` with Python 3 and no dependencies:

```bash
python3 receipt.py               # compact receipts for examples a, b, c
python3 receipt.py --expanded    # per-attempt expanded receipts
python3 -m unittest -v           # positive cases + negative controls
```

---

## 1. Audit: source-backed facts at the pinned revision

All paths are relative to the repo root. Line ranges are for `947cfdae`. The docs were treated as claims to check, not as the source of truth.

### 1.1 Data flow (as implemented)

1. Each backend projects its wire usage onto `TokenUsage` and wraps it in a per-call `ResponseMetadata`. OpenAI-compatible: `crates/lib/src/openai.rs:208-246` (`Usage` and its detail structs), `:248-288` (`Usage::into_token_usage`), `:291-314` (`build_metadata`). Anthropic: `crates/lib/src/anthropic.rs:184-219` (`AnthUsage`, `into_token_usage`), `:608-625` (`build_metadata`).
2. `llm_call_with_retry` (`crates/lib/src/runtime.rs:323-366`) retries transient errors (`LlmError::is_retryable`, `crates/lib/src/error.rs:134-142`), up to `max_retries` (default 3, `crates/lib/src/config.rs:645-648`). It returns only the successful response or the final error. A failed attempt leaves just a `warn!` log line (`runtime.rs:343-355`).
3. In the ReAct loop (`runtime.rs:413-895`), each successful call emits `RuntimeRecord::ModelResponse { model_sequence, metadata, terminal, .. }` (`runtime.rs:52-70`; emitted at `:452-463`, `:530-541`, `:559-570`, `:591-602`, `:622-633`) and feeds `MetadataAccumulator::record` (`runtime.rs:196-263`). `model_sequence` increments only after a successful call (`runtime.rs:893`).
4. `SessionRuntimeRecorder` (`crates/lib/src/server/mod.rs:64-147`) turns each record into a `TurnTranscriptRecord { request_id, attempt_id, sequence, timestamp, message }` (`crates/lib/src/session/mod.rs:497-524`). Non-terminal records are persisted immediately to the `turn_transcript` store (`session/mod.rs:490`, `:1282-1301`). The terminal record is held in memory and committed together with the final entry (`session/mod.rs:1212-1280`).
5. On success, the turn's **summed** `ResponseMetadata` goes on the assistant `SessionEntry` of type `Message` (`server/mod.rs:3248-3255`; `SessionEntry` is at `session/mod.rs:89-104`). On error, the turn writes an `EntryType::Error` entry with `metadata: None` (`server/mod.rs:3256-3266`). A silent turn (empty body) writes no entry (`server/mod.rs:3232-3247`).
6. Every user-facing display folds **only** `SessionEntry.metadata`:
   - `/costs` and `chaz usage`: `collect_usage` and `collect_session_usage` (`crates/lib/src/session/usage.rs:111-226`), `UsageTotals::record` (`:93-106`), the `Message`-only filter `include_entry` (`:228-238`), and `render_text` (`:242-323`). The CLI wrapper is `crates/bin/src/main.rs:441-477`.
   - `/info`: `format_usage_summary` (`crates/lib/src/commands/session.rs:219-268`)
   - TUI status bar: `usage_segment` and `ctx_segment` (`crates/bin/src/bridge/tui/view/mod.rs:746-801`), plus `human_tokens` (`:1266-1275`)
   - the `chaz` tool `usage` query (`crates/lib/src/tools/chaz.rs:158-175`)
   - Schedule fires also copy the turn's summed metadata into `ScheduleFire.usage`, but only on `Ok` (`crates/lib/src/server/schedule.rs:365-371`; `crates/lib/src/agent_db.rs:491-504`).

### 1.2 Source-to-display table

Durability key: **D** = durable (eidetica), **T** = transient (memory or log only), **M** = missing (not captured anywhere).

| Dimension | Where it originates (path:lines, symbol) | Where it is stored | Dur. | Reaches a display? |
| --- | --- | --- | --- | --- |
| Turn request identity | `session/mod.rs:105-127` `TurnRequestId` (row key of the triggering entry) | `TurnTranscriptRecord.request_id`, `TurnAttempt.request_id` | D | No (only `/interrupted` / retry flows) |
| Turn-attempt identity + order | `session/mod.rs:137-150` `TurnAttempt { attempt_id, generation, status, started_at, completed_at }`; `start_turn_attempt` `:1175-1198` | `turn_attempts` store (`session/mod.rs:451`) | D | No usage display |
| Model-call identity in an attempt | `runtime.rs:54-61` `model_sequence`; `server/mod.rs:91` `sequence` | `turn_transcript` | D (successful calls only) | No |
| HTTP retry attempt identity (try n of call) | `runtime.rs:337` loop variable `attempt` | `warn!` log field `attempt` (`runtime.rs:345-352`) | T | No |
| Retry outcome / error class | `error.rs:134-142` `is_retryable`, `status()` | `warn!` log only; the final error string is stored in the `Error` entry content (`server/mod.rs:3260`) | T (per try), D (final string) | Only as `Error: …` text |
| Backoff delay | `runtime.rs:283-291` `backoff_delay` | `warn!` log `delay_ms` | T | No |
| Provider response id | `runtime.rs:149-151` `ResponseMetadata.response_id` | per call in `turn_transcript`; on the `Message` it is the **last** call's only (`runtime.rs:217`) | D | No |
| Model that answered | `runtime.rs:144` `model` | per call in `turn_transcript`; the `Message` keeps the **last** call's (`runtime.rs:215`) | D | `/costs` "By model", `/info` "Models" (both attribute the whole turn to the last model) |
| Upstream provider | `runtime.rs:147-148` `provider` (openai.rs only; anthropic.rs sets `None`, `:619`) | per call / last call | D | No |
| Prompt / completion / total tokens | `runtime.rs:182-185` `TokenUsage`; `openai.rs:209-216` (`#[serde(default)]` → 0 if absent) | per call in `turn_transcript`; summed on `Message` (`runtime.rs:220-228`) | D | `/costs`, `/info`, TUI, `chaz` tool |
| Cache read (subset of prompt) | `runtime.rs:186-187` `cached_tokens`; OpenRouter write back-out `openai.rs:254-268`; Anthropic fold-in `anthropic.rs:202-219` | as above; the sum treats `None` as 0 once any call reports one (`runtime.rs:229-232`) | D | `/costs`, `/info`, TUI `% cached` |
| Cache write | `runtime.rs:188-189` `cache_creation_tokens` | as above | D | `chaz usage --json` / `chaz` tool only; not in text renders |
| Reasoning tokens | `runtime.rs:190-191` | as above | D | JSON / tool only |
| Reported cost | `runtime.rs:192-193` `cost_usd: Option<f64>` (OpenRouter `usage.include=true`, `openai.rs:372`; never set by anthropic.rs) | per call; summed over `Some` values only (`runtime.rs:245-247`) | D | `/costs`, `/info`, TUI as `${:.4}` |
| Estimated cost | none: "we do not compute cost locally" (`runtime.rs:178-180`) | — | M | — |
| Cost correction / reconciliation | none | — | M | — |
| "Usage was absent" flag | none: `build_metadata` uses `usage.unwrap_or_default()` (`openai.rs:308`, `anthropic.rs:621`) | stored as zeros | M | Displayed as 0 |
| Context occupancy | `runtime.rs:153-161` `context_tokens` = the last call's prompt (`runtime.rs:259`) | `Message` only | D | TUI `ctx N%` (`view/mod.rs:790-801`) |
| Turn-attempt elapsed time | `TurnAttempt.started_at` / `completed_at` (`session/mod.rs:142,149`; set at `:1191`, `:1220`) | `turn_attempts` | D | No |
| Per-call latency | none (`TurnTranscriptRecord.timestamp` is the record-write time, `server/mod.rs:128`) | — | M (only gaps between records can be inferred) | No |
| Usage of calls before a turn fails | per-call `ModelResponse` records of the failed attempt | `turn_transcript` under the failed `attempt_id` | D | **No.** The `Error` entry has `metadata: None` |
| Usage of an interrupted attempt | its non-terminal records | `turn_transcript` | D | **No** |
| Silent-turn usage | `RuntimeOutcome.metadata` | not written to the session (`server/mod.rs:3232-3247`); `ScheduleFire.usage` only for schedule fires | T / D (schedule fires) | No |

### 1.3 Mismatches between the docs and the implementation

1. **"calls" means turns.** `docs/src/user_guide/usage.md:27-29` and `render_text` (`usage.rs:263-268`) report "N calls". The value is incremented once per `Message` entry (`usage.rs:95`), and each entry is already a sum over a whole ReAct loop. The same applies to `/info` (`commands/session.rs:229`) and the `chaz` tool's `total.calls`.
2. **"on every assistant turn" / "matches what was actually billed"** (`usage.md:3`, `:15`). Turns that end in an error, interrupted attempts and silent turns contribute nothing, even though the transcript durably holds per-call usage for the successful calls inside them. HTTP attempts that failed and may have been billed (timeouts) are not recorded anywhere.
3. **"Error entries have no ResponseMetadata (no LLM call happened for them)"** (`usage.md:69`). This is false for a turn that failed after one or more successful calls.
4. **"distinguishes 'no cost data' from '$0.00'"** (`usage.md:13`, `:65`, `:71`). This holds only at the all-or-nothing level (`cost_reported`, `usage.rs:53-55`). In a mixed turn or session (some calls with cost, some without), the displayed `$X` is an unlabelled lower bound. Example (c) shows `$0.0042` today for a turn whose second call had no reported cost.
5. **Missing usage collapses to zero.** `ResponseMetadata`'s doc says missing fields "surface as `None`/`0`" (`runtime.rs:137-139`). For the core counts it is always `0`, and every display sums it as a real zero.
6. **Per-model attribution.** The `Message` metadata carries the *last* call's `model` (`runtime.rs:198`, `:215`). `/costs` "By model" therefore assigns the whole turn's tokens and cost to that model, even when an earlier call in the turn was answered by a different (fallback) model.
7. **"there is no separate billing log"** (`docs/src/architecture/sessions.md:38`). That is true, but `turn_transcript` (described at `sessions.md:32`, `:92`) already holds per-call metadata durably. The usage docs do not mention it as a usage source, and nothing reads it for usage.
8. **Unverified:** `openai.rs` reads flat `cache_read_input_tokens` (`:226-228`) but, unlike `anthropic.rs`, does not fold it into `prompt_tokens`. If an OpenAI-compatible endpoint returned Anthropic-style flat fields with an uncached `prompt_tokens`, `cached` could exceed `prompt`. I did not verify whether any real endpoint does this.

---

## 2. The experiment (modeled, not source-backed)

`receipt.py` folds synthetic fixtures into a per-turn receipt. Its fixture format nests: turn request → turn attempts (gen) → model calls (`model_sequence`) → HTTP attempts (try *n*). The format is illustrative only.

Every rendered field carries a provenance marker:

- `[S]` source-backed: chaz persists the equivalent field today (§1.2)
- `[H]` hypothetical: the field is not persisted today. This covers HTTP-try rows, per-try latency, backoff and the cost correction.
- `[M]` modeled assumption made by this experiment

**Accounting rules:**

- Each HTTP attempt is counted at most once, keyed by `(attempt_id, model_sequence, n)`.
- Cache read, cache write and reasoning are *subsets*. They are shown as "of which" and never added to the totals.
- Persisted turn metadata (the `SessionEntry.metadata` analogue) is used only as a cross-check and never summed.
- Unknown usage renders as `unavail.` / `?`. Totals then become `>=` lower bounds with a `PARTIAL` line.
- Reported and estimated cost are kept as separate figures and never summed into one number.
- Reported cost keeps the provider's decimal precision (it is stored as a string in the fixture and parsed into a `Decimal`). Estimates are rounded to 2 significant figures and marked `~ … est.`
- A hypothetical correction replaces the original cost rather than adding to it, and the original stays visible.

**Modeled assumptions `[M]`:**

- HTTP 429/5xx rejections ran no inference and are shown as `n/a`.
- Timeouts and network errors have **unknown** usage.
- The rate card in fixture (c) is invented.
- Wire normalization for OpenRouter (write back-out) and Anthropic (cache fold-in) mirrors `openai.rs` / `anthropic.rs` by reading them. It does not call them.
- The `today` line imitates the `/info` text format (`commands/session.rs:254-257`) applied to the fixture's persisted turn metadata. It is a model of the current display, not output from the current code.

### Sample outputs

These are excerpts. The full captures are in `samples/compact.txt`, `samples/expanded.txt` and `samples/unittest.txt`.

**(a) Completed turn: an interrupted attempt, then a retried attempt with HTTP retries (compact)**

```text
[a] Completed turn: interrupted first attempt, retried second attempt with HTTP retries
    request req-a | 2 turn attempt(s) | 4 model call(s) | 6 HTTP attempt(s), 2 retried
    tokens  in 19.1k (of which 13.2k cache-read) | out 985 (of which 64 reasoning)
    cost    $0.036435 reported (4 call(s))
    elapsed gen 1 unavailable (interrupted); gen 2 41s (completed) [S wall clock incl. tools/backoff]
    today   [S-modeled /info + TUI fold] 1 call | 14900 prompt + 805 completion (13200 cached) | $0.0211
```

**(b) Failed turn with missing usage (expanded excerpt)**

```text
    tokens  in >=3.0k | out >=150
    PARTIAL: usage unavailable for 4 of 7 HTTP attempt(s); totals are lower bounds
    cost    >=$0.001725 reported (1 call(s)) + unknown for 4 attempt(s)
    today   [S-modeled /info + TUI fold] nothing: no Message entry with metadata (failed/interrupted attempts write none)
      #1.1.1      timeout       unavail.  unavail.       ?       ?     ?     ?  unknown                   120,000ms
      #1.1.2      ok            unavail.  unavail.       ?       ?     ?     ?  unknown                     2,500ms
                  today: response had no usage block; build_metadata stores usage.unwrap_or_default() -> recorded as 0 tokens [S]
      #1.2.1      server_error  n/a [M]        n/a     n/a     n/a   n/a   n/a  n/a [M]                       200ms
```

**(c) Provider-specific usage, a correction and an estimate (expanded excerpt)**

```text
    cost    >=$0.00398 reported (1 call(s), 1 corrected [H]) + ~$0.0087 est. (1 call(s), illustrative rate card [M])
    today   [S-modeled /info + TUI fold] 1 call | 16300 prompt + 550 completion (12100 cached) | $0.0042
      #1.0.1      ok            reported     8,000   4,700   1,500   300     -  $0.00398 corr[H] was $0.004215    3,100ms
                  note: openrouter cached_tokens 6,200 includes 1,500 cache writes -> 4,700 cache reads (same back-out as openai.rs)
      #1.1.1      ok            reported     8,300   7,400       0   250     -  ~$0.0087 est.[M]            4,200ms
                  note: anthropic input_tokens 900 excludes cache; folded to prompt 8,300 (same as anthropic.rs); no cost reported
```

### Assertions and negative controls

`check_receipt` runs downstream of `Aggregator.aggregate` **and** both renderers. It recomputes the expected result independently from the fixture and checks:

- unique usage sources
- counted sources match the attempts that actually reported usage
- per-dimension totals
- cache and reasoning subsets stay within their parents
- call and attempt counts
- the partial flag, plus `PARTIAL` / `>=` in the compact text
- each unknown-usage row renders `unavail.` with no bare `0`
- reported cost excludes estimates
- estimates are labelled `est.`
- the persisted cross-check

Positive tests also compare against hand-computed numbers in each fixture's `expected_hand_computed` block.

The broken controls live in `test_receipt.py`. Each one subclasses the aggregator and goes through the same render and check pipeline:

| Control | Bug it models | Fixtures | Diagnostic it must produce |
| --- | --- | --- | --- |
| `DoubleCountAggregator` | sums the per-call transcript **and** the persisted turn metadata | a, c | `counted sources … differ from …` |
| `UnknownAsZeroAggregator` | unknown usage becomes 0, as `unwrap_or_default()` does | b | `partial flag …` |
| `CacheAddedToInputAggregator` | cache reads added on top of prompt | a, c | `prompt_tokens: receipt total … != oracle …` |

---

## 3. Actual execution (this environment, Python 3.11.15)

| Command | Exit | Final summary (verbatim) |
| --- | --- | --- |
| `python3 receipt.py` | 0 | `3 receipt(s), 0 invariant failure(s)` |
| `python3 receipt.py --expanded` | 0 | `3 receipt(s), 0 invariant failure(s)` |
| `python3 -m unittest -v` | 0 | `Ran 9 tests in 0.004s` / `OK` (timing varies by run) |

Negative-control diagnostics from the unittest run (stderr), verbatim:

```text
NEGATIVE CONTROL cache_added_to_input on [a]: CAUGHT (1 violation(s)); first: prompt_tokens: receipt total 32300 != oracle 19100
NEGATIVE CONTROL cache_added_to_input on [c]: CAUGHT (1 violation(s)); first: prompt_tokens: receipt total 28400 != oracle 16300
NEGATIVE CONTROL double_count on [a]: CAUGHT (6 violation(s)); first: counted sources ["('att-a2', 'persisted', 0)"] differ from attempts that actually reported usage
NEGATIVE CONTROL double_count on [c]: CAUGHT (6 violation(s)); first: counted sources ["('att-c1', 'persisted', 0)"] differ from attempts that actually reported usage
NEGATIVE CONTROL unknown_as_zero on [b]: CAUGHT (7 violation(s)); first: counted sources ["('att-b1', 1, 1)", "('att-b1', 1, 2)", "('att-b1', 2, 2)", "('att-b1', 2, 3)"] differ from attempts that actually reported usage
NEGATIVE CONTROL SUMMARY: 5/5 broken runs caught across 3 controls
```

**Repository gate:** I did **not** run `nix develop .# -c just nix full`. No Rust file changed, so this experiment makes no claim about a production Rust gate. The tests above exercise a toy over synthetic fixtures and do not prove the production runtime's accounting.

---

## 4. Smallest plausible future production slice (described, not chosen or implemented)

A read-only receipt view for **one completed or failed turn attempt**, built from records that already exist: `turn_attempts` plus that attempt's `turn_transcript` `ModelResponse` metadata, with per-call rows by `model_sequence`.

That alone would:

- show per-call usage for failed and interrupted attempts, which exists durably today but is never displayed
- give honest call counts and per-call model attribution
- give wall-clock elapsed time from `started_at` / `completed_at`

It would need no schema change. Its known limits are the gaps below: no HTTP-retry rows, and absent usage is indistinguishable from a real 0.

## 5. Source gaps (for any honest receipt)

- Failed HTTP attempts are not persisted: no row, no error class, no backoff, and no usage, even when the provider may have billed a timed-out request.
- There is no "usage absent" marker. `unwrap_or_default()` writes 0 (`openai.rs:308`, `anthropic.rs:621`), and `Usage` fields default to 0 via serde (`openai.rs:209-216`).
- No per-call latency is recorded. Record timestamps are write times.
- `cost_usd` is an `f64`, so the provider's decimal string precision is lost. There is no cost provenance (reported vs estimated) and no correction field.
- The turn-level `Message` metadata keeps only the last call's `model` / `provider` / `response_id`.
- The `cached_tokens` `Option` sum turns `None` into 0 once any call reports a value (`runtime.rs:229-232`). This mixes "not reported" with 0 inside a turn.

## 6. Unresolved product decisions

- Should usage from failed and interrupted attempts count toward session and agent totals, or only appear on receipts?
- Are 429/5xx rejections zero-cost by policy, per provider, or always "unknown"? (This experiment assumed n/a.)
- Is an estimated cost ever shown? If so, where do rates come from, how are they versioned, and how prominent is the label?
- Should "calls" in `/costs`, `/info` and the `chaz` tool be renamed to turns, or recounted from the transcript? Either way it changes `chaz usage --json` semantics.
- How should a later provider correction be stored (superseding vs append-only), and which value does a total use?
- Should silent-turn usage be attributed to the session, not just to schedule fires?
- Where do HTTP-attempt records live (transcript vs a separate store), and do they sync between peers?

---

## Verdict: PARTIAL

**What worked**
- An honest receipt is expressible and checkable. Retries and turn-level re-attempts are counted once, cache counts stay subsets, unknown usage renders as unknown with visibly partial totals, and reported, corrected and estimated costs stay separate.
- The independent check caught all three deliberately broken aggregators (5/5 runs), each with the expected diagnostic.
- The audit found that per-call usage, including from failed and interrupted attempts, already exists durably in `turn_transcript`. Every display ignores it.

**What didn't**
- Several fields the demo renders cannot be backed by current records: HTTP-try rows, per-try latency and backoff, an "absent usage" flag, cost provenance and corrections. They are marked `[H]` / `[M]`, so the demo shows a target, not what the current data supports.
- The `today` line is a modeled imitation of the current display, not output from the Rust code.
- The repository gate was not run.

**Surprises**
- `/costs` and `/info` "calls" count turns, not LLM calls.
- A turn that fails after successful calls drops all of its usage from every display.
- A response without a usage block is recorded as real zeros.
- In a mixed turn, the displayed cost is an unlabelled lower bound (example c: `$0.0042` today vs `>=$0.00398` reported + `~$0.0087` est.).

**Recommendation for the real build**
- Start from the read-only, per-attempt transcript view in §4 before adding any new persisted fields.
- In the same change, make absent usage representable (`Option` rather than `unwrap_or_default`). Otherwise every later aggregate inherits unknown-as-zero.
- Settle the §6 decisions before persisting HTTP-retry records or any estimated cost.

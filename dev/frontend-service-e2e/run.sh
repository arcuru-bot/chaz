#!/usr/bin/env bash
set -euo pipefail

CHAZ_BIN="${CHAZ_BIN:-target/debug/chaz}"
STUB_LLM="${STUB_LLM:-dev/matrix-e2e/stub_llm.py}"
WORKSPACE="$(mktemp -d -t chaz-frontend-service-e2e-XXXXXX)"
STATE="$WORKSPACE/state"
KEEP="${KEEP:-0}"
CONFIG="$WORKSPACE/config.yaml"
DAEMON_PID=""
DISABLED_DAEMON_PID=""
STUB_PID=""
PRINT_A_PID=""
PRINT_B_PID=""
CLEANUP_REGRESSION="${CLEANUP_REGRESSION:-0}"
CLEANUP_REGRESSION_DAEMON_ADOPTED=0

terminate_and_wait() {
	local pid="$1" what="$2"
	[[ -n $pid ]] || return 0
	kill -TERM "$pid" 2>/dev/null || true
	for _ in $(seq 1 50); do
		kill -0 "$pid" 2>/dev/null || return 0
		sleep 0.1
	done
	kill -KILL "$pid" 2>/dev/null || true
	for _ in $(seq 1 50); do
		kill -0 "$pid" 2>/dev/null || return 0
		sleep 0.1
	done
	printf 'cleanup could not stop %s (pid %s)\n' "$what" "$pid" >&2
	return 1
}

cleanup() {
	local status=$? cleanup_failed=0
	terminate_and_wait "$DAEMON_PID" "service daemon" || cleanup_failed=1
	terminate_and_wait "$DISABLED_DAEMON_PID" "service-disabled daemon" || cleanup_failed=1
	terminate_and_wait "$PRINT_A_PID" "first print frontend" || cleanup_failed=1
	terminate_and_wait "$PRINT_B_PID" "second print frontend" || cleanup_failed=1
	terminate_and_wait "$STUB_PID" "stub LLM" || cleanup_failed=1
	if [[ $CLEANUP_REGRESSION == 1 && $CLEANUP_REGRESSION_DAEMON_ADOPTED == 1 && $cleanup_failed -eq 0 ]]; then
		printf 'PASS — forced post-autostart failure left no service daemon\n' >&2
	fi
	if [[ $KEEP == 1 ]]; then
		printf 'kept frontend service workspace: %s\n' "$WORKSPACE" >&2
	else
		rm -rf "$WORKSPACE"
	fi
	if ((cleanup_failed)); then
		exit 1
	fi
	exit "$status"
}
trap cleanup EXIT INT TERM

fail() {
	printf 'FAIL — %s\n' "$*" >&2
	exit 1
}

wait_for() {
	local what="$1" timeout="$2"
	shift 2
	local deadline=$((SECONDS + timeout))
	while ((SECONDS < deadline)); do
		if "$@"; then
			return 0
		fi
		sleep 0.05
	done
	fail "timed out waiting for $what"
}

daemon_pid_for_socket() {
	ss -xlpn | awk -v socket="$STATE/eidetica.sock" '
		index($0, socket) && match($0, /pid=[0-9]+/) { pid = substr($0, RSTART + 4, RLENGTH - 4) }
		END { print pid }
	'
}

adopt_daemon() {
	local deadline=$((SECONDS + 30))
	while ((SECONDS < deadline)); do
		DAEMON_PID="$(daemon_pid_for_socket)"
		[[ -n $DAEMON_PID ]] && return 0
		sleep 0.05
	done
	DAEMON_PID=""
	return 1
}

STUB_PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
STUB_LLM_REPLY_WITH_REQUEST=1 \
	python3 "$STUB_LLM" "$STUB_PORT" "frontend service stub reply" \
	>"$WORKSPACE/stub-llm.stdout" 2>&1 &
STUB_PID="$!"
wait_for "stub LLM" 30 sh -c \
	"curl -sf --max-time 2 http://127.0.0.1:$STUB_PORT/v1/models >/dev/null"

cat >"$CONFIG" <<EOF
state_dir: "$STATE"
service:
  enabled: true
backends:
  - name: stub
    type: openaicompatible
    api_base: http://127.0.0.1:$STUB_PORT/v1
    api_key: test
    models:
      - name: stub
agents:
  - name: chaz
    model: stub
    system_prompt: test fixture
    default_memory_banks: [shared-notes]
default_agents: [chaz]
EOF

# Concurrent cold clients must all converge on one daemon and one backend.
declare -a pids=()
for i in $(seq 1 8); do
	"$CHAZ_BIN" --config "$CONFIG" usage --json >"$WORKSPACE/usage-$i.json" \
		2>"$WORKSPACE/usage-$i.err" &
	pids+=("$!")
done
adopt_daemon || fail "no daemon owns the service socket"
CLEANUP_REGRESSION_DAEMON_ADOPTED=1
for pid in "${pids[@]}"; do
	wait "$pid" || fail "a concurrent usage client failed"
done
[[ -S $STATE/eidetica.sock ]] || fail "service socket was removed before clients completed"
[[ $(stat -c %a "$STATE/eidetica.sock") == 600 ]] || fail "service socket is not mode 0600"
[[ $(stat -c %a "$STATE") == 700 ]] || fail "state directory is not mode 0700"
[[ $(ps -o sid= -p "$DAEMON_PID" | tr -d ' ') == "$DAEMON_PID" ]] ||
	fail "auto-started daemon did not detach into its own session"
[[ $(pgrep -fc "^.*/${CHAZ_BIN##*/} --config $CONFIG daemon$") == 1 ]] ||
	fail "concurrent clients started more than one daemon"

if [[ $CLEANUP_REGRESSION == 1 ]]; then
	fail "forced post-autostart failure"
fi

# `--print` exercises the transport-only frontend path against the daemon's
# runtime. Start B after A's request has crossed the daemon context boundary.
# The request-tagged stub and trace assertions distinguish the two turns even
# though both clients use the same generic frontend protocol.
REQUESTS_BEFORE="$(grep -c '^stub_llm: request:' "$WORKSPACE/stub-llm.stdout" || true)"
"$CHAZ_BIN" --config "$CONFIG" --print --session duplicate-turn hello \
	>"$WORKSPACE/print-a.out" 2>"$WORKSPACE/print-a.err" &
PRINT_A_PID="$!"
wait_for "first daemon model request" 30 sh -c \
	"test \$(grep -c '^stub_llm: request:' '$WORKSPACE/stub-llm.stdout' || true) -eq $((REQUESTS_BEFORE + 1))"
"$CHAZ_BIN" --config "$CONFIG" --print --session duplicate-turn world \
	>"$WORKSPACE/print-b.out" 2>"$WORKSPACE/print-b.err" &
PRINT_B_PID="$!"
wait "$PRINT_A_PID" || fail "first concurrent print frontend failed"
PRINT_A_PID=""
wait "$PRINT_B_PID" || fail "second concurrent print frontend failed"
PRINT_B_PID=""
grep -q 'frontend service stub reply: hello' "$WORKSPACE/print-a.out" ||
	fail "first print frontend did not receive its hello turn"
# Both print clients may observe the first reply: that is a transport race, not
# a second runtime. The stub trace below is the authoritative daemon boundary.
REQUESTS_AFTER="$(grep -c '^stub_llm: request:' "$WORKSPACE/stub-llm.stdout" || true)"
[[ $((REQUESTS_AFTER - REQUESTS_BEFORE)) -eq 2 ]] ||
	fail "named frontend writes produced $((REQUESTS_AFTER - REQUESTS_BEFORE)) daemon turns instead of two"
[[ $(grep -c '^stub_llm: request: .*world' "$WORKSPACE/stub-llm.stdout" || true) -eq 1 ]] ||
	fail "world was not incorporated by exactly one daemon turn"

# A subsequent command reads the same named session.
PRINT_OUT="$("$CHAZ_BIN" --config "$CONFIG" --print --session print-shared hello \
	2>"$WORKSPACE/print.err")" || fail "print frontend failed"
[[ $PRINT_OUT == *"stub"* ]] || fail "print frontend returned an unexpected response"
"$CHAZ_BIN" --config "$CONFIG" cmd '/info' --session print-shared \
	>"$WORKSPACE/print-info.out" 2>"$WORKSPACE/print-info.err" ||
	fail "command could not open the print session"
grep -q 'Messages: 2' "$WORKSPACE/print-info.out" ||
	fail "command did not see the print frontend's user and agent messages"

# Client A writes; client B and usage both see the same daemon-hosted state.
"$CHAZ_BIN" --config "$CONFIG" cmd '/name from-client-a' --session shared \
	>"$WORKSPACE/client-a.out" 2>"$WORKSPACE/client-a.err" || fail "client A failed"
"$CHAZ_BIN" --config "$CONFIG" cmd '/info' --session shared \
	>"$WORKSPACE/client-b.out" 2>"$WORKSPACE/client-b.err" || fail "client B failed"
grep -q 'Name: from-client-a' "$WORKSPACE/client-b.out" ||
	fail "client B did not see client A's write"
"$CHAZ_BIN" --config "$CONFIG" usage --json >"$WORKSPACE/final-usage.json" \
	2>"$WORKSPACE/final-usage.err" || fail "usage client failed"
jq -e '.per_session[] | select(.name == "from-client-a")' "$WORKSPACE/final-usage.json" \
	>/dev/null || fail "usage did not see the command client's session"

# Hosted entities a client cannot classify for itself. A connected Instance
# must prove each per-DB key before it may read that tree, so its catalog walk
# yields nothing; the daemon's published peer-local index is the only way these
# reach a frontend. Memory banks are the load-bearing case — without the index
# the memory extension resolves no bank and every memory tool fails.
"$CHAZ_BIN" --config "$CONFIG" cmd '/agent hosted' >"$WORKSPACE/hosted-agents.out" \
	2>"$WORKSPACE/hosted-agents.err" || fail "hosted agent index client failed"
grep -q 'chaz' "$WORKSPACE/hosted-agents.out" ||
	fail "client did not see the daemon's hosted agent index"
"$CHAZ_BIN" --config "$CONFIG" cmd '/memory list' --session print-shared \
	>"$WORKSPACE/hosted-banks.out" 2>"$WORKSPACE/hosted-banks.err" ||
	fail "hosted memory bank index client failed"
grep -q 'shared-notes' "$WORKSPACE/hosted-banks.out" ||
	fail "client did not see the daemon's hosted memory bank index"

# A client's build must not overwrite the daemon's published catalog with its
# own empty pre-hydration view. A later independent client must still see both.
"$CHAZ_BIN" --config "$CONFIG" cmd '/agent hosted' --session print-shared \
	>"$WORKSPACE/hosted-agents-again.out" 2>"$WORKSPACE/hosted-agents-again.err" ||
	fail "later hosted agent index client failed"
grep -q 'chaz' "$WORKSPACE/hosted-agents-again.out" ||
	fail "a client overwrote the daemon's hosted agent index"
"$CHAZ_BIN" --config "$CONFIG" cmd '/memory list' --session print-shared \
	>"$WORKSPACE/hosted-banks-again.out" 2>"$WORKSPACE/hosted-banks-again.err" ||
	fail "later hosted memory bank index client failed"
grep -q 'shared-notes' "$WORKSPACE/hosted-banks-again.out" ||
	fail "a client overwrote the daemon's hosted memory bank index"

# Lifecycle mutations happen in a frontend process, so publishing only during
# daemon startup would leave the next fresh client with the old catalog.
"$CHAZ_BIN" --config "$CONFIG" cmd '/agent new frontend-mutant' \
	>"$WORKSPACE/catalog-create.out" 2>"$WORKSPACE/catalog-create.err" ||
	fail "client could not create a hosted agent"
"$CHAZ_BIN" --config "$CONFIG" cmd '/agent hosted' \
	>"$WORKSPACE/catalog-after-create.out" 2>"$WORKSPACE/catalog-after-create.err" ||
	fail "fresh client could not read the published created agent"
grep -q 'frontend-mutant' "$WORKSPACE/catalog-after-create.out" ||
	fail "fresh client did not see the newly published hosted agent"
"$CHAZ_BIN" --config "$CONFIG" cmd '/agent delete frontend-mutant' \
	>"$WORKSPACE/catalog-delete.out" 2>"$WORKSPACE/catalog-delete.err" ||
	fail "client could not delete the hosted agent"
"$CHAZ_BIN" --config "$CONFIG" cmd '/agent hosted' \
	>"$WORKSPACE/catalog-after-delete.out" 2>"$WORKSPACE/catalog-after-delete.err" ||
	fail "fresh client could not read the published deleted agent catalog"
if grep -q 'frontend-mutant' "$WORKSPACE/catalog-after-delete.out"; then
	fail "fresh client still saw a deleted hosted agent"
fi

# Closest deterministic headless equivalent to daemon+TUI coexistence: the
# TUI and cmd share exactly this bootstrap/build/session stack, while cmd avoids
# requiring a pseudo-terminal.
"$CHAZ_BIN" --config "$CONFIG" cmd '/sessions' >"$WORKSPACE/coexist.out" \
	2>"$WORKSPACE/coexist.err" || fail "headless frontend coexistence client failed"
[[ $(pgrep -fc "^.*/${CHAZ_BIN##*/} --config $CONFIG daemon$") == 1 ]] ||
	fail "frontend coexistence changed daemon ownership"

kill -TERM "$DAEMON_PID"
wait_for "daemon shutdown" 30 sh -c "! kill -0 $DAEMON_PID 2>/dev/null"
DAEMON_PID=""
[[ ! -e $STATE/eidetica.sock ]] || fail "clean shutdown left the service socket behind"

# Disabling the local service does not relax sole-backend ownership. This
# daemon has no socket to test, so a second invocation can only be excluded by
# the state-directory daemon claim before it opens SQLite.
DISABLED_STATE="$WORKSPACE/service-disabled-state"
DISABLED_CONFIG="$WORKSPACE/service-disabled-config.yaml"
sed "s|state_dir: \"$STATE\"|state_dir: \"$DISABLED_STATE\"|; s/enabled: true/enabled: false/" \
	"$CONFIG" >"$DISABLED_CONFIG"
"$CHAZ_BIN" --config "$DISABLED_CONFIG" daemon \
	>"$WORKSPACE/service-disabled-daemon.out" 2>"$WORKSPACE/service-disabled-daemon.err" &
DISABLED_DAEMON_PID="$!"
wait_for "service-disabled daemon database" 30 test -f "$DISABLED_STATE/eidetica.db"
kill -0 "$DISABLED_DAEMON_PID" 2>/dev/null || fail "service-disabled daemon exited unexpectedly"
if "$CHAZ_BIN" --config "$DISABLED_CONFIG" daemon \
	>"$WORKSPACE/service-disabled-second.out" 2>"$WORKSPACE/service-disabled-second.err"; then
	fail "a second service-disabled daemon opened the same backend"
fi
grep -q 'refusing to start a second opener' "$WORKSPACE/service-disabled-second.err" ||
	fail "service-disabled second daemon did not report the sole-opener refusal"
kill -TERM "$DISABLED_DAEMON_PID"
wait_for "service-disabled daemon shutdown" 30 sh -c "! kill -0 $DISABLED_DAEMON_PID 2>/dev/null"
DISABLED_DAEMON_PID=""

printf 'PASS — 8 concurrent frontends converged on one detached daemon\n'
printf 'PASS — concurrent --print clients left two ordered model turns daemon-owned\n'
printf 'PASS — --print completed a real callback-driven turn over the service\n'
printf 'PASS — command and usage clients had bidirectional state visibility\n'
printf 'PASS — clients read the hosted agent and memory bank indices over the service\n'
printf 'PASS — fresh clients converge after hosted agent creation and deletion\n'
printf 'PASS — headless frontend coexistence preserved sole daemon ownership\n'
printf 'PASS — service-disabled daemons still exclude a second backend opener\n'

# Event-Driven Homepage Data Buffer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make homepage data updates event-driven through a versioned in-process buffer, fix the watchlist parameter contract, and make manual refresh perform a real rebuild.

**Architecture:** `DashboardService` remains the source/projection builder, while a new `DataUpdateBuffer` stores the latest committed change set and wakes subscribers. `StreamHub` channels build once for the initial snapshot, then wait for relevant buffer commits instead of rebuilding on every interval; a bounded timeout remains only as a recovery/phase safety net. The existing service caches remain authoritative for payload contents, while the buffer provides atomic revision/change notifications and the latest commit metadata.

**Tech Stack:** Python 3, asyncio, threading, FastAPI WebSockets, pytest, TypeScript/React.

---

### Task 1: Lock the contracts with failing tests

**Files:**
- Create: `tests/test_data_update_buffer.py`
- Modify: `tests/test_live_stream_api.py`
- Modify: `tests/test_terminal_stream_api.py`

- [ ] **Step 1: Write the failing buffer tests**

Cover: monotonically increasing versions; one queued notification per subscriber with coalescing to the newest commit; latest snapshot/change access; unsubscribe stops notifications.

- [ ] **Step 2: Write the failing WebSocket tests**

Add a test that sends `watchlistCodes` and asserts `_live_terminal_params` preserves the codes. Add a test where a fake terminal builder returns payload A, a manual refresh is sent, and payload B is received without changing the subscription parameters.

- [ ] **Step 3: Run only these tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_data_update_buffer.py tests/test_live_stream_api.py tests/test_terminal_stream_api.py -q
```

Expected: failures for the missing buffer module/behavior, camelCase parsing, and refresh rebuild.

### Task 2: Implement the versioned in-process update buffer

**Files:**
- Create: `app/data_update_buffer.py`
- Test: `tests/test_data_update_buffer.py`

- [ ] **Step 1: Add `DataCommit` and `DataUpdateBuffer`**

Implement a thread-safe buffer with:

```python
commit(changes: Mapping[str, object], *, reason: str = "") -> DataCommit
snapshot() -> DataCommit
subscribe() -> BufferSubscription
```

`DataCommit` contains `version`, UTC/monotonic commit time, `changed_sections`, `reason`, and an immutable mapping of latest section values. `commit` atomically replaces changed sections, increments the version, and wakes subscribers from worker threads using `loop.call_soon_threadsafe`. Subscriber queues are bounded to one item and coalesce stale notifications so a slow channel receives the newest version rather than a backlog.

- [ ] **Step 2: Run buffer tests and verify GREEN**

Run the Task 1 command with `tests/test_data_update_buffer.py`; expect all buffer tests to pass.

### Task 3: Make StreamHub event-driven and refreshable

**Files:**
- Modify: `app/stream_hub.py`
- Modify: `app/main.py`
- Modify: `tests/test_live_stream_api.py`
- Modify: `tests/test_terminal_stream_api.py`

- [ ] **Step 1: Inject one shared buffer into StreamHub**

Extend `StreamHub` and `StreamChannel` to accept `DataUpdateBuffer`. Give each `ChannelSpec` an `interests` set. A channel performs its initial build, then waits for a matching commit or explicit refresh. Relevant default interests are `terminal`, `index_minutes`, `dark_pool`, `detail`, `mini_chart`, and `*`; a timeout only prevents a stuck channel and must not call `build` when no change arrived.

- [ ] **Step 2: Add explicit channel refresh**

Add `StreamHub.refresh(subscription)` / `StreamChannel.request_refresh()` using an asyncio event. The `/ws/live` `refresh` command must request a rebuild on the existing channel and send the resulting snapshot/delta; it must not tear down and reuse the old latest payload.

- [ ] **Step 3: Normalize both watchlist parameter spellings at the boundary**

Accept `watchlist_codes` and the frontend's `watchlistCodes` in `_live_watchlist_codes` and `_live_terminal_params`; emit only the normalized snake_case field internally. Add the same compatibility rule to detail channel watchlist extraction.

- [ ] **Step 4: Run WebSocket tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_live_stream_api.py tests/test_terminal_stream_api.py -q
```

Expected: all existing protocol tests plus the new camelCase/refresh tests pass.

### Task 4: Publish service/cache changes into the buffer

**Files:**
- Modify: `app/services.py`
- Modify: `app/main.py`
- Modify: `tests/test_dashboard_service.py`
- Modify: `tests/test_first_paint_perf.py`

- [ ] **Step 1: Add optional buffer injection to DashboardService**

Keep tests and custom callers compatible with `data_update_buffer=None`. Add a small `_publish_data_update` helper that commits section names and reason without blocking the request path.

- [ ] **Step 2: Publish atomic context commits**

After a successful `_refresh_context`, publish one commit covering `market`, `sectors`, `sector_flow`, `terminal`, `watchlist`, and `detail`. After watchlist/position mutations publish `watchlist`/`terminal`. After mini-chart warm batches complete, publish `mini_chart`/`terminal` so deferred rows are pushed immediately rather than waiting for the static 30-second interval.

- [ ] **Step 3: Wire one buffer into the app**

Construct `DataUpdateBuffer` before `StreamHub` and `DashboardService` in `app/main.py`, pass the same instance to both, and close subscriptions cleanly with the application lifecycle.

- [ ] **Step 4: Run service and performance regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_dashboard_service.py tests/test_first_paint_perf.py tests/test_live_stream_api.py -q
```

Expected: existing cache/prewarm behavior remains green and mini-chart completion now produces a buffer commit.

### Task 5: Verify frontend contract and production build

**Files:**
- Modify: `web/src/hooks/useTerminalStream.ts` only if needed to emit the normalized key consistently.
- Test: `web/scripts/test-local-watchlist-placeholders.mjs` or a new focused script if a frontend contract assertion is needed.

- [ ] **Step 1: Build the frontend**

Run `npm run build` in `web/`; expect exit code 0. Do not mix unrelated ESLint cleanup into this change.

- [ ] **Step 2: Run focused browser smoke test**

Use Playwright against the local server with a local watchlist code not present on page 1. Confirm the self-selected row receives real price/mini-chart/t-analysis data after the initial snapshot and after a manual refresh.

### Task 6: Full verification and handoff

- [ ] **Step 1: Run backend regression suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass; report the existing Starlette deprecation warning separately.

- [ ] **Step 2: Run targeted lint and record baseline lint debt**

Run ESLint only against modified TypeScript files. Record that full-project lint still contains the pre-existing errors in unrelated components if they remain.

- [ ] **Step 3: Review diff and commit**

Inspect `git diff --check`, `git status`, and the complete diff. Commit only the buffer, protocol, tests, and plan changes; preserve unrelated user files such as `app/principal.py` and `tests/test_principal.py`.

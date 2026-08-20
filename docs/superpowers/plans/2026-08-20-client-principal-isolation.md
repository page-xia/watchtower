# Client Principal Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace global watchlist/position state with client-scoped server persistence while keeping one shared market-data computation for fast, timely, accurate multi-user dashboards.

**Architecture:** The browser stores one random `client_id`; HTTP sends it as `X-Client-ID` and WebSocket subscriptions send it as `client_id`. A principal repository stores each client’s watchlist, positions, revision, and one-time migration state. `DashboardService` builds market facts and base ranking once, then applies a bounded principal overlay for each response; no request may fall back to the old global JSON files.

**Tech Stack:** FastAPI, Pydantic v2, PyMySQL, JSON local fallback, React/TypeScript/Vite, pytest, existing StreamHub/live WebSocket.

---

## File Map and Ownership

| File | Responsibility after this plan |
|---|---|
| `app/principal.py` | Validate and represent anonymous/formal principals. |
| `app/user_state.py` | Principal state models, repository protocol, mutation/import result types. |
| `app/user_state_json.py` | Locked atomic JSON repository for local development/tests. |
| `app/user_state_mysql.py` | RDS MySQL repository, schema bootstrap, transaction/revision handling. |
| `app/config.py` | User-store backend and MySQL environment configuration. |
| `app/models.py` | Personalization metadata in API payload models. |
| `app/services.py` | Public market context, principal state lookup, overlay, scoped writes, cache keys. |
| `app/main.py` | Principal extraction, personal endpoints, legacy import, HTTP/WS forwarding. |
| `app/stream_delta.py` | Revision/personalization fields in delta metadata. |
| `web/src/lib/clientIdentity.ts` | One browser `client_id` generator/validator. |
| `web/src/lib/api.ts` | `X-Client-ID`, server-authoritative watchlist calls, legacy import. |
| `web/src/lib/pushSubscription.ts` | Reuse `clientIdentity` instead of generating a second identity. |
| `web/src/hooks/useTerminalStream.ts` | Send `client_id`; consume user-scoped terminal deltas. |
| `web/src/hooks/useLiveChannel.ts` | Add identity to personal channel subscriptions. |
| `web/src/App.tsx` | Bootstrap/migrate/fetch canonical user state; remove local watchlist authority. |
| `web/src/types/api.ts` | Watchlist envelope and personalization fields. |
| `scripts/init_user_store.py` | Explicit MySQL schema bootstrap for deployment. |
| `scripts/probe_user_isolation.py` | Two-client production/local isolation smoke test. |
| `tests/test_principal.py` | Identity validation tests. |
| `tests/test_user_state.py` | JSON repository isolation/revision/migration tests. |
| `tests/test_user_state_mysql.py` | SQL/transaction contract tests. |
| `tests/test_api.py` | HTTP principal and personal endpoint tests. |
| `tests/test_live_stream_api.py` | WebSocket principal/channel isolation tests. |
| `tests/test_dashboard_service.py` | Shared market base and user overlay tests. |
| `tests/test_stream_delta.py` | Personal metadata delta tests. |
| `web/scripts/test-client-identity.mjs` | Browser identity and migration helper tests. |
| `README.md`, `.deploy/*.env.example` | Configuration, migration, rollback, and security runbook. |

## Task 1: Add the principal value object and identity contract

**Files:**
- Create: `app/principal.py`
- Create: `tests/test_principal.py`

- [ ] **Step 1: Write the failing tests**

```python
from app.principal import Principal, PrincipalValidationError, principal_from_client_id


def test_uuid_client_id_normalizes_to_anonymous_principal() -> None:
    principal = principal_from_client_id(" 550e8400-e29b-41d4-a716-446655440000 ")
    assert principal == Principal(type="anonymous_client", id="550e8400-e29b-41d4-a716-446655440000")


def test_invalid_client_id_is_rejected() -> None:
    for value in ("", "a", "../../watchlist", "x" * 65, None):
        try:
            principal_from_client_id(value)
        except PrincipalValidationError:
            continue
        raise AssertionError(f"expected invalid client id: {value!r}")


def test_principal_storage_key_is_stable_and_logs_use_digest() -> None:
    principal = Principal(type="anonymous_client", id="550e8400-e29b-41d4-a716-446655440000")
    assert principal.storage_key == "anonymous_client:550e8400-e29b-41d4-a716-446655440000"
    assert len(principal.log_digest) == 16
    assert principal.id not in principal.log_digest
```

- [ ] **Step 2: Run the tests and verify the intended failure**

Run: `..venv\Scripts\python.exe -m pytest tests/test_principal.py -q`

Expected: FAIL because `app.principal` and `principal_from_client_id` do not exist.

- [ ] **Step 3: Implement the minimal principal module**

Implement a frozen dataclass with `type`, `id`, `storage_key`, and a SHA-256 based `log_digest`; accept only `^[A-Za-z0-9_-]{8,64}$`, normalize surrounding whitespace, and raise `PrincipalValidationError` for missing or invalid values. Do not accept an owner/principal field from request bodies.

- [ ] **Step 4: Run the focused tests**

Run: `..venv\Scripts\python.exe -m pytest tests/test_principal.py -q`

Expected: `3 passed`.

- [ ] **Step 5: Commit the identity contract**

```powershell
git add app/principal.py tests/test_principal.py
git commit -m "feat: add client principal identity contract"
```

## Task 2: Define principal state models and a locked JSON repository

**Files:**
- Create: `app/user_state.py`
- Create: `app/user_state_json.py`
- Create: `tests/test_user_state.py`

- [ ] **Step 1: Write isolation, revision, and migration tests**

```python
from app.models import PositionRecord, WatchlistItem
from app.principal import Principal
from app.user_state import LegacyImportResult
from app.user_state_json import JsonPrincipalStateRepository


def test_watchlist_and_positions_are_isolated_by_principal(tmp_path) -> None:
    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    alice = Principal("anonymous_client", "alice-0001")
    bob = Principal("anonymous_client", "bob-0001")
    repo.upsert_watchlist(alice, WatchlistItem(code="300476", name="胜宏科技"))
    repo.upsert_position(bob, PositionRecord(code="300476", name="胜宏科技", cost=10, quantity=100, available_quantity=0))
    assert [item.code for item in repo.list_watchlist(alice)] == ["300476"]
    assert repo.list_watchlist(bob) == []
    assert [item.code for item in repo.list_positions(bob)] == ["300476"]
    assert repo.list_positions(alice) == []


def test_successful_mutation_increments_revision(tmp_path) -> None:
    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    principal = Principal("anonymous_client", "alice-0001")
    first = repo.upsert_watchlist(principal, WatchlistItem(code="300476", name="胜宏科技"))
    second = repo.delete_watchlist(principal, "300476", expected_revision=first.revision)
    assert first.revision == 1
    assert second.revision == 2


def test_legacy_import_is_idempotent_and_never_resurrects_existing_state(tmp_path) -> None:
    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    principal = Principal("anonymous_client", "alice-0001")
    old = [WatchlistItem(code="300476", name="胜宏科技")]
    first = repo.import_legacy_watchlist_once(principal, old)
    again = repo.import_legacy_watchlist_once(principal, [WatchlistItem(code="000001", name="平安银行")])
    assert isinstance(first, LegacyImportResult) and first.applied is True
    assert again.applied is False
    assert [item.code for item in repo.list_watchlist(principal)] == ["300476"]
```

- [ ] **Step 2: Run the tests and verify they fail for the missing repository**

Run: `..venv\Scripts\python.exe -m pytest tests/test_user_state.py -q`

Expected: FAIL because `app.user_state` and `JsonPrincipalStateRepository` do not exist.

- [ ] **Step 3: Define the repository models and protocol**

Add `PrincipalState(revision, watchlist, positions, personalization_status)`, `PrincipalMutation(revision, item)`, and `LegacyImportResult(applied, reason, revision, items)`. Define protocol methods exactly as listed in the design document. Use normalized six-digit codes from existing Pydantic models and cap imports at 200 items.

- [ ] **Step 4: Implement atomic JSON persistence**

Store one top-level bucket per `principal.storage_key`; guard reads/writes with a process lock; write a sibling `.tmp` file followed by `Path.replace`; create missing buckets with revision `0`; enforce expected-revision conflicts with a dedicated `RevisionConflict` exception; record `browser_watchlist_v1` before returning from legacy import.

- [ ] **Step 5: Run the focused repository tests**

Run: `..venv\Scripts\python.exe -m pytest tests/test_user_state.py -q`

Expected: `3 passed`.

- [ ] **Step 6: Commit the repository contract and local backend**

```powershell
git add app/user_state.py app/user_state_json.py tests/test_user_state.py
git commit -m "feat: add principal-scoped local state repository"
```

## Task 3: Add the RDS MySQL repository and configuration

**Files:**
- Create: `app/user_state_mysql.py`
- Create: `scripts/init_user_store.py`
- Create: `tests/test_user_state_mysql.py`
- Modify: `app/config.py`
- Modify: `README.md`
- Create: `.deploy/watchtower.env.example`

- [ ] **Step 1: Write the SQL and transaction contract tests**

```python
def test_mysql_repository_scopes_every_query_by_principal(fake_connection) -> None:
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: fake_connection)
    principal = Principal("anonymous_client", "alice-0001")
    repo.list_watchlist(principal)
    sql = " ".join(fake_connection.last_cursor.execute_calls[0][0].split())
    assert "principal_type = %s" in sql
    assert "principal_id = %s" in sql
    assert fake_connection.last_cursor.execute_calls[0][1][:2] == ("anonymous_client", "alice-0001")


def test_mysql_mutation_updates_revision_in_one_transaction(fake_connection) -> None:
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: fake_connection)
    result = repo.upsert_watchlist(Principal("anonymous_client", "alice-0001"), WatchlistItem(code="300476", name="胜宏科技"))
    assert result.revision == 1
    assert fake_connection.commit_count == 1
    assert fake_connection.rollback_count == 0
```

- [ ] **Step 2: Run the tests and verify the SQL contract fails**

Run: `..venv\Scripts\python.exe -m pytest tests/test_user_state_mysql.py -q`

Expected: FAIL because the repository and fake connection fixtures are not implemented.

- [ ] **Step 3: Add user-store settings**

In `AppSettings`, add `user_store_backend` (`json` locally, `mysql` in production), `user_store_file`, `user_mysql_host`, `user_mysql_port`, `user_mysql_user`, `user_mysql_pwd`, `user_mysql_db` (default `watchtower_user`), connect timeout, and pool size. Expose only backend/db name/availability in public config; never expose password or client IDs.

- [ ] **Step 4: Implement schema bootstrap and scoped SQL**

Implement the four tables from the approved design. Every `SELECT`, `INSERT`, `UPDATE`, and `DELETE` includes both `principal_type` and `principal_id`; mutation transactions lock/create `principal_states`, update the target row, increment revision, and commit. Convert JSON columns to/from existing `WatchlistItem` and `PositionRecord` models. Raise `UserStateUnavailable` on connection failure and `RevisionConflict` on stale writes.

- [ ] **Step 5: Implement the explicit bootstrap script**

`scripts/init_user_store.py` must load `settings.user_mysql_*`, refuse to run when backend is not MySQL, create the database tables, print table names and a redacted connection target, and exit nonzero on any SQL error. It must not import or copy `data/watchlist.json` or `data/positions.json`.

- [ ] **Step 6: Run repository tests and config checks**

Run: `..venv\Scripts\python.exe -m pytest tests/test_user_state_mysql.py tests/test_user_state.py -q`

Expected: all focused repository tests pass; no real RDS connection is required because SQL is tested through the fake connection.

- [ ] **Step 7: Commit the MySQL backend and configuration**

```powershell
git add app/user_state_mysql.py app/config.py scripts/init_user_store.py tests/test_user_state_mysql.py README.md .deploy/watchtower.env.example
git commit -m "feat: add mysql principal state backend"
```

## Task 4: Refactor `DashboardService` into public market data plus principal overlay

**Files:**
- Modify: `app/services.py:165-205`, `app/services.py:363-733`, `app/services.py:1875-1995`, `app/services.py:3188-3550`, `app/services.py:4746-5165`, `app/services.py:6720-7060`
- Modify: `app/models.py:1190-1220`
- Modify: `tests/test_dashboard_service.py`

- [ ] **Step 1: Add failing service tests for public sharing and private overlay**

```python
def test_two_principals_share_market_base_but_receive_private_overlay(service, monkeypatch) -> None:
    calls = {"context": 0}
    original = service._get_context

    def counted_context():
        calls["context"] += 1
        return original()

    monkeypatch.setattr(service, "_get_context", counted_context)
    alice = Principal("anonymous_client", "alice-0001")
    bob = Principal("anonymous_client", "bob-0001")
    service.upsert_watchlist(alice, WatchlistItem(code="300476", name="胜宏科技"))
    alice_payload = service.terminal(principal=alice, page_size=20)
    bob_payload = service.terminal(principal=bob, page_size=20)
    assert alice_payload.watchlist_codes == ["300476"]
    assert bob_payload.watchlist_codes == []
    assert calls["context"] <= 2


def test_public_activity_score_does_not_change_when_a_user_adds_watchlist(service) -> None:
    public = service.public_terminal(page_size=20)
    service.upsert_watchlist(Principal("anonymous_client", "alice-0001"), WatchlistItem(code="300476", name="胜宏科技"))
    after = service.public_terminal(page_size=20)
    assert [(item.code, item.activity_score) for item in public.stock_board.items] == [
        (item.code, item.activity_score) for item in after.stock_board.items
    ]
```

- [ ] **Step 2: Run the service tests and confirm they fail**

Run: `..venv\Scripts\python.exe -m pytest tests/test_dashboard_service.py -k "principal or public_terminal" -q`

Expected: FAIL because `Principal`, principal-aware service methods, and public/overlay separation do not exist.

- [ ] **Step 3: Inject the principal repository and remove global user state from market refresh**

Construct `DashboardService` with `user_state_store`; keep old `watchlist_store` and `position_store` out of runtime context construction. Make `_get_context()` build public signals with empty personal watchlist/positions. Keep theme configuration and easy_tdx board mappings unchanged. Add `_principal_state(principal)` with a 30-second per-principal cache and immediate replacement after writes.

- [ ] **Step 4: Split base ranking from personal ordering**

Change `_board_sort_key_for_quote` and `_board_sort_key` so their first tuple element is no longer `watchlisted/position`; public entries sort only by requested metric, activity score, and code. Add `_apply_principal_overlay(entries, state, sort)` that partitions entries into user pinned and normal lists while preserving each partition’s public order, then page-slices the result. Keep public market totals and scores unchanged.

- [ ] **Step 5: Add principal-aware service methods and mutation invalidation**

Add `principal: Principal | None = None` to terminal, dashboard, stock board, sector rank, search, signal detail, chart, overlay, extras, and daily methods. Missing principal means an empty personal state. Add `list_watchlist`, `upsert_watchlist`, `delete_watchlist`, `list_positions`, `upsert_position`, and `delete_position` methods that require a principal, write through the repository, replace only that principal’s cache, and clear only that principal’s payload cache. Add `public_terminal()` for the sharing test and internal use.

- [ ] **Step 6: Overlay user-specific fields without changing public signal truth**

When building board rows, pass the principal’s watchlist/position maps only to `watchlisted`, `position`, tags, and T+1 restriction fields. For details, use the public signal plus the principal position when producing execution risks. Do not call `data_source.fetch()` or rebuild the public signal list per user.

- [ ] **Step 7: Extend payload metadata**

Add `personalization_status` (`ready`, `missing_identity`, `unavailable`) and `personalization_revision` to `TerminalPayload` and `DashboardPayload`; keep `watchlist_codes` as a response field generated by the server state, never by request input.

- [ ] **Step 8: Run focused service tests and regression tests**

Run: `..venv\Scripts\python.exe -m pytest tests/test_dashboard_service.py -k "principal or public_terminal" -q`  
Expected: the new tests pass.

Then run: `..venv\Scripts\python.exe -m pytest tests/test_dashboard_service.py tests/test_formula_engine.py -q`  
Expected: all selected legacy tests pass; any changed assertion must verify public activity/signal values remain identical across principals.

- [ ] **Step 9: Commit the service split**

```powershell
git add app/services.py app/models.py tests/test_dashboard_service.py
git commit -m "refactor: separate public market data from principal overlays"
```

## Task 5: Make HTTP APIs principal-aware and add one-time legacy import

**Files:**
- Modify: `app/main.py:1-40`, `app/main.py:405-490`, `app/main.py:514-630`, `app/main.py:664-690`, `app/main.py:725-762`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing HTTP isolation tests**

```python
def test_watchlist_header_is_required_and_isolated(monkeypatch) -> None:
    client = TestClient(app)
    alice = {"X-Client-ID": "alice-0001"}
    bob = {"X-Client-ID": "bob-0001"}
    assert client.post("/api/watchlist", headers=alice, json={"code": "300476", "name": "胜宏科技"}).status_code == 200
    assert [row["code"] for row in client.get("/api/watchlist", headers=alice).json()["items"]] == ["300476"]
    assert client.get("/api/watchlist", headers=bob).json()["items"] == []
    assert client.get("/api/watchlist").status_code == 422


def test_watchlist_codes_query_cannot_override_server_state(monkeypatch) -> None:
    client = TestClient(app)
    headers = {"X-Client-ID": "alice-0001"}
    client.post("/api/watchlist", headers=headers, json={"code": "300476", "name": "胜宏科技"})
    response = client.get("/api/dashboard?view=terminal&watchlist_codes=000001", headers=headers)
    assert response.json()["watchlist_codes"] == ["300476"]
```

- [ ] **Step 2: Run the API tests and verify they fail**

Run: `..venv\Scripts\python.exe -m pytest tests/test_api.py -k "header_is_required or cannot_override" -q`

Expected: FAIL because current endpoints use global stores and request query codes.

- [ ] **Step 3: Add one dependency for identity extraction**

Implement `principal_from_http(request, x_client_id)` in `app/main.py` using the shared validator. Add a separate `require_principal` helper that raises `HTTPException(422)` for personal writes; read endpoints call `optional_principal` and pass `None` when absent. Never read identity from a JSON body.

- [ ] **Step 4: Change personal endpoint response envelopes**

Return `{items, revision, personalization_status}` from `/api/watchlist` and `/api/positions`; route methods call the principal-aware service. Keep status 409 for stale revisions and 503 for unavailable user storage. Preserve item validation through existing Pydantic models.

- [ ] **Step 5: Add the legacy import endpoint**

Add `POST /api/watchlist/import-legacy` with `X-Client-ID`; accept `{items: WatchlistItem[]}` only, cap at 200, call `import_legacy_watchlist_once`, and return the canonical envelope plus `{migration: {applied, reason}}`. The endpoint must never read `data/watchlist.json` on the server.

- [ ] **Step 6: Thread identity into all personal HTTP reads**

Pass the optional principal to terminal/dashboard/stock-board/search/detail/chart/overlay/extras/daily. Remove `_client_watchlist_kwargs` as a source of truth. Keep a one-release parser for `watchlist_codes` only to prove it is ignored by tests, then delete it in the cleanup task.

- [ ] **Step 7: Run focused and full API tests**

Run: `..venv\Scripts\python.exe -m pytest tests/test_api.py -k "watchlist or positions or principal or terminal" -q`  
Expected: new isolation tests and updated endpoint tests pass.

Then run: `..venv\Scripts\python.exe -m pytest tests/test_api.py -q`  
Expected: all API tests pass with response-envelope assertions updated.

- [ ] **Step 8: Commit the HTTP boundary**

```powershell
git add app/main.py tests/test_api.py
git commit -m "feat: scope personal APIs to client principal"
```

## Task 6: Make live WebSocket channels principal-safe

**Files:**
- Modify: `app/main.py:950-1060`, `app/main.py:1235-1410`
- Modify: `app/stream_delta.py`
- Modify: `tests/test_live_stream_api.py`
- Modify: `tests/test_stream_delta.py`

- [ ] **Step 1: Write failing two-client WebSocket tests**

```python
def test_live_terminal_isolated_by_client_id(monkeypatch) -> None:
    seen = []

    def fake_terminal(**kwargs):
        principal = kwargs.get("principal")
        seen.append(principal.id if principal else None)
        payload = _terminal_payload()
        payload["watchlist_codes"] = ["300476"] if principal and principal.id == "alice-0001" else []
        return _FakeModel(payload)

    monkeypatch.setattr(service, "terminal", fake_terminal)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as alice, client.websocket_connect("/ws/live") as bob:
            alice.send_json({"type": "subscribe", "channel": "terminal", "params": {"client_id": "alice-0001"}})
            bob.send_json({"type": "subscribe", "channel": "terminal", "params": {"client_id": "bob-0001"}})
            assert _receive_channel(alice, "terminal")["data"]["watchlist_codes"] == ["300476"]
            assert _receive_channel(bob, "terminal")["data"]["watchlist_codes"] == []
    assert set(seen) == {"alice-0001", "bob-0001"}
```

- [ ] **Step 2: Run the test and verify the current protocol fails**

Run: `..venv\Scripts\python.exe -m pytest tests/test_live_stream_api.py::test_live_terminal_isolated_by_client_id -q`

Expected: FAIL because current `_live_terminal_params` reads only `watchlist_codes` and has no principal.

- [ ] **Step 3: Parse and validate `client_id` in live parameters**

Extend `StreamParams` with `client_id: str | None` and `principal: Principal | None`; `_live_terminal_params` reads `client_id`, validates it, and creates an optional principal. Invalid IDs produce a channel error without subscribing. `_live_channel_key` includes the complete principal storage key plus normalized view parameters.

- [ ] **Step 4: Forward the principal through live channel specs**

Pass `principal` to `service.terminal`, detail chart, detail overlay, and detail daily. Do not pass client-supplied `watchlist_codes` into service methods. A missing ID is an empty personalization state for reads.

- [ ] **Step 5: Add revision metadata to delta comparison**

Add `personalization_status` and `personalization_revision` to `_META_FIELDS`; their change produces a delta even when market items are unchanged. The frontend delta merger must retain these fields from `meta`.

- [ ] **Step 6: Run live and delta tests**

Run: `..venv\Scripts\python.exe -m pytest tests/test_live_stream_api.py tests/test_stream_delta.py -q`  
Expected: all tests pass, including same-market/different-principal isolation and revision-change delta cases.

- [ ] **Step 7: Commit the WebSocket boundary**

```powershell
git add app/main.py app/stream_delta.py tests/test_live_stream_api.py tests/test_stream_delta.py
git commit -m "feat: isolate live channels by client principal"
```

## Task 7: Move the frontend to server-authoritative principal state

**Files:**
- Create: `web/src/lib/clientIdentity.ts`
- Modify: `web/src/lib/pushSubscription.ts`
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/hooks/useTerminalStream.ts`
- Modify: `web/src/hooks/useLiveChannel.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/types/api.ts`
- Create: `web/scripts/test-client-identity.mjs`

- [ ] **Step 1: Write failing identity and header tests**

```javascript
import assert from "node:assert/strict"
import { clientIdFromStorage, CLIENT_ID_KEY } from "../src/lib/clientIdentity.ts"

const storage = new Map()
const fakeWindow = { localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) } }
const first = clientIdFromStorage(fakeWindow)
const second = clientIdFromStorage(fakeWindow)
assert.match(first, /^[A-Za-z0-9_-]{8,64}$/)
assert.equal(first, second)
assert.equal(CLIENT_ID_KEY, "watchtower.client-id.v1")
```

Add a fetch stub assertion that `/api/watchlist` and `/api/dashboard` receive `X-Client-ID` and never append `watchlist_codes`.

- [ ] **Step 2: Run the browser helper test and verify it fails**

Run: `node web/scripts/test-client-identity.mjs`

Expected: FAIL because `clientIdentity.ts` and header injection do not exist.

- [ ] **Step 3: Extract one identity implementation**

Move UUID generation and validation from `pushSubscription.ts` into `clientIdentity.ts`; export `getClientId()` and `CLIENT_ID_KEY`. `pushSubscription.ts` imports it and no longer generates an independent fallback identity.

- [ ] **Step 4: Add identity headers and canonical watchlist API calls**

In `api.ts`, add `clientHeaders()` returning `{ "X-Client-ID": getClientId() }`; merge it with JSON headers for writes. Change `listWatchlist`, `addWatchlist`, and `removeWatchlist` to consume the `{items, revision, personalization_status}` envelope. Add `importLegacyWatchlist(items)` and use an in-flight GET key that includes the client ID only through headers, not the URL.

- [ ] **Step 5: Replace browser watchlist authority in `App.tsx`**

On startup, load server state; if the old `watchtower.watchlist.v1` key exists, call the one-time import endpoint, then delete the old key. Maintain `serverWatchlist` and `personalizationRevision` in React state. Use local data only as optimistic UI while a server request is pending. After add/remove, replace state with the response envelope and call `liveSocket.refresh("terminal")`.

- [ ] **Step 6: Add `client_id` to all personal live subscriptions**

`useTerminalStream` sends `{ client_id: getClientId(), sector, boardLevel, ... }`. `useLiveChannel` accepts a `personal` boolean or receives params from callers; detail chart/overlay/daily subscriptions include `client_id`. `index_minutes` and public dark-pool channels do not need it.

- [ ] **Step 7: Update TypeScript models and delta merge**

Add `personalization_status`, `personalization_revision`, and `items/revision` envelope types. Keep `applyLocalWatchlistToPayload` only for optimistic marking during a pending mutation; it must not add placeholder stocks after the server response is ready and must not overwrite `watchlist_preview` from the server.

- [ ] **Step 8: Run frontend checks**

Run: `node web/scripts/test-client-identity.mjs`  
Expected: PASS.

Run: `npm --prefix web run build`  
Expected: TypeScript compilation and Vite build succeed.

Run: `npm --prefix web run lint`  
Expected: ESLint succeeds with no new warnings.

- [ ] **Step 9: Commit the frontend identity migration**

```powershell
git add web/src/lib/clientIdentity.ts web/src/lib/pushSubscription.ts web/src/lib/api.ts web/src/hooks/useTerminalStream.ts web/src/hooks/useLiveChannel.ts web/src/App.tsx web/src/types/api.ts web/scripts/test-client-identity.mjs
git commit -m "feat: make frontend personal state client-scoped"
```

## Task 8: Remove global fallback paths and add operational migration tooling

**Files:**
- Modify: `app/services.py`
- Modify: `app/main.py`
- Modify: `app/config.py`
- Modify: `scripts/deploy_aliyun.ps1`
- Modify: `README.md`
- Modify: `.dockerignore`
- Create: `scripts/probe_user_isolation.py`
- Create: `tests/test_user_store_failure.py`

- [ ] **Step 1: Write the no-fallback failure test**

```python
def test_user_store_failure_does_not_read_global_watchlist(monkeypatch, tmp_path) -> None:
    global_file = tmp_path / "watchlist.json"
    global_file.write_text('[{"code":"300476","name":"不应读取"}]', encoding="utf-8")
    failing_store = FailingPrincipalStateRepository()
    service = DashboardService(make_settings(tmp_path, watchlist_file=global_file), user_state_store=failing_store)
    payload = service.terminal(principal=Principal("anonymous_client", "alice-0001"), page_size=20)
    assert payload.personalization_status == "unavailable"
    assert payload.watchlist_codes == []
```

- [ ] **Step 2: Run the failure test and verify current fallback behavior fails it**

Run: `..venv\Scripts\python.exe -m pytest tests/test_user_store_failure.py -q`

Expected: FAIL until all service construction and personal reads stop consulting `WatchlistStore`/`PositionStore`.

- [ ] **Step 3: Make production backend selection explicit**

Build `DashboardService` with `build_principal_state_repository(settings)`. In production settings require MySQL; if initialization or a read fails, return the unavailable personalization status. Remove runtime calls to `self.watchlist_store.list_items()` and `self.position_store.list_items()` from context refresh and board candidate selection.

- [ ] **Step 4: Add deployment bootstrap and smoke probe**

Update `scripts/deploy_aliyun.ps1` to run `scripts/init_user_store.py` before restarting the container and to run `scripts/probe_user_isolation.py` after HTTPS health succeeds. The probe generates two UUIDs, adds one stock to A, checks B is empty, opens two terminal requests, deletes A’s stock, and exits nonzero on any cross-user result; it prints only short digests.

- [ ] **Step 5: Document environment and rollback**

Document `WATCH_USER_STORE_BACKEND=mysql`, `WATCH_USER_MYSQL_HOST`, `WATCH_USER_MYSQL_PORT`, `WATCH_USER_MYSQL_USER`, `WATCH_USER_MYSQL_PWD`, `WATCH_USER_MYSQL_DB`, schema bootstrap, HTTPS requirement, 200-item limits, old file non-use, and the emergency rollback rule (disable personal writes rather than re-enable global state).

- [ ] **Step 6: Run failure and deployment-script checks**

Run: `..venv\Scripts\python.exe -m pytest tests/test_user_store_failure.py -q`  
Expected: PASS with no global file read.

Run: `..venv\Scripts\python.exe scripts/probe_user_isolation.py --base-url http://127.0.0.1:8788 --dry-run`  
Expected: validation output only and exit code 0; no database writes in dry-run mode.

- [ ] **Step 7: Commit operational safeguards**

```powershell
git add app/services.py app/main.py app/config.py scripts/deploy_aliyun.ps1 scripts/probe_user_isolation.py tests/test_user_store_failure.py README.md .dockerignore
git commit -m "ops: add principal store deployment and no-fallback safeguards"
```

## Task 9: Add integration, performance, and production acceptance tests

**Files:**
- Create: `tests/test_principal_isolation_integration.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_live_stream_api.py`
- Modify: `tests/test_dashboard_service.py`
- Modify: `README.md`

- [ ] **Step 1: Write the two-principal integration test**

Create two random principals against an in-memory/JSON repository, add A’s `300476`, subscribe both to the same market parameters, assert public `(code, price, activity_score, signal_score)` values are equal, assert A’s item is pinned and B’s is not, delete A’s item, and assert A’s next delta removes the pin while B’s payload remains byte-equivalent except for its own timestamps.

- [ ] **Step 2: Write the shared-computation counter test**

Monkeypatch the market data source and public context builder, issue terminal calls for 50 different principals with the same market parameters, and assert the upstream snapshot/context builder call count is `1` within the configured live cache TTL. Assert each user state repository lookup is scoped to its own principal.

- [ ] **Step 3: Write the stale-write and migration tests**

Use two mutations with the same expected revision and assert exactly one succeeds; call legacy import twice and assert only the first call can write; pre-populate a canonical server item, import a conflicting legacy item, and assert the canonical server item wins.

- [ ] **Step 4: Run the complete backend suite**

Run: `..venv\Scripts\python.exe -m pytest -q`

Expected: all backend tests pass, including existing easy_tdx capability, dashboard, API, stream, persistence, and message tests.

- [ ] **Step 5: Run the frontend build/lint suite**

Run: `node web/scripts/test-client-identity.mjs; npm --prefix web run build; npm --prefix web run lint`

Expected: all three commands succeed.

- [ ] **Step 6: Run the local two-client smoke test**

Start the local server with `WATCH_USER_STORE_BACKEND=json`, then run:

```powershell
..venv\Scripts\python.exe scripts/probe_user_isolation.py --base-url http://127.0.0.1:8788
```

Expected: A-only watchlist, B-empty watchlist, distinct terminal personal revisions, successful delete, and exit code 0.

- [ ] **Step 7: Record performance evidence**

Run the probe with 50 generated client IDs and capture public context count, personal overlay p95, terminal p95, and upstream request count. Acceptance is: public context/market fetch count remains one per cache window; overlay p95 < 50 ms; hot user-state query p95 < 20 ms; A write visible within one WS tick.

- [ ] **Step 8: Commit integration coverage**

```powershell
git add tests/test_principal_isolation_integration.py tests/test_api.py tests/test_live_stream_api.py tests/test_dashboard_service.py README.md
git commit -m "test: verify multi-client principal isolation and performance"
```

## Task 10: Final verification and release checkpoint

**Files:**
- Modify only the file named by a failing verification command, then rerun that command before proceeding.

- [ ] **Step 1: Check the worktree and generated artifacts**

Run: `git status --short; git diff --check; rg -n "data/watchlist\.json|data/positions\.json|watchlist_codes" app web/src`.

Expected: no runtime service path reads the two global files; `watchlist_codes` appears only as a response/compatibility assertion scheduled for removal, not as an authority input.

- [ ] **Step 2: Run the required project capability probes**

Run:

```powershell
.\.venv\Scripts\python.exe scripts/probe_easy_tdx_capabilities.py --codes 300476,300308,000001
.\.venv\Scripts\python.exe scripts/probe_easy_tdx_capabilities.py --codes 300476,300308,000001 --date 20260807
```

Expected: existing current and historical L1 capability checks complete without changes to the market-data boundary.

- [ ] **Step 3: Run full verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix web run build
npm --prefix web run lint
```

Expected: all commands succeed with no new warnings or failed tests.

- [ ] **Step 4: Run production-style smoke and inspect redacted status**

Run `scripts/probe_user_isolation.py` against the deployed HTTPS URL with two fresh IDs, verify `/api/health`, verify user-store backend status does not expose secrets, and confirm nginx/WebSocket HTTPS connectivity.

- [ ] **Step 5: Commit only after verification**

```powershell
git status --short
git log -5 --oneline
```

Create the final implementation commit only after all tests and smoke checks pass. Do not commit generated `web/dist` or local principal data unless the deployment script explicitly packages the build artifact.

## Spec Coverage Self-Review

- Principal model and future migration: Tasks 1, 2, 5, and 7.
- MySQL schema, JSON fallback, revision transactions: Tasks 2 and 3.
- Public market/shared cache versus user overlay: Task 4 and Task 9.
- HTTP/WS identity and channel isolation: Tasks 5 and 6.
- Server-authoritative browser migration: Task 7.
- No global fallback, observability, deployment, rollback: Task 8 and Task 10.
- Performance and accuracy acceptance: Tasks 4, 9, and 10.
- TDD red/green checkpoints: every implementation task starts with a failing test and an explicit command.

## Plan Self-Review

- No unfinished markers or unspecified “handle appropriately” steps are used.
- Repository method names and payload field names are consistent across Tasks 2–7.
- Missing identity is always empty personalization for reads and rejected for writes.
- `client_id` is never accepted from a request body and `watchlist_codes` is never a source of truth.
- Public market calculations do not depend on a principal; user overlays do not modify public market facts.
- Every task has exact files, a failing-test command, an implementation boundary, a passing-test command, and a commit boundary.

"""Dump sample JSON responses from key API endpoints for frontend rebuild reference."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

OUT = Path("data/runtime/api_samples")
OUT.mkdir(parents=True, exist_ok=True)

client = TestClient(app)


def dump(name: str, path: str, **params) -> dict | list | None:
    try:
        resp = client.get(path, params=params or None, timeout=120)
        data = resp.json()
        (OUT / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
        print(f"{name}: status={resp.status_code} bytes={len(resp.content)}")
        return data
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: FAILED {exc}")
        return None


term = dump("terminal", "/api/dashboard", view="terminal", page_size=10, sort="activity")
dump("market_state", "/api/market/state")
dump("sectors_rank", "/api/sectors/rank")
dump("opening_decision", "/api/opening/decision")
dump("capabilities", "/api/market/capabilities")
dump("messages_status", "/api/messages/status")
dump("research_status", "/api/research/status")

# pick a stock code from the terminal board for detail sampling
code = None
if isinstance(term, dict):
    stocks = ((term.get("board") or {}).get("stocks")) or term.get("stocks") or []
    if stocks:
        code = stocks[0].get("code")
print("sample code:", code)
if code:
    dump("signal_detail", f"/api/signals/{code}/detail")
    dump("signal_detail_compact", f"/api/signals/{code}/detail", compact=True)
    dump("signal_chart", f"/api/signals/{code}/detail/chart")
    dump("signal_overlay", f"/api/signals/{code}/detail/overlay")
    dump("signal_extras", f"/api/signals/{code}/detail/extras", include_capital_flow=True)
    dump("transactions", f"/api/transactions/{code}", count=240)
    dump("auction", f"/api/auction/{code}")

print("done ->", OUT)

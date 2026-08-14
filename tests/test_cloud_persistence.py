from __future__ import annotations

import json

import httpx

from app.cloud_persistence import CloudBaseNoSqlStateStore


def test_cloudbase_nosql_state_store_upserts_and_reads_json() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.method == "PATCH":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["upsert"] is True
            assert payload["data"]["$set"]["namespace"] == "settings"
            assert payload["data"]["$set"]["key"] == "watchlist"
            assert "_id" not in payload["data"]["$set"]
            return httpx.Response(200, json={"updated": 1, "matched": 1})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "_id": "settings__watchlist",
                    "namespace": "settings",
                    "key": "watchlist",
                    "encoding": "json",
                    "payload": [{"code": "300476", "name": "胜宏科技"}],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    store = CloudBaseNoSqlStateStore(
        env_id="server-d2g7x597t019f5cb0",
        token="test-token",
        collection="watchtower_state",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    store.set_json("settings", "watchlist", [{"code": "300476", "name": "胜宏科技"}])

    assert store.get_json("settings", "watchlist") == [{"code": "300476", "name": "胜宏科技"}]
    assert requests[0].method == "PATCH"
    assert "/collections/watchtower_state/documents/settings__watchlist" in str(requests[0].url)
    assert requests[1].method == "GET"


def test_cloudbase_nosql_state_store_decodes_compressed_payload() -> None:
    saved_doc: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal saved_doc
        if request.method == "PATCH":
            saved_doc = json.loads(request.content.decode("utf-8"))["data"]["$set"]
            assert saved_doc["encoding"] == "zlib+base64"
            assert saved_doc["payload"] is None
            return httpx.Response(200, json={"updated": 1, "matched": 1})
        if request.method == "GET":
            return httpx.Response(200, json=saved_doc)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    store = CloudBaseNoSqlStateStore(
        env_id="server-d2g7x597t019f5cb0",
        token="test-token",
        collection="watchtower_state",
        compress_min_bytes=16,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    value = {"quotes": [{"code": f"{index:06d}", "name": "测试", "amount": index} for index in range(20)]}

    store.set_json("latest_context", "latest", value)

    assert store.get_json("latest_context", "latest") == value


def test_cloudbase_nosql_state_store_decodes_strict_ejson_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "_id": "settings__positions",
                "encoding": "json",
                "payload": [
                    {
                        "code": "300308",
                        "quantity": {"$numberInt": "100"},
                        "available_quantity": {"$numberLong": "80"},
                        "cost": {"$numberDouble": "102.5"},
                    }
                ],
            },
        )

    store = CloudBaseNoSqlStateStore(
        env_id="server-d2g7x597t019f5cb0",
        token="token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert store.get_json("settings", "positions") == [
        {"code": "300308", "quantity": 100, "available_quantity": 80, "cost": 102.5}
    ]

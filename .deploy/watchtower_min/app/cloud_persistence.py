from __future__ import annotations

import base64
import json
import re
import time
import zlib
from typing import Any

import httpx


class CloudPersistenceError(RuntimeError):
    pass


class CloudBaseNoSqlStateStore:
    """Small server-side state store backed by CloudBase NoSQL REST API.

    This is deliberately limited to keyed JSON documents. High-frequency market
    ticks stay in the local trajectory SQLite cache; the cloud store holds the
    latest recoverable dashboard frame and small operator state.
    """

    def __init__(
        self,
        *,
        env_id: str,
        token: str,
        collection: str = "watchtower_state",
        instance: str = "(default)",
        database: str = "(default)",
        base_url: str | None = None,
        timeout: float = 3.0,
        compress_min_bytes: int = 64 * 1024,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.env_id = str(env_id or "").strip()
        self.token = str(token or "").strip()
        self.collection = str(collection or "").strip() or "watchtower_state"
        self.instance = str(instance or "").strip() or "(default)"
        self.database = str(database or "").strip() or "(default)"
        self.timeout = max(0.5, float(timeout or 3.0))
        self.compress_min_bytes = max(0, int(compress_min_bytes or 0))
        self._client = http_client
        root = str(base_url or f"https://{self.env_id}.api.tcloudbasegateway.com").rstrip("/")
        self._base = f"{root}/v1/database/instances/{self.instance}/databases/{self.database}"

    @property
    def available(self) -> bool:
        return bool(self.env_id and self.token)

    def get_json(self, namespace: str, key: str, default: Any = None) -> Any:
        if not self.available:
            return default
        response = self._request("GET", self._doc_url(namespace, key))
        if response.status_code == 404:
            return default
        if response.status_code >= 400:
            raise CloudPersistenceError(self._error_message(response))
        payload = response.json()
        return self._decode_doc(payload, default)

    def set_json(self, namespace: str, key: str, value: Any) -> None:
        if not self.available:
            return
        doc_id = self._doc_id(namespace, key)
        body = {
            "data": {
                "$set": {
                    "namespace": str(namespace),
                    "key": str(key),
                    "updated_at": int(time.time()),
                    **self._encode_payload(value),
                }
            },
            "upsert": True,
            "returnDoc": False,
        }
        response = self._request("PATCH", self._doc_url(namespace, key), json=body)
        if response.status_code >= 400:
            raise CloudPersistenceError(self._error_message(response))

    def delete_json(self, namespace: str, key: str) -> None:
        if not self.available:
            return
        response = self._request("DELETE", self._doc_url(namespace, key))
        if response.status_code not in {200, 404}:
            raise CloudPersistenceError(self._error_message(response))

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"}
        if self._client is not None:
            return self._client.request(method, url, headers=headers, **kwargs)
        with httpx.Client(timeout=self.timeout) as client:
            return client.request(method, url, headers=headers, **kwargs)

    def _doc_url(self, namespace: str, key: str) -> str:
        return f"{self._base}/collections/{self.collection}/documents/{self._doc_id(namespace, key)}"

    def _doc_id(self, namespace: str, key: str) -> str:
        raw = f"{namespace}__{key}"
        value = re.sub(r"[^0-9A-Za-z_.:-]+", "_", raw).strip("_")
        return value[:120] or "watchtower_state"

    def _encode_payload(self, value: Any) -> dict[str, Any]:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(text.encode("utf-8")) >= self.compress_min_bytes:
            packed = base64.b64encode(zlib.compress(text.encode("utf-8"), 1)).decode("ascii")
            return {"encoding": "zlib+base64", "payload": None, "payload_blob": packed}
        return {"encoding": "json", "payload": value, "payload_blob": ""}

    def _decode_doc(self, doc: dict[str, Any], default: Any = None) -> Any:
        encoding = str(doc.get("encoding") or "json")
        if encoding == "zlib+base64":
            blob = str(doc.get("payload_blob") or "")
            if not blob:
                return default
            try:
                text = zlib.decompress(base64.b64decode(blob.encode("ascii"))).decode("utf-8")
                return json.loads(text)
            except Exception as exc:
                raise CloudPersistenceError(f"failed to decode cloud payload: {exc}") from exc
        if "payload" in doc:
            return self._decode_ejson(doc.get("payload"))
        return default

    def _decode_ejson(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._decode_ejson(item) for item in value]
        if not isinstance(value, dict):
            return value
        if len(value) == 1:
            if "$numberInt" in value:
                return int(value["$numberInt"])
            if "$numberLong" in value:
                return int(value["$numberLong"])
            if "$numberDouble" in value:
                raw = value["$numberDouble"]
                return float(raw) if raw not in {"NaN", "Infinity", "-Infinity"} else raw
            if "$numberDecimal" in value:
                return float(value["$numberDecimal"])
            if "$oid" in value:
                return str(value["$oid"])
        if "$date" in value and len(value) == 1:
            raw = value["$date"]
            if isinstance(raw, dict) and "$numberLong" in raw:
                return int(raw["$numberLong"])
            return raw
        return {key: self._decode_ejson(item) for key, item in value.items()}

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        return f"CloudBase NoSQL request failed: {response.status_code} {payload}"

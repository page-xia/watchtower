"""One-off: call Tencent Cloud TCB API with tcb CLI temp credentials.

Usage: .\.venv\Scripts\python.exe scripts/tcb_api_call.py <Action> '<json-params>'
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import urllib.request

AUTH_FILE = Path.home() / ".config" / ".cloudbase" / "auth.json"
HOST = os.environ.get("TCB_API_HOST", "tcb.tencentcloudapi.com")
SERVICE = os.environ.get("TCB_API_SERVICE", "tcb")
VERSION = os.environ.get("TCB_API_VERSION", "2018-06-08")
REGION = os.environ.get("TCB_API_REGION", "ap-shanghai")


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def call(action: str, params: dict) -> dict:
    cred = json.loads(AUTH_FILE.read_text(encoding="utf-8"))["credential"]
    secret_id = cred["tmpSecretId"]
    secret_key = cred["tmpSecretKey"]
    token = cred["tmpToken"]

    payload = json.dumps(params)
    timestamp = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))

    canonical_headers = f"content-type:application/json\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"

    credential_scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = (
        "TC3-HMAC-SHA256\n"
        f"{timestamp}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, SERVICE)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    req = urllib.request.Request(
        f"https://{HOST}/",
        data=payload.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Host": HOST,
            "Authorization": authorization,
            "X-TC-Action": action,
            "X-TC-Version": VERSION,
            "X-TC-Region": REGION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    action = sys.argv[1]
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    result = call(action, params)
    print(json.dumps(result, ensure_ascii=False, indent=1)[:4000])

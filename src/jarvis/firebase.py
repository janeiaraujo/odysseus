"""Minimal Firestore and FCM client over REST. Optional: nothing requires it.

Authenticates with a service-account JSON kept outside the repo, or talks to the
official emulator when FIRESTORE_EMULATOR_HOST is set. No extra dependency:
token signing uses `cryptography`, already required by the project.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.jarvis import config

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/datastore https://www.googleapis.com/auth/firebase.messaging"


class FirebaseNotConfigured(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def to_value(value: Any) -> Dict[str, Any]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: to_value(v) for k, v in value.items()}}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [to_value(v) for v in value]}}
    return {"stringValue": str(value)}


def from_value(value: Dict[str, Any]) -> Any:
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return value["booleanValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "mapValue" in value:
        return {k: from_value(v) for k, v in value["mapValue"].get("fields", {}).items()}
    if "arrayValue" in value:
        return [from_value(v) for v in value["arrayValue"].get("values", [])]
    return next(iter(value.values()), None)


class FirestoreClient:
    def __init__(self, project_id: str, *, credentials: Optional[Dict[str, Any]] = None, emulator_host: Optional[str] = None, transport: Any = None):
        self.project_id, self._credentials, self._transport = project_id, credentials, transport
        self._emulator = emulator_host
        base = f"http://{emulator_host}" if emulator_host else "https://firestore.googleapis.com"
        self.base = f"{base}/v1/projects/{project_id}/databases/(default)/documents"
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    @classmethod
    def from_config(cls) -> "FirestoreClient":
        fb = config.get("firebase", {}) or {}
        emulator = os.environ.get("FIRESTORE_EMULATOR_HOST")
        project = fb.get("project_id") or os.environ.get("JARVIS_FIREBASE_PROJECT", "")
        if emulator:
            return cls(project or "demo-jarvis", emulator_host=emulator)
        path = fb.get("credentials_file") or os.environ.get("JARVIS_FIREBASE_CREDENTIALS", "")
        if not path or not os.path.isfile(path):
            raise FirebaseNotConfigured("arquivo de conta de servico do Firebase não configurado")
        with open(path, encoding="utf-8") as f:
            credentials = json.load(f)
        return cls(project or credentials.get("project_id", ""), credentials=credentials)

    async def _auth_headers(self, client: httpx.AsyncClient) -> Dict[str, str]:
        if self._emulator:
            return {"Authorization": "Bearer owner"}
        if self._token and time.time() < self._token_expiry - 60:
            return {"Authorization": f"Bearer {self._token}"}
        if not self._credentials:
            raise FirebaseNotConfigured("sem credenciais")
        now = int(time.time())
        claims = {"iss": self._credentials["client_email"], "scope": SCOPES, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}
        signing_input = f"{_b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())}.{_b64(json.dumps(claims).encode())}"
        key = serialization.load_pem_private_key(self._credentials["private_key"].encode(), password=None)
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        resp = await client.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{signing_input}.{_b64(signature)}"})
        resp.raise_for_status()
        body = resp.json()
        self._token, self._token_expiry = body["access_token"], now + int(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
            headers = await self._auth_headers(client)
            return await client.request(method, url, headers=headers, **kwargs)

    async def upsert(self, path: str, fields: Dict[str, Any]) -> None:
        resp = await self._request("PATCH", f"{self.base}/{path}", json={"fields": {k: to_value(v) for k, v in fields.items()}})
        resp.raise_for_status()

    async def get(self, path: str) -> Optional[Dict[str, Any]]:
        resp = await self._request("GET", f"{self.base}/{path}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return {k: from_value(v) for k, v in resp.json().get("fields", {}).items()}

    async def delete(self, path: str) -> None:
        resp = await self._request("DELETE", f"{self.base}/{path}")
        if resp.status_code != 404:
            resp.raise_for_status()

    async def list(self, collection_path: str, page_size: int = 300) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        token = ""
        while True:
            params: Dict[str, Any] = {"pageSize": page_size}
            if token:
                params["pageToken"] = token
            resp = await self._request("GET", f"{self.base}/{collection_path}", params=params)
            if resp.status_code == 404:
                return out
            resp.raise_for_status()
            body = resp.json()
            for doc in body.get("documents", []):
                out.append({"id": doc["name"].rsplit("/", 1)[-1], **{k: from_value(v) for k, v in doc.get("fields", {}).items()}})
            token = body.get("nextPageToken", "")
            if not token:
                return out

    async def send_push(self, device_token: str, title: str, body: str) -> None:
        """Firebase Cloud Messaging HTTP v1."""
        if self._emulator:
            raise FirebaseNotConfigured("FCM não existe no emulador")
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        resp = await self._request("POST", url, json={"message": {"token": device_token, "notification": {"title": title, "body": body}}})
        resp.raise_for_status()


async def check_connection(client: Optional[FirestoreClient] = None) -> Dict[str, Any]:
    """Real round trip against Firestore: write, read back, delete."""
    client = client or FirestoreClient.from_config()
    path = f"_jarvis/healthcheck-{int(time.time())}"
    await client.upsert(path, {"ok": True})
    read = await client.get(path)
    await client.delete(path)
    return {"ok": bool(read and read.get("ok")), "project": client.project_id, "deleted": await client.get(path) is None}

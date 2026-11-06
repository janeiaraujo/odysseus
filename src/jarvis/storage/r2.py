"""Cloudflare R2 over its S3-compatible API, using AWS Signature V4 and httpx.

No boto3: this single small client keeps the install light. Credentials come from
the environment and should be an R2 API token scoped to one bucket.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

from src.jarvis.storage.base import IntegrityError, ObjectInfo, StorageBackend, StorageError, sha256_file, validate_key

_EMPTY_HASH = hashlib.sha256(b"").hexdigest()
_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def sign_v4(method: str, url: str, headers: Dict[str, str], payload_hash: str, access_key: str, secret_key: str,
            region: str = "auto", service: str = "s3", now: Optional[dt.datetime] = None) -> Dict[str, str]:
    """Return headers including Authorization, per AWS SigV4."""
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    parsed = urlparse(url)
    signed = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    signed["host"] = parsed.netloc
    signed["x-amz-content-sha256"] = payload_hash
    signed["x-amz-date"] = amz_date
    names = sorted(signed)
    query = "&".join(sorted(parsed.query.split("&"))) if parsed.query else ""
    canonical = "\n".join([method, parsed.path or "/", query, "".join(f"{n}:{signed[n]}\n" for n in names), ";".join(names), payload_hash])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = f"AWS4{secret_key}".encode()
    for part in (datestamp, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = dict(headers)
    out.update({"x-amz-content-sha256": payload_hash, "x-amz-date": amz_date,
                "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"})
    return out


class R2Backend(StorageBackend):
    name = "r2"
    enforce_quota = True

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, *, transport: Any = None, timeout: float = 120):
        self.endpoint, self.bucket = endpoint.rstrip("/"), bucket
        self._access, self._secret = access_key, secret_key
        self._client = httpx.Client(timeout=timeout, transport=transport)

    @classmethod
    def from_environment(cls) -> "R2Backend":
        env = os.environ
        account = env.get("JARVIS_R2_ACCOUNT_ID", "")
        endpoint = env.get("JARVIS_R2_ENDPOINT") or (f"https://{account}.r2.cloudflarestorage.com" if account else "")
        bucket, access, secret = env.get("JARVIS_R2_BUCKET", ""), env.get("JARVIS_R2_ACCESS_KEY_ID", ""), env.get("JARVIS_R2_SECRET_ACCESS_KEY", "")
        missing = [n for n, v in (("JARVIS_R2_ACCOUNT_ID", endpoint), ("JARVIS_R2_BUCKET", bucket), ("JARVIS_R2_ACCESS_KEY_ID", access), ("JARVIS_R2_SECRET_ACCESS_KEY", secret)) if not v]
        if missing:
            raise StorageError("R2 não configurado; faltam: " + ", ".join(missing))
        return cls(endpoint, bucket, access, secret)

    def _url(self, key: str = "", query: str = "") -> str:
        path = f"/{self.bucket}" + (f"/{quote(key, safe='/-_.~')}" if key else "")
        return f"{self.endpoint}{path}" + (f"?{query}" if query else "")

    def _request(self, method: str, url: str, *, content: Any = None, payload_hash: str = _EMPTY_HASH,
                 headers: Optional[Dict[str, str]] = None, ok: Tuple[int, ...] = (200,)) -> httpx.Response:
        signed = sign_v4(method, url, headers or {}, payload_hash, self._access, self._secret)
        try:
            resp = self._client.request(method, url, content=content, headers=signed)
        except httpx.HTTPError as exc:
            raise StorageError(f"falha de rede no R2: {type(exc).__name__}") from None
        if resp.status_code not in ok:
            raise StorageError(f"R2 respondeu {resp.status_code} em {method}")
        return resp

    def put_bytes(self, key: str, data: bytes, metadata: Optional[Dict[str, str]] = None) -> ObjectInfo:
        validate_key(key)
        self._check_quota("A")
        digest = hashlib.sha256(data).hexdigest()
        headers = {"x-amz-meta-sha256": digest, **{f"x-amz-meta-{k.lower()}": str(v) for k, v in (metadata or {}).items()}}
        try:
            self._request("PUT", self._url(key), content=data, payload_hash=digest, headers=headers)
        except StorageError:
            self._meter("put", "A", len(data), ok=False)
            raise
        self._meter("put", "A", len(data))
        return ObjectInfo(key=key, size=len(data), sha256=digest, metadata=metadata or {})

    def put_file(self, key: str, path: Path) -> Dict[str, Any]:
        """Streamed upload with the checksum verified by the server."""
        validate_key(key)
        self._check_quota("A")
        path = Path(path)
        digest, size = sha256_file(path), path.stat().st_size
        with open(path, "rb") as f:
            self._request("PUT", self._url(key), content=f, payload_hash=digest,
                          headers={"x-amz-meta-sha256": digest, "content-length": str(size)})
        self._meter("put", "A", size)
        info = self.head(key)
        if info is None or info.size != size:
            raise IntegrityError(f"tamanho divergente após upload: {key}")
        return {"key": key, "size": size, "sha256": digest}

    def get_bytes(self, key: str) -> bytes:
        validate_key(key)
        self._check_quota("B")
        resp = self._request("GET", self._url(key))
        data = resp.content
        self._meter("get", "B", len(data))
        expected = resp.headers.get("x-amz-meta-sha256")
        if expected and expected != hashlib.sha256(data).hexdigest():
            raise IntegrityError(f"checksum divergente: {key}")
        return data

    def head(self, key: str) -> Optional[ObjectInfo]:
        validate_key(key)
        resp = self._request("HEAD", self._url(key), ok=(200, 404))
        self._meter("head", "B")
        if resp.status_code == 404:
            return None
        meta = {k[len("x-amz-meta-"):]: v for k, v in resp.headers.items() if k.lower().startswith("x-amz-meta-")}
        return ObjectInfo(key=key, size=int(resp.headers.get("content-length", 0)), sha256=meta.pop("sha256", ""),
                          last_modified=resp.headers.get("last-modified", ""), metadata=meta)

    def delete(self, key: str) -> None:
        validate_key(key)
        self._request("DELETE", self._url(key), ok=(200, 204, 404))
        self._meter("delete", "free")

    def list(self, prefix: str = "", limit: int = 100000) -> List[ObjectInfo]:
        out: List[ObjectInfo] = []
        token = ""
        while len(out) < limit:
            self._check_quota("A")
            query = "list-type=2&max-keys=1000" + (f"&prefix={quote(prefix, safe='')}" if prefix else "")
            if token:
                query += f"&continuation-token={quote(token, safe='')}"
            root = ET.fromstring(self._request("GET", self._url(query=query)).text)
            self._meter("list", "A")
            for item in root.findall("s3:Contents", _NS):
                out.append(ObjectInfo(key=item.findtext("s3:Key", "", _NS), size=int(item.findtext("s3:Size", "0", _NS)),
                                      last_modified=item.findtext("s3:LastModified", "", _NS)))
            token = root.findtext("s3:NextContinuationToken", "", _NS)
            if not token:
                break
        return out[:limit]

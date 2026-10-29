"""Cloudflare R2 object storage for the web layer, on the standard library.

R2 speaks the S3 API, so every request is signed with AWS Signature Version 4 (region
"auto", service "s3") and sent with urllib. accounts.py keeps uploaded profiles and Pro
build PDFs here when R2 is configured, so the hosted app on Vercel and the local Studio
read and write the same files. The engine (pipeline.py, jobboard.py) never imports this.

    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET   (all four, or none)
    R2_ENDPOINT   (optional: a jurisdiction endpoint, https://<account>.eu.r2.cloudflarestorage.com)

Object keys put everything an account owns under users/<id>/, so deleting an account
deletes one prefix. Error messages name the method, key and status only: response bodies
and credentials are never included.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Mapping

REGION = "auto"
SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
TIMEOUT = 30
MAX_GET_BYTES = 50_000_000
PRESIGN_MAX_SECONDS = 7 * 24 * 3600
ENV_KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class StorageError(Exception):
    """An R2 request failed; `status` is the HTTP status (0 for a network error)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _uri_encode(text: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(text, safe=safe)


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str = REGION, service: str = SERVICE) -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    return _hmac(_hmac(_hmac(k_date, region), service), "aws4_request")


def _canonical_query(query: Mapping[str, str]) -> str:
    return "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items()))


def _signature(method: str, url: str, query: Mapping[str, str], headers: Mapping[str, str],
               payload_hash: str, secret_key: str, now: dt.datetime, region: str,
               service: str) -> tuple[str, str]:
    """(signature, signed header list) for one request; `url` carries no query string."""
    parts = urllib.parse.urlsplit(url)
    canonical_headers = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    canonical_headers["host"] = parts.netloc
    signed = ";".join(sorted(canonical_headers))
    canonical = "\n".join([
        method,
        _uri_encode(urllib.parse.unquote(parts.path) or "/", safe="-_.~/"),
        _canonical_query(query),
        "".join(f"{k}:{canonical_headers[k]}\n" for k in sorted(canonical_headers)),
        signed,
        payload_hash])
    date = now.strftime("%Y%m%d")
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, now.strftime("%Y%m%dT%H%M%SZ"), scope,
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = signing_key(secret_key, date, region, service)
    return hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest(), signed


def sign_headers(method: str, url: str, headers: Mapping[str, str], payload_hash: str,
                 access_key: str, secret_key: str, now: dt.datetime,
                 query: Mapping[str, str] | None = None, region: str = REGION,
                 service: str = SERVICE) -> dict[str, str]:
    """The request headers plus x-amz-date, x-amz-content-sha256 and Authorization."""
    out = {**headers, "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
           "x-amz-content-sha256": payload_hash}
    signature, signed = _signature(method, url, query or {}, out, payload_hash, secret_key, now,
                                   region, service)
    scope = f"{now.strftime('%Y%m%d')}/{region}/{service}/aws4_request"
    out["Authorization"] = (f"{ALGORITHM} Credential={access_key}/{scope}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out


def presign_url(method: str, url: str, access_key: str, secret_key: str, now: dt.datetime,
                expires: int, query: Mapping[str, str] | None = None, region: str = REGION,
                service: str = SERVICE) -> str:
    """A query-string-signed URL that works without credentials until it expires."""
    if not 1 <= expires <= PRESIGN_MAX_SECONDS:
        raise ValueError("A presigned URL lasts 1 second to 7 days")
    scope = f"{now.strftime('%Y%m%d')}/{region}/{service}/aws4_request"
    params = {**(query or {}), "X-Amz-Algorithm": ALGORITHM,
              "X-Amz-Credential": f"{access_key}/{scope}",
              "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ"), "X-Amz-Expires": str(expires),
              "X-Amz-SignedHeaders": "host"}
    signature, _signed = _signature(method, url, params, {}, UNSIGNED_PAYLOAD, secret_key, now,
                                    region, service)
    return f"{url}?{_canonical_query(params)}&X-Amz-Signature={signature}"


class R2:
    """One bucket, addressed path-style: https://<account>.r2.cloudflarestorage.com/<bucket>/<key>."""

    def __init__(self, account_id: str, access_key_id: str, secret_access_key: str, bucket: str,
                 endpoint: str | None = None, opener: Callable[..., Any] = urllib.request.urlopen,
                 clock: Callable[[], dt.datetime] | None = None) -> None:
        self.endpoint = (endpoint or f"https://{account_id}.r2.cloudflarestorage.com").rstrip("/")
        self.bucket = bucket
        self._access_key = access_key_id
        self._secret_key = secret_access_key
        self._opener = opener
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def __repr__(self) -> str:  # never print the credentials
        return f"R2(bucket={self.bucket!r}, endpoint={self.endpoint!r})"

    def url(self, key: str = "") -> str:
        return f"{self.endpoint}/{_uri_encode(self.bucket)}/{_uri_encode(key, safe='-_.~/')}"

    def _request(self, method: str, key: str, body: bytes = b"",
                 headers: Mapping[str, str] | None = None,
                 query: Mapping[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        url = self.url(key)
        signed = sign_headers(method, url, dict(headers or {}), hashlib.sha256(body).hexdigest(),
                              self._access_key, self._secret_key, self._clock(), query)
        full = f"{url}?{_canonical_query(query)}" if query else url
        request = urllib.request.Request(full, data=body if method in ("PUT", "POST") else None,
                                         headers=signed, method=method)
        try:
            with self._opener(request, timeout=TIMEOUT) as response:
                data = response.read(MAX_GET_BYTES + 1)
                if len(data) > MAX_GET_BYTES:
                    raise StorageError(f"R2 {method} {key}: response larger than "
                                       f"{MAX_GET_BYTES} bytes")
                return response.status, dict(response.headers.items()), data
        except urllib.error.HTTPError as exc:
            exc.close()  # the error body is never read or logged
            raise StorageError(f"R2 {method} {key}: HTTP {exc.code}", exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise StorageError(f"R2 {method} {key}: {type(exc).__name__}") from None

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._request("PUT", key, data, {"content-type": content_type})

    def get(self, key: str) -> bytes:
        return self._request("GET", key)[2]

    def delete(self, key: str) -> None:
        try:
            self._request("DELETE", key)
        except StorageError as exc:
            if exc.status != 404:  # already gone is fine
                raise

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            root = ET.fromstring(self._request("GET", "", query=query)[2])
            keys += [node.text or "" for node in root.iter(f"{S3_NS}Key")]
            token = root.findtext(f"{S3_NS}NextContinuationToken")
            if root.findtext(f"{S3_NS}IsTruncated") != "true" or not token:
                return keys

    def delete_prefix(self, prefix: str) -> int:
        if not prefix.endswith("/"):
            raise ValueError("Only a folder prefix ending in '/' can be deleted")
        keys = self.list(prefix)
        for key in keys:
            self.delete(key)
        return len(keys)

    def presign_get(self, key: str, expires: int = 300,
                    query: Mapping[str, str] | None = None) -> str:
        return presign_url("GET", self.url(key), self._access_key, self._secret_key,
                           self._clock(), expires, query)


def from_env(env: Mapping[str, str] | None = None, **kwargs: Any) -> R2 | None:
    """The configured bucket, or None when no R2_* variable is set."""
    env = os.environ if env is None else env
    values = {name: (env.get(name) or "").strip() for name in ENV_KEYS}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise StorageError(f"R2 is partly configured; set {', '.join(missing)} too (or none "
                           f"of {', '.join(ENV_KEYS)})")
    kwargs.setdefault("endpoint", (env.get("R2_ENDPOINT") or "").strip() or None)
    return R2(values["R2_ACCOUNT_ID"], values["R2_ACCESS_KEY_ID"],
              values["R2_SECRET_ACCESS_KEY"], values["R2_BUCKET"], **kwargs)

"""업비트 Open API 인증 토큰 생성.

PyJWT 의존성을 두지 않고 표준 라이브러리(hmac/hashlib/base64)만으로
HS512 JWT를 직접 서명한다. 배포 환경에서 cryptography 빌드 이슈를
피하기 위한 선택이며, 업비트가 요구하는 형식과 동일하다.

업비트 규격:
  header  : {"alg": "HS512", "typ": "JWT"}
  payload : access_key, nonce, (query_hash, query_hash_alg="SHA512")
  header  : Authorization: Bearer <token>

query_hash는 URL 인코딩되지 않은 쿼리 문자열의 SHA512 hexdigest다.
POST는 같은 파라미터를 JSON으로 전송한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from typing import Any, Mapping
from urllib.parse import urlencode, unquote


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def encode_query(params: Mapping[str, Any] | None) -> str:
    """업비트가 기대하는 형태로 쿼리 문자열을 만든다.

    리스트 값(예: markets=['KRW-BTC','KRW-ETH'])은 doseq 로 반복 전개한다.
    uuids/identifiers 배열 파라미터도 같은 규칙을 따른다.
    """
    if not params:
        return ""
    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                flat.append((f"{key}[]", str(item)))
        else:
            flat.append((key, str(value)))
    return urlencode(flat)


def make_jwt(access_key: str, secret_key: str, params: Mapping[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"access_key": access_key, "nonce": str(uuid.uuid4())}
    query_string = unquote(encode_query(params))
    if query_string:
        payload["query_hash"] = hashlib.sha512(query_string.encode("utf-8")).hexdigest()
        payload["query_hash_alg"] = "SHA512"

    header = {"alg": "HS512", "typ": "JWT"}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()),
        _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha512).digest()
    segments.append(_b64url(signature))
    return ".".join(segments)


def auth_header(access_key: str, secret_key: str, params: Mapping[str, Any] | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(access_key, secret_key, params)}"}

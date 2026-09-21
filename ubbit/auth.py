"""업비트 Open API 인증 토큰 생성.

PyJWT 의존성을 두지 않고 표준 라이브러리(hmac/hashlib/base64)만으로
HS256 JWT를 직접 서명한다. 배포 환경에서 cryptography 빌드 이슈를
피하기 위한 선택이며, 업비트가 요구하는 형식과 동일하다.

업비트 규격:
  header  : {"alg": "HS256", "typ": "JWT"}
  payload : access_key, nonce, (query_hash, query_hash_alg="SHA512")
  header  : Authorization: Bearer <token>

query_hash 는 "실제로 전송하는 쿼리 문자열"의 SHA512 hexdigest 여야 한다.
따라서 클라이언트는 여기서 만든 쿼리 문자열을 그대로 요청에 사용해야 한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from typing import Any, Mapping
from urllib.parse import urlencode


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
    query_string = encode_query(params)
    if query_string:
        payload["query_hash"] = hashlib.sha512(query_string.encode("utf-8")).hexdigest()
        payload["query_hash_alg"] = "SHA512"

    header = {"alg": "HS256", "typ": "JWT"}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()),
        _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    segments.append(_b64url(signature))
    return ".".join(segments)


def auth_header(access_key: str, secret_key: str, params: Mapping[str, Any] | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(access_key, secret_key, params)}"}

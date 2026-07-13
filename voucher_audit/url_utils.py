from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_openai_base_url(base_url: str) -> str:
    b = (base_url or "").strip()
    if not b:
        return ""
    u = urlparse(b)
    path = (u.path or "/").rstrip("/")
    if path.endswith("/v1"):
        path += "/"
    else:
        path = path + "/v1/"
    if not path.startswith("/"):
        path = "/" + path
    return urlunparse(u._replace(path=path))

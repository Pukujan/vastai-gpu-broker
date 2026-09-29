"""Read-only, unauthenticated Hugging Face Hub metadata client.

Uses public Hub model search/info/resolve endpoints. File metadata is fetched
through Hub metadata only; weight/shard bodies are never downloaded. Small
README/config/index files are captured with SHA-256 digests for evidence review.
Transport can be injected for offline tests.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class HubRetrievalError(Exception):
    """Sanitized public retrieval failure; intentionally omits response bodies."""


class HuggingFaceHubClient:
    def __init__(self, *, transport: Callable[[str, float, int], bytes] | None = None,
                 timeout: float = 10.0, max_capture_bytes: int = 1_000_000,
                 max_results: int = 100, endpoint: str = "https://huggingface.co"):
        if timeout <= 0 or max_capture_bytes <= 0 or not 1 <= max_results <= 1000:
            raise ValueError("invalid Hub client bounds")
        self.timeout = timeout
        self.max_capture_bytes = max_capture_bytes
        self.max_results = max_results
        self.endpoint = endpoint.rstrip("/")
        self._transport = transport or self._http_get

    @staticmethod
    def _http_get(url: str, timeout: float, limit: int) -> bytes:
        request = Request(url, headers={"Accept": "application/json, text/plain;q=0.9", "User-Agent": "vastai-gpu-broker/0.1"})
        with urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise HubRetrievalError("response exceeds configured metadata bound")
        return data

    def _get(self, url: str, *, limit: int | None = None) -> bytes:
        try:
            data = self._transport(url, self.timeout, limit or self.max_capture_bytes)
            if not isinstance(data, bytes):
                raise HubRetrievalError("transport returned a non-byte response")
            if len(data) > (limit or self.max_capture_bytes):
                raise HubRetrievalError("response exceeds configured metadata bound")
            return data
        except HubRetrievalError:
            raise
        except HTTPError as exc:
            raise HubRetrievalError(f"Hub request failed with HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            kind = "timeout" if isinstance(exc, TimeoutError) else "network or response error"
            raise HubRetrievalError(f"Hub retrieval failed: {kind}") from None
        except Exception:
            raise HubRetrievalError("Hub retrieval failed: transport error") from None

    def _json(self, url: str) -> Any:
        try:
            return json.loads(self._get(url, limit=4_000_000))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HubRetrievalError("Hub returned invalid JSON") from None

    def _info(self, repo_id: str) -> dict[str, Any]:
        # `blobs=true` returns per-file size and LFS metadata without file bodies.
        url = f"{self.endpoint}/api/models/{quote(repo_id, safe='/')}?blobs=true"
        info = self._json(url)
        if not isinstance(info, Mapping):
            raise HubRetrievalError("Hub returned an invalid model record")
        return dict(info)

    def _capture(self, repo_id: str, revision: str, path: str, role: str) -> dict[str, Any] | None:
        # Only small text/config indexes are fetched. No weight, tokenizer, or
        # arbitrary repository file is read as executable content.
        if path.lower() != "readme.md" and not path.lower().endswith(("config.json", ".index.json")):
            return None
        url = f"{self.endpoint}/{quote(repo_id, safe='/')}/resolve/{quote(revision, safe='')}/{quote(path, safe='/')}"
        try:
            raw = self._get(url, limit=self.max_capture_bytes)
            content = raw.decode("utf-8")
        except HubRetrievalError as exc:
            if "HTTP 404" not in str(exc):
                raise
            # Optional source files can be absent; retrieval failures stay errors.
            return {"source_id": f"hf:{repo_id}@{revision}:{path}", "url": url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "role": role, "available": False, "content_digest": None, "content": None}
        except UnicodeDecodeError:
            raise HubRetrievalError("Hub text capture is not valid UTF-8") from None
        return {"source_id": f"hf:{repo_id}@{revision}:{path}", "url": url,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "role": role, "available": True, "content_digest": "sha256:" + sha256(raw).hexdigest(),
                "content": content}

    def _hydrate(self, record: Mapping[str, Any]) -> dict[str, Any]:
        repo_id = record.get("id")
        revision = record.get("sha")
        if not isinstance(repo_id, str) or not re.fullmatch(r"[^/]+/[^/]+", repo_id) or not isinstance(revision, str):
            raise HubRetrievalError("Hub result has no exact repository revision")
        files = record.get("siblings", [])
        captures = []
        for f in files:
            if not isinstance(f, Mapping):
                continue
            path = f.get("rfilename")
            if not isinstance(path, str):
                continue
            role = "publisher_model_card" if path.lower() == "readme.md" else "model_config_or_manifest"
            captured = self._capture(repo_id, revision, path, role)
            if captured:
                captures.append(captured)
        metadata_content = json.dumps(dict(record), sort_keys=True, separators=(",", ":"), default=str)
        metadata_bytes = metadata_content.encode()
        return {**dict(record), "captures": captures,
                "metadata_source": {"source_id": f"hf-api:{repo_id}@{revision}",
                                    "url": f"{self.endpoint}/api/models/{repo_id}?blobs=true",
                                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                    "retrieved": True, "revision": revision,
                                    "content_digest": "sha256:" + sha256(metadata_bytes).hexdigest(),
                                    "content": metadata_content,
                                    "role": "huggingface_hub_model_info"}}

    def fetch(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """Adapt a route query to Hub records compatible with `resolve_request`."""
        try:
            exact = query.get("repository") if query.get("kind") == "exact_repository" else None
            if exact:
                info = self._info(str(exact))
                records = [info]
            else:
                search = query.get("base_id") or query.get("query") or ""
                if not str(search).strip():
                    return {"records": [], "error": "empty_search_query"}
                params = urlencode({"search": str(search), "limit": self.max_results, "full": "true"})
                listing = self._json(f"{self.endpoint}/api/models?{params}")
                if not isinstance(listing, list):
                    raise HubRetrievalError("Hub search returned an invalid model list")
                # Hydrate exact revisions for their files/card metadata. The list
                # limit is explicit; candidate discovery never claims completeness.
                records = []
                for item in listing[:self.max_results]:
                    if isinstance(item, Mapping) and item.get("id"):
                        records.append(self._info(str(item["id"])))
            return {"records": [self._hydrate(r) for r in records],
                    "retrieval": {"source": "huggingface_hub", "result_limit": self.max_results,
                                  "potentially_truncated": len(records) >= self.max_results}}
        except HubRetrievalError as exc:
            return {"records": [], "error": str(exc), "error_code": "SOURCE_RETRIEVAL_FAILED"}

    __call__ = fetch

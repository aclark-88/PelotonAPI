"""OpenAI embeddings client — text-embedding-3-small, 1536-dim, batches of 100."""

from __future__ import annotations

import os
from uuid import UUID

import httpx
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gtm.db.repositories.runs import RunsRepo

MODEL = "text-embedding-3-small"
URL = "https://api.openai.com/v1/embeddings"
BATCH = 100

_retry = retry(
    retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(4),
    reraise=True,
)


class Embedder:
    SOURCE = "embedder"

    def __init__(self, runs_repo: RunsRepo | None = None, api_key: str | None = None) -> None:
        load_dotenv(encoding="utf-8-sig")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY missing from environment/.env")
        self.runs = runs_repo or RunsRepo()
        self.current_run_id: UUID | None = None
        self._http = httpx.Client(
            timeout=60, headers={"Authorization": f"Bearer {self.api_key}"}
        )

    @_retry
    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = self._http.post(URL, json={"model": MODEL, "input": texts})
        resp.raise_for_status()
        data = resp.json()
        try:
            self.runs.archive_payload(
                source=self.SOURCE,
                response={"model": data.get("model"), "usage": data.get("usage"),
                          "count": len(texts)},  # vectors omitted: bulky, derivable
                request={"op": "embed", "texts": texts},
                source_run_id=self.current_run_id,
            )
        except Exception:
            pass
        return [item["embedding"] for item in sorted(data["data"], key=lambda d: d["index"])]

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            vectors.extend(self._embed_batch(texts[i : i + BATCH]))
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

"""LLM seam for drafting and reasoning.

TWO implementations, picked by how the system is being run:

1. InjectedLLM (DEFAULT — orchestrator mode). Skills run inside Claude Code,
   so the orchestrating Claude IS the model. The orchestrator calls the
   skill's prepare_prompt() helper, writes the copy itself, and injects the
   output here. No API key, no second model, no extra spend. The skill's
   validator still gates everything regardless of who generated it.

2. LLMClient (headless fallback). Direct Anthropic API for unattended cron
   runs with no Claude session attached. Requires ANTHROPIC_API_KEY; entirely
   optional — drafting is human-reviewed anyway, so it can simply wait for
   the next orchestrated session instead.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gtm.db.repositories.runs import RunsRepo

DEFAULT_MODEL = "claude-sonnet-4-6"


class InjectedLLM:
    """The orchestrating Claude's pre-written output, behind the LLM seam.

    Queue semantics: each complete() pops the next response; the last one
    repeats if the skill asks again (e.g. a validation-retry round-trip the
    orchestrator chose not to pre-author).
    """

    def __init__(self, responses: list[str], model_label: str = "claude-code-orchestrator") -> None:
        if not responses:
            raise ValueError("InjectedLLM needs at least one response")
        self.responses = list(responses)
        self.model_label = model_label
        self.calls: list[dict[str, str]] = []
        self.current_run_id: UUID | None = None

    def complete(self, system: str, user: str, **kwargs: Any) -> str:
        self.calls.append({"system": system, "user": user})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def usage_snapshot(self) -> dict[str, int]:
        # token accounting lives with the orchestrating session, not here
        return {"input_tokens": 0, "output_tokens": 0}


class LLMClient:
    SOURCE = "llm"

    def __init__(
        self,
        runs_repo: RunsRepo | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        load_dotenv(encoding="utf-8-sig")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY missing from environment/.env")
        import anthropic  # deferred import

        self._client = anthropic.Anthropic(api_key=self.api_key)
        self._anthropic = anthropic
        self.model = model
        self.runs = runs_repo or RunsRepo()
        self.current_run_id: UUID | None = None
        self.input_tokens = 0
        self.output_tokens = 0

    def complete(
        self,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.7,
    ) -> str:
        retrying = retry(
            retry=retry_if_exception_type(
                (self._anthropic.APIConnectionError, self._anthropic.RateLimitError,
                 self._anthropic.InternalServerError)
            ),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            stop=stop_after_attempt(4),
            reraise=True,
        )

        @retrying
        def _call() -> Any:
            return self._client.messages.create(
                model=model or self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )

        response = _call()
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        try:
            self.runs.archive_payload(
                source=self.SOURCE,
                response={
                    "text": text,
                    "model": response.model,
                    "usage": {
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    },
                },
                request={"op": "complete", "system": system, "user": user,
                         "model": model or self.model},
                source_run_id=self.current_run_id,
            )
        except Exception:
            pass
        return text

    def usage_snapshot(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

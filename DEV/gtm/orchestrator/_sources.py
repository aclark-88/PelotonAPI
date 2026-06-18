"""Build the SourceBundle from whatever credentials exist.

Each source constructs only if its credential is present; missing ones stay
None and skills degrade per their contracts. The LLM slot stays None in
headless runs — drafting is queued for an orchestrated Claude Code session
(see [[orchestrator-is-the-llm]] convention) unless ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

from typing import Any, Callable

from gtm.skills._shared.sources import SourceBundle


def _try(factory: Callable[[], Any], name: str, missing: list[str]) -> Any | None:
    try:
        return factory()
    except Exception as exc:
        missing.append(f"{name}: {exc}")
        return None


def build_sources() -> tuple[SourceBundle, list[str]]:
    missing: list[str] = []

    def edgar():
        from gtm.skills._shared.edgar import EdgarSource
        return EdgarSource()

    def apollo():
        from gtm.skills._shared.apollo import ApolloClient
        return ApolloClient()

    def hubspot():
        from gtm.skills._shared.hubspot import HubSpotClient
        return HubSpotClient()

    def heyreach():
        from gtm.skills._shared.heyreach import HeyReachClient
        return HeyReachClient()

    def web():
        from gtm.skills._shared.web_search import TavilySearch
        return TavilySearch()

    def embedder():
        from gtm.skills._shared.embedder import Embedder
        return Embedder()

    def llm():
        from gtm.skills._shared.llm import LLMClient
        return LLMClient()

    def slack():
        from gtm.skills._shared.slack import SlackNotifier
        return SlackNotifier()

    bundle = SourceBundle(
        edgar=_try(edgar, "edgar", missing),
        apollo=_try(apollo, "apollo", missing),
        hubspot=_try(hubspot, "hubspot", missing),
        heyreach=_try(heyreach, "heyreach", missing),
        web=_try(web, "web", missing),
        embedder=_try(embedder, "embedder", missing),
        llm=_try(llm, "llm", missing),
        slack=_try(slack, "slack", missing),
    )
    return bundle, missing


def notify(bundle: SourceBundle, text: str) -> None:
    """Best-effort Slack post; silent when unconfigured."""
    if bundle.slack is not None:
        try:
            bundle.slack.post(text)
        except Exception:
            pass

"""Fournisseur LLM optionnel (sortie JSON structurée).

Le LLM n'est utilisé que là où il apporte de la valeur : compréhension du contexte,
classification fine des moments, hooks, titres et captions. Sans clé API, tout le
pipeline fonctionne avec les règles (NullLLM).
"""
from __future__ import annotations

import json
import os
from typing import Protocol

from app.core.config import Settings, get_settings
from app.core.utils import get_logger

log = get_logger("llm")


class LLMProvider(Protocol):
    name: str
    available: bool

    def generate_json(self, system: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict | None: ...


class NullLLM:
    name = "none"
    available = False

    def generate_json(self, system: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict | None:
        return None


class AnthropicLLM:
    """Claude via le SDK officiel `anthropic`, sortie contrainte par un JSON schema."""

    def __init__(self, model: str, effort: str = "low", timeout: float = 60):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=2)
        self.model = model
        self.effort = effort
        self.name = f"anthropic:{model}"
        self.available = True

    def generate_json(self, system: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict | None:
        a = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # en cas de refus, l'API relance sur un modèle de repli
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except a.RateLimitError as exc:
            log.warning("LLM : limite de débit atteinte (%s) — repli sur les règles", exc)
            return None
        except a.APIStatusError as exc:
            log.warning("LLM : erreur API %s — repli sur les règles", exc.status_code)
            return None
        except a.APIConnectionError as exc:
            log.warning("LLM : connexion impossible (%s) — repli sur les règles", exc)
            return None
        if response.stop_reason == "refusal":
            log.warning("LLM : requête refusée — repli sur les règles")
            return None
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


def build_llm(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    use = s.get("scoring.use_llm", "auto")
    provider = s.get("llm.provider", "anthropic")
    if use is False or str(use).lower() == "false" or provider == "none":
        return NullLLM()
    has_creds = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if not has_creds and str(use).lower() == "auto":
        return NullLLM()
    try:
        return AnthropicLLM(s.get("llm.model", "claude-opus-5-5"), s.get("llm.effort", "low"),
                            float(s.get("llm.timeout", 60)))
    except Exception as exc:  # SDK absent, etc.
        log.warning("LLM indisponible : %s", exc)
        return NullLLM()


# ---------------------------------------------------------------- affinage des moments
MOMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "category": {"type": "string", "enum": [
                        "unexpected_reaction", "funny", "rage", "victory_clutch", "fail", "action", "quote",
                        "rare_event", "tension_release", "not_interesting"]},
                    "interest": {"type": "integer", "description": "potentiel viral 0-100"},
                    "humor": {"type": "number"},
                    "quotability": {"type": "number"},
                    "context_independence": {"type": "number"},
                    "reason": {"type": "string", "description": "une phrase en français, fondée sur la transcription"},
                },
                "required": ["index", "category", "interest", "humor", "quotability", "context_independence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

MOMENT_SYSTEM = (
    "Tu es un éditeur expert en clips gaming courts (TikTok, Shorts, Reels). Tu évalues des extraits de "
    "streams à partir de leur transcription et de signaux mesurés (pics sonores, mouvement). Juge le potentiel "
    "viral réel : réaction forte, surprise, humour, clutch, phrase mémorable, compréhensible sans contexte. "
    "Fonde chaque raison uniquement sur ce qui figure dans la transcription et les signaux ; n'invente rien. "
    "Les scores humor, quotability et context_independence sont entre 0 et 1."
)


def refine_moments(llm: LLMProvider, items: list[dict]) -> dict[int, dict]:
    if not llm.available or not items:
        return {}
    prompt = "Évalue chacun de ces moments candidats :\n\n" + json.dumps(items, ensure_ascii=False, indent=1)
    data = llm.generate_json(MOMENT_SYSTEM, prompt, MOMENT_SCHEMA)
    if not data:
        return {}
    return {int(m["index"]): m for m in data.get("moments", []) if "index" in m}

"""Fixed settings for the six-role workflow used in the paper."""

from dataclasses import dataclass
from datetime import date
import math
from typing import Any, Mapping

QUANTITATIVE_EXPERT = "quantitative_expert"
CHART_EXPERT = "chart_expert"
NEWS_READER = "news_reader"
NEWS_ASSISTANT = "news_assistant"
NEWS_EXPERT = "news_expert"
DECISION_SUPERVISOR = "decision_supervisor"
AGENT_IDS = (QUANTITATIVE_EXPERT, CHART_EXPERT, NEWS_READER, NEWS_ASSISTANT, NEWS_EXPERT, DECISION_SUPERVISOR)
REFERENCE_START_DATE = date(2024, 9, 1)
END_DATE = date(2026, 4, 30)
WINDOW_SIZE = 126
PREVIOUS_WINDOWS = 6
HORIZON_DAYS = 1
CRASH_PERCENTILE = 0.05
ANOMALY_THRESHOLD = 0.5
RANDOM_SEED = 42
EXPECTED_CHART_COUNT = 3


class ConfigurationError(ValueError):
    pass


def anomaly_threshold_for(crash_percentile: float, score_threshold: float = ANOMALY_THRESHOLD) -> float:
    if float(crash_percentile) not in {0.01, 0.05}:
        raise ConfigurationError("crash_percentile must be 0.01 or 0.05.")
    threshold = float(score_threshold)
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ConfigurationError("anomaly_threshold must be a finite number in (0, 1].")
    return threshold


@dataclass(frozen=True)
class AgentRoute:
    provider: str
    model: str
    modality: str

    def public_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model, "modality": self.modality}


@dataclass(frozen=True)
class RuntimeConfig:
    openai_timeout_seconds: float = 300.0
    openai_max_output_tokens: int = 32768
    openai_max_retries: int = 2
    openai_reasoning_effort: str = "low"
    crash_percentile: float = CRASH_PERCENTILE
    max_concurrent_model_calls: int = 8
    random_seed: int = RANDOM_SEED

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any] | None = None) -> "RuntimeConfig":
        values = dict(settings or {})
        if values.get("model_profile", "online") != "online":
            raise ConfigurationError("The paper workflow uses the online profile. Use the custom builder for other models.")
        if values.get("supervisor_mode", "independent_news") != "independent_news":
            raise ConfigurationError("The paper workflow uses one independent supervisor and no debate.")
        config = cls(openai_timeout_seconds=float(values.get("openai_timeout_seconds", 300)), openai_max_output_tokens=int(values.get("openai_max_output_tokens", 32768)), openai_max_retries=int(values.get("openai_max_retries", 2)), openai_reasoning_effort=str(values.get("openai_reasoning_effort", "low")), crash_percentile=float(values.get("crash_percentile", CRASH_PERCENTILE)), max_concurrent_model_calls=int(values.get("max_concurrent_model_calls", 8)), random_seed=int(values.get("random_seed", RANDOM_SEED)))
        config.validate()
        return config

    def validate(self) -> None:
        if not math.isfinite(self.openai_timeout_seconds) or self.openai_timeout_seconds <= 0:
            raise ConfigurationError("The API timeout must be positive.")
        if self.openai_max_output_tokens <= 0 or self.openai_max_retries < 0:
            raise ConfigurationError("The token limit must be positive and retries cannot be negative.")
        if self.openai_reasoning_effort != "low":
            raise ConfigurationError("The paper workflow uses low reasoning effort.")
        if self.crash_percentile not in {0.01, 0.05}:
            raise ConfigurationError("crash_percentile must be 0.01 or 0.05.")
        if not 1 <= self.max_concurrent_model_calls <= 8:
            raise ConfigurationError("Use between 1 and 8 concurrent model calls.")

    def route_for(self, agent_id: str) -> AgentRoute:
        if agent_id not in AGENT_IDS:
            raise ConfigurationError(f"Unknown agent: {agent_id!r}.")
        model = "gpt-5-nano" if agent_id == NEWS_ASSISTANT else "gpt-5-mini"
        modality = "vision" if agent_id in {CHART_EXPERT, DECISION_SUPERVISOR} else "text"
        return AgentRoute("openai_responses", model, modality)

    def agent_routes(self) -> dict[str, dict[str, str]]:
        return {agent_id: self.route_for(agent_id).public_dict() for agent_id in AGENT_IDS}

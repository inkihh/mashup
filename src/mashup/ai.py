"""AI provider abstraction — supports Anthropic (Claude) and DeepSeek."""

import logging
import os

logger = logging.getLogger("mashup.ai")

EFFECT_TYPES = ["high_pass", "low_pass", "reverb", "delay", "compressor"]


def get_enabled_effects() -> list[str]:
    """Return list of effect type names enabled via env vars."""
    enabled = []
    for effect in EFFECT_TYPES:
        env_key = f"EFFECT_{effect.upper()}"
        if os.getenv(env_key, "true").lower() in ("true", "1", "yes"):
            enabled.append(effect)
    return enabled

# Default models per provider
DEFAULT_MODELS = {
    "anthropic": {
        "select": "claude-opus-4-6",
        "plan": "claude-sonnet-4-6",
    },
    "deepseek": {
        "select": "deepseek-chat",
        "plan": "deepseek-reasoner",
    },
}


def get_provider(task: str = "plan") -> str:
    """Get provider for a task."""
    return os.getenv("AI_PROVIDER", "anthropic").lower()


def get_default_model(task: str) -> str:
    """Get the default model for a task ('select' or 'plan')."""
    provider = get_provider(task)
    return DEFAULT_MODELS.get(provider, DEFAULT_MODELS["anthropic"])[task]


def chat(
    prompt: str,
    *,
    model: str | None = None,
    task: str = "plan",
    max_tokens: int = 4096,
    web_search: bool = False,
) -> str:
    """Send a prompt and return the text response.

    Args:
        prompt: The user message.
        model: Model ID override. If None, uses the default for the provider/task.
        task: Task type ('select' or 'plan') for default model selection.
        max_tokens: Max response tokens.
        web_search: Enable web search (Anthropic only).

    Returns:
        The model's text response.
    """
    provider = get_provider(task)
    if model is None:
        model = get_default_model(task)

    logger.info("AI request: provider=%s, model=%s, task=%s, web_search=%s", provider, model, task, web_search)

    if provider == "anthropic":
        return _chat_anthropic(prompt, model=model, max_tokens=max_tokens, web_search=web_search)
    elif provider == "deepseek":
        return _chat_deepseek(prompt, model=model, max_tokens=max_tokens)
    else:
        raise ValueError(f"Unknown AI provider: {provider}. Set AI_PROVIDER to 'anthropic' or 'deepseek'.")


def _chat_anthropic(prompt: str, *, model: str, max_tokens: int, web_search: bool) -> str:
    import anthropic

    client = anthropic.Anthropic()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if web_search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search"}]

    message = client.messages.create(**kwargs)
    logger.debug("Anthropic usage: %s", message.usage)

    for block in message.content:
        if block.type == "text" and block.text.strip():
            return block.text.strip()

    raise RuntimeError("No text content in Anthropic response")


def _chat_deepseek(prompt: str, *, model: str, max_tokens: int) -> str:
    from openai import OpenAI

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # Reasoner models need more tokens for thinking + output
    if "reasoner" in model:
        max_tokens = max(max_tokens, 16384)

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    logger.debug("DeepSeek usage: %s", response.usage)
    logger.debug("DeepSeek finish_reason: %s", response.choices[0].finish_reason)

    msg = response.choices[0].message
    content = msg.content or ""
    reasoning = getattr(msg, "reasoning_content", "") or ""

    if reasoning:
        logger.debug("DeepSeek reasoning (%d chars): %s...", len(reasoning), reasoning[:200])

    # deepseek-reasoner puts the final answer in content, reasoning in reasoning_content
    # If content has no JSON but reasoning does, use reasoning
    if "{" not in content and "{" in reasoning:
        logger.info("Using reasoning_content (content had no JSON)")
        content = reasoning

    if not content.strip():
        raise RuntimeError("No content in DeepSeek response")
    return content.strip()

"""
SKYNET — Bot & Orchestrator Configuration

Centralises every setting needed by the Telegram bot, AI router,
SKYNET Core orchestrator, and SKYNET Ledger (database).

SECURITY: In production, load secrets from environment variables.
"""

import os


def _str_env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = _str_env(
    "TELEGRAM_BOT_TOKEN",
    "8524123888:AAFxY-nqK0gLGt87pdStkxXEpPoA6bjBHu4",
)

# Only this Telegram user ID can issue commands.
ALLOWED_USER_ID: int = _int_env("TELEGRAM_ALLOWED_USER_ID", 7152683074)

# Gateway HTTP API (runs on the same machine).
GATEWAY_API_URL: str = "http://127.0.0.1:8766"

# Default working directory for worker actions.
# Can be overridden with SKYNET_DEFAULT_WORKING_DIR / OPENCLAW_DEFAULT_WORKING_DIR.
if os.name == "nt":
    _default_working_dir = r"E:\MyProjects\clawd-sandbox"
else:
    _default_working_dir = "/home/ubuntu/skynet"

DEFAULT_WORKING_DIR: str = os.environ.get(
    "SKYNET_DEFAULT_WORKING_DIR",
    os.environ.get("OPENCLAW_DEFAULT_WORKING_DIR", _default_working_dir),
)

# ---------------------------------------------------------------------------
# AI Provider API Keys
# ---------------------------------------------------------------------------
GOOGLE_AI_API_KEY: str = os.environ.get("GOOGLE_AI_API_KEY", "")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_ONLY_MODE: bool = os.environ.get(
    "GEMINI_ONLY_MODE",
    os.environ.get("OPENCLAW_GEMINI_ONLY_MODE", "0"),
).strip().lower() in {"1", "true", "yes", "on"}
GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.environ.get(
    "OPENROUTER_MODEL",
    "qwen/qwen3-next-80b-a3b-instruct:free",
)
OPENROUTER_FALLBACK_MODELS: str = os.environ.get(
    "OPENROUTER_FALLBACK_MODELS",
    (
        "qwen/qwen3-vl-235b-a22b-thinking,"
        "openai/gpt-oss-120b:free,"
        "qwen/qwen3-coder:free,"
        "google/gemma-3n-e2b-it:free,"
        "deepseek/deepseek-r1-0528:free,"
        "meta-llama/llama-3.3-70b-instruct:free"
    ),
)
DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
AI_PROVIDER_PRIORITY: str = os.environ.get(
    "AI_PROVIDER_PRIORITY",
    "gemini,groq,openrouter,deepseek,openai,claude,ollama",
)
AUTO_APPROVE_GIT_ACTIONS: bool = os.environ.get(
    "AUTO_APPROVE_GIT_ACTIONS",
    os.environ.get("OPENCLAW_AUTO_APPROVE_GIT_ACTIONS", "1"),
).strip().lower() in {"1", "true", "yes", "on"}
AUTO_APPROVE_AND_START: bool = os.environ.get(
    "AUTO_APPROVE_AND_START",
    os.environ.get("OPENCLAW_AUTO_APPROVE_AND_START", "1"),
).strip().lower() in {"1", "true", "yes", "on"}
AUTO_PLAN_MIN_IDEAS: int = _int_env(
    "AUTO_PLAN_MIN_IDEAS",
    _int_env("OPENCLAW_AUTO_PLAN_MIN_IDEAS", 3),
)
AUTO_BOOTSTRAP_PROJECT: bool = os.environ.get(
    "AUTO_BOOTSTRAP_PROJECT",
    os.environ.get("OPENCLAW_AUTO_BOOTSTRAP_PROJECT", "1"),
).strip().lower() in {"1", "true", "yes", "on"}
AUTO_BOOTSTRAP_STRICT: bool = os.environ.get(
    "AUTO_BOOTSTRAP_STRICT",
    os.environ.get("OPENCLAW_AUTO_BOOTSTRAP_STRICT", "0"),
).strip().lower() in {"1", "true", "yes", "on"}
AUTO_CREATE_GITHUB_REPO: bool = os.environ.get(
    "AUTO_CREATE_GITHUB_REPO",
    os.environ.get("OPENCLAW_AUTO_CREATE_GITHUB_REPO", "1"),
).strip().lower() in {"1", "true", "yes", "on"}
AUTO_CREATE_GITHUB_PRIVATE: bool = os.environ.get(
    "AUTO_CREATE_GITHUB_PRIVATE",
    os.environ.get("OPENCLAW_AUTO_CREATE_GITHUB_PRIVATE", "0"),
).strip().lower() in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Ollama (local laptop LLM)
# ---------------------------------------------------------------------------
OLLAMA_DEFAULT_MODEL: str = os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen2.5-coder:7b")

# ---------------------------------------------------------------------------
# Web Search
# ---------------------------------------------------------------------------
BRAVE_SEARCH_API_KEY: str = os.environ.get("BRAVE_SEARCH_API_KEY", "")

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
GITHUB_PAT: str = os.environ.get("GITHUB_PAT", "")
GITHUB_USERNAME: str = os.environ.get("GITHUB_USERNAME", "")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH: str = os.environ.get(
    "SKYNET_DB_PATH",
    os.environ.get(
        "OPENCLAW_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "skynet.db"),
    ),
)

# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
PROJECT_BASE_DIR: str = os.environ.get(
    "SKYNET_PROJECT_BASE_DIR",
    os.environ.get("OPENCLAW_PROJECT_BASE_DIR", r"E:\MyProjects"),
)

# ---------------------------------------------------------------------------
# S3 (AWS free tier artifact storage)
# ---------------------------------------------------------------------------
S3_BUCKET: str = os.environ.get("SKYNET_S3_BUCKET", "openclaw-artifacts")
S3_PREFIX: str = os.environ.get(
    "SKYNET_S3_PREFIX", os.environ.get("OPENCLAW_S3_PREFIX", "openclaw/"),
)
AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# External SKILL.md packs (OpenClaw community skills)
# ---------------------------------------------------------------------------
EXTERNAL_SKILLS_DIR: str = os.environ.get(
    "SKYNET_EXTERNAL_SKILLS_DIR",
    os.environ.get(
        "OPENCLAW_EXTERNAL_SKILLS_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "external-skills"),
    ),
)
_raw_external_skill_urls = os.environ.get(
    "SKYNET_EXTERNAL_SKILL_URLS",
    os.environ.get("OPENCLAW_EXTERNAL_SKILL_URLS", ""),
)
EXTERNAL_SKILL_URLS: list[str] = [
    u.strip()
    for u in _raw_external_skill_urls.replace("\n", ",").split(",")
    if u.strip()
]

_default_always_on_prompt_skills = (
    "memory-governance,"
    "agent-memory,"
    "self-improvement,"
    "memory-management,"
    "claude-reflection"
)
_raw_always_on_prompt_skills = os.environ.get(
    "SKYNET_ALWAYS_ON_PROMPT_SKILLS",
    os.environ.get("OPENCLAW_ALWAYS_ON_PROMPT_SKILLS", _default_always_on_prompt_skills),
)
ALWAYS_ON_PROMPT_SKILLS: list[str] = [
    s.strip()
    for s in _raw_always_on_prompt_skills.replace("\n", ",").split(",")
    if s.strip()
]
ALWAYS_ON_PROMPT_SNIPPET_CHARS: int = int(
    os.environ.get(
        "SKYNET_ALWAYS_ON_PROMPT_SNIPPET_CHARS",
        os.environ.get("OPENCLAW_ALWAYS_ON_PROMPT_SNIPPET_CHARS", "1200"),
    ),
)

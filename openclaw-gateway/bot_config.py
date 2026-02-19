"""
SKYNET — Bot & Orchestrator Configuration

Centralises every setting needed by the Telegram bot, AI router,
SKYNET Core orchestrator, and SKYNET Ledger (database).

SECURITY: In production, load secrets from environment variables.
"""

import os

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8524123888:AAFxY-nqK0gLGt87pdStkxXEpPoA6bjBHu4",
)

# Only this Telegram user ID can issue commands.
ALLOWED_USER_ID: int = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "7152683074"))

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
GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

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
    os.environ.get("OPENCLAW_PROJECT_BASE_DIR", r"E:\OpenClaw\projects"),
)

# ---------------------------------------------------------------------------
# S3 (AWS free tier artifact storage)
# ---------------------------------------------------------------------------
S3_BUCKET: str = os.environ.get("SKYNET_S3_BUCKET", "openclaw-artifacts")
S3_PREFIX: str = os.environ.get(
    "SKYNET_S3_PREFIX", os.environ.get("OPENCLAW_S3_PREFIX", "openclaw/"),
)
AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")

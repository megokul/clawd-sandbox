"""
SKYNET Gateway — Entry Point
Codename: CHATHAN

Starts all components in a single asyncio event loop:
  - WebSocket server (public, port 8765) — CHATHAN worker connection
  - HTTP API (loopback, port 8766) — internal action dispatch
  - Telegram bot (polling) — SKYNET Gateway interface
  - Project orchestrator — SKYNET Core AI-driven lifecycle
  - AI provider router — multi-provider free-tier rotation

Usage:
    python main.py

Environment variables (required):
    SKYNET_AUTH_TOKEN       Shared secret — must match the CHATHAN worker.
    TELEGRAM_BOT_TOKEN     Telegram bot token from BotFather.

Optional:
    DISABLE_TELEGRAM_BOT   Set to "1" or "true" to run in API-only mode (no bot).
    SKYNET_LOG_LEVEL       DEBUG | INFO | WARNING | ERROR (default: INFO)
    SKYNET_TLS_CERT        Path to TLS certificate.
    SKYNET_TLS_KEY         Path to TLS private key.
    GOOGLE_AI_API_KEY      Gemini API key (recommended for free tier).
    GROQ_API_KEY           Groq API key.
    + other AI provider keys (see bot_config.py)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import gateway_config as cfg
import bot_config
from gateway import start_ws_server
from api import start_http_api


def _configure_logging() -> None:
    level = getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _print_banner() -> None:
    print(
        r"""
  ____  _  ____   ___   _ _____ _____
 / ___|| |/ /\ \ / / \ | | ____|_   _|
 \___ \| ' /  \ V /|  \| |  _|   | |
  ___) | . \   | | | |\  | |___  | |
 |____/|_|\_\  |_| |_| \_|_____| |_|
      Codename: CHATHAN

  WebSocket : 0.0.0.0:{ws_port}
  HTTP API  : {http_host}:{http_port}
  TLS cert  : {tls}
  Telegram  : enabled
  DB        : {db}
""".format(
            ws_port=cfg.WS_PORT,
            http_host=cfg.HTTP_HOST,
            http_port=cfg.HTTP_PORT,
            tls=cfg.TLS_CERT if cfg.TLS_CERT else "DISABLED",
            db=bot_config.DB_PATH,
        )
    )


async def _main() -> None:
    _configure_logging()
    _print_banner()

    logger = logging.getLogger("skynet")

    # ---- Validate required secrets ----
    if not cfg.AUTH_TOKEN:
        logger.error(
            "SKYNET_AUTH_TOKEN is not set.\n"
            "  export SKYNET_AUTH_TOKEN=$(python3 -c "
            "\"import secrets; print(secrets.token_urlsafe(48))\")"
        )
        sys.exit(1)

    if not bot_config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
        sys.exit(1)

    # ---- Ensure data directory exists ----
    db_dir = os.path.dirname(bot_config.DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # ---- Initialize SQLite database ----
    from db.schema import init_db

    db = await init_db(bot_config.DB_PATH)
    logger.info("Database initialized at %s", bot_config.DB_PATH)

    # ---- Build AI provider router ----
    from ai.provider_router import ProviderRouter, build_providers

    provider_config = {
        "OLLAMA_DEFAULT_MODEL": bot_config.OLLAMA_DEFAULT_MODEL,
        "GOOGLE_AI_API_KEY": bot_config.GOOGLE_AI_API_KEY,
        "GROQ_API_KEY": bot_config.GROQ_API_KEY,
        "OPENROUTER_API_KEY": bot_config.OPENROUTER_API_KEY,
        "DEEPSEEK_API_KEY": bot_config.DEEPSEEK_API_KEY,
        "OPENAI_API_KEY": bot_config.OPENAI_API_KEY,
        "ANTHROPIC_API_KEY": bot_config.ANTHROPIC_API_KEY,
    }
    providers = build_providers(provider_config)
    router = ProviderRouter(providers, db)
    await router.restore_usage()
    logger.info("AI router ready with %d provider(s).", len(providers))

    # ---- Web search ----
    from search.web_search import WebSearcher

    searcher = WebSearcher(bot_config.BRAVE_SEARCH_API_KEY)

    # ---- SKYNET Policy Engine ----
    from policy.engine import PolicyEngine

    policy_engine = PolicyEngine()
    logger.info("Policy engine online.")

    # ---- SKYNET Skill Registry ----
    from skills.registry import build_default_registry

    skill_registry = build_default_registry(
        external_skills_dir=bot_config.EXTERNAL_SKILLS_DIR,
        external_skill_urls=bot_config.EXTERNAL_SKILL_URLS,
    )
    logger.info(
        "Skill registry loaded (%d total; %d prompt-only).",
        skill_registry.skill_count,
        skill_registry.prompt_skill_count,
    )

    # ---- SKYNET Memory Manager ----
    from memory.manager import MemoryManager

    s3_storage = None
    try:
        from storage.s3_client import S3Storage
        s3_storage = S3Storage(
            bucket=bot_config.S3_BUCKET,
            prefix=bot_config.S3_PREFIX,
            region=bot_config.AWS_REGION,
        )
    except Exception:
        logger.info("S3 storage not configured — memory sync disabled.")

    memory_manager = MemoryManager(
        db=db,
        gateway_api_url=bot_config.GATEWAY_API_URL,
        s3=s3_storage,
    )

    # ---- CHATHAN Execution Engine ----
    from chathan.execution.engine import ExecutionEngine
    from chathan.providers.chathan_provider import ChathanProvider

    execution_engine = ExecutionEngine(policy_engine=policy_engine)
    execution_engine.register(ChathanProvider(bot_config.GATEWAY_API_URL))
    logger.info(
        "Execution engine ready (providers: %s).",
        ", ".join(execution_engine.available_providers),
    )

    # ---- SKYNET Sentinel ----
    from sentinel.monitor import SentinelMonitor
    from sentinel.alert import AlertDispatcher

    sentinel = SentinelMonitor(
        gateway_api_url=bot_config.GATEWAY_API_URL,
        db=db,
        s3=s3_storage,
    )
    alert_dispatcher = AlertDispatcher()
    logger.info("Sentinel monitor online.")

    # ---- SKYNET Heartbeat Scheduler ----
    from heartbeat.scheduler import HeartbeatScheduler, HeartbeatTask
    from heartbeat.tasks import DEFAULT_TASKS

    heartbeat = HeartbeatScheduler(tick_interval=60)

    # Create a simple context namespace for heartbeat tasks.
    class _HBContext:
        pass
    hb_ctx = _HBContext()
    hb_ctx.sentinel = sentinel
    hb_ctx.alert_dispatcher = alert_dispatcher
    hb_ctx.memory_manager = memory_manager
    hb_ctx.s3 = s3_storage
    hb_ctx.db = db
    hb_ctx.gateway_api_url = bot_config.GATEWAY_API_URL
    hb_ctx.active_project_ids = []

    for task_def in DEFAULT_TASKS:
        heartbeat.register(HeartbeatTask(
            name=task_def["name"],
            description=task_def["description"],
            interval_seconds=task_def["interval_seconds"],
            handler=task_def["handler"],
            context=hb_ctx,
        ))

    # ---- Orchestrator ----
    import telegram_bot
    from orchestrator.scheduler import Scheduler
    from orchestrator.project_manager import ProjectManager

    scheduler = Scheduler(
        db=db,
        router=router,
        searcher=searcher,
        gateway_api_url=bot_config.GATEWAY_API_URL,
        on_progress=telegram_bot.on_project_progress,
        request_approval=telegram_bot.request_worker_approval,
        skill_registry=skill_registry,
        memory_manager=memory_manager,
    )
    sentinel.scheduler = scheduler

    project_manager = ProjectManager(
        db=db,
        router=router,
        searcher=searcher,
        scheduler=scheduler,
        project_base_dir=bot_config.PROJECT_BASE_DIR,
    )

    logger.info("Project orchestrator ready (max %d parallel).", scheduler.max_parallel)

    # ---- Inject dependencies into Telegram bot ----
    telegram_bot.set_dependencies(
        project_manager,
        router,
        heartbeat=heartbeat,
        sentinel=sentinel,
        searcher=searcher,
        skill_registry=skill_registry,
    )

    # ---- Start core servers ----
    ws_server = await start_ws_server()
    http_runner = await start_http_api()

    # ---- Start Heartbeat Scheduler ----
    await heartbeat.start()
    logger.info("Heartbeat scheduler started (%d tasks).", heartbeat.task_count)

    # ---- Start Telegram bot (non-blocking polling) ----
    bot_app = telegram_bot.build_app()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot polling started.")

    logger.info("SKYNET initializing...")
    logger.info("Codename: CHATHAN active.")
    logger.info("Policy engine online.")
    logger.info("Worker connected — waiting for connections...")
    logger.info("System ready.")

    try:
        # Run forever.
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        # Graceful shutdown.
        logger.info("Shutting down…")

        # Stop Heartbeat scheduler.
        await heartbeat.stop()

        # Stop Telegram bot.
        try:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
        except Exception:
            logger.exception("Error stopping Telegram bot.")

        # Cancel all running project workers.
        scheduler.cancel_all()

        # Stop WebSocket + HTTP servers.
        ws_server.close()
        await ws_server.wait_closed()
        await http_runner.cleanup()

        # Close database.
        await db.close()

        logger.info("SKYNET shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

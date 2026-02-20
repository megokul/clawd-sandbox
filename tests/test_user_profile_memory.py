"""Gateway user profile memory storage tests."""

from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_user_profile_memory_crud() -> None:
    repo_root = Path(__file__).parent.parent
    schema_path = repo_root / "openclaw-gateway" / "db" / "schema.py"
    store_path = repo_root / "openclaw-gateway" / "db" / "store.py"

    schema = _load_module(schema_path, "oc_gateway_schema")
    store = _load_module(store_path, "oc_gateway_store")

    db = await schema.init_db(":memory:")
    try:
        user = await store.ensure_user(
            db,
            telegram_user_id=12345,
            username="tester",
            first_name="Test",
            last_name="User",
        )
        assert user["telegram_user_id"] == 12345
        assert int(user["memory_enabled"]) == 1

        fact = await store.add_or_update_profile_fact(
            db,
            user_id=int(user["id"]),
            fact_key="timezone",
            fact_value="UTC+05:30",
            confidence=0.9,
        )
        assert fact["fact_key"] == "timezone"
        assert fact["fact_value"] == "UTC+05:30"

        await store.upsert_user_preference(
            db,
            user_id=int(user["id"]),
            pref_key="tone.no_emojis",
            pref_value="true",
        )
        prefs = await store.get_user_preferences(db, user_id=int(user["id"]))
        assert len(prefs) == 1
        assert prefs[0]["pref_key"] == "tone.no_emojis"

        conv_id = await store.add_user_conversation(
            db,
            user_id=int(user["id"]),
            role="user",
            content="My timezone is UTC+05:30",
        )
        assert conv_id > 0

        audit_id = await store.add_memory_audit_log(
            db,
            user_id=int(user["id"]),
            action="fact_upsert",
            target_type="fact",
            target_key="timezone",
            detail="timezone=UTC+05:30",
        )
        assert audit_id > 0

        removed = await store.forget_profile_facts(
            db,
            user_id=int(user["id"]),
            key_or_text="timezone",
        )
        assert removed == 1

        facts = await store.list_profile_facts(db, user_id=int(user["id"]), active_only=True)
        assert facts == []

        await store.set_user_memory_enabled(db, user_id=int(user["id"]), enabled=False)
        reloaded = await store.get_user_by_id(db, int(user["id"]))
        assert reloaded is not None
        assert int(reloaded["memory_enabled"]) == 0
    finally:
        await db.close()

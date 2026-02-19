"""
SKYNET - External Prompt Skill Loader

Loads OpenClaw-style SKILL.md files from a local directory and optional
GitHub URLs, then returns normalized prompt-skill records that can be
injected into system prompts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger("skynet.skills.external_loader")


@dataclass
class ExternalPromptSkill:
    name: str
    description: str
    content: str
    source: str


_GITHUB_HOSTS = {"github.com", "www.github.com", "raw.githubusercontent.com"}


def _safe_name(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()
    if out:
        return out[:80]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"skill-{digest}"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """
    Parse simple YAML frontmatter.

    Supports top-level `key: value` pairs only.
    """
    if not text.startswith("---\n"):
        return {}, text

    lines = text.splitlines()
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, text

    meta: dict[str, str] = {}
    for line in lines[1:end_idx]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            meta[key] = value

    body = "\n".join(lines[end_idx + 1 :]).lstrip()
    return meta, body


def _github_url_to_raw_skill(url: str) -> tuple[str, str] | None:
    """
    Convert github.com/raw.githubusercontent.com URL to raw SKILL.md URL.

    Returns `(raw_url, suggested_name)` or None when unsupported.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc not in _GITHUB_HOSTS:
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc == "raw.githubusercontent.com":
        if len(parts) < 4:
            return None
        owner, repo, ref = parts[0], parts[1], parts[2]
        skill_path_parts = parts[3:]
        if not skill_path_parts:
            return None
        if skill_path_parts[-1].lower() != "skill.md":
            skill_path_parts.append("SKILL.md")
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{'/'.join(skill_path_parts)}"
        suggested_name = skill_path_parts[-2] if len(skill_path_parts) >= 2 else repo
        return raw_url, suggested_name

    if len(parts) < 5:
        return None
    owner, repo = parts[0], parts[1]
    mode = parts[2]
    ref = parts[3]
    path_parts = parts[4:]

    if mode not in {"tree", "blob"}:
        return None
    if not path_parts:
        return None
    if path_parts[-1].lower() != "skill.md":
        path_parts.append("SKILL.md")

    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{'/'.join(path_parts)}"
    suggested_name = path_parts[-2] if len(path_parts) >= 2 else repo
    return raw_url, suggested_name


def _download_text(url: str, timeout_seconds: int = 15) -> str:
    req = Request(
        url,
        headers={"User-Agent": "skynet-openclaw-gateway/1.0"},
        method="GET",
    )
    with urlopen(req, timeout=timeout_seconds) as resp:
        content_type = resp.headers.get("Content-Type", "").lower()
        if "text" not in content_type and "markdown" not in content_type and "application/octet-stream" not in content_type:
            raise ValueError(f"Unexpected content type: {content_type}")
        data = resp.read()
    if len(data) > 512_000:
        raise ValueError("Remote SKILL.md exceeds 512KB limit.")
    return data.decode("utf-8", errors="replace")


def sync_remote_skill_urls(skill_urls: list[str], cache_root: str) -> list[Path]:
    """Download remote SKILL.md files into cache_root and return file paths."""
    if not skill_urls:
        return []

    cache_dir = Path(cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []

    for raw_input in skill_urls:
        item = raw_input.strip()
        if not item:
            continue
        converted = _github_url_to_raw_skill(item)
        if converted is None:
            logger.warning("Skipping unsupported skill URL: %s", item)
            continue

        raw_url, suggested_name = converted
        skill_dir = cache_dir / _safe_name(suggested_name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        try:
            text = _download_text(raw_url)
            skill_file.write_text(text, encoding="utf-8")
            out.append(skill_file)
            logger.info("Fetched external skill: %s", raw_url)
        except Exception as exc:
            logger.warning("Failed to fetch external skill from %s: %s", raw_url, exc)

    return out


def _read_skill_file(path: Path, max_chars: int) -> ExternalPromptSkill | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None

    meta, body = _parse_frontmatter(raw)
    name = (meta.get("name") or path.parent.name or path.stem).strip()
    description = (meta.get("description") or "").strip()
    if not description:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                description = line[:200]
                break
    body = body.strip()
    if len(body) > max_chars:
        body = body[:max_chars] + "\n\n... (truncated)"

    if not body:
        return None
    return ExternalPromptSkill(
        name=name,
        description=description or "External prompt skill",
        content=body,
        source=str(path),
    )


def load_external_prompt_skills(
    skills_root: str,
    *,
    skill_urls: list[str] | None = None,
    max_chars_per_skill: int = 16_000,
) -> list[ExternalPromptSkill]:
    """
    Load SKILL.md files from local directory + optional remote URLs.

    Remote URLs are cached under `<skills_root>/.remote-cache`.
    """
    root = Path(skills_root)
    root.mkdir(parents=True, exist_ok=True)

    if skill_urls:
        sync_remote_skill_urls(skill_urls, str(root / ".remote-cache"))

    files = sorted(root.rglob("SKILL.md"))
    loaded: list[ExternalPromptSkill] = []
    for path in files:
        item = _read_skill_file(path, max_chars=max_chars_per_skill)
        if item is None:
            continue
        loaded.append(item)

    # Deduplicate by lowercase name (prefer first encountered).
    dedup: dict[str, ExternalPromptSkill] = {}
    for item in loaded:
        key = item.name.strip().lower()
        if key and key not in dedup:
            dedup[key] = item
    return list(dedup.values())

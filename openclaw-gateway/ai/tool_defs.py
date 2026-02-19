"""
SKYNET — AI Tool Definitions (Backward Compatibility)

In v3 tools are provided by the Skill system (skills/ package).
This module preserves CODING_TOOLS and PLANNING_TOOLS for code
that still imports them directly, and adds helpers to build
tool lists from the SkillRegistry.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from skills.registry import SkillRegistry


def get_coding_tools(registry: SkillRegistry) -> list[dict[str, Any]]:
    """Build coding tool list from the skill registry."""
    return registry.get_all_tools()


def get_planning_tools(registry: SkillRegistry) -> list[dict[str, Any]]:
    """Build planning tool list (read-only + search)."""
    return [
        t for t in registry.get_all_tools()
        if t["name"] in ("web_search", "list_directory", "file_read")
    ]


# --- Backward compatibility: static tool lists for old imports ---

CODING_TOOLS: list[dict] = [
    # --- File Operations ---
    {
        "name": "file_write",
        "description": (
            "Create or overwrite a file with the given content. "
            "Parent directories are created automatically. "
            "The file path must be an absolute path within the project directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute file path"},
                "content": {"type": "string", "description": "Complete file content"},
            },
            "required": ["file", "content"],
        },
    },
    {
        "name": "file_read",
        "description": "Read the contents of a file. Returns file content as text (max 64 KB).",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute file path"},
            },
            "required": ["file"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories. Returns names with [DIR] prefix for dirs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Absolute directory path"},
                "recursive": {
                    "type": "boolean",
                    "description": "List recursively (max depth 3). Default: false.",
                },
            },
            "required": ["directory"],
        },
    },
    {
        "name": "create_directory",
        "description": "Create a directory and any missing parent directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Absolute directory path"},
            },
            "required": ["directory"],
        },
    },
    # --- Git Operations ---
    {
        "name": "git_init",
        "description": "Initialize a new git repository with 'main' as default branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Directory for the repo"},
            },
            "required": ["working_dir"],
        },
    },
    {
        "name": "git_status",
        "description": "Show git working tree status (porcelain format).",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Git repo directory"},
            },
            "required": ["working_dir"],
        },
    },
    {
        "name": "git_add_all",
        "description": "Stage all changes including untracked files (git add -A).",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Git repo directory"},
            },
            "required": ["working_dir"],
        },
    },
    {
        "name": "git_commit",
        "description": "Commit staged changes with the given message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Git repo directory"},
                "message": {"type": "string", "description": "Commit message"},
            },
            "required": ["working_dir", "message"],
        },
    },
    {
        "name": "git_push",
        "description": "Push commits to the remote repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Git repo directory"},
                "remote": {"type": "string", "description": "Remote name (default: origin)"},
                "branch": {"type": "string", "description": "Branch name (default: main)"},
            },
            "required": ["working_dir"],
        },
    },
    # --- GitHub ---
    {
        "name": "gh_create_repo",
        "description": "Create a new GitHub repository and push the initial code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Local git repo directory"},
                "repo_name": {"type": "string", "description": "Repository name on GitHub"},
                "description": {"type": "string", "description": "Repo description"},
                "private": {"type": "boolean", "description": "Private repo (default: false)"},
            },
            "required": ["working_dir", "repo_name"],
        },
    },
    # --- Build & Test ---
    {
        "name": "run_tests",
        "description": "Run the project test suite. Returns stdout, stderr, and exit code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Project directory"},
                "runner": {
                    "type": "string",
                    "enum": ["pytest", "npm"],
                    "description": "Test runner (default: pytest)",
                },
            },
            "required": ["working_dir"],
        },
    },
    {
        "name": "install_dependencies",
        "description": "Install project dependencies from requirements.txt (pip) or package.json (npm).",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Project directory"},
                "manager": {
                    "type": "string",
                    "enum": ["pip", "npm"],
                    "description": "Package manager (default: pip)",
                },
            },
            "required": ["working_dir"],
        },
    },
    {
        "name": "lint_project",
        "description": "Run linting on the project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Project directory"},
                "linter": {
                    "type": "string",
                    "enum": ["ruff", "eslint"],
                    "description": "Linter (default: ruff)",
                },
            },
            "required": ["working_dir"],
        },
    },
    {
        "name": "build_project",
        "description": "Build the project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "description": "Project directory"},
                "build_tool": {
                    "type": "string",
                    "enum": ["npm", "python"],
                    "description": "Build tool (default: npm)",
                },
            },
            "required": ["working_dir"],
        },
    },
    # --- Search ---
    {
        "name": "web_search",
        "description": (
            "Search the web for programming resources, library documentation, "
            "API references, or implementation examples."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {
                    "type": "integer",
                    "description": "Number of results (default: 5, max: 10)",
                },
            },
            "required": ["query"],
        },
    },
    # --- IDE ---
    {
        "name": "open_in_vscode",
        "description": "Open a project directory or file in VS Code on the laptop.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to open in VS Code"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "check_coding_agents",
        "description": (
            "Check if local coding agent CLIs are installed on the laptop. "
            "Reports Codex, Claude, and Cline availability."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "run_coding_agent",
        "description": (
            "Run a local coding agent CLI in non-interactive mode on the laptop "
            "(Codex, Claude, or Cline)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": ["codex", "claude", "cline"],
                    "description": "Which coding agent to run.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Task prompt to send to the coding agent.",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional project directory on the laptop.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Optional timeout (30-3600, default 1800).",
                },
            },
            "required": ["agent", "prompt"],
        },
    },
    {
        "name": "configure_coding_agent",
        "description": (
            "Configure local coding agent provider/model. "
            "Currently supports switching Cline provider authentication."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": ["cline"],
                    "description": "Coding agent to configure.",
                },
                "provider": {
                    "type": "string",
                    "enum": ["gemini", "deepseek", "groq", "openrouter", "openai", "anthropic"],
                    "description": "Provider to set for Cline.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model id for that provider.",
                },
                "base_url": {
                    "type": "string",
                    "description": "Optional custom base URL.",
                },
            },
            "required": ["agent", "provider"],
        },
    },
]

# Subset for the planning phase (read-only + search).
PLANNING_TOOLS: list[dict] = [
    t for t in CODING_TOOLS
    if t["name"] in ("web_search", "list_directory", "file_read")
]

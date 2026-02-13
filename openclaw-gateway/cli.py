"""
OpenClaw Gateway Server — Interactive CLI

Run from the EC2 instance to manually dispatch actions to the
connected laptop agent.

Usage:
    python cli.py

Sends HTTP requests to the local API at http://127.0.0.1:8766.
Requires the gateway (main.py) to be running.
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error

import gateway_config as cfg

API_BASE = f"http://{cfg.HTTP_HOST}:{cfg.HTTP_PORT}"

# The actions the agent supports, for reference in the menu.
KNOWN_ACTIONS = {
    # AUTO tier
    "1": ("git_status",            {"working_dir": ""}),
    "2": ("run_tests",             {"working_dir": "", "runner": "pytest"}),
    "3": ("lint_project",          {"working_dir": "", "linter": "ruff"}),
    "4": ("start_dev_server",      {"working_dir": "", "framework": "npm"}),
    "5": ("build_project",         {"working_dir": "", "build_tool": "npm"}),
    # CONFIRM tier
    "6": ("git_commit",            {"working_dir": "", "message": ""}),
    "7": ("install_dependencies",  {"working_dir": "", "manager": "pip"}),
    "8": ("file_write",            {"file": "", "content": ""}),
    "9": ("docker_build",          {"working_dir": "", "tag": ""}),
    "10": ("docker_compose_up",    {"working_dir": ""}),
}


def _post(endpoint: str, body: dict | None = None) -> dict:
    """POST JSON to the local HTTP API and return the parsed response."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"{API_BASE}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=130) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())
    except urllib.error.URLError as exc:
        return {"error": f"Cannot reach gateway API: {exc.reason}"}


def _get(endpoint: str) -> dict:
    req = urllib.request.Request(f"{API_BASE}{endpoint}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())
    except urllib.error.URLError as exc:
        return {"error": f"Cannot reach gateway API: {exc.reason}"}


def _print_menu() -> None:
    print(
        """
============================================================
  OpenClaw Gateway — Action Dispatcher CLI
============================================================
  AUTO tier (executes immediately on laptop):
    1) git_status           2) run_tests
    3) lint_project         4) start_dev_server
    5) build_project

  CONFIRM tier (asks operator on laptop for approval):
    6) git_commit           7) install_dependencies
    8) file_write           9) docker_build
   10) docker_compose_up

  Controls:
    s) Status — check agent connection
    e) Emergency stop
    r) Resume after stop
    q) Quit
============================================================"""
    )


def _fill_params(params: dict) -> dict:
    """Prompt the user to fill in parameter values."""
    filled = {}
    for key, default in params.items():
        value = input(f"  {key} [{default}]: ").strip()
        filled[key] = value if value else default
    return filled


def main() -> None:
    _print_menu()

    while True:
        try:
            choice = input("\n> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if choice == "q":
            print("Bye.")
            break

        if choice == "s":
            result = _get("/status")
            connected = result.get("agent_connected", False)
            status = "CONNECTED" if connected else "NOT CONNECTED"
            print(f"  Agent: {status}")
            continue

        if choice == "e":
            confirm = input("  Confirm emergency stop? [y/N]: ").strip().lower()
            if confirm in ("y", "yes"):
                print(f"  {json.dumps(_post('/emergency-stop'), indent=2)}")
            continue

        if choice == "r":
            print(f"  {json.dumps(_post('/resume'), indent=2)}")
            continue

        if choice in KNOWN_ACTIONS:
            action, param_template = KNOWN_ACTIONS[choice]
            print(f"\n  Action: {action}")
            print("  Fill parameters (press Enter for default):")
            params = _fill_params(param_template)

            print(f"\n  Sending '{action}' …")
            result = _post("/action", {"action": action, "params": params})
            print(f"\n  Response:\n{json.dumps(result, indent=2)}")
            continue

        print("  Unknown choice. Enter a number, 's', 'e', 'r', or 'q'.")


if __name__ == "__main__":
    main()

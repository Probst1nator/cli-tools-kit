"""The installer's own settings file: auto-update, custom icons, icon generation."""

from __future__ import annotations

import json
import os
from typing import Optional

from . import state


def load_config() -> dict:
    """Load config from file, return empty dict if not found."""
    try:
        with open(state.CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(config: dict):
    """Save config to file."""
    os.makedirs(state.CONFIG_DIR, exist_ok=True)
    with open(state.CONFIG_FILE, "w") as f:
        json.dump(config, f)


def get_auto_update_on_startup() -> bool:
    """Whether this GUI should silently apply pending local updates the next
    time it launches (the "Auto-update on startup" checkbox next to the
    Up-to-date badge)."""
    return bool(load_config().get("auto_update_on_startup", False))


def set_auto_update_on_startup(enabled: bool):
    """Persist the 'Auto-update on startup' checkbox state."""
    config = load_config()
    config["auto_update_on_startup"] = enabled
    save_config(config)


def get_custom_icon_path(tool_key: str) -> Optional[str]:
    """Get custom icon path for a tool if one exists.

    Args:
        tool_key: Unique key like "Category_ToolName"

    Returns:
        Path to custom icon file, or None
    """
    config = load_config()
    custom_icons = config.get("custom_icons", {})
    icon_path = custom_icons.get(tool_key)
    if icon_path and os.path.exists(icon_path):
        return icon_path
    return None


def set_custom_icon(tool_key: str, icon_path: str):
    """Set a custom icon for a tool.

    Args:
        tool_key: Unique key like "Category_ToolName"
        icon_path: Path to the icon file
    """
    config = load_config()
    if "custom_icons" not in config:
        config["custom_icons"] = {}
    config["custom_icons"][tool_key] = icon_path
    save_config(config)


def clear_custom_icon(tool_key: str):
    """Remove custom icon for a tool, reverting to default."""
    config = load_config()
    if "custom_icons" in config and tool_key in config["custom_icons"]:
        del config["custom_icons"][tool_key]
        save_config(config)


def get_icon_gen_settings() -> dict:
    """Get saved icon generation settings.

    Returns:
        Dict with keys: steps, samples, guidance, selected_models, expanded, sys_icons_expanded
    """
    config = load_config()
    return config.get("icon_gen_settings", {
        "steps": 20,
        "samples": 4,
        "guidance": 7.5,
        "selected_models": [],  # Empty means select all available
        "expanded": True,  # Show advanced options by default
        "sys_icons_expanded": True
    })


def save_icon_gen_settings(steps: int, samples: int, guidance: float, selected_models: list, expanded: bool, sys_icons_expanded: bool = True):
    """Save icon generation settings."""
    config = load_config()
    config["icon_gen_settings"] = {
        "steps": steps,
        "samples": samples,
        "guidance": guidance,
        "selected_models": selected_models,
        "expanded": expanded,
        "sys_icons_expanded": sys_icons_expanded
    }
    save_config(config)


def get_tool_prompt(tool_key: str, default_name: str) -> str:
    """Get saved prompt for a specific tool."""
    config = load_config()
    prompts = config.get("icon_prompts", {})
    return prompts.get(tool_key, f"app icon for {default_name}, flat design, minimal")


def save_tool_prompt(tool_key: str, prompt: str):
    """Save prompt for a specific tool."""
    config = load_config()
    if "icon_prompts" not in config:
        config["icon_prompts"] = {}
    config["icon_prompts"][tool_key] = prompt
    save_config(config)

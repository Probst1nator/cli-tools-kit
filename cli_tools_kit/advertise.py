"""Helper for the `--advertise` JSON convention.

Every tool that opts into the installer protocol must emit a JSON list of
metadata dicts when invoked with `--advertise`, then exit 0, BEFORE any heavy
imports. This is enforced by a 5-second timeout in the installer's discovery
probe; tools that import slow modules before answering the probe time out and
disappear from the GUI.

Usage:

    # At the very top of your tool's main script:
    import sys
    from cli_tools_kit import ToolMetadata, advertise

    if "--advertise" in sys.argv:
        advertise(ToolMetadata(
            name="My Tool",
            desktop_file="my_tool.desktop",
            icon="utilities-terminal",
            desc="Does the thing",
            tags=["CLI"],
            alias="mytool",
        ))

    # Heavy imports AFTER the advertise check
    import pandas as pd
    ...
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, NoReturn, Union

from .tool_installer import ToolMetadata


def advertise(metadata: Union[ToolMetadata, List[ToolMetadata]]) -> NoReturn:
    """Emit the --advertise JSON for one or more variants, then exit 0.

    Call this inside an `if "--advertise" in sys.argv:` guard at the top of
    your tool's main script. Heavy imports must come after the guard so the
    installer's 5-second discovery probe doesn't time out.

    The output schema includes `tags` (defaults to `["GUI", "Icon"]` when
    unset) and `alias` (only when the tool is CLI-only or explicitly sets one).
    """
    items = list(metadata) if isinstance(metadata, (list, tuple)) else [metadata]
    out = []
    for m in items:
        tags = m.tags if m.tags else ["GUI", "Icon"]
        record = {
            "name": m.name,
            "desktop_file": m.desktop_file,
            "icon": m.icon,
            "desc": m.desc,
            "terminal": m.terminal,
            "args": m.args or [],
            "tags": tags,
        }
        # alias is surfaced when the tool is CLI-only (no Icon tag) OR when
        # an Icon tool explicitly opts into a shell alias alongside the .desktop.
        if "Icon" not in tags or m.alias:
            record["alias"] = m.alias or os.path.splitext(m.desktop_file)[0]
            if m.alias_args is not None:
                record["alias_args"] = list(m.alias_args)
        # Optional fields are emitted only when set, so a tool that never
        # touched them produces exactly the pre-0.2.0 record.
        for key in ("capability", "domain", "category", "skill_name", "skill_status"):
            value = getattr(m, key, None)
            if value:
                record[key] = value
        # Conditions are a list, so they get the same emit-only-when-set
        # treatment but keep their type.
        conditions = getattr(m, "autostart_conditions", None)
        if conditions:
            record["autostart_conditions"] = list(conditions)
        out.append(record)
    print(json.dumps(out))
    sys.exit(0)

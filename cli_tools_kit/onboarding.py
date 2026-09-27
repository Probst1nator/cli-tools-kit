"""First-run setup for an organisation adopting the installer.

Running the engine bare — ``cli-tool-installer`` in a fresh tree, with no
wrapper and no :class:`~cli_tools_kit.InstallerIdentity` — used to open a GUI
titled "probable.work - Tools Installer" that wrote ``ai_tools_manager.desktop``
and claimed the first-party alias file. That is never what a third party wants,
and they had no way to know it happened.

So the bare engine stops and offers setup instead. Two ways through it:

* **Hand the prompt to your coding agent** (default). :func:`agent_prompt`
  prints a self-contained brief. Paste it into Claude Code (or any agent with
  an ``AskUserQuestion``-style tool), and the agent interviews you for the four
  things it cannot guess, then writes the wrapper. This is the common case:
  the kit is mostly adopted from inside an agent session.
* **Answer here** (``--setup --interactive``). :func:`scripted_setup` asks the
  same four questions on stdin and writes the same file.

Both end at one generated ``installer.py`` produced by :func:`wrapper_source`,
so the two paths cannot drift apart.
"""

import os
import re
import sys
from typing import Optional

from .identity import InstallerIdentity

WRAPPER_NAME = "installer.py"


def _slugify(text: str) -> str:
    """Best-effort org name → slug: ``Acme Corp Tools`` → ``acme-corp-tools``."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "my-tools"


def wrapper_source(
    identity: InstallerIdentity,
    group_by: str = "capability",
    tools_dirname: Optional[str] = None,
) -> str:
    """The ``installer.py`` an organisation drops at the root of its tool tree.

    Deliberately short: every line that is not identity is a default worth
    keeping. ``tools_dirname`` names a subdirectory holding the tools when they
    do not sit directly at the tree root.
    """
    roots = ""
    if tools_dirname:
        roots = (
            f'\n    # Tools live in {tools_dirname}/ rather than beside this file.\n'
            f'    discovery_roots=[os.path.join(HERE, "{tools_dirname}")],'
        )
    title = identity.title or identity.display_title
    return f'''#!/usr/bin/env python3
"""Installer for the {title} tool tree.

Discovers every tool under this directory that answers --advertise, and lets you
install or remove its desktop entry, shell alias and Claude Code skill.

    python3 {WRAPPER_NAME}            # GUI
    python3 {WRAPPER_NAME} --list     # what was discovered
    python3 {WRAPPER_NAME} --check    # headless login reconciliation

See PROTOCOL.md in cli-tools-kit for what a tool must advertise to show up here.
"""

import os

from cli_tools_kit import InstallerIdentity
from cli_tools_kit.gui_installer import run

HERE = os.path.dirname(os.path.abspath(__file__))

# Everything this installer claims on a host derives from the slug: the config
# directory, the alias file, the icon cache, its own .desktop entry, the WM
# class, and the Keywords marker its orphan sweeper matches on. Change the slug
# and you move house — existing shortcuts keep the old names.
IDENTITY = InstallerIdentity(
    slug="{identity.slug}",
    title="{title}",
    icon="{identity.icon}",
)

if __name__ == "__main__":
    run(
        identity=IDENTITY,
        root_dir=HERE,
        entry_script=__file__,{roots}
        # "capability" bands the GUI rows by each tool's advertised capability
        # word; "category" bands by whatever label your discoverer assigns.
        group_by="{group_by}",
    )
'''


def agent_prompt(root_dir: str) -> str:
    """The brief a user pastes into their coding agent to be set up.

    Written to be read by an agent, not a person: it states the goal, the four
    unknowns, where to look things up, and what "done" means. It asks the agent
    to interview the user through a structured question tool rather than
    guessing, because three of the four answers are naming decisions that are
    expensive to change afterwards.
    """
    return f"""\
Set up a cli-tools-kit installer for my organisation in {root_dir}.

Context you need:
- cli-tools-kit is an installed Python package (`pip install cli-tools-kit`).
  The engine is `cli_tools_kit.gui_installer.run()`; the identity type is
  `cli_tools_kit.InstallerIdentity`. Read the package's README.md section
  "Reusing the installer in your org", and PROTOCOL.md for the tool-side
  `--advertise` contract. There is a complete working wrapper plus an example
  tool in the package repo under `examples/org-installer/` — copy that shape.
- The installer discovers tools by running each candidate's `main.py
  --advertise` (5 second timeout) and reading the JSON it prints.

Ask me, using your structured question tool (AskUserQuestion or equivalent) —
do not guess, these are naming decisions that are painful to change once
shortcuts exist on people's machines:
1. The slug: one lowercase token identifying my org's installer. It becomes
   ~/.config/<slug>/, ~/.<slug>_aliases, <slug>-installer.desktop, the WM
   class, and the desktop Keywords marker. Offer a couple of candidates
   derived from the directory name and from my organisation's name.
2. The window title users will see.
3. The desktop icon: a freedesktop icon name (e.g. system-software-install,
   applications-utilities) or an absolute path to a PNG.
4. Whether my tools sit directly in {root_dir} or in a subdirectory, and
   whether the installer should band rows by each tool's advertised
   `capability` word or by a `category` label I assign myself. Look at the
   directory first and propose what actually fits rather than asking blind.

Then:
- Write {root_dir}/{WRAPPER_NAME} using `InstallerIdentity` and `run()`. Keep
  it to identity plus root_dir/entry_script — every other knob has a default
  worth keeping. `python3 -m cli_tools_kit --setup --print-wrapper` prints a
  correct skeleton you can start from.
- Run `python3 {WRAPPER_NAME} --list` and show me what it discovered. If a tool
  I expected is missing, its `--advertise` is the thing to fix: it must print
  JSON and exit BEFORE any heavy import, or it trips the 5s timeout.
- Do NOT run `--install` for me. Tell me what it would install and let me
  decide.

If any of my existing tools do not yet answer `--advertise`, show me the
PROTOCOL.md skeleton and offer to add it to one tool as a worked example
before doing the rest.
"""


def _ask(prompt: str, default: str = "") -> str:
    """One stdin question with a shown default; empty answer takes the default."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1) from None
    return answer or default


def scripted_setup(root_dir: str) -> int:
    """Interview the user on stdin and write the wrapper. Returns an exit code."""
    print(f"\nSetting up a cli-tools-kit installer in {root_dir}\n")
    print("Four questions. Press Enter to take the default in brackets.\n")

    default_slug = _slugify(os.path.basename(os.path.abspath(root_dir)))
    while True:
        slug = _ask("Slug (lowercase, identifies your installer on the host)", default_slug)
        try:
            identity = InstallerIdentity(slug=slug)
            break
        except ValueError as exc:
            print(f"  {exc}\n")

    title = _ask("Window title", f"{slug} Tools")
    icon = _ask("Desktop icon (freedesktop name or absolute path)", "system-software-install")
    group_by = ""
    while group_by not in ("capability", "category"):
        group_by = _ask("Band rows by 'capability' or 'category'", "capability")

    identity = InstallerIdentity(slug=slug, title=title, icon=icon)
    target = os.path.join(root_dir, WRAPPER_NAME)

    if os.path.exists(target):
        if _ask(f"\n{target} exists. Overwrite? (y/N)", "N").lower() not in ("y", "yes"):
            print("Left it alone. Nothing written.")
            return 1

    with open(target, "w", encoding="utf-8") as fh:
        fh.write(wrapper_source(identity, group_by=group_by))

    print(f"\nWrote {target}\n")
    print("It will claim these names on this host:")
    print(f"  desktop entry   {identity.self_desktop_file}")
    print(f"  window class    {identity.self_wm_class}")
    print(f"  config          {identity.config_path}")
    print(f"  aliases         {identity.aliases_path}")
    print(f"  desktop marker  Keywords={identity.desktop_keywords}")
    print(f"\nNext: python3 {WRAPPER_NAME} --list")
    return 0


def print_onboarding(root_dir: str) -> int:
    """What the bare engine shows instead of opening a mis-branded GUI."""
    print(f"""
cli-tools-kit — no installer is configured for this tree.

Running the engine directly would open an installer that claims the default
first-party names on this host, which is almost certainly not what you want.
Set up your own instead; it is one small file.

  Paste the brief below into your coding agent (Claude Code or similar) and it
  will interview you and write it:

{"-" * 72}""")
    print(agent_prompt(os.path.abspath(root_dir)))
    print(f"""{"-" * 72}

  Or answer the same questions here:

      python3 -m cli_tools_kit --setup --interactive

  Or just print the wrapper skeleton and edit it yourself:

      python3 -m cli_tools_kit --setup --print-wrapper
""")
    return 0


def main(argv: Optional[list] = None) -> int:
    """``python3 -m cli_tools_kit`` — setup, and nothing else."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m cli_tools_kit",
        description="Set up a cli-tools-kit installer for your organisation.",
    )
    parser.add_argument("--setup", action="store_true",
                        help="show setup instructions (the default action)")
    parser.add_argument("--interactive", action="store_true",
                        help="answer the setup questions here instead of via an agent")
    parser.add_argument("--print-wrapper", action="store_true",
                        help="print an installer.py skeleton to stdout and exit")
    parser.add_argument("--slug", default="",
                        help="slug for --print-wrapper (default: this directory's name)")
    parser.add_argument("--root", default=os.getcwd(),
                        help="the tool tree to set up (default: current directory)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    root = os.path.abspath(args.root)

    if args.print_wrapper:
        slug = args.slug or _slugify(os.path.basename(root))
        print(wrapper_source(InstallerIdentity(slug=slug, title=f"{slug} Tools")), end="")
        return 0
    if args.interactive:
        return scripted_setup(root)
    return print_onboarding(root)

# The `--advertise` Installer Protocol

This document specifies the convention that lets a parent installer discover,
display, and install/remove a tree of self-describing Python tools.

A consuming repository (the "parent") scans for entry-point scripts that
respond to `--advertise` with a JSON list of metadata records. Each record
describes how that tool wants to be installed.

## The probe contract

The parent installer invokes each candidate script as:

```bash
python <script_path> --advertise
```

The script MUST:

1. Print a JSON list of one or more metadata dicts to stdout.
2. Exit with status 0.
3. Do this **before any heavy imports** — the parent enforces a 5-second
   timeout on the probe. Tools that import `requests`, `pandas`, or any other
   slow module before answering the probe will time out and disappear from
   the parent's listing.

## Metadata schema

Each dict in the list describes one installable variant of the tool.

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `name` | str | yes | — | Human-readable label shown in the GUI / list. |
| `desktop_file` | str | yes | — | Filename for the `.desktop` shortcut (e.g. `"my_tool.desktop"`). Also used as alias name if `alias` is unset. |
| `icon` | str | yes | — | Icon name (freedesktop name like `"utilities-terminal"`) or absolute path. |
| `desc` | str | yes | — | Short description shown under the name. |
| `terminal` | bool | no | `False` | If true, launches under `konsole -e`. |
| `args` | list[str] | no | `[]` | Extra CLI args appended to the entry-point invocation. |
| `tags` | list[str] | no | `["GUI", "Icon"]` | Capability tags — see below. |
| `alias` | str | conditional | (derived from `desktop_file`) | Bash alias name. Required when `Icon` is not in tags. |
| `categories` | str | no | `"Utility;"` | `.desktop` Categories field. |
| `skill_name` | str | no | — | Opt into Claude Code skill registration — see "Optional `skill_name`" below. |
| `skill_status` | str | no | — | When the tool registers a skill, it MAY report the installed copy's freshness so the parent can suggest an update. One of `"absent"`, `"current"`, `"stale"` — see "Reporting skill freshness" below. |
| `alias_args` | list[str] | no | (= `args`) | Args used in the bash alias only, when they should differ from `args` (e.g. an alias that auto-injects `--clip`). |
| `capability` | str | no | — | One controlled word naming what the tool does (`scrape`, `tts`, `agent`, …). Parents that group rows use this as the group key — see "Taxonomy" below. |
| `domain` | str | no | — | Free distinguisher inside a `capability` (`youtube`, `embedding`). |
| `category` | str | no | — | Legacy provenance: the folder the tool came from. Nothing keys on it; new tools may omit it. |
| `default_autostart` | bool | no | `False` | Pre-tick the parent's Auto-Start checkbox for this tool. A suggested default only — the user still owns the toggle. See "Autostart" below. |
| `cron_schedule` | str | no | — | Schedule for a non-`Icon` tool's autostart (e.g. `"@reboot"`). Without it, a tool with no `Icon` tag has no autostart mechanism at all. |
| `cron_args` | list[str] | no | `[]` | Args for the cron invocation, when they differ from `args`. |
| `autostart_conditions` | list[str] | no | `[]` | Condition kinds this tool's autostart supports: `"time_window"`, `"network"`. Declares only the kinds — the values are the user's and live in the installer's config. See "Conditional autostart" below. |

All of these are fields of `ToolMetadata`, and `advertise()` emits each optional
one only when it is set. A record that never touched them is byte-identical to
a pre-0.2.0 record, so a parent from either era reads it.

## Taxonomy (`capability` / `domain` / `category`)

`tags` say how a tool *installs* (GUI, CLI, Icon). `capability` says what it
*does*, in one flat controlled word, and is the field a parent installer groups
by — every `agent`, every `tts`, in one cluster regardless of folder. The
vocabulary is owned by the tree, not by this library: the reference list is
`CAPABILITY_VOCAB` in the `tools` monorepo's `validate_structure.py`, and a tree
that adds a word does so deliberately in the same change. Sub-entries of a
multi-variant tool share the parent's `capability`. "Registers a skill" is not
a capability.

Grouping by `capability` is the default, not a rule: a parent installer can set
`gui_installer.run(group_by="category")` to band its rows by the `category`
label its own discoverer assigns instead (the `tools` monorepo does this, with a
semantic group per tool held in a committed JSON file).

## Tags

Tools declare their capabilities via `tags`:

- **`GUI`** — has a graphical window (tkinter / Qt / GTK / etc.)
- **`CLI`** — runs in the terminal
- **`Icon`** — gets a `.desktop` shortcut in `~/.local/share/applications/`

Without `Icon`, the tool is installed as a bash alias in `~/.tools_aliases`
(which the installer auto-sources from `~/.bashrc` on first install). On
Windows there is no alias file: the same `alias` becomes a launcher script
(a shim) in `%LOCALAPPDATA%\<slug>\bin`, which is added to the user PATH.

| Tool type | Tags | `alias` field | Resulting install |
|---|---|---|---|
| GUI app with desktop icon | `["GUI", "Icon"]` | omit | `.desktop` file |
| Terminal app with desktop icon | `["CLI", "Icon"]` | omit | `.desktop` file |
| CLI-only (bash alias) | `["CLI"]` | required | bash alias |
| GUI + CLI with desktop icon | `["GUI", "CLI", "Icon"]` | omit | `.desktop` file |
| GUI + CLI without icon | `["GUI", "CLI"]` | required | bash alias |
| Desktop icon **and** shell alias | `["GUI", "CLI", "Icon"]` | set | `.desktop` file **and** bash alias |

The `alias` field is independent of `Icon`: set it to also install a bash
alias alongside a `.desktop` file (handy for GUI tools you also want to launch
from the terminal).

## Autostart (start on login)

**A tool never writes its own autostart entry.** The parent installer owns
autostart end to end: it creates the entry, toggles it per row, and removes it
again when the tool is removed. A tool's whole part is one advertised field.

Which mechanism applies follows from `tags`:

| Tool type | What the parent does on enable | Where the entry lives |
|---|---|---|
| has `Icon` | symlinks the tool's **installed** `.desktop` | `~/.config/autostart/<desktop_file>` (Linux) · a copy of the `.lnk` in Startup (Windows) |
| no `Icon`, has `cron_schedule` | adds one crontab line `<schedule> <python> <script> <cron_args>` | the user's crontab |
| no `Icon`, no `cron_schedule` | nothing — `enable_autostart` reports "no supported autostart method" | — |

Opting in is declarative:

```python
advertise(ToolMetadata(
    name="My Tool",
    desktop_file="my_tool.desktop",
    icon="utilities-terminal",
    desc="Does the thing",
    tags=["CLI", "Icon"],
    default_autostart=True,      # pre-ticks the box; the user still decides
))
```

Three things to know before building anything around this:

- **The `Icon` entry is a symlink under the same filename**, not a second file,
  and it runs the icon's `Exec` verbatim — entry point plus `args`, nothing else.
  If login should do something other than your normal launch, that has to be the
  icon's own behaviour (an argument-less menu, say), not a separate entry. The
  one exception is a tool with conditions configured — see below.
- **Never hand-write a `.desktop` into `~/.config/autostart`.** It is a parallel
  mechanism the parent cannot see, toggle or clean up, so it outlives removal of
  the tool and can double up with the real entry.
- **`default_autostart` is a suggestion, not state.** It only decides how the
  checkbox starts out. The live answer is the entry on disk, which the user may
  have changed since: read it with `is_autostart_enabled(tool)`.

An autostart entry starts your tool with no terminal and nobody watching, so
prefer a launch that is safe unattended and easy to interrupt over one that
immediately seizes a session.

### Conditional autostart

Some tools should start at login only sometimes — a study launcher during study
hours, a voice assistant on the home network and nowhere else. A tool says which
kinds of condition its autostart supports, and nothing more:

```python
advertise(ToolMetadata(
    name="Jarvis",
    desktop_file="jarvis.desktop",
    icon="audio-input-microphone",
    desc="Voice assistant",
    autostart_conditions=["network"],    # or ["time_window"], or both
))
```

| Condition | Asks | Stored as |
|---|---|---|
| `time_window` | is the clock inside a window? | `{"from": "06:00", "to": "12:00"}` |
| `network` | is one of these Wi-Fi networks active? | `{"ssids": [...], "grace_seconds": 120}` |

Declaring a condition adds a ⚙ beside that tool's Auto-Start checkbox. The
**values** are the user's: they are entered there and saved per host in the
installer's own config (`~/.config/<slug>/autostart.json`), never in the tool.
A tool that hardcodes someone's working hours or network names has put personal
configuration into a shared — often public — repository.

The values are the user's in a second sense too: a tool declaring a condition
gets no say in whether it applies. An unconfigured condition is simply not
enforced, so a tool must still behave correctly when started at any time, on any
network. Treat the condition as the user's convenience, not as a guarantee your
code may rely on.

Once conditions are configured, the parent writes the autostart entry as a real
file instead of the usual symlink, with the tool's own `Exec` wrapped in

```
cli-tools-kit-autostart-gate --slug <installer> --tool <desktop stem> -- <the tool's Exec>
```

The gate evaluates the conditions, execs the real command when they pass, and
exits 0 silently when they do not. The installed `.desktop` in the applications
directory is left alone, so launching from the menu is never gated. `time_window`
is checked once at launch; `network` polls for up to `grace_seconds`, since at
login the wireless link is usually still associating. Neither watches for later
changes — leaving the network does not stop a tool that already started.

Older parents ignore `autostart_conditions` and install the plain symlink, so a
tool that declares one still works against them; it just starts unconditionally.

## Minimal example

```python
#!/usr/bin/env python3
import sys
from cli_tools_kit import ToolMetadata, ToolInstaller, advertise

# MUST be before any heavy imports!
if "--advertise" in sys.argv:
    advertise(ToolMetadata(
        name="Git Commit Suggester",
        desktop_file="cg.desktop",
        icon="git",
        desc="AI-powered commit message suggestions",
        tags=["CLI"],
        alias="cg",
    ))

# Heavy imports AFTER the advertise guard
import argparse
from somewhere_slow import HeavyThing

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()

    installer = ToolInstaller(
        script_path=__file__,
        metadata=ToolMetadata(
            name="Git Commit Suggester",
            desktop_file="cg.desktop",
            icon="git",
            desc="AI-powered commit message suggestions",
            tags=["CLI"],
            alias="cg",
        ),
    )

    if args.install:
        installer.install()
    elif args.remove:
        installer.remove()

if __name__ == "__main__":
    main()
```

## Install output hints (recommended for CLI tools)

When `--install` prints a command the user should run next, prefix it with
`Run: ` on a single line. Compliant parent installer GUIs parse this and
surface a copy button:

```python
# Single command
print("Run: source ~/.bashrc")

# Multiple commands — join with &&
print("Run: source ~/.bashrc && cg --help")
```

`ToolInstaller` does this automatically for tools without an `Icon` tag.

## Cron lines (`CronInstaller`)

Tools that run on a schedule should use `CronInstaller` for idempotent, atomic
cron-line management:

```python
from cli_tools_kit import CronInstaller

cron = CronInstaller("my-tool")  # unique marker for this tool's lines

if args.install:
    installer.install()  # alias / desktop shortcut
    cron.install([
        f"@reboot cd {SCRIPT_DIR} && {sys.executable} {SCRIPT} --daemon",
        f"0 6 * * * cd {SCRIPT_DIR} && {sys.executable} {SCRIPT} --daily",
    ])
elif args.remove:
    installer.remove()
    cron.remove()
```

Each managed line gets a trailing `# cli-tool-kit:<marker>` comment so that:
- Re-installing the same lines is a no-op (idempotent).
- Removing strips only lines bearing this marker — other tools' cron entries
  are untouched.
- Multiple tools (each with their own marker) coexist in one user's crontab
  without colliding.

## Optional `skill_name` — Claude Code skill registration

If a tool can register a Claude Code skill (a `~/.claude/skills/<name>/SKILL.md`),
add `"skill_name": "<name>"` to its advertise dict. Compliant parent installer
GUIs then show a per-row **Skill** checkbox; checking it runs the tool's
`--install-skill`, unchecking runs `--uninstall-skill`. The Install checkbox
auto-checks Skill when toggled on.

Contract the tool must satisfy:

- Expose `--install-skill` and `--uninstall-skill` CLI flags, both idempotent.
- `--install-skill` writes `~/.claude/skills/<skill_name>/SKILL.md` from an
  inline `SKILL_MD_CONTENT` constant (single source of truth — never edit the
  on-disk file directly).
- `--uninstall-skill` removes that file and the empty dir.
- The tool's existing `--install` may call `_install_skill()` as a best-effort
  final step so direct CLI use stays one-shot.

The skill name is the directory under `~/.claude/skills/` and can differ from
the tool name (e.g. tool `studon-client` registers skill `studon`).

### Reporting skill freshness (`skill_status`)

A skill installed once can drift from the tool's bundled version as the tool
evolves. To let the parent installer *take note and suggest the update* — rather
than silently keeping a stale skill — a tool SHOULD also report `skill_status`
in its advertise dict:

| value | meaning |
|---|---|
| `"absent"` | the skill is not installed |
| `"current"` | the installed `SKILL.md` matches the bundled `SKILL_MD_CONTENT` |
| `"stale"` | installed, but the content differs — an update is available |

Compute it at advertise time (it must stay cheap — no heavy imports), e.g. for a
single-file skill:

```python
def _skill_status() -> str:
    if not SKILL_FILE.is_file():
        return "absent"
    return "current" if SKILL_FILE.read_text(encoding="utf-8") == SKILL_MD_CONTENT else "stale"

if "--advertise" in sys.argv:
    print(json.dumps([{**METADATA, "skill_status": _skill_status()}]))
    sys.exit(0)
```

For multi-file skills, `cli_tools_kit.skill_status(skill_name, bundled_files)`
does the comparison over a `{relative_path: text}` mapping (content-hash based,
order-independent), returning the same three values.

When a tool reports `"stale"`, a compliant parent flags the row (e.g.
`⟳ Skill update`), prints a suggestion at discovery time, and re-runs
`--install-skill` on apply to refresh the skill in place. Tools that omit
`skill_status` keep the old behaviour — the installer only distinguishes
installed-vs-absent.

## Known dialect: system-script menus

`prob_ubuntu_environment/main.py` reuses the `--advertise` *word* for a
different contract and is **not** a consumer of this protocol: scripts are bash
or Python, the probe returns a single dict (a list is tolerated, first element
wins), the record carries a `sudo` bool and free-form tags such as `update` or
`destructive`, the timeout is 3 s, and the parent *runs* scripts instead of
installing them. Do not point a compliant parent at that tree, and do not expect
its scripts to answer a compliant probe.

## When reinstallation is required

`.desktop` files contain hardcoded absolute paths. After certain changes you
must `--remove` then `--install` to update them.

| Change | Reinstall? | Reason |
|---|---|---|
| Tool directory renamed/moved | **yes** | Path in `.desktop` file is now invalid |
| `.desktop` filename changed | **yes** | Old file remains, new one not created |
| New entry points added | **yes** | New shortcuts don't exist yet |
| Icon or display name changed | **yes** | Stored in `.desktop` file |
| New dependencies in `requirements.txt` | **yes** | Need `pip install` |
| Code changes in `.py` files | no | Script re-read on each launch |
| Internal module changes | no | Python reloads on each run |
| Data directory changes | no | Paths resolved dynamically in code |

## The sources file (`installer.toml`)

Where the `--advertise` probe describes one tool, this file describes where the
tools come from: a parent installer that offers tools from more than one repo
lists them here. `cli_tools_kit.sources` reads it; README § Sources has the
worked example.

The file sits next to the installer and is tracked. It holds an array of tables:

```toml
[[source]]
name = "acme/lab"
path = "lab-tools"
url = "https://github.com/acme/lab-tools"
```

| Key | Required | Meaning |
|---|---|---|
| `name` | yes, unless `org` | The source's identity. Slashes make directory levels, so `acme/lab` clones to `<root>/acme/lab`. A table with neither a name nor an org is reported and dropped. |
| `path` | no | A checkout to use as it is, relative to the file it is written in. `~` is expanded. |
| `url` | no | Where to clone from when no path is on disk. Must be `https://`; anything else is reported and dropped. |
| `org` | no | A GitHub organisation whose topic-tagged repos each become a source named after the repo. Mutually exclusive with `name`, `url` and `path`. |
| `topic` | no | The topic a repo of that org needs to be offered. `cli-tool-kit` when omitted. |
| `exclude` | no | Repo names of that org to skip. |
| `include` | no | Repo names of that org to offer, an allowlist. Overrides `exclude`; `topic` is still required. |

An `org` entry is expanded before resolution: the org's repos are listed once,
the tagged and non-archived ones become ordinary sources, and the four steps
below then apply to each unchanged. An explicit `[[source]]` whose `name`
matches a listed repo wins over the listing. The listing is cached for a day,
`--refresh` fetches again, a failure falls back to the cache and then to the
directories under the root, and `--check` never fetches. README § Sources has
the details.

A top-level `root` is **not** written in this file. It is a per-machine fact and
belongs in `installer.local.toml`.

`installer.local.toml` sits next to `installer.toml`, is optional, and is
gitignored by convention. It holds a top-level `root` (a string, `~` expanded)
and `[[source]]` tables matched to the tracked file by `name`, each adding or
replacing that source's `path`. A `url` is never overridden and an unmatched
name is ignored.

Resolution order for one source, first hit wins:

1. `path` from `installer.local.toml`, if that directory exists.
2. `path` from `installer.toml`, if that directory exists.
3. `<root>/<name>`, if that directory exists.
4. A full clone of `url` into `<root>/<name>`.

The root is the `--root DIR` flag if given, else `root` from the local file, else
the user's answer to "Where should the tools be installed?". The answer is
written into `installer.local.toml` as its `root`, above any `[[source]]` tables
the file already holds, so the question is asked once per machine. A `root` that
is already in that file is never overwritten. The suggestion is
`<current directory>/<name>` as an absolute path, where `<name>` is the
`default_root_name` the wrapper passed to `run_installer` (`tools` by default).
The chosen directory is created if it does not exist.

A headless run — `--list`, `--apply`, `--check`, or the text screen without a
terminal — never asks. It takes the suggestion and prints one line:

```
root: /home/me/tools (pass --root to change)
```

A resolved repo may hold an `installer.toml` of its own. Its `[[source]]` tables
are read and resolved the same way, one nested level deep and no further, with
paths relative to that file and clones under the same root. A path that has
already been resolved is not visited again, so a file pointing back at its
parent cannot loop, and the result lists each path once.

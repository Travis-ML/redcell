"""Bash command policy: command-aware matching for the permission engine.

The permission engine's generic content matcher only does substring matching. For
shell tools (``run_command``/``run_script``) that's too blunt — you want to allow
``git status`` but not ``git push``, or deny ``curl`` anywhere in a compound. This
module provides command-aware matching with the hardening patterns the harness
documented, which double as redcell's red-team bypass matrix:

- **exact** (``git status``), **prefix** (``git:*``), **wildcard** (``git *``);
- **compound guard**: an *allow* rule never matches a compound command
  (``a && b``, ``a | b``, ``a; b``) so ``cd:*`` can't bless ``cd /x && curl evil``;
  *deny*/*ask* rules DO match any sub-command of a compound;
- **wrapper stripping** (``timeout``/``nice``/``env``/``xargs``…) before matching;
- **env-var asymmetry**: allow rules strip only a safe env allowlist, so
  ``LD_PRELOAD=x cmd`` won't slip through an allow; deny/ask strip all env.

Two special content tokens wire the dangerous-exec/removal detectors into rules:
``EXEC`` matches any interpreter/exec/exfil command, ``RM`` any dangerous removal.
"""

from __future__ import annotations

import re

# Operators that chain or expand a command — their presence makes it "compound".
_COMPOUND = re.compile(r"\|\||&&|;|\||\n|`|\$\(")
_SPLIT = re.compile(r"\|\||&&|;|\||\n")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Command wrappers that delegate to a following command; peeled before matching.
_WRAPPERS = frozenset(
    {"timeout", "nice", "nohup", "time", "env", "xargs", "stdbuf", "ionice", "setsid"}
)
# Env vars safe to ignore in an allow rule (deliberately EXCLUDES PATH, LD_PRELOAD,
# LD_LIBRARY_PATH, PYTHONPATH, NODE_OPTIONS — those change what code runs).
_SAFE_ENV = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "HOME", "USER", "PWD", "SHLVL"})

# Commands that hand the model arbitrary execution or network exfiltration.
DANGEROUS_EXEC = frozenset(
    {
        "python",
        "python3",
        "node",
        "nodejs",
        "ruby",
        "perl",
        "php",
        "lua",
        "bash",
        "sh",
        "zsh",
        "ksh",
        "fish",
        "dash",
        "eval",
        "exec",
        "source",
        "env",
        "xargs",
        "sudo",
        "su",
        "doas",
        "npx",
        "bunx",
        "pip",
        "gem",
        "make",
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "gh",
    }
)


def is_compound(command: str) -> bool:
    """True if the command chains/pipes/expands (so it's more than one action)."""
    return bool(_COMPOUND.search(command))


def split_commands(command: str) -> list[str]:
    """Split a (possibly compound) command into its sub-commands."""
    return [p.strip() for p in _SPLIT.split(command) if p.strip()]


def strip_wrappers(command: str) -> str:
    """Peel leading wrapper commands (timeout/nice/env/…) and their flags.

    Best-effort: bails (returns the input unchanged) if a wrapper's args contain
    shell expansion, so a crafted ``timeout -k$(id) 10 ls`` is not naively peeled.
    """
    if "$" in command or "`" in command:
        return command
    tokens = command.split()
    i = 0
    while i < len(tokens) and tokens[i] in _WRAPPERS:
        i += 1
        # Consume the wrapper's own flags/numeric args (e.g. `nice -n 5`,
        # `timeout 10`, `timeout -k 5 10`) up to the wrapped command.
        while i < len(tokens) and (tokens[i].startswith("-") or tokens[i].isdigit()):
            i += 1
    return " ".join(tokens[i:]) or command


def strip_env_assignments(command: str, *, safe_only: bool) -> str:
    """Drop leading ``FOO=bar`` assignments. ``safe_only`` keeps unsafe ones in place.

    For an allow rule (``safe_only=True``), an unsafe assignment (e.g. ``LD_PRELOAD``)
    stops the stripping, so the command no longer matches a bare-command allow rule.
    """
    tokens = command.split()
    i = 0
    while i < len(tokens) and _ENV_ASSIGN.match(tokens[i]):
        if safe_only and tokens[i].split("=", 1)[0] not in _SAFE_ENV:
            break
        i += 1
    return " ".join(tokens[i:])


def _base_command(sub: str) -> str:
    """First word of a sub-command, basename-stripped (``/usr/bin/python`` → ``python``)."""
    words = sub.split()
    if not words:
        return ""
    return words[0].rsplit("/", 1)[-1]


def is_dangerous_command(command: str) -> bool:
    """True if any sub-command invokes an interpreter / exec / exfil tool."""
    normalized = strip_env_assignments(strip_wrappers(command), safe_only=False)
    return any(_base_command(sub) in DANGEROUS_EXEC for sub in split_commands(normalized))


def is_dangerous_removal(command: str) -> bool:
    """True if any sub-command is a destructive ``rm``/``rmdir`` of a sensitive path."""
    for sub in split_commands(strip_env_assignments(strip_wrappers(command), safe_only=False)):
        words = sub.split()
        if not words or _base_command(sub) not in {"rm", "rmdir"}:
            continue
        targets = [w for w in words[1:] if not w.startswith("-")]
        if "-rf" in sub or "-fr" in sub or any(w.startswith("-") and "r" in w for w in words[1:]):
            if not targets or any(_is_sensitive_path(t) for t in targets):
                return True
        if any(_is_sensitive_path(t) for t in targets):
            return True
    return False


def _is_sensitive_path(path: str) -> bool:
    p = path.rstrip("/")
    return (
        p in ("", "/", "~", "*", "/*", ".", "..")
        or p.startswith("/etc")
        or p.startswith("/usr")
        or p.startswith("/var")
        or p.startswith("/boot")
        or p.startswith("~")
        or p.endswith("/*")
    )


def match_command(rule_content: str, command: str) -> bool:
    """Match a single (non-compound) command against one rule content shape.

    Shapes: ``git status`` (multi-word = exact), ``curl`` (bare word = match by
    command name, so it covers ``curl http://x``), ``git:*`` (prefix, word
    boundary), ``git *`` (wildcard regex).
    """
    rc, cmd = rule_content.strip(), command.strip()
    if rc.endswith(":*"):  # prefix shape, word-boundary
        prefix = rc[:-2].strip()
        return cmd == prefix or cmd.startswith(prefix + " ")
    if "*" in rc:  # wildcard shape -> regex
        return re.fullmatch(re.escape(rc).replace(r"\*", ".*"), cmd) is not None
    if " " not in rc:  # bare command word -> match by command name
        return cmd == rc or cmd.startswith(rc + " ")
    return cmd == rc  # multi-word exact


def command_rule_matches(behavior: str, rule_content: str, raw_command: str) -> bool:
    """Whether a shell rule (``behavior``/``rule_content``) matches ``raw_command``."""
    safe_only = behavior == "allow"
    command = strip_env_assignments(strip_wrappers(raw_command), safe_only=safe_only)
    subs = split_commands(command)

    token = rule_content.strip().upper()
    if token == "EXEC":
        return is_dangerous_command(raw_command)
    if token == "RM":
        return is_dangerous_removal(raw_command)

    if behavior == "allow":
        # An allow must be a single, whole command — never blesses a compound.
        return len(subs) == 1 and match_command(rule_content, subs[0])
    # deny / ask: match if ANY sub-command matches (compounds included).
    return any(match_command(rule_content, sub) for sub in subs)


def extract_command(arguments: dict) -> str | None:
    """Pull the command/script string out of a shell tool call's arguments."""
    for key in ("command", "cmd", "script", "commands"):
        val = arguments.get(key)
        if isinstance(val, str):
            return val
    for val in arguments.values():  # fall back to the first string argument
        if isinstance(val, str):
            return val
    return None

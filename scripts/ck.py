#!/usr/bin/env python3
"""context-keeper: keep conversational nuance alive across Claude Code compaction.

One stdlib-only entry point for everything the context-keeper skill
needs that should not depend on the model:

* ``hook <Event>``      handlers registered in ~/.claude/settings.json
                        (SessionStart, Stop, PostToolBatch, PreCompact, PostCompact)
* ``path`` / ``saved``  resolve and register this session's note
* ``card`` / ``status`` show the resume card and the current context usage
* ``rollup``            mechanical roll-up of decisions into decisions.md / archive
* ``install-hooks`` / ``uninstall-hooks`` / ``disable`` / ``enable``
* ``--self-test``

Hook handlers fail open: on any unexpected input or error they exit 0 and print
nothing. The only non-zero exit is the deliberate one-time block of a manual
``/compact`` while the note is stale.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path


SKILL_NAME = "context-keeper"
DATA_DIRNAME = ".context-keeper"
GITIGNORE_BODY = "# managed by context-keeper\nsessions/\nraw/\nstate/\n*.lock\n*.tmp\n"

BANDS = (60, 75, 90)
MAX_NUDGES_PER_EPOCH = 3
EMERGENCY_RATIO = 0.10
EMERGENCY_MIN_TOKENS = 25_000
BATCH_CHECK_BYTES = 20_000
STALE_HUMANS_SAVED = 2
STALE_HUMANS_NEVER = 3
STALE_CHARS = 3_000
BLOCK_COOLDOWN_SEC = 600
NOTE_WARN_CHARS = 5_000
NOTE_MAX_CHARS = 8_000
CARD_BUDGET_UNITS = 6_000
POINTER_MAX_NOTES = 3
POINTER_MAX_AGE_DAYS = 14
ACTIVE_ELSEWHERE_SEC = 600
DECISIONS_MAX = 200
DECISIONS_KEEP = 150
RETENTION_DAYS = 30
LOCK_STALE_SEC = 30
WINDOW_1M = 967_000
WINDOW_200K = 200_000
TAIL_START_BYTES = 256 * 1024
TAIL_MAX_BYTES = 64 * 1024 * 1024
RAW_HUMAN_MAX = 20_000
RAW_ASSISTANT_MAX = 8_000

SECTIONS = (
    "目的",
    "決定と理由",
    "却下した案",
    "ユーザーの訂正・好み",
    "制約",
    "用語",
    "現在の状態",
    "次の一手",
    "未解決",
)
CARD_ORDER = (
    "現在の状態",
    "次の一手",
    "ユーザーの訂正・好み",
    "制約",
    "決定と理由",
    "未解決",
    "却下した案",
    "用語",
    "目的",
)
HOOK_TIMEOUTS = {
    "SessionStart": 15,
    "Stop": 15,
    "PostToolBatch": 10,
    "PreCompact": 30,
    "PostCompact": 15,
    "PostModelSwitch": 10,
}

SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
DECISION_RE = re.compile(r"^\s*[-*]\s*\[(D-(\d{8})-(?:[0-9A-Za-z]{2,8}-)?\d+)\]\s*(.+?)\s*$")
SUPERSEDES_RE = re.compile(r"\(supersedes:\s*([^)]*)\)")
DECISION_ID_RE = re.compile(r"D-\d{8}-(?:[0-9A-Za-z]{2,8}-)?\d+")
SOURCE_TAG_RE = re.compile(r"\s*〔[^〕]*〕\s*$")
MODEL_RE = re.compile(r"(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?!\d)")


class CkError(RuntimeError):
    """A user-facing error for the CLI subcommands."""


# --------------------------------------------------------------------------- io


def now_ts() -> str:
    """UTC timestamp in the transcript's own format, so string order == time order."""
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "不明"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "たった今"
    if seconds < 3600:
        return f"{seconds // 60}分前"
    if seconds < 86400:
        return f"{seconds // 3600}時間前"
    return f"{seconds // 86400}日前"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def write_text_atomic(path: Path, text: str, follow_symlink: bool = False) -> None:
    # Only the user's own settings.json is followed (dotfiles setups); a symlink inside project data
    # (which may come from a cloned repo) is replaced, never written through.
    if follow_symlink:
        with contextlib.suppress(OSError):
            if path.is_symlink():
                path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n") as handle:
            handle.write(text)
        # mkstemp creates 0600; keep the existing mode, or the umask default for new files.
        with contextlib.suppress(OSError):
            if path.exists() and not path.is_symlink():
                shutil.copymode(path, tmp)
            else:
                os.chmod(tmp, 0o666 & ~_umask())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: target briefly open elsewhere
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise CkError(f"refusing to append through a symlink: {path}")
    with open(path, "a", encoding="utf-8", errors="replace", newline="\n") as handle:
        handle.write(text)


def load_json(path: Path, default):
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def save_json(path: Path, data) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 5.0):
    """O_EXCL lock file; a lock older than LOCK_STALE_SEC is considered abandoned."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_SEC:
                    path.unlink()
                    continue
            except OSError:
                pass  # vanished or not removable: fall through to the deadline check
            if time.monotonic() > deadline:
                raise CkError(f"lock busy, or a stale lock cannot be removed: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")


def emit_json(obj: dict) -> None:
    # ASCII-escaped JSON survives any console code page (cp932 included).
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")


# ---------------------------------------------------------------------- project


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def private_dir() -> Path:
    """Per-user scratch for the probe cache and error log (never the shared /tmp)."""
    path = config_dir() / "context-keeper"
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def main_checkout_from_gitfile(gitfile: Path) -> Path | None:
    """`.git` file of a linked worktree -> the main checkout (submodules stay put)."""
    try:
        match = re.match(r"gitdir:\s*(.+)", read_text(gitfile).strip())
    except OSError:
        return None
    if not match:
        return None
    gitdir = Path(match.group(1).strip())
    if not gitdir.is_absolute():
        gitdir = (gitfile.parent / gitdir).resolve()
    parts = gitdir.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == ".git":
            if index + 1 < len(parts) and parts[index + 1] == "worktrees":
                return Path(*parts[:index])
            return None
    return None


def find_project_root(start: Path) -> Path:
    start = Path(os.path.abspath(start))
    home = None
    with contextlib.suppress(OSError, RuntimeError):
        home = Path.home().resolve()
    for directory in (start, *start.parents):
        if home is not None and directory != start and directory.resolve() == home:
            break  # a dotfiles repo in ~/.git must not swallow every non-git project
        git = directory / ".git"
        if git.is_dir():
            return directory
        if git.is_file():
            return main_checkout_from_gitfile(git) or directory
    return start


def project_start(explicit: str | None = None, hook_input: dict | None = None) -> Path:
    for candidate in (explicit, os.environ.get("CLAUDE_PROJECT_DIR"), (hook_input or {}).get("cwd")):
        if candidate:
            return Path(candidate)
    return Path.cwd()


class Ctx:
    """Paths and state for one project + one session."""

    def __init__(self, root: Path, session_id: str | None, start: Path | None = None):
        if session_id is not None and not SESSION_RE.match(session_id):
            raise CkError(f"invalid session id: {session_id!r}")
        self.root = root
        self.start = Path(os.path.abspath(start)) if start else root
        self.data = root / DATA_DIRNAME
        self.sid = session_id
        self._state: dict | None = None

    @classmethod
    def for_start(cls, start: Path, session_id: str | None) -> "Ctx":
        return cls(find_project_root(start), session_id, start)

    # --- activation
    @property
    def sid8(self) -> str:
        return (self.sid or "")[:8]

    def inactive_reason(self) -> str | None:
        if truthy(os.environ.get("CONTEXT_KEEPER_DISABLE")):
            return "CONTEXT_KEEPER_DISABLE is set"
        with contextlib.suppress(OSError):
            if self.root.resolve() == Path.home().resolve():
                return "home directory is not treated as a project"
        if (self.data / "disabled").exists():
            return f"disabled for this project ({self.data / 'disabled'})"
        return None

    def ensure(self) -> None:
        for path in (self.data, *(self.data / sub for sub in ("sessions", "raw", "state", "archive"))):
            if path.is_symlink():
                raise CkError(f"refusing a symlinked data directory: {path}")
        for sub in ("sessions", "raw", "state", "archive"):
            (self.data / sub).mkdir(parents=True, exist_ok=True)
        for private in ("raw", "state"):  # dialogue dumps stay readable by the owner only
            with contextlib.suppress(OSError):
                os.chmod(self.data / private, 0o700)
        gitignore = self.data / ".gitignore"
        if not gitignore.exists():
            write_text_atomic(gitignore, GITIGNORE_BODY)

    # --- files
    def note_path(self) -> Path:
        existing = self.find_note()
        if existing:
            return existing
        today = dt.datetime.now().strftime("%Y%m%d")
        return self.data / "sessions" / f"{today}_{self.sid8}.md"

    def find_note(self) -> Path | None:
        if not self.sid:
            return None
        matches = sorted((self.data / "sessions").glob(f"*_{self.sid8}.md"))
        return matches[0] if matches else None

    @property
    def raw_path(self) -> Path:
        return self.data / "raw" / f"{self.sid}.md"

    @property
    def state_path(self) -> Path:
        return self.data / "state" / f"{self.sid}.json"

    @property
    def decisions_path(self) -> Path:
        return self.data / "decisions.md"

    @property
    def calibration_path(self) -> Path:
        return self.data / "state" / "calibration.json"

    # --- state
    def state(self) -> dict:
        if self._state is None:
            base = {
                "epoch": 0,
                "epoch_ts": None,
                "bands": [],
                "nudges": 0,
                "emergency": False,
                "last_nudge_ts": None,
                "saved_offset": None,
                "saved_at": None,
                "dump_offset": 0,
                "last_block_ts": 0,
                "transcript_path": None,
                "model": None,
                "band_window": None,
                "stale_at_compact": None,
            }
            stored = load_json(self.state_path, {}) if self.sid else {}
            base.update(stored)
            self._state = base
        return self._state

    def save_state(self) -> None:
        if self.sid:
            self.ensure()
            save_json(self.state_path, self.state())

    def transcript(self, hint: str | None = None) -> Path | None:
        candidates = [hint, self.state().get("transcript_path")]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        if self.sid:
            for match in (config_dir() / "projects").glob(f"*/{self.sid}.jsonl"):
                return match
        return None


# ------------------------------------------------------------------- transcript


def _entry_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return ""
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return ""


def is_compact_marker(entry: dict) -> bool:
    return bool(entry.get("isCompactSummary")) or (
        entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"
    )


def choice_text(entry: dict) -> str | None:
    """Answers the user gave through AskUserQuestion: decisions, so they count as human."""
    result = entry.get("toolUseResult")
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        return None
    lines = [f"[選択] {question} → {answer}" for question, answer in result["answers"].items()]
    return "\n".join(lines) or None


def human_text(entry: dict) -> str | None:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    if entry.get("isCompactSummary"):
        return None
    choice = choice_text(entry)
    if choice:
        return choice
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    text = _entry_text(message.get("content"))
    if not text.strip():
        return None
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return text if origin.get("kind") == "human" else None
    return None if text.lstrip().startswith("<") else text


def assistant_text(entry: dict) -> str | None:
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("model") == "<synthetic>":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    text = "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return text if text.strip() else None


def usage_of(entry: dict) -> tuple[int, str] | None:
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    model = str(message.get("model") or "")
    usage = message.get("usage")
    if model == "<synthetic>" or not isinstance(usage, dict):
        return None
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return (total, model) if total > 0 else None


def _iter_lines(chunk: bytes):
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def last_usage(path: Path, since_ts: str | None = None) -> tuple[int, str, str] | None:
    """Latest main-chain usage (tokens, model, timestamp) after the last compaction."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    window = TAIL_START_BYTES
    with open(path, "rb") as handle:
        while True:
            start = max(0, size - window)
            handle.seek(start)
            chunk = handle.read(size - start)
            if start > 0:
                newline = chunk.find(b"\n")
                chunk = chunk[newline + 1 :] if newline >= 0 else b""
            found = None
            boundary = False
            for line in chunk.split(b"\n"):
                if b'"usage"' not in line and b"compact" not in line.lower():
                    continue
                for entry in _iter_lines(line):
                    if is_compact_marker(entry):
                        found, boundary = None, True
                        continue
                    usage = usage_of(entry)
                    if not usage:
                        continue
                    stamp = str(entry.get("timestamp") or "")
                    if since_ts and stamp and stamp < since_ts:
                        continue
                    found = (usage[0], usage[1], stamp)
            if found or boundary or start == 0 or window >= TAIL_MAX_BYTES:
                return found
            window *= 4


def read_dialogue(path: Path, offset: int) -> tuple[list[tuple[str, str, str]], int]:
    """Human prompts and visible assistant text from `offset`; returns (turns, new_offset)."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if offset > size:
        offset = 0
    turns: list[tuple[str, str, str]] = []
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(size - offset)
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    data = data[: end + 1]
    for line in data.split(b"\n"):
        if (b'"type":"tool_result"' in line and b'"answers"' not in line) or b'"type":"tool_use"' in line:
            continue
        for entry in _iter_lines(line):
            stamp = str(entry.get("timestamp") or "")
            text = human_text(entry)
            if text is not None:
                turns.append(("user", stamp, text))
                continue
            text = assistant_text(entry)
            if text is not None:
                turns.append(("assistant", stamp, text))
    return turns, offset + len(data)


def transcript_size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


# ----------------------------------------------------------------------- window


def canonical_model(model: str | None) -> str:
    value = (model or "").strip().lower()
    value = value.replace("[1m]", "")
    value = re.sub(r"^(?:[a-z]{2}\.)?anthropic\.", "", value)
    value = re.sub(r"-v\d+(?::\d+)?$", "", value)
    value = re.sub(r"@\d{8}$", "", value)
    value = re.sub(r"-\d{8}$", "", value)
    return value


def parse_window(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().lower().replace(",", "").replace("_", "")
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", text)
        if not match:
            return None
        number = float(match.group(1))
        if match.group(2) == "k":
            number *= 1_000
        elif match.group(2) == "m":
            number *= 1_000_000
    if 100 <= number <= 1000:
        number *= 1_000
    return int(number) if number >= 10_000 else None


def _family(model: str | None) -> tuple[str, int, int] | None:
    match = MODEL_RE.search(canonical_model(model))
    if not match:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3) or 0)


def _third_party() -> bool:
    return any(
        truthy(os.environ.get(name))
        for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
    )


def default_window(model: str | None) -> tuple[int, str]:
    """Where Claude Code compacts when no window is configured (model-config: default thresholds)."""
    if truthy(os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT")):
        return WINDOW_200K, "default (CLAUDE_CODE_DISABLE_1M_CONTEXT)"
    if "[1m]" in (model or "").lower():
        return WINDOW_1M, "default ([1m] model)"
    family = _family(model)
    if family:
        name, major, minor = family
        # Sonnet 5+ and Fable run with 1M on every provider; Opus 4.7+ only on the Anthropic API.
        if name == "fable" or (name == "sonnet" and major >= 5):
            return WINDOW_1M, f"default (native 1M: {canonical_model(model)})"
        if name == "opus" and (major, minor) >= (4, 7) and not _third_party():
            return WINDOW_1M, f"default (native 1M: {canonical_model(model)})"
    return WINDOW_200K, f"default (200K: {canonical_model(model) or 'unknown model'})"


def managed_settings_path() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if os.name == "nt":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClaudeCode" / "managed-settings.json"
    return Path("/etc/claude-code/managed-settings.json")


def window_from_settings(data: dict, model: str | None) -> int | None:
    model_settings = data.get("modelSettings")
    canon = canonical_model(model)
    if isinstance(model_settings, dict) and canon:
        for key, value in model_settings.items():
            key_canon = canonical_model(str(key))
            if isinstance(value, dict) and key_canon and (canon == key_canon or canon.startswith(key_canon)):
                parsed = parse_window(value.get("autoCompactWindow"))
                if parsed:
                    return parsed
    return parse_window(data.get("autoCompactWindow"))


def effective_model(ctx: Ctx, model: str | None) -> str | None:
    """Transcripts carry the bare API id; the model the session selected may add [1m]."""
    selected = selected_model(ctx.sid)
    if not (isinstance(selected, str) and "[1m]" in selected.lower() and model):
        return model
    chosen, actual = canonical_model(selected), canonical_model(model)
    # Full id ('claude-opus-4-6[1m]') or alias ('opus[1m]') of the same family both count.
    if chosen == actual or (chosen in ("opus", "sonnet", "haiku", "fable") and f"{chosen}-" in actual):
        return f"{model}[1m]"
    return model


def model_cap(model: str | None) -> int:
    """Claude Code caps a configured window at the model's context window; cap only when that is surely 200K."""
    if truthy(os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT")):
        return WINDOW_200K
    if "[1m]" in (model or "").lower():
        return 1_000_000
    family = _family(model)
    if family:
        name, major, minor = family
        if name == "haiku" or (name == "opus" and (major, minor) < (4, 6)) or (name == "sonnet" and (major, minor) < (4, 6)):
            return WINDOW_200K
    return 1_000_000  # 4.6 models have a [1m] variant we may not see; unknown ids: don't guess low


def calibration_key(model: str | None) -> str:
    return canonical_model(model) + ("[1m]" if "[1m]" in (model or "").lower() else "")


def _env_window() -> int | None:
    value = (os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or "").strip()
    return int(value) if value.isdigit() and int(value) >= 10_000 else None  # plain token count only


def resolve_window(ctx: Ctx, model: str | None, observed: int | None = None) -> tuple[int, str]:
    model = effective_model(ctx, model)
    window, source = _env_window(), "env CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    if window is None:
        start = ctx.start
        files = [("managed settings", managed_settings_path())]
        for label, name in (("local settings", "settings.local.json"), ("project settings", "settings.json")):
            for base in dict.fromkeys((start, ctx.root)):  # session dir first, then repository root
                files.append((label, base / ".claude" / name))
        files.append(("user settings", config_dir() / "settings.json"))
        for label, path in files:
            value = window_from_settings(load_json(path, {}), model)
            if value:
                window, source = value, f"{label} autoCompactWindow"
                break
    if window is None:
        key = calibration_key(model)
        calibration = load_json(ctx.calibration_path, {})
        value = calibration.get(key) if key else None
        if isinstance(value, int) and value > 0:
            if observed and observed > value * 1.02:
                calibration.pop(key, None)  # stale (e.g. a one-launch --autocompact); fall back below
                with contextlib.suppress(OSError, CkError):
                    save_json(ctx.calibration_path, calibration)
            else:
                window, source = value, "calibrated from last auto-compaction"
    configured = window
    if window is None:
        window, source = default_window(model)
    elif window > model_cap(model):
        window = model_cap(model)
        source += f" → capped at the model window {window // 1000}K"
    if observed and observed > window * 1.02:
        # Our view of the model was wrong: step up to the next plausible line, not straight past it.
        ladder = sorted({v for v in (configured, model_cap(model), WINDOW_1M) if v})
        window = next((v for v in ladder if v >= observed * 1.02), int(observed * 1.05))
        source += " → raised (observed usage exceeded it)"
    return window, source


# ------------------------------------------------------------------------ notes


def note_sections(text: str) -> dict[str, str]:
    """Split on `## ` headings; a `### ` heading starts a section only if it is exactly a section name."""
    sections: dict[str, str] = {}
    current = None
    buffer: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^(#{2,3})\s+(.+?)\s*$", line)
        title = heading.group(2) if heading else ""
        is_section = bool(heading) and (
            (heading.group(1) == "##") or title in SECTIONS
        )
        if is_section:
            if current is not None:
                _merge_section(sections, current, buffer)
            current = next((name for name in SECTIONS if title.startswith(name)), title)
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        _merge_section(sections, current, buffer)
    return sections


def _merge_section(sections: dict[str, str], name: str, buffer: list[str]) -> None:
    body = "\n".join(buffer).strip()
    sections[name] = f"{sections[name]}\n{body}".strip() if name in sections else body


def staleness(ctx: Ctx, transcript: Path | None) -> tuple[bool, int, int]:
    """(stale, human prompts since save, visible chars since save)."""
    if not transcript:
        return False, 0, 0
    state = ctx.state()
    saved = state.get("saved_offset")
    turns, _ = read_dialogue(transcript, int(saved or 0))
    humans = sum(1 for role, _, _ in turns if role == "user")
    chars = sum(len(text) for _, _, text in turns)
    if saved is None:
        return humans >= STALE_HUMANS_NEVER, humans, chars
    return humans >= STALE_HUMANS_SAVED or chars >= STALE_CHARS, humans, chars


def build_card(ctx: Ctx, budget: int = CARD_BUDGET_UNITS) -> str:
    note = ctx.find_note()
    footer = (
        f"\n全文: {note or ctx.note_path()} / 決定ログ: {ctx.decisions_path} / "
        f"圧縮直前の対話原文: {ctx.raw_path}\n"
        f"ノートの更新は skill `{SKILL_NAME}` の save 手順で行う。"
    )
    if not note:
        if ctx.raw_path.exists():
            return (
                f"context-keeper: このセッションの作業ノートは未保存。圧縮直前の対話原文は {ctx.raw_path} に退避済み"
                f"(細部が必要なら grep する)。ノートは skill `{SKILL_NAME}` の save 手順で作れる。"
            )
        return ""
    state = ctx.state()
    saved_age = ago(time.time() - (parse_ts(state.get("saved_at")) or note.stat().st_mtime))
    behind = state.get("stale_at_compact") or {}
    if behind.get("humans") or behind.get("chars", 0) >= STALE_CHARS:
        rule = (
            f"注意: このノートは保存後の人間の発言 {behind.get('humans', 0)} 件(約 {behind.get('chars', 0)} 字)を反映していない。"
            "保存後に変わった訂正・制約・決定・却下・用語・状態は圧縮要約(細部は対話原文)を優先し、"
            "要約に無い項目だけこのカードを使う。\n"
        )
    else:
        rule = (
            "優先順位: 「現在の状態」「次の一手」はこのカードより新しい会話を優先する。"
            "「ユーザーの訂正・好み」「却下した案」「制約」「用語」「決定と理由」は、圧縮要約よりこのカードを優先する。\n"
        )
    header = f"context-keeper 再開カード: 圧縮前に保存した作業ノート({saved_age}に保存)の抜粋。\n{rule}"
    remaining = budget - utf16_len(header) - utf16_len(footer)
    sections = note_sections(read_text(note))
    parts: list[str] = []
    for name in CARD_ORDER:
        body = sections.get(name, "").strip()
        if not body or remaining <= 40:
            continue
        block = f"\n## {name}\n{body}\n"
        if utf16_len(block) <= remaining:
            parts.append(block)
            remaining -= utf16_len(block)
            continue
        tail = "\n…(続きはノート全文)\n"
        lines = [f"\n## {name}"]
        used = utf16_len(lines[0]) + utf16_len(tail)
        for line in body.splitlines():
            cost = utf16_len(line) + 1
            if used + cost > remaining:
                break
            lines.append(line)
            used += cost
        if len(lines) > 1:
            block = "\n".join(lines) + tail
            parts.append(block)
            remaining -= utf16_len(block)
    return header + "".join(parts) + footer


def pointer_text(ctx: Ctx, only_self: bool = False) -> str:
    sessions = ctx.data / "sessions"
    if not sessions.is_dir():
        return ""
    now = time.time()
    if only_self:
        note = ctx.find_note()
        if not note:
            return ""
        return (
            f"context-keeper: このセッションの作業ノート {note}({ago(now - note.stat().st_mtime)}に更新)。"
            f"更新は skill `{SKILL_NAME}` の save 手順。"
        )
    notes = []
    for note in sessions.glob("*.md"):
        with contextlib.suppress(OSError):
            age = now - note.stat().st_mtime
            if age <= POINTER_MAX_AGE_DAYS * 86400 and not note.name.endswith(f"_{ctx.sid8}.md"):
                notes.append((age, note))
    if not notes:
        return ""
    notes.sort()
    lines = ["context-keeper: このプロジェクトには過去セッションの作業ノートがある(続きの作業なら該当ノートを読む):"]
    for age, note in notes[:POINTER_MAX_NOTES]:
        flag = "・別セッションで進行中の可能性" if age < ACTIVE_ELSEWHERE_SEC else ""
        lines.append(f"- {note}({ago(age)}に更新{flag})")
    if ctx.decisions_path.exists():
        count = sum(1 for line in read_text(ctx.decisions_path).splitlines() if DECISION_RE.match(line))
        lines.append(f"決定ログ: {ctx.decisions_path}({count}件)")
    return "\n".join(lines[:5])


# ---------------------------------------------------------------------- rollup


def _decision_key(decision_id: str) -> tuple:
    numbers = re.findall(r"\d+", decision_id)
    tail = re.sub(r"^D-\d{8}-", "", decision_id)
    return (numbers[0] if numbers else "", tail.rsplit("-", 1)[0] if "-" in tail else "", int(numbers[-1]) if numbers else 0)


def collect_decisions(ctx: Ctx) -> tuple[dict[str, tuple[str, str]], set[str]]:
    found: dict[str, tuple[str, str]] = {}
    superseded: set[str] = set()
    notes = sorted((ctx.data / "sessions").glob("*.md"), key=lambda p: p.stat().st_mtime)
    for note in notes:
        # Only the 決定と理由 section defines decisions; an ID cited elsewhere is a reference.
        body = note_sections(read_text(note)).get("決定と理由", "")
        for line in body.splitlines():
            match = DECISION_RE.match(line)
            if not match:
                continue
            text = SOURCE_TAG_RE.sub("", match.group(3))
            found[match.group(1)] = (text, note.stem)
            for group in SUPERSEDES_RE.findall(text):
                superseded.update(DECISION_ID_RE.findall(group))
    return found, superseded


def archived_ids(ctx: Ctx) -> set[str]:
    ids: set[str] = set()
    for path in (ctx.data / "archive").glob("*.md"):
        for line in read_text(path).splitlines():
            match = DECISION_RE.match(line)
            if match:
                ids.add(match.group(1))
    return ids


def rollup(ctx: Ctx) -> dict:
    ctx.ensure()
    with file_lock(ctx.data / "decisions.lock"):
        active: dict[str, tuple[str, str]] = {}
        if ctx.decisions_path.exists():
            for line in read_text(ctx.decisions_path).splitlines():
                match = DECISION_RE.match(line)
                if match:
                    source = re.search(r"〔([^〕]*)〕\s*$", match.group(3))
                    active[match.group(1)] = (SOURCE_TAG_RE.sub("", match.group(3)), source.group(1) if source else "")
        from_notes, superseded = collect_decisions(ctx)
        archived = archived_ids(ctx)
        for decision_id, value in from_notes.items():
            if decision_id not in archived:
                active[decision_id] = value
        for text, _ in list(active.values()):
            for group in SUPERSEDES_RE.findall(text):
                superseded.update(DECISION_ID_RE.findall(group))
        # An archived ID that reappears without being superseded is a reused ID: report, don't drop silently.
        reused = sorted(
            decision_id
            for decision_id, (_, source) in from_notes.items()
            if decision_id in archived and decision_id not in superseded and source.endswith(ctx.sid8 or "\0")
        )

        to_archive: list[tuple[str, str, str]] = []
        for decision_id in sorted(superseded & set(active)):
            text, _ = active.pop(decision_id)
            to_archive.append((decision_id, text, "撤回"))
        ordered = sorted(active, key=_decision_key, reverse=True)
        if len(ordered) > DECISIONS_MAX:
            for decision_id in ordered[DECISIONS_KEEP:]:
                text, _ = active.pop(decision_id)
                to_archive.append((decision_id, text, "古い決定"))
            ordered = ordered[:DECISIONS_KEEP]

        for decision_id, text, reason in to_archive:
            month = re.match(r"D-(\d{4})(\d{2})", decision_id)
            path = ctx.data / "archive" / (f"{month.group(1)}-{month.group(2)}.md" if month else "undated.md")
            if not path.exists():
                append_text(path, "# 決定アーカイブ\n\n")
            summary = text.split(" — ")[0][:160]
            append_text(path, f"- [{decision_id}] {summary}({reason})\n")

        lines = [
            "# 決定ログ",
            "",
            "context-keeper が各セッションノートの「決定と理由」から rollup のたびに再生成する。1行=1決定、新しい順。",
            "このファイルを直接直しても次の rollup で戻る。直すときはノートの該当行を直し、取り消すときはノートに",
            "「- [新しいID] 撤回: 理由 (supersedes: 古いID)」を書く。撤回・古い決定は archive/ に1行で移る。",
            "",
        ]
        for decision_id in ordered:
            text, source = active[decision_id]
            tag = f" 〔{source}〕" if source else ""
            lines.append(f"- [{decision_id}] {text}{tag}")
        write_text_atomic(ctx.decisions_path, "\n".join(lines) + "\n")

    pruned = 0
    cutoff = time.time() - RETENTION_DAYS * 86400
    for sub in ("raw", "state"):
        for path in (ctx.data / sub).glob("*"):
            if path.name == "calibration.json":
                continue
            with contextlib.suppress(OSError):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    pruned += 1
    return {"decisions": len(ordered), "archived": len(to_archive), "pruned": pruned, "reused": reused}


# ------------------------------------------------------------------------ hooks


def _skip_for_mode(data: dict) -> bool:
    return data.get("permission_mode") == "plan" or bool(data.get("agent_id"))


def _session_ctx(data: dict) -> Ctx | None:
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not SESSION_RE.match(session_id):
        return None
    ctx = Ctx.for_start(project_start(None, data), session_id)
    if ctx.inactive_reason():
        return None
    return ctx


def _nudge_message(ctx: Ctx, tokens: int, window: int, source: str, humans: int, chars: int, emergency: bool) -> str:
    state = ctx.state()
    note = ctx.find_note()
    pct = round(tokens * 100 / window)
    if note:
        saved_age = ago(time.time() - (parse_ts(state.get("saved_at")) or note.stat().st_mtime))
        note_state = f"作業ノート {note} の最終保存は{saved_age}"
    else:
        note_state = f"作業ノートは未作成(保存先 {ctx.note_path()})"
    repeated = ""
    if state.get("last_nudge_ts") and (state.get("saved_at") or "") < state["last_nudge_ts"]:
        repeated = "前回の checkpoint 通知の後もノートは保存されていない。"
    if emergency:
        lead = "まもなく自動圧縮される。"
        timing = "これは作業途中の通知で、保存の後は中断した作業に戻ってよい。"
    else:
        lead = ""
        timing = (
            "これはターン終了時の通知で、保存の後はパスを1行で伝えてターンを終える"
            "(新しい作業は始めず、ユーザーへの質問や確認待ちはそのまま残す)。"
        )
    return (
        f"context-keeper: コンテキストは自動圧縮ラインの {pct}%({tokens // 1000}K / {window // 1000}K、基準: {source})。{lead}"
        f"{note_state}。前回保存後の人間の発言は {humans} 件、未保存の会話テキストは約 {chars} 字。{repeated}"
        f"checkpoint due: Skill ツールで `{SKILL_NAME}` を引数 save で呼ぶと、許可済みのコマンドでノートを更新できる"
        "(コマンドの許可は呼んだターンだけ有効なので、本文を読み込み済みでも呼び直す)。"
        "保存すると、圧縮後もこの会話のニュアンス(決定の理由・却下した案・ユーザーの訂正)が再開カードとして復元される。"
        f"{timing}"
    )


def _record_usage_context(ctx: Ctx, data: dict, model: str) -> None:
    state = ctx.state()
    if data.get("transcript_path"):
        state["transcript_path"] = data["transcript_path"]
    if model:
        state["model"] = model


def _rearm(state: dict, window: int, pct: float) -> bool:
    """Bands are percentages of one window: re-arm them when the window changes or usage falls back."""
    previous = state.get("band_window")
    fired = state.get("bands") or []
    changed = bool(previous) and previous != window
    dropped = bool(fired) and pct < min(fired) - 10
    state["band_window"] = window
    if changed or dropped:
        state["bands"] = [band for band in fired if band <= pct]
        state["nudges"] = 0
        state["emergency"] = False
        return True
    return False


def _save_if_tracked(ctx: Ctx) -> None:
    if ctx.state_path.exists():
        ctx.save_state()


def hook_stop(data: dict) -> int:
    if data.get("stop_hook_active") or _skip_for_mode(data):
        return 0
    ctx = _session_ctx(data)
    if not ctx:
        return 0
    transcript = ctx.transcript(data.get("transcript_path"))
    if not transcript:
        return 0
    state = ctx.state()
    usage = last_usage(transcript, state.get("epoch_ts"))
    if not usage:
        return 0
    tokens, model, _ = usage
    window, source = resolve_window(ctx, model, tokens)
    pct = tokens * 100 / window
    rearmed = _rearm(state, window, pct)
    crossed = [band for band in BANDS if pct >= band and band not in state["bands"]]
    if not crossed:
        if rearmed:
            _save_if_tracked(ctx)
        return 0
    stale, humans, chars = staleness(ctx, transcript)
    if not stale:
        # Fresh note: leave the band armed so a later Stop re-checks once the note goes stale.
        if rearmed:
            _save_if_tracked(ctx)
        return 0
    _record_usage_context(ctx, data, model)
    state["bands"] = sorted(set(state["bands"]) | set(crossed))
    if state["nudges"] >= MAX_NUDGES_PER_EPOCH:
        ctx.save_state()
        return 0
    message = _nudge_message(ctx, tokens, window, source, humans, chars, emergency=False)
    state["nudges"] += 1
    state["last_nudge_ts"] = now_ts()
    ctx.save_state()
    emit_json(
        {
            "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": message},
            "systemMessage": f"context-keeper: コンテキスト {round(pct)}% — Claude に作業ノートの保存を促しました",
        }
    )
    return 0


def _probe_cache(session_id: str) -> Path:
    return private_dir() / "probe" / f"{session_id}.size"


def hook_post_tool_batch(data: dict) -> int:
    if _skip_for_mode(data):
        return 0
    ctx = _session_ctx(data)
    if not ctx:
        return 0
    transcript = ctx.transcript(data.get("transcript_path"))
    if not transcript:
        return 0
    size = transcript_size(transcript)
    cache = _probe_cache(ctx.sid)
    try:
        last = int(read_text(cache).strip() or 0)
    except (OSError, ValueError):
        last = 0
    if last <= size < last + BATCH_CHECK_BYTES:
        return 0
    with contextlib.suppress(OSError):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(str(size), encoding="utf-8")
    state = ctx.state()
    usage = last_usage(transcript, state.get("epoch_ts"))
    if not usage:
        return 0
    tokens, model, _ = usage
    window, source = resolve_window(ctx, model, tokens)
    rearmed = _rearm(state, window, tokens * 100 / window)
    if state.get("emergency") or window - tokens >= max(window * EMERGENCY_RATIO, EMERGENCY_MIN_TOKENS):
        if rearmed:
            _save_if_tracked(ctx)
        return 0
    # Past the emergency line: re-check staleness on every probe until a nudge is sent.
    # The emergency nudge is exempt from MAX_NUDGES_PER_EPOCH; it is the last chance before compaction.
    stale, humans, chars = staleness(ctx, transcript)
    if not stale:
        if rearmed:
            _save_if_tracked(ctx)
        return 0
    _record_usage_context(ctx, data, model)
    message = _nudge_message(ctx, tokens, window, source, humans, chars, emergency=True)
    state["emergency"] = True
    state["nudges"] += 1
    state["last_nudge_ts"] = now_ts()
    state["bands"] = sorted(set(state["bands"]) | {band for band in BANDS if tokens * 100 / window >= band})
    ctx.save_state()
    emit_json(
        {
            "hookSpecificOutput": {"hookEventName": "PostToolBatch", "additionalContext": message},
            "systemMessage": "context-keeper: まもなく自動圧縮 — Claude に作業ノートの保存を促しました",
        }
    )
    return 0


def _dump_dialogue(ctx: Ctx, transcript: Path, title: str) -> int:
    state = ctx.state()
    turns, new_offset = read_dialogue(transcript, int(state.get("dump_offset") or 0))
    state["dump_offset"] = new_offset
    if not turns:
        return 0
    ctx.ensure()
    out = [f"\n## {title}\n"]
    for role, stamp, text in turns:
        limit = RAW_HUMAN_MAX if role == "user" else RAW_ASSISTANT_MAX
        if len(text) > limit:
            text = text[:limit] + f"\n…({len(text) - limit} 字省略)"
        out.append(f"\n### {'user' if role == 'user' else 'assistant'} {stamp}\n{text}\n")
    if not ctx.raw_path.exists():
        append_text(ctx.raw_path, f"# 対話原文 session {ctx.sid}\n\ncontext-keeper が圧縮直前に退避した、人間の発言と assistant の本文(ツール出力は含まない)。\n")
    append_text(ctx.raw_path, "".join(out))
    return len(turns)


def hook_pre_compact(data: dict) -> int:
    if data.get("agent_id"):
        return 0
    ctx = _session_ctx(data)
    if not ctx:
        return 0
    transcript = ctx.transcript(data.get("transcript_path"))
    if not transcript:
        return 0
    state = ctx.state()
    trigger = data.get("trigger") or "auto"
    usage = last_usage(transcript, state.get("epoch_ts"))
    if usage:
        _record_usage_context(ctx, data, usage[1])
    stale, humans, chars = staleness(ctx, transcript)
    if trigger == "manual":
        now = time.time()
        if stale and now - float(state.get("last_block_ts") or 0) > BLOCK_COOLDOWN_SEC:
            state["last_block_ts"] = now
            ctx.save_state()
            sys.stderr.write(
                f"context-keeper: 作業ノートが未保存です(前回保存後の発言 {humans} 件)。"
                f"Claude に「ノートを保存して」と伝えるか /{SKILL_NAME} save を実行してから /compact してください。"
                f"{BLOCK_COOLDOWN_SEC // 60}分以内にもう一度 /compact すれば、そのまま圧縮します。\n"
            )
            return 2
    # What the note misses at this compaction; the resume card uses it to set its priority rule.
    state["stale_at_compact"] = {"humans": humans, "chars": chars} if ctx.find_note() else None
    tokens = f"、約 {usage[0] // 1000}K tokens" if usage else ""
    _dump_dialogue(ctx, transcript, f"{now_ts()} 圧縮前の退避(trigger={trigger}{tokens})")
    if trigger == "auto" and usage:
        # Calibrate only when no explicit window was configured; a temporary /autocompact must not linger.
        _, source = resolve_window(ctx, usage[1])
        if source.startswith(("default", "calibrated")):
            calibration = load_json(ctx.calibration_path, {})
            calibration[calibration_key(effective_model(ctx, usage[1]))] = usage[0]
            save_json(ctx.calibration_path, calibration)
    ctx.save_state()
    return 0


def _new_epoch(ctx: Ctx) -> None:
    state = ctx.state()
    # PostCompact and SessionStart(compact) both fire for one compaction: count it once.
    previous = parse_ts(state.get("epoch_ts"))
    if previous is None or time.time() - previous > 120:
        state["epoch"] = int(state.get("epoch") or 0) + 1
    state["epoch_ts"] = now_ts()
    state["bands"] = []
    state["nudges"] = 0
    state["emergency"] = False
    state["band_window"] = None
    with contextlib.suppress(OSError):
        _probe_cache(ctx.sid).unlink()


def hook_post_compact(data: dict) -> int:
    if data.get("agent_id"):
        return 0
    ctx = _session_ctx(data)
    if not ctx:
        return 0
    summary = data.get("compact_summary")
    if isinstance(summary, str) and summary.strip():
        ctx.ensure()
        append_text(ctx.raw_path, f"\n## {now_ts()} 圧縮後の要約(compact_summary, trigger={data.get('trigger')})\n\n{summary.strip()}\n")
    _new_epoch(ctx)
    ctx.save_state()
    return 0


def _selected_model_path(session_id: str) -> Path:
    return private_dir() / "models" / f"{session_id}.txt"


def remember_selected_model(session_id: str, model) -> None:
    if isinstance(model, str) and model.strip():
        with contextlib.suppress(OSError):
            path = _selected_model_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(model.strip(), encoding="utf-8")


def selected_model(session_id: str | None) -> str | None:
    if not session_id:
        return None
    with contextlib.suppress(OSError):
        return read_text(_selected_model_path(session_id)).strip() or None
    return None


def prune_private_dir() -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    base = private_dir()
    for sub in ("models", "probe"):
        for path in (base / sub).glob("*"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
    log = base / "errors.log"
    with contextlib.suppress(OSError):
        if log.stat().st_size > 256 * 1024:
            os.replace(log, base / "errors.log.old")


def hook_session_start(data: dict) -> int:
    ctx = _session_ctx(data)
    if not ctx:
        return 0
    remember_selected_model(ctx.sid, data.get("model"))
    source = data.get("source") or "startup"
    if source == "startup":
        prune_private_dir()
    if not ctx.data.is_dir():
        return 0
    if source == "compact":
        _new_epoch(ctx)
        if data.get("transcript_path"):
            ctx.state()["transcript_path"] = data["transcript_path"]
        ctx.save_state()
        text = build_card(ctx)
    elif source == "resume":
        text = pointer_text(ctx, only_self=True)
    else:
        text = pointer_text(ctx)
    if text:
        emit_json({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
    return 0


def hook_post_model_switch(data: dict) -> int:
    """Remember the selected model (it may carry [1m], which transcripts drop). Prints nothing."""
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not SESSION_RE.match(session_id):
        return 0
    if truthy(os.environ.get("CONTEXT_KEEPER_DISABLE")):
        return 0
    to_model = data.get("to_model")
    requested = data.get("requested_model")
    if not isinstance(to_model, str) or not to_model:
        return 0
    wants_1m = "[1m]" in to_model.lower() or (isinstance(requested, str) and "[1m]" in requested.lower())
    remember_selected_model(session_id, to_model if "[1m]" in to_model.lower() or not wants_1m else f"{to_model}[1m]")
    return 0


HOOKS = {
    "SessionStart": hook_session_start,
    "Stop": hook_stop,
    "PostToolBatch": hook_post_tool_batch,
    "PreCompact": hook_pre_compact,
    "PostCompact": hook_post_compact,
    "PostModelSwitch": hook_post_model_switch,
}


def debug_log(message: str) -> None:
    with contextlib.suppress(OSError):
        with open(private_dir() / "errors.log", "a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"{now_ts()} {message}\n")


def run_hook(event: str, raw: bytes) -> int:
    handler = HOOKS.get(event)
    if not handler:
        return 0
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        if not isinstance(data, dict):
            return 0
        return handler(data)
    except Exception as exc:  # noqa: BLE001 - hooks must fail open
        debug_log(f"{event}: {type(exc).__name__}: {exc}")
        return 0


# ------------------------------------------------------------- install-hooks


def own_hook(hook: dict) -> bool:
    """True for entries that run this script: installed under .../context-keeper/scripts/ck.py, or this very file.

    The leading slash keeps a differently named copy (e.g. `<prefix>_context-keeper`) from matching.
    """
    pieces = [str(hook.get("command") or "")] + [str(a) for a in hook.get("args") or []]
    here = os.path.abspath(__file__).replace("\\", "/")
    return any(
        f"/{SKILL_NAME}/scripts/ck.py" in piece.replace("\\", "/") or piece.replace("\\", "/") == here
        for piece in pieces
    )


def strip_own_hooks(settings: dict) -> int:
    hooks = settings.get("hooks")
    removed = 0
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [h for h in group["hooks"] if not (isinstance(h, dict) and own_hook(h))]
            removed += len(group["hooks"]) - len(kept)
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def python_executable() -> str:
    base = getattr(sys, "_base_executable", None)
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix) and base:
        return base
    return sys.executable


def script_path_for_hooks() -> str:
    here = Path(os.path.abspath(__file__))
    linked = config_dir() / "skills" / SKILL_NAME / "scripts" / "ck.py"
    with contextlib.suppress(OSError):
        if linked.exists() and linked.resolve() == here.resolve():
            return str(linked)
    return str(here)


# `python ck.py` exits 2 when the file is missing (moved repo, renamed skill, deleted link), and
# exit 2 blocks PreCompact/Stop. This launcher exits 0 instead and passes ck.py's own exit code through.
HOOK_LAUNCHER = (
    "import os,runpy,sys; p=sys.argv[1]; sys.argv=sys.argv[1:]; "
    "os.path.isfile(p) and runpy.run_path(p, run_name='__main__')"
)


def build_hook_entries() -> dict:
    python = python_executable()
    script = script_path_for_hooks()
    entries = {}
    for event, timeout in HOOK_TIMEOUTS.items():
        # -I: isolated mode, so a project file such as random.py in the hook's cwd can't shadow the stdlib.
        hook = {"type": "command", "command": python, "args": ["-I", "-c", HOOK_LAUNCHER, script, "hook", event], "timeout": timeout}
        entries[event] = {"hooks": [hook]}
    return entries


def install_hooks(settings_path: Path, dry_run: bool = False, uninstall: bool = False) -> str:
    if settings_path.exists():
        try:
            settings = json.loads(read_text(settings_path) or "{}")
        except ValueError as exc:
            raise CkError(f"{settings_path} is not valid JSON ({exc}); fix it first, nothing was changed") from exc
        if not isinstance(settings, dict):
            raise CkError(f"{settings_path} does not contain a JSON object; nothing was changed")
    else:
        settings = {}
    if "hooks" in settings and not isinstance(settings["hooks"], dict):
        raise CkError(f'"hooks" in {settings_path} is not an object; nothing was changed')
    removed = strip_own_hooks(settings)
    added = 0
    if not uninstall:
        hooks = settings.setdefault("hooks", {})
        for event, group in build_hook_entries().items():
            if not isinstance(hooks.get(event, []), list):
                raise CkError(f'"hooks.{event}" in {settings_path} is not a list; nothing was changed')
            hooks.setdefault(event, []).append(group)
            added += 1
    rendered = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    if dry_run:
        return rendered
    if settings_path.exists():
        target = settings_path.resolve()
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.context-keeper-backup-{stamp}")
        shutil.copy2(target, backup)
    write_text_atomic(settings_path, rendered, follow_symlink=True)
    verb = "uninstalled" if uninstall else "installed"
    return f"{verb}: removed {removed} old hook(s), added {added} hook(s) in {settings_path}"


def hooks_installed(settings_path: Path) -> list[str]:
    settings = load_json(settings_path, {})
    events = []
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for hook in (group or {}).get("hooks") or []:
                if isinstance(hook, dict) and own_hook(hook):
                    events.append(event)
    return sorted(set(events))


# --------------------------------------------------------------------------- cli


def cli_ctx(args) -> Ctx:
    ctx = Ctx.for_start(project_start(args.project), args.session)
    return ctx


def cmd_path(args) -> int:
    ctx = cli_ctx(args)
    reason = ctx.inactive_reason()
    if reason:
        raise CkError(f"context-keeper is inactive here: {reason}")
    ctx.ensure()
    note = ctx.note_path()
    text = read_text(note) if note.exists() else ""
    today = dt.datetime.now().strftime("%Y%m%d")
    sid4 = ctx.sid8[:4]
    # Number past every ID ever issued today by this session, not just those still in the note.
    pattern = re.compile(rf"\[D-{today}-{re.escape(sid4)}-(\d+)\]")
    sources = [text]
    for path in [ctx.decisions_path, *sorted((ctx.data / "archive").glob("*.md"))]:
        with contextlib.suppress(OSError):
            sources.append(read_text(path))
    numbers = [int(n) for source in sources for n in pattern.findall(source)]
    print(f"note: {note}")
    print(f"exists: {'yes' if note.exists() else 'no'} ({len(text)} chars; warn>{NOTE_WARN_CHARS}, max {NOTE_MAX_CHARS})")
    print(f"next decision id: D-{today}-{sid4}-{max(numbers, default=0) + 1}")
    print(f"decisions: {ctx.decisions_path}")
    print(f"raw: {ctx.raw_path}")
    if not note.exists():
        others = sorted(
            (p for p in (ctx.data / "sessions").glob("*.md") if p != note),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        recent = [p for p in others if time.time() - p.stat().st_mtime <= POINTER_MAX_AGE_DAYS * 86400]
        if recent:
            print(f"previous note: {recent[0]} (carry over still-valid corrections/constraints/terms/rejections if continuing)")
    return 0


def cmd_saved(args) -> int:
    ctx = cli_ctx(args)
    note = ctx.find_note()
    if not note:
        raise CkError(f"no note for this session under {ctx.data / 'sessions'}; write it first (see `path`)")
    text = read_text(note)
    if len(text) > NOTE_MAX_CHARS:
        raise CkError(
            f"note is {len(text)} chars (> {NOTE_MAX_CHARS}). Compress older parts — keep reasons, "
            "rejected options, user corrections and every [D-…] id — then run `saved` again."
        )
    sections = note_sections(text)
    missing = [name for name in SECTIONS if name not in sections]
    state = ctx.state()
    transcript = ctx.transcript()
    state["saved_offset"] = transcript_size(transcript)
    state["saved_at"] = now_ts()
    state["stale_at_compact"] = None
    if transcript:
        state["transcript_path"] = str(transcript)
    ctx.save_state()
    result = rollup(ctx)
    print(f"saved: {note} ({len(text)} chars)")
    if len(text) > NOTE_WARN_CHARS:
        print(f"warning: over {NOTE_WARN_CHARS} chars — compress older parts on the next save")
    if missing:
        print("warning: missing sections: " + ", ".join(missing))
    if result["reused"]:
        print("warning: already archived, renumber with the next id from `path`: " + ", ".join(result["reused"]))
    if not transcript:
        print("warning: transcript not found; staleness tracking falls back to 'never saved'")
    print(f"rollup: {result['decisions']} decisions in {ctx.decisions_path}, archived {result['archived']}, pruned {result['pruned']}")
    return 0


def cmd_card(args) -> int:
    ctx = cli_ctx(args)
    text = build_card(ctx) or pointer_text(ctx)
    print(text or "context-keeper: no note found for this session or project")
    return 0


def cmd_status(args) -> int:
    ctx = cli_ctx(args)
    settings_path = config_dir() / "settings.json"
    installed = hooks_installed(settings_path)
    print(f"project: {ctx.root}")
    print(f"data: {ctx.data}{'' if ctx.data.is_dir() else ' (not created yet)'}")
    reason = ctx.inactive_reason()
    print(f"active: {'no — ' + reason if reason else 'yes'}")
    print(f"hooks in {settings_path}: {', '.join(installed) if installed else 'not installed'}")
    print(f"python: {python_executable()}")
    print(f"script: {Path(os.path.abspath(__file__))}")
    if not ctx.sid:
        return 0
    transcript = ctx.transcript()
    state = ctx.state()
    usage = last_usage(transcript, state.get("epoch_ts")) if transcript else None
    if usage:
        window, source = resolve_window(ctx, usage[1], usage[0])
        print(f"context: {usage[0] // 1000}K / {window // 1000}K = {round(usage[0] * 100 / window)}% ({source}; model {usage[1]})")
    else:
        print("context: unknown (no usage recorded since the last compaction)")
    note = ctx.find_note()
    if note:
        saved = state.get("saved_at")
        print(f"note: {note} ({len(read_text(note))} chars, saved {ago(time.time() - parse_ts(saved)) if saved else 'never via `saved`'})")
    else:
        print(f"note: none yet (would be {ctx.note_path()})")
    stale, humans, chars = staleness(ctx, transcript)
    print(f"stale: {'yes' if stale else 'no'} ({humans} human prompts, {chars} chars since last save)")
    print(f"epoch: {state.get('epoch')} bands fired: {state.get('bands')} nudges: {state.get('nudges')}")
    return 0


def cmd_rollup(args) -> int:
    ctx = cli_ctx(args)
    result = rollup(ctx)
    print(f"rollup: {result['decisions']} decisions in {ctx.decisions_path}, archived {result['archived']}, pruned {result['pruned']}")
    return 0


def cmd_toggle(args, disable: bool) -> int:
    ctx = cli_ctx(args)
    marker = ctx.data / "disabled"
    if disable:
        ctx.ensure()
        write_text_atomic(marker, "context-keeper disabled for this project\n")
        print(f"disabled: {marker}")
    else:
        with contextlib.suppress(FileNotFoundError):
            marker.unlink()
        print(f"enabled: {ctx.root}")
    return 0


def cmd_install(args, uninstall: bool) -> int:
    settings_path = Path(args.settings) if args.settings else config_dir() / "settings.json"
    print(install_hooks(settings_path, dry_run=args.dry_run, uninstall=uninstall))
    return 0


# ---------------------------------------------------------------------- self-test


def _write_transcript(path: Path, entries: list[dict]) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def _human(text: str, stamp: str) -> dict:
    return {"type": "user", "timestamp": stamp, "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def _assistant(text: str | None, tokens: int, stamp: str, model: str = "claude-opus-5-5", **extra) -> dict:
    content = [{"type": "text", "text": text}] if text else [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": tokens - 2, "output_tokens": 0}
    entry = {"type": "assistant", "timestamp": stamp, "message": {"model": model, "content": content, "usage": usage}}
    entry.update(extra)
    return entry


def _call(event: str, payload: dict) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_hook(event, json.dumps(payload).encode("utf-8"))
    return code, out.getvalue(), err.getvalue()


def _check(condition: bool, label: str) -> None:
    if not condition:
        raise CkError(f"self-test failed: {label}")


def self_test() -> None:
    saved_env = {k: os.environ.get(k) for k in (
        "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDE_CODE_DISABLE_1M_CONTEXT", "CONTEXT_KEEPER_DISABLE", "CLAUDE_CODE_USE_BEDROCK",
    )}
    for key in saved_env:
        os.environ.pop(key, None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _self_test_in(Path(tmp))
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _self_test_in(tmp: Path) -> None:
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp / "claude-config")
    (tmp / "claude-config").mkdir()

    # project root + worktree resolution
    project = tmp / "proj"
    (project / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    (project / "sub").mkdir()
    worktree = tmp / "wt1"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {project / '.git' / 'worktrees' / 'wt1'}\n", encoding="utf-8")
    _check(find_project_root(project / "sub") == project, "root from subdirectory")
    _check(find_project_root(worktree) == project, "worktree -> main checkout")

    # model windows and parsing
    _check(default_window("claude-opus-5-5")[0] == WINDOW_1M, "opus 5.5 is 1M")
    _check(default_window("claude-haiku-4-5-20251001")[0] == WINDOW_200K, "haiku 4.5 is 200K")
    _check(default_window("claude-opus-4-6")[0] == WINDOW_200K, "opus 4.6 is 200K")
    _check(default_window("claude-opus-4-6[1m]")[0] == WINDOW_1M, "[1m] suffix")
    _check(default_window("us.anthropic.claude-sonnet-5-5-v1:0")[0] == WINDOW_1M, "bedrock-style id canonicalized")
    os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
    _check(default_window("claude-opus-5-5")[0] == WINDOW_200K, "third-party provider defaults to 200K")
    os.environ.pop("CLAUDE_CODE_USE_BEDROCK")
    os.environ["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "1"
    _check(default_window("claude-sonnet-5-5")[0] == WINDOW_200K, "disable 1M env")
    os.environ.pop("CLAUDE_CODE_DISABLE_1M_CONTEXT")
    for raw, expected in (("500k", 500_000), ("1M", 1_000_000), (200, 200_000), ("200000", 200_000), ("x", None), (True, None)):
        _check(parse_window(raw) == expected, f"parse_window({raw!r})")

    session = "0d1e2f3a-4b5c-6d7e-8f90-a1b2c3d4e5f6"
    ctx = Ctx(project, session)
    (project / ".claude").mkdir()
    save_json(config_dir() / "settings.json", {"autoCompactWindow": 500_000})
    _check(resolve_window(ctx, "claude-opus-5-5")[0] == 500_000, "user settings window")
    save_json(project / ".claude" / "settings.json", {"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": "300k"}}})
    _check(resolve_window(ctx, "claude-opus-5-5")[0] == 300_000, "project modelSettings beats user")
    os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "100000"
    _check(resolve_window(ctx, "claude-opus-5-5")[0] == 100_000, "env beats settings")
    os.environ.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    _check(resolve_window(ctx, "claude-opus-5-5", observed=400_000)[0] == WINDOW_1M, "observed usage raises window")
    (project / ".claude" / "settings.json").unlink()
    save_json(config_dir() / "settings.json", {"autoCompactWindow": 100_000})

    # transcript fixture
    transcript = tmp / "claude-config" / "projects" / "-proj" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True)
    stamp = "2026-01-02T01:00:{:02d}.000Z"  # always in the past
    _write_transcript(transcript, [
        _human("Aではなく B 案でいく。理由は再現性。", stamp.format(1)),
        _assistant("了解、B 案で進めます。", 30_000, stamp.format(2)),
        {"type": "user", "timestamp": stamp.format(3), "message": {"content": [{"type": "tool_result", "content": "SECRET-TOOL-OUTPUT"}]}},
        {"type": "user", "isMeta": True, "timestamp": stamp.format(4), "message": {"content": "<command-name>/model</command-name>"}},
        {"type": "user", "timestamp": stamp.format(5), "origin": {"kind": "task-notification"}, "message": {"content": "<task-notification>x</task-notification>"}},
        {"type": "user", "timestamp": stamp.format(5), "toolUseResult": {"questions": [], "answers": {"保存先は?": "プロジェクト内"}},
         "message": {"content": [{"type": "tool_result", "content": "Your questions have been answered"}]}},
        _assistant("サブエージェントの出力", 999_000, stamp.format(6), isSidechain=True),
        _assistant(None, 50_000, stamp.format(7)),
        _human("用語は「区間」ではなく「窓」と呼ぶ。", stamp.format(8)),
        _assistant("「窓」に統一します 🪟", 55_000, stamp.format(9)),
        _assistant("x", 0, stamp.format(10), model="<synthetic>"),
    ])
    usage = last_usage(transcript)
    _check(usage is not None and usage[0] == 55_000, "last usage skips sidechain/synthetic")
    turns, _ = read_dialogue(transcript, 0)
    _check([r for r, _, _ in turns] == ["user", "assistant", "user", "user", "assistant"], "dialogue keeps humans + visible text only")
    _check("[選択] 保存先は? → プロジェクト内" in turns[2][2], "AskUserQuestion answers count as human input")
    _check(all("SECRET-TOOL-OUTPUT" not in t for _, _, t in turns), "tool output excluded")

    payload = {"session_id": session, "transcript_path": str(transcript), "cwd": str(project / "sub"), "permission_mode": "default"}

    # Stop: 55% -> nothing, no files created
    code, out, _ = _call("Stop", {**payload, "hook_event_name": "Stop", "stop_hook_active": False})
    _check(code == 0 and out == "", "stop below first band is silent")
    _check(not (project / DATA_DIRNAME).exists(), "no data dir before anything happens")

    _write_transcript(transcript, [_human("締め切りは金曜まで。", stamp.format(11)), _assistant("金曜締切で計画します。", 62_000, stamp.format(12))])
    code, out, _ = _call("Stop", {**payload, "hook_event_name": "Stop", "stop_hook_active": True})
    _check(out == "", "stop_hook_active suppresses nudge")
    code, out, _ = _call("Stop", {**payload, "hook_event_name": "Stop", "permission_mode": "plan"})
    _check(out == "", "plan mode suppresses nudge")
    code, out, _ = _call("Stop", {**payload, "hook_event_name": "Stop"})
    _check(code == 0 and out, "stop at 62% with stale note nudges")
    nudge = json.loads(out)
    _check(nudge["hookSpecificOutput"]["hookEventName"] == "Stop", "nudge event name")
    _check(SKILL_NAME in nudge["hookSpecificOutput"]["additionalContext"], "nudge names the skill")
    _check(nudge["systemMessage"], "nudge notifies the user")
    _check((project / DATA_DIRNAME / ".gitignore").exists(), "gitignore created lazily")
    code, out, _ = _call("Stop", {**payload, "hook_event_name": "Stop"})
    _check(out == "", "same band does not fire twice")

    # save a note, then the next band stays quiet because nothing is stale
    note = ctx.note_path()
    big_note = "\n".join(
        [
            "# 作業ノート",
            "## 目的\nnuance を残す",
            "## 決定と理由\n- [D-20261006-0d1e-1] B 案を採用 — 再現性が高い — 却下: A 案(再現性が低い)",
            "- [D-20261006-0d1e-2] 用語は「窓」 — ユーザーの指定 (supersedes: D-20261001-0d1e-1)",
            "## 却下した案\n- A 案: 再現性が低い",
            "## ユーザーの訂正・好み\n- 「区間」ではなく「窓」と呼ぶ 🪟",
            "## 制約\n- 締め切りは金曜",
            "## 用語\n- 窓: 評価区間",
            "## 現在の状態\n" + "\n".join(f"- 状態 {i} 😀 日本語テキスト" for i in range(400)),
            "## 次の一手\n- 実験を回す",
            "## 未解決\n- なし",
        ]
    )
    write_text_atomic(note, big_note)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            cmd_saved(argparse.Namespace(project=str(project), session=session))
        _check(False, "saved rejects an oversized note")
    except CkError:
        pass
    small_note = big_note.split("## 現在の状態")[0] + "## 現在の状態\n- 実装中\n## 次の一手\n- 実験を回す\n## 未解決\n- なし\n"
    write_text_atomic(note, small_note)
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_saved(argparse.Namespace(project=str(project), session=session))
    decisions = read_text(ctx.decisions_path)
    _check("[D-20261006-0d1e-1]" in decisions and "[D-20261006-0d1e-2]" in decisions, "rollup collects decisions")
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_rollup(argparse.Namespace(project=str(project), session=session))
    _check(read_text(ctx.decisions_path) == decisions, "rollup is idempotent")

    _write_transcript(transcript, [_assistant("続けます", 77_000, stamp.format(13))])
    ctx._state = None
    code, out, _ = _call("Stop", {**payload, "hook_event_name": "Stop"})
    _check(out == "", "fresh note suppresses the next band")
    _check(75 not in Ctx(project, session).state()["bands"], "band stays armed while the note is fresh")

    # PreCompact manual: block once when stale, pass the second time
    _write_transcript(transcript, [
        _human("やっぱり図は横向き。", stamp.format(14)), _assistant("横向きにします", 78_000, stamp.format(15)),
        _human("凡例は右上。", stamp.format(16)), _assistant("右上に置きます", 79_000, stamp.format(17)),
    ])
    pre = {**payload, "hook_event_name": "PreCompact", "trigger": "manual", "custom_instructions": None}
    code, out, err = _call("PreCompact", pre)
    _check(code == 2 and "context-keeper" in err and out == "", "manual compact blocked once when stale")
    code, out, err = _call("PreCompact", pre)
    _check(code == 0, "second manual compact passes")
    raw = read_text(Ctx(project, session).raw_path)
    _check("横向き" in raw and "SECRET-TOOL-OUTPUT" not in raw, "raw dump has dialogue, not tool output")

    # PreCompact auto never blocks; calibration only when no explicit window is configured
    code, _, _ = _call("PreCompact", {**pre, "trigger": "auto"})
    _check(code == 0, "auto compact never blocked")
    _check(not Ctx(project, session).calibration_path.exists(), "no calibration while settings define the window")
    save_json(config_dir() / "settings.json", {})
    code, _, _ = _call("PreCompact", {**pre, "trigger": "auto"})
    _check(load_json(Ctx(project, session).calibration_path, {}).get("claude-opus-5-5") == 79_000, "calibration recorded")
    _check(Ctx(project, session).state()["stale_at_compact"]["humans"] == 2, "staleness at compaction recorded")
    Ctx(project, session).calibration_path.unlink()
    save_json(config_dir() / "settings.json", {"autoCompactWindow": 100_000})

    # PostCompact + SessionStart(compact): new epoch, old usage ignored, card injected
    code, _, _ = _call("PostCompact", {**payload, "hook_event_name": "PostCompact", "trigger": "auto", "compact_summary": "要約本文"})
    _check(code == 0 and "要約本文" in read_text(Ctx(project, session).raw_path), "compact summary saved")
    _check(last_usage(transcript, Ctx(project, session).state()["epoch_ts"]) is None, "pre-compaction usage ignored")
    code, out, _ = _call("SessionStart", {**payload, "hook_event_name": "SessionStart", "source": "compact"})
    card = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    _check("窓" in card and SKILL_NAME in card, "card carries nuance + skill")
    _check("反映していない" in card and "圧縮要約" in card, "stale note: card defers to the newer summary")
    _check(utf16_len(card) <= CARD_BUDGET_UNITS, "card within budget")
    fresh_ctx = Ctx(project, session)
    fresh_ctx.state()["stale_at_compact"] = None
    _check("このカードを優先" in build_card(fresh_ctx), "fresh note: card wins for corrections")
    write_text_atomic(note, big_note)
    big_card = build_card(fresh_ctx)
    _check(utf16_len(big_card) <= CARD_BUDGET_UNITS and "続きはノート全文" in big_card, "large note truncated to budget")
    _check(big_card.index("## 現在の状態") < big_card.index("## 用語") if "## 用語" in big_card else True, "card priority order")

    # SessionStart startup in another session points at the note; garbage input fails open
    other = "9f8e7d6c-0000-1111-2222-333344445555"
    code, out, _ = _call("SessionStart", {**payload, "session_id": other, "hook_event_name": "SessionStart", "source": "startup"})
    _check(out and note.name in json.loads(out)["hookSpecificOutput"]["additionalContext"], "startup pointer lists recent notes")
    code, out, _ = _call("SessionStart", {**payload, "session_id": "../../etc", "source": "startup"})
    _check(code == 0 and out == "", "invalid session id ignored")
    out_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf):
        code = run_hook("Stop", b"not json")
    _check(code == 0 and out_buf.getvalue() == "", "garbage stdin fails open")

    # disable marker silences everything
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_toggle(argparse.Namespace(project=str(project), session=None), disable=True)
    code, out, _ = _call("SessionStart", {**payload, "session_id": other, "source": "startup"})
    _check(out == "", "disabled project is silent")
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_toggle(argparse.Namespace(project=str(project), session=None), disable=False)

    # rollup: supersedes + overflow go to archive and are not resurrected
    lines = [f"- [D-202609{d:02d}-0d1e-{i}] 決定 {d}-{i} — 理由" for d in range(1, 29) for i in range(1, 9)]
    write_text_atomic(ctx.data / "sessions" / "20260901_0d1e2f3a.md", "## 決定と理由\n" + "\n".join(lines) + "\n- [D-20261006-0d1e-3] 新方針 (supersedes: D-20260901-0d1e-1)\n")
    result = rollup(ctx)
    _check(result["decisions"] == DECISIONS_KEEP, "overflow trimmed to keep size")
    archive = read_text(ctx.data / "archive" / "2026-09.md")
    _check("[D-20260901-0d1e-1]" in archive and "撤回" in archive, "superseded decision archived")
    again = rollup(ctx)
    _check(again["archived"] == 0 and again["decisions"] == DECISIONS_KEEP, "archived decisions stay archived")

    # lock contention
    lock = ctx.data / "test.lock"
    with file_lock(lock):
        try:
            with file_lock(lock, timeout=0.2):
                _check(False, "second lock must not be acquired")
        except CkError:
            pass
    lock.write_text("123", encoding="utf-8")
    old = time.time() - LOCK_STALE_SEC - 5
    os.utime(lock, (old, old))
    with file_lock(lock, timeout=0.5):
        pass

    # install / uninstall hooks keeps foreign hooks and is idempotent
    settings_path = config_dir() / "settings.json"
    save_json(settings_path, {"autoCompactWindow": 100_000, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo other"}]}]}})
    install_hooks(settings_path)
    install_hooks(settings_path)
    installed = load_json(settings_path, {})
    _check(len(installed["hooks"]["Stop"]) == 2, "install is idempotent and keeps foreign hooks")
    _check(sorted(hooks_installed(settings_path)) == sorted(HOOK_TIMEOUTS), "all events registered")
    _check(any(p.name.startswith("settings.json.context-keeper-backup-") for p in settings_path.parent.iterdir()), "backup written")
    install_hooks(settings_path, uninstall=True)
    after = load_json(settings_path, {})
    _check(after["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "echo other"}]}]}, "uninstall removes only ours")
    write_text_atomic(settings_path, "{broken")
    try:
        install_hooks(settings_path)
        _check(False, "broken settings must abort")
    except CkError:
        _check(read_text(settings_path) == "{broken", "broken settings untouched")

    # a missing ck.py must exit 0 through the hook launcher; ck.py's own exit 2 must pass through
    import subprocess
    gone = subprocess.run([python_executable(), "-I", "-c", HOOK_LAUNCHER, str(tmp / "missing" / "ck.py"), "hook", "PreCompact"],
                          input=b"{}", capture_output=True)
    _check(gone.returncode == 0, "launcher: missing script exits 0")
    exits2 = tmp / "exit2.py"
    exits2.write_text("import sys; sys.exit(2)\n", encoding="utf-8")
    passed = subprocess.run([python_executable(), "-I", "-c", HOOK_LAUNCHER, str(exits2), "hook", "PreCompact"], capture_output=True)
    _check(passed.returncode == 2, "launcher: deliberate exit 2 passes through")
    bad = subprocess.run([python_executable(), os.path.abspath(__file__), "hook"], input=b"", capture_output=True)
    _check(bad.returncode == 0 and bad.stdout == b"", "hook without event exits 0")

    shadow = tmp / "shadow"
    shadow.mkdir()
    (shadow / "random.py").write_text("import sys; sys.exit(2)\n", encoding="utf-8")
    (shadow / "json.py").write_text("import sys; sys.exit(2)\n", encoding="utf-8")
    shadowed = subprocess.run([python_executable(), "-I", "-c", HOOK_LAUNCHER, os.path.abspath(__file__), "hook", "Stop"],
                              input=b"{}", capture_output=True, cwd=str(shadow))
    _check(shadowed.returncode == 0, "launcher: project files can't shadow the stdlib")
    other = {"command": "python3", "args": ["/x/other_" + SKILL_NAME + "/scripts/ck.py", "hook", "Stop"]}
    _check(not own_hook(other), "a differently named copy is not treated as ours")
    _check(own_hook({"command": "python3", "args": [f"/x/{SKILL_NAME}/scripts/ck.py", "hook", "Stop"]}), "installed path is recognised")
    entry = build_hook_entries()["Stop"]["hooks"][0]
    _check(entry["args"][:2] == ["-I", "-c"] and own_hook(entry), "installed entries run isolated and stay recognisable")

    # windows: cap configured windows at 200K models, [1m] via the selected model, re-arm on change
    save_json(config_dir() / "settings.json", {"autoCompactWindow": 500_000})
    _check(resolve_window(ctx, "claude-haiku-4-5-20251001")[0] == WINDOW_200K, "configured window capped for 200K model")
    save_json(config_dir() / "settings.json", {})
    remember_selected_model(session, "claude-opus-4-6[1m]")
    _check(resolve_window(Ctx(project, session), "claude-opus-4-6")[0] == WINDOW_1M, "selected [1m] model widens window")
    _call("PostModelSwitch", {"session_id": session, "to_model": "claude-opus-4-6", "requested_model": None})
    _check(resolve_window(Ctx(project, session), "claude-opus-4-6")[0] == WINDOW_200K, "model switch updates selection")
    latch = {"bands": [60, 75, 90], "nudges": 3, "emergency": True, "band_window": WINDOW_200K}
    _check(_rearm(latch, WINDOW_1M, 31.0) and latch["bands"] == [] and latch["nudges"] == 0, "latches re-armed on window change")
    latch = {"bands": [60, 75], "nudges": 2, "emergency": False, "band_window": WINDOW_1M}
    _check(not _rearm(latch, WINDOW_1M, 80.0) and latch["bands"] == [60, 75], "latches kept on same window")

    remember_selected_model(session, "opus[1m]")
    _check(resolve_window(Ctx(project, session), "claude-opus-4-6")[0] == WINDOW_1M, "alias opus[1m] widens window")
    _call("PostModelSwitch", {"session_id": session, "to_model": "claude-opus-4-6", "requested_model": "opus[1m]"})
    _check(selected_model(session) == "claude-opus-4-6[1m]", "model switch stores resolved id + [1m]")
    _call("PostModelSwitch", {"session_id": session, "to_model": "claude-opus-4-6", "requested_model": "opus"})
    os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
    _check(default_window("us.anthropic.claude-sonnet-5-v1:0")[0] == WINDOW_1M, "sonnet 5 is 1M on third-party providers")
    os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "500000"
    _check(resolve_window(ctx, "us.anthropic.claude-sonnet-5-v1:0", 300_000)[0] == 500_000, "explicit window kept for sonnet 5 on bedrock")
    os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "500k"
    _check(resolve_window(ctx, "claude-haiku-4-5")[1].startswith("default"), "env accepts plain token counts only")
    os.environ.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    os.environ.pop("CLAUDE_CODE_USE_BEDROCK")
    save_json(ctx.calibration_path, {"claude-haiku-4-5": 165_000})
    _check(resolve_window(ctx, "claude-haiku-4-5", 170_000)[0] == WINDOW_200K, "stale calibration dropped without jumping to 1M")
    save_json(config_dir() / "settings.json", {"autoCompactWindow": 500_000})
    _check(resolve_window(ctx, "claude-haiku-4-5", 210_000)[0] == 500_000, "raise steps to the configured window first")
    save_json(config_dir() / "settings.json", {})
    sub = project / "paper"
    (sub / ".claude").mkdir(parents=True)
    save_json(sub / ".claude" / "settings.json", {"autoCompactWindow": 300_000})
    _check(resolve_window(Ctx.for_start(sub, session), "claude-opus-5-5")[0] == 300_000, "project settings read from the session dir")

    # note sections: ### subheadings stay inside their section; decisions only from 決定と理由
    sections = note_sections("## 現在の状態\n### 実験A\n- 走行中\n### 用語集\n- x\n## 未解決\n- [D-20261006-0d1e-1] を再確認\n")
    _check("実験A" in sections["現在の状態"] and "用語" not in sections, "subheadings stay in their section")
    write_text_atomic(ctx.data / "sessions" / "20261006_0d1e2f3a.md", read_text(note).replace("## 未解決\n- なし", "## 未解決\n- [D-20261006-0d1e-1] の閾値を再確認"))
    rollup(ctx)
    _check("B 案を採用" in read_text(ctx.decisions_path), "citing an id outside 決定と理由 does not overwrite it")

    twice = note_sections("## 決定と理由\n- [D-20261006-0d1e-1] a\n## 現在の状態\n- x\n## 決定と理由（撤回）\n- [D-20261006-0d1e-9] b\n")
    _check("D-20261006-0d1e-1" in twice["決定と理由"] and "D-20261006-0d1e-9" in twice["決定と理由"], "repeated sections are merged")

    # project data symlinks are replaced, never written through
    outside = tmp / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    ctx.decisions_path.unlink()
    try:
        os.symlink(outside, ctx.decisions_path)
        can_symlink = True
    except (OSError, NotImplementedError):  # Windows without symlink privilege
        can_symlink = False
    if can_symlink:
        rollup(ctx)
        _check(read_text(outside) == "keep me" and not ctx.decisions_path.is_symlink(), "symlinked decisions.md not followed")
        linked = tmp / "proj2"
        (linked / ".git").mkdir(parents=True)
        os.symlink(tmp / "elsewhere", linked / DATA_DIRNAME)
        try:
            Ctx(linked, session).ensure()
            _check(False, "symlinked data dir must be refused")
        except CkError:
            pass

    # next decision id looks past ids already rolled up
    buf = io.StringIO()
    write_text_atomic(note, "## 決定と理由\n")
    with contextlib.redirect_stdout(buf):
        cmd_path(argparse.Namespace(project=str(project), session=session))
    today = dt.datetime.now().strftime("%Y%m%d")
    used = [int(n) for n in re.findall(rf"\[D-{today}-0d1e-(\d+)\]", read_text(ctx.decisions_path))]
    _check(f"next decision id: D-{today}-0d1e-{max(used, default=0) + 1}" in buf.getvalue(), "next id skips rolled-up ids")
    _check("raw:" in buf.getvalue() and "decisions:" in buf.getvalue(), "path prints absolute data paths")

    # a dotfiles repo at $HOME must not swallow non-git projects
    fake_home = tmp / "home"
    (fake_home / ".git").mkdir(parents=True)
    (fake_home / "research" / "exp").mkdir(parents=True)
    real = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(fake_home)
    try:
        _check(find_project_root(fake_home / "research" / "exp") == fake_home / "research" / "exp", "home .git ignored")
    finally:
        for key, value in real.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# -------------------------------------------------------------------------- main


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="context-keeper helper for the context-keeper skill")
    parser.add_argument("--self-test", action="store_true", help="run the built-in tests and exit")
    sub = parser.add_subparsers(dest="command")

    hook = sub.add_parser("hook", help="run a hook handler (stdin: hook JSON)")
    # Documentation only: main() dispatches `hook` before argparse so a bad command line still exits 0.
    hook.add_argument("event", help="one of: " + ", ".join(sorted(HOOKS)))

    for name, helptext in (
        ("path", "print this session's note path and the next decision id"),
        ("saved", "register the note as saved (size check + rollup)"),
        ("card", "print the resume card"),
        ("status", "print context usage, note freshness and hook installation"),
        ("rollup", "roll decisions up into decisions.md / archive"),
        ("disable", "disable context-keeper for this project"),
        ("enable", "re-enable context-keeper for this project"),
    ):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("--project", help="project directory (default: $CLAUDE_PROJECT_DIR or cwd)")
        cmd.add_argument("--session", help="Claude Code session id")

    for name in ("install-hooks", "uninstall-hooks"):
        cmd = sub.add_parser(name, help=f"{name.split('-')[0]} the hooks in ~/.claude/settings.json")
        cmd.add_argument("--settings", help="settings file (default: $CLAUDE_CONFIG_DIR/settings.json)")
        cmd.add_argument("--dry-run", action="store_true", help="print the resulting settings instead of writing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        # Hook mode bypasses argparse: a malformed command line must still exit 0, never 2.
        try:
            configure_stdio()
            return run_hook(argv[1] if len(argv) > 1 else "", sys.stdin.buffer.read())
        except Exception:  # noqa: BLE001 - hooks must fail open
            return 0
    configure_stdio()
    args = parse_args(argv)
    if args.self_test:
        self_test()
        print("self-test passed")
        return 0
    commands = {
        "path": cmd_path,
        "saved": cmd_saved,
        "card": cmd_card,
        "status": cmd_status,
        "rollup": cmd_rollup,
        "disable": lambda a: cmd_toggle(a, disable=True),
        "enable": lambda a: cmd_toggle(a, disable=False),
        "install-hooks": lambda a: cmd_install(a, uninstall=False),
        "uninstall-hooks": lambda a: cmd_install(a, uninstall=True),
    }
    if args.command not in commands:
        parse_args(["--help"])
        return 2
    try:
        return commands[args.command](args)
    except CkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

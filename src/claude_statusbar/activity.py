"""Live-session activity, parsed from the Claude Code transcript JSONL.

Surfaces the "what is Claude doing right now" signals that the quota/cache
line can't show: todo progress, the currently-running tool, dispatched
subagents, plus cheap session stats (duration, lines changed) that Claude
Code already hands us on stdin.

Design constraints (this runs on the render hot path, once per refresh):
  * pure stdlib, no subprocess / no heavy imports (see test_import_perf)
  * the transcript scan is a bounded reverse-tail read (same 320KB budget as
    core._last_assistant_info) — never a full forward pass over a multi-MB
    file.

The reverse-tail direction is exactly right for "current" state: the newest
TodoWrite, the running tool, and the active agent all sit at the file tail,
and tool_result blocks (which mark a tool as finished) appear *after* their
tool_use, so scanning newest-first we meet the result before the use.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Reuse the same byte budget as the cache-age reader so a giant transcript
# can't blow up render time.
_CHUNK = 32 * 1024
_MAX_BYTES = 10 * _CHUNK
_MAX_AGENTS = 3
# Tools completed within this window get shown with age-based fading.
_RECENT_TOOL_WINDOW_S = 2.5
_RECENT_TOOL_PHASE1_S = 1.0   # bright phase ends here
_RECENT_TOOL_PHASE2_S = 2.5   # muted phase ends → gone
# Completed agents shown for this long after they finish. The first _AGENT_HOLD_S
# holds a solid status circle (green ●/red ●) so completion is unmistakable at the
# bar's ~1Hz refresh; the remainder fades it to invisible.
_AGENT_FADE_S = 3.0
_AGENT_HOLD_S = 1.0
# Completed agents stay in the progress line for this long.
# Conversation turns take minutes; agents completing 5-10min ago must still show.
_PROGRESS_FADE_S = 600.0
# Conservative fallback TTL when the transcript carries no cache-write signal
# (caching disabled / pre-breakdown transcript). Matches Anthropic's base 5min.
_FALLBACK_TTL_S = 300

_MCP_RE = re.compile(r"^mcp__.+__.+$")
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")

# Tools whose meaningful argument is a filesystem path → show the basename.
_FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}
_PATTERN_TOOLS = {"Glob", "Grep"}
_AGENT_TOOLS = {"Task", "Agent"}

_BASH_MAX = 30


# ---------------------------------------------------------------------------
# Pure formatting helpers
# ---------------------------------------------------------------------------
# Bash leading tokens that aren't the "real" program. Wrappers (env/sudo/…)
# precede the program in the same segment; standalone builtins (cd/export/…)
# are their own segment, so we skip past them to the next pipeline stage.
# Wrappers and shell keywords that precede the real program in the same
# segment (skip the token, keep parsing). `do`/`then`/`else` introduce a loop
# or conditional body — the program we want follows them.
_BASH_WRAP = {"env", "sudo", "nohup", "time", "exec", "command", "builtin",
              "xargs", "do", "then", "else"}
# Standalone builtins / loop heads that own their whole segment — skip to the
# next pipeline stage to find the real program.
_BASH_SKIP = {"cd", "export", "set", "source", ".", ":", "true", "false",
              "fi", "done", "while", "for", "if"}
_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")


def _bash_program(cmd: str) -> str:
    """Best-effort 'what program is this' for a Bash command, for the active-tool
    chip. Splits on pipes / && / || / ; / newlines, strips leading VAR=val
    assignments and wrapper commands (env/sudo/time/…), skips standalone
    builtins (cd/export/…) to the next stage, and returns the basename of the
    first real program. Keeps the bar readable instead of dumping a heredoc.
    """
    fallback = ""
    for seg in re.split(r"[\n;]|&&|\|\||\|", cmd):
        toks = seg.strip().split()
        i = 0
        while i < len(toks) and (_ASSIGN_RE.match(toks[i])
                                 or toks[i].rsplit("/", 1)[-1] in _BASH_WRAP):
            i += 1
        if i >= len(toks):
            continue
        prog = toks[i].strip("'\"")
        base = prog.rsplit("/", 1)[-1] if "/" in prog else prog
        if base in _BASH_SKIP:
            fallback = fallback or base
            continue
        if base:
            return base[:24]
    return fallback[:24]


def extract_target(name: str, inp: Dict[str, Any]) -> str:
    """The meaningful argument to show beside a tool name.

    Read/Write/Edit → file basename; Glob/Grep → pattern; Bash → the command
    truncated; Skill → the skill name. Unknown tools / missing args → "".
    """
    if not isinstance(inp, dict):
        return ""
    if name in _FILE_TOOLS:
        path = inp.get("file_path") or inp.get("path") or ""
        if not isinstance(path, str):  # malformed transcript — don't crash
            return ""
        return os.path.basename(path.rstrip("/")) if path else ""
    if name in _PATTERN_TOOLS:
        return str(inp.get("pattern") or "")
    if name == "Bash":
        cmd = str(inp.get("command") or "").strip()
        prog = _bash_program(cmd)
        if prog:
            return prog
        return cmd[:_BASH_MAX] + "…" if len(cmd) > _BASH_MAX else cmd
    if name == "Skill":
        return str(inp.get("skill") or "")
    return ""


def shorten_tool_name(name: str, max_len: int = 20) -> str:
    """`mcp__figma__get_screenshot` → `get_screenshot`, then ellipsis-truncate."""
    if _MCP_RE.match(name or ""):
        name = name.split("__")[-1]
    if len(name) > max_len:
        return name[: max_len - 1] + "…"
    return name


def format_duration_short(ms: int) -> str:
    """Coarse session duration: `45s`, `12m`, `1h05m`. 0 → ""."""
    try:
        s = int(ms) // 1000
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def format_lines(added: int, removed: int) -> str:
    """Session line delta: `+182 -47`, `+5`, `-3`. Both 0 → ""."""
    try:
        a, r = int(added), int(removed)
    except (TypeError, ValueError):
        return ""
    parts = []
    if a > 0:
        parts.append(f"+{a}")
    if r > 0:
        parts.append(f"-{r}")
    return " ".join(parts)


def format_elapsed_short(seconds: float) -> str:
    """Live elapsed for a running agent/tool: `<1s`, `45s`, `2m15s`, `1h05m`."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 1:
        return "<1s"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def format_cache_countdown(age_seconds: Optional[float],
                           detected_ttl: Optional[int],
                           ttl_override: Optional[int] = None) -> str:
    """Format the prompt-cache countdown from a turn age + its detected TTL.

    Shared by ``core.get_cache_age_text`` and the merged single-scan render
    path so both produce byte-identical output:
      - "COLD" when there's no assistant turn (age None) or the cache expired.
      - "XhMMmSSs" / "MmSSs" / "Ys" remaining otherwise. Seconds are always
        shown (so the bar visibly ticks); sub-minute omits 'm' (the styles
        layer keys yellow off the missing 'm').
    """
    if age_seconds is None:
        return "COLD"
    ttl = ttl_override if ttl_override is not None else (
        detected_ttl if detected_ttl is not None else _FALLBACK_TTL_S)
    age = 0.0 if age_seconds < 0 else age_seconds  # clamp future timestamps
    remaining = ttl - age
    if remaining <= 0:
        return "COLD"
    remaining_int = int(remaining) if remaining == int(remaining) else int(remaining) + 1
    secs = remaining_int % 60
    if remaining_int >= 3600:
        return f"{remaining_int // 3600}h{(remaining_int % 3600) // 60:02d}m{secs:02d}s"
    mins = remaining_int // 60
    if mins > 0:
        return f"{mins}m{secs:02d}s"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Transcript scan
# ---------------------------------------------------------------------------
@dataclass
class ActivityInfo:
    """A snapshot of live session activity, all fields optional/empty."""

    todos: List[Tuple[str, str]] = field(default_factory=list)
    # (display_name, target) of the tool with no result yet, or None.
    active_tool: Optional[Tuple[str, str]] = None
    # [(display_name, target, age_seconds)] for tools completed within the last
    # RECENT_TOOL_WINDOW_S seconds, sorted newest-first. Used for fading display.
    recent_completed: List[Tuple[str, str, float]] = field(default_factory=list)
    # running subagents: [{name, model, description, elapsed_seconds, background}]
    agents: List[Dict[str, Any]] = field(default_factory=list)
    # recently finished subagents (within _AGENT_FADE_S): [{...agent, completed_age}]
    recently_finished_agents: List[Dict[str, Any]] = field(default_factory=list)
    # MCP server → completed call count (e.g. {"gmail": 3, "drive": 1}).
    mcp_counts: Dict[str, int] = field(default_factory=dict)
    # skill name → invocation count (e.g. {"reflect": 1, "deep-research": 2}).
    skill_counts: Dict[str, int] = field(default_factory=dict)
    # total agents spawned in scan window (running + finished).
    agent_total: int = 0
    # total completed tool calls in the scan window.
    tool_total: int = 0
    # tool calls that returned an error result.
    error_count: int = 0
    # WebFetch + WebSearch calls.
    web_count: int = 0
    # unique file basenames touched (Read/Write/Edit/MultiEdit).
    files_touched: List[str] = field(default_factory=list)
    # prompt-cache countdown inputs, gathered in the same scan (see
    # format_cache_countdown): age of the newest assistant turn + the TTL it
    # applied. None when the transcript carries no assistant turn / no signal.
    cache_age_seconds: Optional[float] = None
    cache_ttl: Optional[int] = None

    @property
    def todos_total(self) -> int:
        return len(self.todos)

    @property
    def todos_done(self) -> int:
        return sum(1 for _, s in self.todos if s == "completed")

    @property
    def in_progress_todo(self) -> Optional[str]:
        for content, status in self.todos:
            if status == "in_progress":
                return content
        return None

    def is_empty(self) -> bool:
        return not (self.todos or self.active_tool
                    or self.recent_completed or self.agents)


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse a transcript ISO timestamp to an aware UTC datetime, or None."""
    if not isinstance(ts_str, str) or not ts_str:
        return None
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _entry_cache_ttl(entry: Dict[str, Any]) -> Optional[int]:
    """The prompt-cache TTL Anthropic applied on this turn (3600/300), or None.

    Read from `message.usage.cache_creation`: a nonzero `ephemeral_1h_input_tokens`
    means a 1-hour `cache_control` ttl, `ephemeral_5m_input_tokens` means 5min.
    """
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    cc = usage.get("cache_creation")
    if not isinstance(cc, dict):
        return None
    if (cc.get("ephemeral_1h_input_tokens") or 0) > 0:
        return 3600
    if (cc.get("ephemeral_5m_input_tokens") or 0) > 0:
        return 300
    return None


def _content_blocks(entry: Dict[str, Any]) -> List[Any]:
    msg = entry.get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, list):
            return c
    return []


def _scan_agents_full(transcript_path: str, now: datetime):
    """Forward full-file scan for Agent/Task tool_use and tool_result blocks only.

    Returns (running, recently_finished, agent_total, seen_result_ids, enqueued_ids).
    recently_finished contains agents that completed within _AGENT_FADE_S seconds.
    """
    agents_running: List[Dict[str, Any]] = []
    agent_total = 0
    seen_results: set = set()
    # tid → age_seconds when the result was written (for fading completed agents).
    result_ages: Dict[str, float] = {}
    enqueued: set = set()
    # tool_use ids of agents seen so far. Inline-agent tool_result lines carry
    # only the tool_use_id (no "Task"/"Agent" keyword), so the cheap keyword
    # pre-filter would skip them and the agent would never resolve as finished
    # — leaving a ghost "running" agent that lingers until _STALE_S. We also
    # admit result lines that reference an already-seen agent id; a use always
    # precedes its result in file order, so the id is known by the time we get
    # there. Full-file (unbounded) scan, so every result resolves regardless of
    # transcript size — unlike the bounded tail reconciliation in read_activity.
    agent_tids: set = set()
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                hit = ("Agent" in raw or "Task" in raw or "enqueue" in raw)
                if not hit and agent_tids and ("tool_result" in raw or "tool_use_id" in raw):
                    hit = any(t in raw for t in agent_tids)
                if not hit:
                    continue
                try:
                    entry = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    continue
                entry_ts = _parse_ts(entry.get("timestamp", ""))
                entry_age = (now - entry_ts).total_seconds() if entry_ts else None
                if entry.get("type") == "queue-operation" and entry.get("operation") == "enqueue":
                    for m in _TOOL_USE_ID_RE.finditer(str(entry.get("content") or "")):
                        tid = m.group(1)
                        enqueued.add(tid)
                        if entry_age is not None:
                            result_ages[tid] = entry_age
                    continue
                for b in _content_blocks(entry):
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "tool_result":
                        tid = b.get("tool_use_id")
                        if tid:
                            seen_results.add(tid)
                            if entry_age is not None:
                                result_ages[tid] = entry_age
                            if b.get("is_error"):
                                result_ages[tid + ":error"] = 1  # sentinel
                        continue
                    if bt != "tool_use":
                        continue
                    name = b.get("name") or ""
                    if name not in _AGENT_TOOLS:
                        continue
                    inp = b.get("input") or {}
                    if not isinstance(inp, dict):
                        inp = {}
                    tid = b.get("id")
                    if tid:
                        agent_tids.add(tid)
                    background = bool(inp.get("run_in_background"))
                    agent_total += 1
                    start = _parse_ts(entry.get("timestamp", ""))
                    elapsed = max(0.0, (now - start).total_seconds()) if start else 0.0
                    agents_running.append({
                        "_tid": tid,
                        "_background": background,
                        "name": str(inp.get("subagent_type") or "agent"),
                        "description": str(inp.get("description") or ""),
                        "model": str(inp.get("model") or ""),
                        "elapsed_seconds": elapsed,
                        "background": background,
                    })
    except OSError:
        pass
    _STALE_S = 300.0
    # Unresponsive threshold: agent running > 90s with no output yet → yellow warning.
    _UNRESPONSIVE_S = 90.0
    running, recently_finished = [], []
    for ag in agents_running:
        tid = ag["_tid"]
        done = (tid in enqueued) if ag["_background"] else (tid in seen_results)
        if done:
            age = result_ages.get(tid, 999.0)
            errored = (tid + ":error") in result_ages
            if age <= _PROGRESS_FADE_S:
                recently_finished.append({**ag, "completed_age": age, "error": errored})
        elif ag["elapsed_seconds"] <= _STALE_S:
            unresponsive = ag["elapsed_seconds"] > _UNRESPONSIVE_S
            running.append({**ag, "unresponsive": unresponsive})
    return running[-_MAX_AGENTS:], recently_finished, agent_total, seen_results, enqueued


def _iter_entries_reverse(transcript_path: str):
    """Yield parsed JSONL entries newest-first, bounded to _MAX_BYTES of tail.

    Mirrors core._last_assistant_info's chunked reverse read so a multi-MB
    transcript never costs more than the byte budget per render.
    """
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return
            buf = b""
            pos = size
            scanned = 0
            while pos > 0 and scanned < _MAX_BYTES:
                read = min(_CHUNK, pos)
                pos -= read
                scanned += read
                f.seek(pos)
                buf = f.read(read) + buf
                lines = buf.split(b"\n")
                if pos > 0:
                    buf = lines[0]
                    candidates = lines[1:]
                else:
                    buf = b""
                    candidates = lines
                for raw in reversed(candidates):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        yield json.loads(raw)
                    except (ValueError, json.JSONDecodeError):
                        continue
    except OSError:
        return


def read_activity(transcript_path: str,
                  now: Optional[datetime] = None) -> ActivityInfo:
    """Scan the transcript tail (newest-first) for live activity.

    Extracts, in one bounded reverse-tail pass:
      * todos      — the newest TodoWrite list (last-write-wins: TodoWrite
                     carries the full list, so the first one we meet scanning
                     backward is the current state).
      * active_tool — the newest tool_use with no tool_result yet. Results
                     (in `user` entries) appear after their use in file order,
                     so scanning newest-first we meet the result before the
                     use; a use we reach without a recorded result is running.
      * completed_counts — frequency rollup of recently-finished tools.
      * agents     — running subagents (Task/Agent) with live elapsed time.
                     Inline agents finish via their tool_result; background
                     ones (run_in_background) finish via a queue-operation
                     `enqueue` whose content carries their <tool-use-id>.
    """
    now = now or datetime.now(timezone.utc)
    info = ActivityInfo()
    # Full forward scan for agents — the only signal that can be arbitrarily old.
    running_agents, recently_finished, agent_total, seen_results, enqueued = _scan_agents_full(transcript_path, now)
    info.agents = running_agents
    info.recently_finished_agents = recently_finished
    info.agent_total = agent_total
    todos_found = False
    # tool_use_id → age in seconds when its tool_result was seen (for fading).
    tool_result_times: Dict[str, float] = {}
    recent: List[Tuple[str, str, float]] = []
    for entry in _iter_entries_reverse(transcript_path):
        if entry.get("type") == "queue-operation" and entry.get("operation") == "enqueue":
            for m in _TOOL_USE_ID_RE.finditer(str(entry.get("content") or "")):
                enqueued.add(m.group(1))
            continue
        # Entry-level timestamp → age in seconds, used to timestamp completions.
        entry_ts = _parse_ts(entry.get("timestamp", ""))
        entry_age = (now - entry_ts).total_seconds() if entry_ts is not None else None
        # Cache countdown (same scan): newest assistant turn's age + the TTL
        # the newest cache-writing turn applied. Decoupled like core's reader.
        if entry.get("type") == "assistant":
            if info.cache_age_seconds is None and entry_age is not None:
                info.cache_age_seconds = entry_age
            if info.cache_ttl is None:
                t = _entry_cache_ttl(entry)
                if t is not None:
                    info.cache_ttl = t
        for b in _content_blocks(entry):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    seen_results.add(tid)
                    if entry_age is not None:
                        tool_result_times[tid] = entry_age
                if b.get("is_error"):
                    info.error_count += 1
                continue
            if bt != "tool_use":
                continue
            name = b.get("name") or ""
            # A malformed/corrupt transcript can carry a non-dict input that
            # still parses as valid JSON — normalize so no branch below
            # dereferences a str/list/int (which would crash the whole render).
            inp = b.get("input")
            if not isinstance(inp, dict):
                inp = {}
            if name == "TodoWrite":
                if not todos_found:
                    todos = inp.get("todos")
                    if isinstance(todos, list):
                        info.todos = [
                            (str(t.get("content", "")), str(t.get("status", "")))
                            for t in todos if isinstance(t, dict)
                        ]
                        todos_found = True
                continue
            if name in _AGENT_TOOLS:
                continue  # handled by _scan_agents_full
            if name in ("TaskCreate", "TaskUpdate"):
                continue
            tid = b.get("id")
            completed_at = tool_result_times.get(tid) if tid else None
            is_done = tid in seen_results
            # MCP tools: mcp__<server>__<tool>
            if _MCP_RE.match(name):
                if is_done:
                    server = name.split("__")[1] if name.count("__") >= 2 else name
                    info.mcp_counts[server] = info.mcp_counts.get(server, 0) + 1
                    info.tool_total += 1
                elif info.active_tool is None:
                    info.active_tool = (shorten_tool_name(name), "")
                continue
            # Skill invocations
            if name == "Skill":
                if is_done:
                    skill = str(inp.get("skill") or "?")
                    info.skill_counts[skill] = info.skill_counts.get(skill, 0) + 1
                    info.tool_total += 1
                elif info.active_tool is None:
                    info.active_tool = ("Skill", str(inp.get("skill") or ""))
                continue
            # Web calls tracked separately
            if name in ("WebFetch", "WebSearch"):
                if is_done:
                    info.web_count += 1
                    info.tool_total += 1
                elif info.active_tool is None:
                    info.active_tool = (name, str(inp.get("url") or inp.get("query") or "")[:30])
                continue
            # Files touched (unique basenames)
            if name in _FILE_TOOLS and is_done:
                path = inp.get("file_path") or inp.get("path") or ""
                if path:
                    bn = os.path.basename(str(path).rstrip("/"))
                    if bn and bn not in info.files_touched:
                        info.files_touched.append(bn)
            display = shorten_tool_name(name)
            if is_done:
                info.tool_total += 1
                if completed_at is not None and completed_at <= _RECENT_TOOL_WINDOW_S:
                    target = extract_target(name, inp)
                    recent.append((display, target, completed_at))
            elif info.active_tool is None:
                info.active_tool = (display, extract_target(name, inp))
    # Newest first (smallest age).
    info.recent_completed = sorted(recent, key=lambda t: t[2])

    # Re-filter running agents using tool_result_times from the tail scan.
    # _scan_agents_full misses inline agent tool_results (pre-filter skips them);
    # the reverse scan sees them all. Move any newly-resolved agents to recently_finished.
    still_running = []
    for ag in info.agents:
        tid = ag["_tid"]
        if tid in tool_result_times:
            age = tool_result_times[tid]
            if age <= _AGENT_FADE_S:
                info.recently_finished_agents.append({**ag, "completed_age": age})
        else:
            still_running.append(ag)
    info.agents = still_running
    return info


def _subagent_progress(jsonl_path: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Derive live progress from one subagent transcript: the tool it's running
    right now (last tool_use with no result), cumulative tool-call count, turns,
    output tokens, context size, and seconds since its last entry (liveness)."""
    turns = ncalls = out_tok = in_tok = ctx = 0
    tool_counts: dict = {}
    pending: List[Tuple[Any, str]] = []   # (tool_use_id, name) awaiting a result
    results: set = set()
    last_ts: Optional[datetime] = None
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    e = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    continue
                ts = _parse_ts(e.get("timestamp", ""))
                if ts is not None:
                    last_ts = ts
                msg = e.get("message") or {}
                if e.get("type") == "assistant":
                    turns += 1
                u = msg.get("usage")
                if isinstance(u, dict):
                    out_tok += int(u.get("output_tokens", 0) or 0)
                    in_tok += int(u.get("input_tokens", 0) or 0)
                    ctx = max(ctx, int(u.get("cache_read_input_tokens", 0) or 0))
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use":
                            ncalls += 1
                            tname = shorten_tool_name(b.get("name") or "?")
                            tool_counts[tname] = tool_counts.get(tname, 0) + 1
                            pending.append((b.get("id"), b.get("name") or "?"))
                        elif b.get("type") == "tool_result":
                            results.add(b.get("tool_use_id"))
    except OSError:
        return None
    active = ""
    last_tool = ""
    for tid, name in reversed(pending):
        if not last_tool:
            last_tool = shorten_tool_name(str(name))
        if tid not in results:
            active = shorten_tool_name(str(name))
            break
    idle = (now - last_ts).total_seconds() if last_ts else None
    return {"active": active or last_tool, "calls": ncalls, "turns": turns,
            "out_tok": out_tok, "in_tok": in_tok, "tool_counts": tool_counts,
            "ctx": ctx, "idle": idle}


def enrich_agent_progress(info: ActivityInfo, transcript_path: str,
                          now: Optional[datetime] = None) -> None:
    """Attach per-agent live progress onto info.agents and info.recently_finished_agents,
    read from the subagent transcripts at <session>/subagents/agent-<id>.jsonl.
    Opt-in — called only when show_agent_progress is set.
    """
    all_agents = list(info.agents) + list(info.recently_finished_agents)
    if not all_agents:
        return
    now = now or datetime.now(timezone.utc)
    sub_dir = os.path.join(os.path.splitext(transcript_path)[0], "subagents")
    try:
        entries = os.listdir(sub_dir)
    except OSError:
        return
    want = {ag.get("_tid") for ag in all_agents if ag.get("_tid")}
    cutoff = now.timestamp() - 600.0
    tid_to_file: Dict[str, str] = {}
    for fn in entries:
        if not fn.endswith(".meta.json"):
            continue
        jsonl = os.path.join(sub_dir, fn[:-len(".meta.json")] + ".jsonl")
        try:
            if os.path.getmtime(jsonl) < cutoff:
                continue
        except OSError:
            continue
        try:
            with open(os.path.join(sub_dir, fn), "r", encoding="utf-8", errors="replace") as mf:
                meta = json.load(mf)
        except (OSError, ValueError):
            continue
        tu = meta.get("toolUseId")
        if tu in want and tu not in tid_to_file:
            tid_to_file[tu] = jsonl
    for ag in all_agents:
        path = tid_to_file.get(ag.get("_tid"))
        if not path:
            continue
        prog = _subagent_progress(path, now)
        if prog:
            ag["progress"] = prog

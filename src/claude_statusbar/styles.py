"""Status-line layout renderers (style = layout, theme = palette).

Each renderer takes the same set of fields as ``progress.format_status_line``
plus a Theme, and returns the final ANSI string.

Adding a new style:
    1. Define ``render_<name>(...) -> str``
    2. Register it in ``RENDERERS``.
"""

from typing import Optional

from .themes import Theme, get_theme

RESET = "\033[0m"
BOLD  = "\033[1m"
ITAL  = "\033[3m"
FAINT = "\033[2m"   # dim/faint attribute — makes a grey recede even further

# Installed version, resolved once and cached. importlib.metadata is ~20ms and
# banned on the per-render import graph, but a lazy call here (only when the
# version segment is on) is fine: it's not an import-time edge, and the daemon
# pays it once per process. Empty string if it can't be determined.
_VERSION_CACHE = None
def _statusbar_version() -> str:
    global _VERSION_CACHE
    if _VERSION_CACHE is None:
        try:
            import importlib.metadata as _m
            _VERSION_CACHE = _m.version("claude-statusbar")
        except Exception:
            _VERSION_CACHE = ""
    return _VERSION_CACHE


def _version_gt(a: str, b: str) -> bool:
    """True if dotted version `a` is newer than `b`. Fail-safe (bad parts → 0)."""
    def parts(v):
        out = []
        for x in str(v).split("."):
            try:
                out.append(int(x))
            except ValueError:
                out.append(0)
        return out
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def _update_hint(path=None) -> str:
    """The newer version string if the cached PyPI check says one is available
    (and the check is recent), else ''. Cheap file read — no network, no
    importlib on the hot path. Written by updater.get_latest_version."""
    try:
        import json as _json
        import time as _t
        from pathlib import Path as _Path
        p = _Path(path) if path is not None else (
            _Path.home() / ".cache" / "claude-statusbar" / "latest_version.json")
        data = _json.loads(p.read_text(encoding="utf-8"))
        latest = str(data.get("version", ""))
        checked_at = float(data.get("checked_at", 0))
        if not latest or _t.time() - checked_at > 7 * 86400:  # stale → no arrow
            return ""
        return latest if _version_gt(latest, _statusbar_version()) else ""
    except Exception:
        return ""

def _fg(rgb): return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
def _bg(rgb): return f"\033[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"

# strip ANSI when use_color is False
import re as _re
_ANSI_RE = _re.compile(r"\033\[[0-9;]*m")
def _strip(s: str) -> str: return _ANSI_RE.sub("", s)

# Density → padding string, shared by all renderers that support it.
DENSITY_PAD = {"compact": "", "regular": " ", "cozy": "  "}


def _severity_color(theme: Theme, pct: Optional[float],
                     warning: float, critical: float) -> tuple:
    if pct is None:
        return theme.mute
    if pct >= critical: return theme.s_hot
    if pct >= warning:  return theme.s_warn
    return theme.s_ok


def _cache_severity(theme: Theme, cache_text: str) -> tuple:
    """Map a countdown cache string to a severity color.

    "COLD"            → s_hot   (red, expired)
    "<1m" remaining   → s_warn  (yellow, ~1min left)
    otherwise         → s_ok    (green, comfortable)

    The "<1m" detection works because the countdown formatter only emits
    sub-minute remainders as bare "Ys" (no 'm', no 'h'). Anything with a
    minute or hour glyph is in the comfortable zone.
    """
    if cache_text == "COLD":
        return theme.s_hot
    # Comfortable: contains 'm' (minutes) or 'h' (hours).
    if "m" in cache_text or "h" in cache_text:
        return theme.s_ok
    return theme.s_warn


# ---------------------------------------------------------------------------
# Style: capsule
# ---------------------------------------------------------------------------
def render_capsule(
    *, msgs_pct, weekly_pct, reset_5h, reset_7d, model,
    lang_body="", cost_text="", cache_age_text="", bypass=False,
    use_color=True, theme: Optional[Theme]=None,
    warning_threshold=30.0, critical_threshold=70.0,
    density: str = "regular",
    show_weekly: bool = True,
    ctx_pct: Optional[float] = None,
    **_ignored,
) -> str:
    theme = theme or get_theme("graphite")
    INK    = _fg(theme.pill_ink)
    EDGE   = _fg(theme.edge)
    MUTE   = _fg(theme.mute)

    pad = DENSITY_PAD.get(density, " ")

    def pill(bg_rgb, body):
        return f"{_bg(bg_rgb)}{INK}{pad}{body}{pad}{RESET}"

    def sev_dot(p):
        if p is None:
            return ""
        col = _severity_color(theme, p, warning_threshold, critical_threshold)
        return f" {_fg(col)}●{RESET}"

    def pct_text(p):
        return "···" if p is None else f"{int(round(p))}%"

    spacer = f"{EDGE} ╱{RESET} "

    parts = []

    five_body = (
        f"{BOLD}◷ 5H{RESET}{INK}{_bg(theme.pill_5h)} {pct_text(msgs_pct)} "
        f"· {reset_5h}{sev_dot(msgs_pct)}{INK}{_bg(theme.pill_5h)}"
    )
    parts.append(pill(theme.pill_5h, five_body))

    if show_weekly:
        week_body = (
            f"{BOLD}☷ 7D{RESET}{INK}{_bg(theme.pill_7d)} {pct_text(weekly_pct)} "
            f"· {reset_7d or '--'}{sev_dot(weekly_pct)}{INK}{_bg(theme.pill_7d)}"
        )
        parts.append(pill(theme.pill_7d, week_body))

    model_body = f"{BOLD}◆{RESET}{INK}{_bg(theme.pill_model)} {model}{sev_dot(ctx_pct)}{INK}{_bg(theme.pill_model)}"
    parts.append(pill(theme.pill_model, model_body))

    if cost_text:
        parts.append(pill(theme.pill_cost, f"$ {cost_text}"))

    if lang_body:
        parts.append(pill(theme.pill_lang, f"📚 {lang_body}"))

    if cache_age_text:
        bg = _cache_severity(theme, cache_age_text)
        parts.append(pill(bg, f"cache {cache_age_text}"))

    line = spacer.join(parts)

    if bypass:
        line += f"  {_fg(theme.s_hot)}{BOLD}⚠ BYPASS{RESET}"

    if not use_color:
        return _strip(line)
    return line


# ---------------------------------------------------------------------------
# Style: hairline
# ---------------------------------------------------------------------------
def render_hairline(
    *, msgs_pct, weekly_pct, reset_5h, reset_7d, model,
    lang_body="", cost_text="", cache_age_text="", bypass=False,
    use_color=True, theme: Optional[Theme]=None,
    warning_threshold=30.0, critical_threshold=70.0,
    density: str = "regular",
    show_weekly: bool = True,
    ctx_pct: Optional[float] = None,
    **_ignored,
) -> str:
    theme = theme or get_theme("graphite")
    INK  = _fg(theme.ink)
    MUTE = _fg(theme.mute)
    EDGE = _fg(theme.edge)

    def mini3(p):
        if p is None:
            return f"{MUTE}···{RESET}"
        cells = []
        for i in range(3):
            slot = (i + 1) * (100 / 3)
            if   p >= slot:                   cells.append("█")
            elif p >= slot - (100 / 3) * 0.66: cells.append("▆")
            elif p >= slot - (100 / 3):       cells.append("▃")
            else:                             cells.append("▁")
        col = _severity_color(theme, p, warning_threshold, critical_threshold)
        return f"{_fg(col)}{''.join(cells)}{RESET}"

    def pct_text(p):
        return "···" if p is None else f"{int(round(p)):>2}%"

    sep_pad = DENSITY_PAD.get(density, " ")
    sep = f"{sep_pad}{EDGE}┊{RESET}{sep_pad}"
    parts = []

    parts.append(
        f"{MUTE}› 5h{RESET} {mini3(msgs_pct)} {INK}{pct_text(msgs_pct)}{RESET} "
        f"{MUTE}↺ {reset_5h}{RESET}"
    )
    if show_weekly:
        parts.append(
            f"{MUTE}› 7d{RESET} {mini3(weekly_pct)} {INK}{pct_text(weekly_pct)}{RESET} "
            f"{MUTE}↺ {reset_7d or '--'}{RESET}"
        )
    # Model line — colored by ctx_pct severity, neutral ink when absent
    if ctx_pct is None:
        model_color = INK
    else:
        col = _severity_color(theme, ctx_pct, warning_threshold, critical_threshold)
        model_color = _fg(col)
    parts.append(f"{MUTE}›{RESET} {model_color}{model}{RESET}")

    if cost_text:
        parts.append(f"{MUTE}$ {INK}{cost_text}{RESET}")

    if lang_body:
        parts.append(f"{MUTE}{lang_body}{RESET}")

    if cache_age_text:
        col = _fg(_cache_severity(theme, cache_age_text))
        parts.append(f"{col}cache {cache_age_text}{RESET}")

    if bypass:
        parts.append(f"{_fg(theme.s_hot)}{BOLD}⚠ BYPASS{RESET}")

    line = sep.join(parts)
    if not use_color:
        return _strip(line)
    return line


# ---------------------------------------------------------------------------
# Style: classic — wraps the existing format_status_line for backward compat
# ---------------------------------------------------------------------------
def render_classic(
    *, msgs_pct, weekly_pct, reset_5h, reset_7d, model,
    lang_body="", cost_text="", cache_age_text="", bypass=False,
    use_color=True, theme: Optional[Theme]=None,
    warning_threshold=30.0, critical_threshold=70.0,
    countdown_emoji: str = "",
    ctx_pct: Optional[float] = None,
    show_ctx_bar: bool = True,
    ctx_eta: str = "",
    shimmer_phase=None,
    projection_5h: str = "",
    projection_7d: str = "",
    forecast_5h: str = "",
    forecast_7d: str = "",
    burn_eta_5h: str = "",
    burn_eta_5h_urgent: bool = False,
    burn_eta_7d: str = "",
    **_ignored,
) -> str:
    from .progress import format_status_line, _fg, colorize, RESET
    theme = theme or get_theme("graphite")
    lang_text = (
        colorize(f"📚 {lang_body}", _fg(theme.s_ok), use_color)
        if lang_body else ""
    )
    result = format_status_line(
        msgs_pct=msgs_pct, tkns_pct=None,
        reset_time=reset_5h, model=model,
        weekly_pct=weekly_pct, reset_time_7d=reset_7d or "",
        ctx_pct=ctx_pct,
        show_ctx_bar=show_ctx_bar,
        ctx_eta=ctx_eta,
        bypass=bypass, use_color=use_color,
        countdown_emoji=countdown_emoji,
        warning_threshold=warning_threshold,
        critical_threshold=critical_threshold,
        lang_text=lang_text,
        cost_text=cost_text,
        theme=theme,
        shimmer_phase=shimmer_phase,
        projection_5h=projection_5h,
        projection_7d=projection_7d,
        forecast_5h=forecast_5h,
        forecast_7d=forecast_7d,
        burn_eta_5h=burn_eta_5h,
        burn_eta_5h_urgent=burn_eta_5h_urgent,
        burn_eta_7d=burn_eta_7d,
    )
    if cache_age_text:
        # Three-level severity: COLD red, <1m yellow, otherwise green.
        if cache_age_text == "COLD":
            col = _fg(theme.s_hot)
        elif "m" in cache_age_text or "h" in cache_age_text:
            col = _fg(theme.s_ok)
        else:
            col = _fg(theme.s_warn)
        mute = _fg(theme.mute)
        reset = RESET if use_color else ""  # don't leak a bare RESET in no-color mode
        result += f"{reset}{colorize(' | ', mute, use_color)}{colorize(f'cache {cache_age_text}', col, use_color)}"
    return result


def _ahead_behind_glyphs(ahead, behind) -> str:
    """`↑2↓1` / `↑3` / `↓1` / "" — only the nonzero directions."""
    out = ""
    if ahead:
        out += f"↑{ahead}"
    if behind:
        out += f"↓{behind}"
    return out


def _stats_segment(duration_text: str, lines_text: str, *, theme: Theme,
                   use_color: bool) -> str:
    """The ` · ⏱ <dur> · +added -removed` tail appended to the identity line.

    Returns "" when neither is present. Diff colors: +added green, -removed red.
    """
    if not (duration_text or lines_text):
        return ""
    # Lines (productivity) first, then the weaker duration signal.
    if not use_color:
        parts = []
        if lines_text:
            parts.append(lines_text)
        if duration_text:
            parts.append(f"⏱ {duration_text}")
        return " · " + " · ".join(parts)
    MUTE = _fg(theme.mute)
    INK  = _fg(theme.ink)
    OK   = _fg(theme.s_ok)
    HOT  = _fg(theme.s_hot)
    segs = []
    if lines_text:
        toks = []
        for tok in lines_text.split():
            c = OK if tok.startswith("+") else HOT if tok.startswith("-") else MUTE
            toks.append(f"{c}{tok}{RESET}")
        segs.append(" ".join(toks))
    if duration_text:
        segs.append(f"{MUTE}⏱{RESET} {INK}{duration_text}{RESET}")
    sep = f" {MUTE}·{RESET} "
    return f" {MUTE}·{RESET} " + sep.join(segs)


def render_identity_line(info, *, theme: Theme, dirty,
                         ahead=None, behind=None,
                         duration_text: str = "", lines_text: str = "",
                         version_text: str = "", update_text: str = "",
                         use_color: bool = True) -> str:
    """Render the 2nd line: `⤷ <project> ⎇ <branch>●↑2↓1 · ⏱ <dur> · +/-lines`.

    `dirty` is True / False / None — None means "unknown" (cache miss);
    in that case we omit the dot rather than asserting clean. `ahead`/`behind`
    are commits relative to upstream (None = unknown/no upstream, 0 = in sync);
    arrows render only for nonzero directions and only inside a git repo.
    `duration_text`/`lines_text` are the session stats, shown here (next to the
    project) rather than on the live-activity line. When the checkout is a
    linked git worktree (`info.is_worktree`), a bare ``[worktree]`` marker is
    appended after the branch — a boolean signal only; the branch already
    says which worktree it is, so the name isn't repeated.
    """
    ab = _ahead_behind_glyphs(ahead, behind) if info.in_git else ""
    stats = _stats_segment(duration_text, lines_text, theme=theme,
                           use_color=use_color)

    if not use_color:
        head = f"⤷ {info.project_name}"
        if not info.in_git:
            tail = " (no git)"
        else:
            branch = info.branch or "?"
            dot = "●" if dirty else ""
            tail = f" ⎇ {branch}{dot}"
            if ab:
                tail += f" {ab}"
        if info.is_worktree:
            tail += " [worktree]"
        ver = f" · v{version_text}" if version_text else ""
        if version_text and update_text:
            ver += f" ↑{update_text}"
        return head + tail + stats + ver

    MUTE = _fg(theme.mute)
    EDGE = _fg(theme.edge)
    INK = _fg(theme.pill_ink)
    HOT = _fg(theme.s_warn)

    head = f"{MUTE}⤷ {info.project_name}{RESET}"
    if not info.in_git:
        body = f" {MUTE}{ITAL}(no git){RESET}"
    else:
        branch = info.branch or "?"
        if info.detached:
            branch_styled = f"{MUTE}{ITAL}{branch}{RESET}"
        else:
            branch_styled = f"{INK}{branch}{RESET}"
        dot = f"{HOT}●{RESET}" if dirty else ""
        body = f" {EDGE}⎇{RESET} {branch_styled}{dot}"
        if ab:
            # Soft accent (not bare mute) — a gentle "unpushed/behind work" nudge.
            body += f" {_fg(theme.s_ok)}{ab}{RESET}"
    if info.is_worktree:
        body += f" {MUTE}[worktree]{RESET}"
    # Version: the faintest thing on the line — edge (darkest grey) + dim
    # attribute, so it's there if you look for it but never competes for attention.
    ver = ""
    if version_text:
        ver = f" {FAINT}{EDGE}· v{version_text}{RESET}"
        # Update available → a soft amber `↑<newver>` nudge (a bit more visible
        # than the version, so you notice there's something to update to).
        if update_text:
            ver += f"{_fg(theme.s_warn)} ↑{update_text}{RESET}"
    return head + body + stats + ver


def render_activity_line(activity, *, theme: Theme, use_color: bool = True,
                         show_todos: bool = False, show_tools: bool = False,
                         show_tool_rollup: bool = False,
                         show_mcp_stats: bool = True,
                         show_skill_stats: bool = True,
                         show_agent_stats: bool = True,
                         show_error_count: bool = True,
                         show_web_count: bool = True,
                         show_files_touched: bool = True) -> str:
    """Render the activity line.

    Segments (all independently toggled):
      todo progress · active tool · tool rollup · MCP stats · skill stats
      · web calls · files touched · errors · subagent count
    """
    MUTE = _fg(theme.mute)
    INK  = _fg(theme.ink)
    OK   = _fg(theme.s_ok)
    WARN = _fg(theme.s_warn)
    HOT  = _fg(theme.s_hot)

    # Two zones so the live action ("what's happening now") doesn't blur into
    # the cumulative session tallies. live_segs: in-progress todo, active tool,
    # recent-tool rollup, running-agent count. stat_segs: web/files/mcp/skills/
    # errors. Joined within a zone by ` · `, between zones by a distinct ` ╎ `.
    live_segs = []
    stat_segs = []

    if show_todos and activity is not None and activity.todos:
        done, total = activity.todos_done, activity.todos_total
        ip = activity.in_progress_todo
        if ip:
            task = ip if len(ip) <= 28 else ip[:27] + "…"
            live_segs.append(f"{OK}▸{RESET} {INK}{task}{RESET} {MUTE}({done}/{total}){RESET}")
        else:
            live_segs.append(f"{OK}▸{RESET} {MUTE}todos {done}/{total}{RESET}")

    if show_tools and activity is not None and activity.active_tool:
        name, target = activity.active_tool
        tail = f" {MUTE}{target}{RESET}" if target else ""
        live_segs.append(f"{WARN}◐{RESET} {INK}{name}{RESET}{tail}")

    if show_tool_rollup and activity is not None and activity.recent_completed:
        from .activity import _RECENT_TOOL_PHASE1_S, _RECENT_TOOL_PHASE2_S
        parts = []
        for name, target, age in activity.recent_completed[:6]:
            if use_color:
                if age < _RECENT_TOOL_PHASE1_S:
                    col = _fg(theme.ink)
                    tgt_col = _fg(theme.mute)
                else:
                    col = _fg(theme.mute)
                    tgt_col = _fg(theme.edge)
            else:
                col = tgt_col = ""
            rst = RESET if use_color else ""
            if target:
                tgt = target if len(target) <= 16 else target[:15] + "…"
                parts.append(f"{col}{name}{rst}{tgt_col}·{tgt}{rst}")
            else:
                parts.append(f"{col}{name}{rst}")
        live_segs.append("  ".join(parts))

    if show_agent_stats and activity is not None and len(activity.agents) > 0:
        running = len(activity.agents)
        live_segs.append(f"{WARN}↳{running}{RESET}")

    if show_web_count and activity is not None and activity.web_count > 0:
        stat_segs.append(f"{MUTE}web{RESET}{INK}×{activity.web_count}{RESET}")

    if show_files_touched and activity is not None and activity.files_touched:
        files = activity.files_touched[:3]
        tail = f"{MUTE}+{len(activity.files_touched)-3}{RESET}" if len(activity.files_touched) > 3 else ""
        parts = f"{MUTE},{RESET}".join(f"{INK}{f}{RESET}" for f in files)
        stat_segs.append(f"{MUTE}[{RESET}{parts}{tail}{MUTE}]{RESET}")

    if show_mcp_stats and activity is not None and activity.mcp_counts:
        mcp_parts = " ".join(
            f"{INK}{srv}{RESET}{MUTE}×{c}{RESET}"
            for srv, c in sorted(activity.mcp_counts.items(), key=lambda x: -x[1])[:4]
        )
        stat_segs.append(f"{MUTE}mcp:{RESET}{mcp_parts}")

    if show_skill_stats and activity is not None and activity.skill_counts:
        skill_parts = " ".join(
            f"{INK}{sk}{RESET}{MUTE}×{c}{RESET}" if c > 1 else f"{INK}{sk}{RESET}"
            for sk, c in sorted(activity.skill_counts.items(), key=lambda x: -x[1])[:4]
        )
        stat_segs.append(f"{MUTE}skills:{RESET}{skill_parts}")

    if show_error_count and activity is not None and activity.error_count > 0:
        stat_segs.append(f"{HOT}!{activity.error_count}{RESET}")

    if not live_segs and not stat_segs:
        return ""
    dot = f" {MUTE}·{RESET} " if use_color else " · "
    live = dot.join(live_segs)
    stats = dot.join(stat_segs)
    if live and stats:
        div = f"  {MUTE}╎{RESET}  " if use_color else "  ╎  "
        line = f"{live}{div}{stats}"
    else:
        line = live or stats
    if not use_color:
        return _strip(line)
    return line


def _lerp_fg(a, b, f):
    """Interpolate between two RGB tuples and return an ANSI fg code."""
    r = int(a[0] + (b[0] - a[0]) * f)
    g = int(a[1] + (b[1] - a[1]) * f)
    b_ = int(a[2] + (b[2] - a[2]) * f)
    return _fg((r, g, b_))


def _fin_col(age, theme, errored=False):
    """Hold solid status color for _AGENT_HOLD_S, then single linear fade to mute."""
    from .activity import _AGENT_HOLD_S, _AGENT_FADE_S
    base = theme.s_hot if errored else theme.s_ok
    if age <= _AGENT_HOLD_S:
        return _fg(base)
    span = max(0.001, _AGENT_FADE_S - _AGENT_HOLD_S)
    f = min(1.0, (age - _AGENT_HOLD_S) / span)
    return _lerp_fg(base, theme.mute, f)


def _elapsed_color(elapsed: float, theme: Theme) -> str:
    """Running-agent duration color: green → yellow → red as time grows."""
    if elapsed < 60:
        return _fg(theme.s_ok)
    if elapsed < 180:
        f = (elapsed - 60) / 120.0
        return _lerp_fg(theme.s_ok, theme.s_warn, f)
    if elapsed < 300:
        f = (elapsed - 180) / 120.0
        return _lerp_fg(theme.s_warn, theme.s_hot, f)
    return _fg(theme.s_hot)


def render_agent_lines(agents, *, recently_finished=None, theme: Theme, use_color: bool = True) -> list:
    """Compact grouped agent line with smooth fading for completed agents.

    Running:  ◐ 3 Explore [1m10s, 1m08s, 1m07s] · 2 agent [45s]
    Finished: ● Explore [8s]  (green circle, fades to invisible over _AGENT_FADE_S; red ● on error)
    """
    from .activity import format_elapsed_short, _AGENT_FADE_S, _AGENT_HOLD_S
    from collections import defaultdict

    recently_finished = [a for a in (recently_finished or [])
                         if a.get("completed_age", 0) <= _AGENT_FADE_S]
    if not agents and not recently_finished:
        return []

    MUTE = _fg(theme.mute)
    INK  = _fg(theme.ink)

    segs = []

    # Running agents grouped by name — elapsed colored green→yellow→red, unresponsive in orange.
    if agents:
        groups: dict = defaultdict(list)
        for ag in agents:
            groups[ag.get("name", "agent")].append(ag)
        for name, ag_list in groups.items():
            n = len(ag_list)
            times_parts = []
            any_unresp = False
            max_elapsed = 0.0
            for ag in ag_list:
                e = ag.get("elapsed_seconds", 0)
                unresponsive = ag.get("unresponsive", False)
                any_unresp = any_unresp or unresponsive
                max_elapsed = max(max_elapsed, e)
                if use_color:
                    col = _fg(theme.s_warn) if unresponsive else _elapsed_color(e, theme)
                else:
                    col = ""
                rst = RESET if use_color else ""
                times_parts.append(f"{col}{format_elapsed_short(e)}{rst}")
            times = f"{MUTE},{RESET} ".join(times_parts) if use_color else ", ".join(times_parts)
            count = f"{INK}{n} {RESET}" if (n > 1 and use_color) else (f"{n} " if n > 1 else "")
            # Spinner mirrors the group's health: orange when any agent in the
            # group is unresponsive, else the green→yellow→red elapsed color of
            # the longest-running one. (Was a flat orange ◐ — which made every
            # healthy agent read as a warning and lost the per-agent green dot.)
            if use_color:
                spin_col = _fg(theme.s_warn) if any_unresp else _elapsed_color(max_elapsed, theme)
                spin = f"{spin_col}◐{RESET}"
            else:
                spin = "◐"
            name_s = f"{INK}{name}{RESET}" if use_color else name
            br_o = f"{MUTE}[{RESET}" if use_color else "["
            br_c = f"{MUTE}]{RESET}" if use_color else "]"
            segs.append(f"{spin} {count}{name_s} {br_o}{times}{br_c}")

    # Recently finished agents — fade 2-phase over _AGENT_FADE_S. Errors show ● in red.
    if recently_finished and use_color:
        fin_groups: dict = defaultdict(list)
        for ag in recently_finished:
            fin_groups[ag.get("name", "agent")].append(ag)
        for name, ag_list in fin_groups.items():
            times_parts = []
            for ag in ag_list:
                age = ag.get("completed_age", 0)
                err = ag.get("error", False)
                col = _fin_col(age, theme, err)
                times_parts.append(f"{col}{format_elapsed_short(age)}{RESET}")
            times = f"{MUTE},{RESET} ".join(times_parts)
            first = ag_list[0]
            grp_err = any(a.get("error") for a in ag_list)
            col = _fin_col(first.get("completed_age", 0), theme, grp_err)
            icon = f"{col}●{RESET}"
            segs.append(f"{icon} {col}{name}{RESET} {MUTE}[{RESET}{times}{MUTE}]{RESET}")

    line = f" {MUTE}·{RESET} ".join(segs) if use_color else " · ".join(segs)
    if not use_color:
        return [_strip(line)]
    return [line]


def _fmt_tok(n) -> str:
    """Compact token count: 1234 → 1.2k, 980 → 980."""
    n = int(n or 0)
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def render_agent_progress_lines(agents, *, recently_finished=None, agent_total=0,
                                theme: Theme, use_color: bool = True) -> list:
    """Live per-subagent progress, read from the subagent transcripts (each agent's
    `progress` dict, populated by activity.enrich_agent_progress):

        ⟳ 20 Explore · Bash · 3t · 4s  ·  ● Explore · Bash · 3t · 4s

    Running agents use ⟳ (health-colored). Finished agents use ● with biphasic
    fade (green/red hold → mute → edge). Groups identical types, caps at 3 groups.
    agent_total is the true count (may exceed len(agents) due to _MAX_AGENTS cap).
    """
    from .activity import format_elapsed_short, _PROGRESS_FADE_S, _AGENT_FADE_S, _AGENT_HOLD_S
    from collections import defaultdict
    all_agents = list(agents or [])
    # Filter recently_finished to the progress fade window (longer than the agent-line fade).
    finished = [a for a in (recently_finished or [])
                if a.get("completed_age", 0) <= _PROGRESS_FADE_S]
    if not all_agents and not finished:
        return []
    MUTE = _fg(theme.mute) if use_color else ""
    INK  = _fg(theme.ink) if use_color else ""
    rst  = RESET if use_color else ""

    segs = []

    def _agg_tool_counts(ag_list):
        """Aggregate tool_counts dicts across agents, return sorted list of (tool, count)."""
        agg: dict = {}
        for ag in ag_list:
            for t, c in ((ag.get("progress") or {}).get("tool_counts") or {}).items():
                agg[t] = agg.get(t, 0) + c
        return sorted(agg.items(), key=lambda x: -x[1])[:3]

    def _tool_breakdown(top_tools, c_ink, c_mute, c_rst):
        if not top_tools:
            return ""
        if c_ink:
            parts = ", ".join(
                f"{c_ink}{t}{c_rst}{c_mute}[{c_rst}{c_ink}{n}{c_rst}{c_mute}]{c_rst}"
                for t, n in top_tools
            )
            return f"{c_mute}({c_rst}{parts}{c_mute}){c_rst}"
        return "(" + ", ".join(f"{t}[{n}]" for t, n in top_tools) + ")"

    def _stats_block(calls, out_tok, in_tok, elapsed, col, c_ink, c_mute, c_rst, use_col):
        parts = []
        if calls:
            parts.append(
                f"{c_ink}{calls}{c_rst}{c_mute} tools{c_rst}" if use_col else f"{calls} tools"
            )
        tok_parts = []
        if out_tok:
            tok_parts.append(f"{c_ink}{_fmt_tok(out_tok)}{c_rst}↑" if use_col else f"{_fmt_tok(out_tok)}^")
        if in_tok:
            tok_parts.append(f"{c_ink}{_fmt_tok(in_tok)}{c_rst}↓" if use_col else f"{_fmt_tok(in_tok)}v")
        if tok_parts:
            parts.append(" ".join(tok_parts))
        parts.append(f"{col}{format_elapsed_short(elapsed)}{c_rst}" if use_col else format_elapsed_short(elapsed))
        sep = f" {c_mute}|{c_rst} " if use_col else " | "
        inner = sep.join(parts)
        if use_col:
            return f"{c_mute}[{c_rst}{inner}{c_mute}]{c_rst}"
        return f"[{inner}]"

    # Running agents — grouped by type, capped at 3 groups.
    if all_agents:
        # Precompute finished counts per name for the running/total display.
        fin_count_by_name: dict = {}
        for ag in finished:
            n = ag.get("name", "agent")
            fin_count_by_name[n] = fin_count_by_name.get(n, 0) + 1

        groups: dict = defaultdict(list)
        for ag in all_agents:
            groups[ag.get("name", "agent")].append(ag)
        for gidx, (name, ag_list) in enumerate(list(groups.items())[:3]):
            n_run = len(ag_list)
            if len(groups) == 1 and agent_total > n_run:
                n_run = agent_total
            n_fin = fin_count_by_name.get(name, 0)
            n_total = n_run + n_fin

            rep = max(ag_list, key=lambda a: a.get("elapsed_seconds", 0))
            e = rep.get("elapsed_seconds", 0)
            prog = rep.get("progress") or {}
            idle = prog.get("idle")
            stalled = rep.get("unresponsive") or (idle is not None and idle > 30)
            col = (_fg(theme.s_warn) if stalled else _elapsed_color(e, theme)) if use_color else ""
            icon = f"{col}⟳{rst}" if use_color else "⟳"

            count_str = f"{n_run}/{n_total}" if n_total > 1 and n_fin > 0 else (str(n_run) if n_run > 1 else "")
            count_part = (f"{col}{count_str} {rst}" if use_color else f"{count_str} ") if count_str else ""

            top_tools = _agg_tool_counts(ag_list)
            calls = sum((a.get("progress") or {}).get("calls", 0) for a in ag_list)
            out_tok = sum((a.get("progress") or {}).get("out_tok", 0) for a in ag_list)
            in_tok = sum((a.get("progress") or {}).get("in_tok", 0) for a in ag_list)

            name_part = f"{INK}{name}{rst}" if use_color else name
            breakdown = _tool_breakdown(top_tools, INK if use_color else "", MUTE if use_color else "", rst)
            stats = _stats_block(calls, out_tok, in_tok, e, col, INK if use_color else "", MUTE if use_color else "", rst, use_color)
            segs.append(f"{icon} {count_part}{name_part}{breakdown}{stats}")

        extra = len(groups) - 3
        if extra > 0:
            segs.append(f"{MUTE}+{extra}{rst}" if use_color else f"+{extra}")

    # Recently finished — group by type, faded. Cap at 2 groups.
    if finished:
        fin_groups: dict = defaultdict(list)
        for ag in finished:
            fin_groups[ag.get("name", "agent")].append(ag)
        for name, ag_list in list(fin_groups.items())[:2]:
            rep = min(ag_list, key=lambda a: a.get("completed_age", 0))
            age = rep.get("completed_age", 0)
            err = any(a.get("error") for a in ag_list)
            col = (_fin_col(age, theme, err) if age <= _AGENT_FADE_S else _fg(theme.mute)) if use_color else ""
            n = len(ag_list)
            count_str = str(n) if n > 1 else ""
            count_part = (f"{col}{count_str} {rst}" if use_color else f"{count_str} ") if count_str else ""

            top_tools = _agg_tool_counts(ag_list)
            calls = sum((a.get("progress") or {}).get("calls", 0) for a in ag_list)
            out_tok = sum((a.get("progress") or {}).get("out_tok", 0) for a in ag_list)
            in_tok = sum((a.get("progress") or {}).get("in_tok", 0) for a in ag_list)
            elapsed = rep.get("elapsed_seconds", 0)

            name_part = f"{col}{name}{rst}" if use_color else name
            breakdown = _tool_breakdown(top_tools, col if use_color else "", MUTE if use_color else "", rst)
            stats = _stats_block(calls, out_tok, in_tok, elapsed, col, col if use_color else "", MUTE if use_color else "", rst, use_color)
            segs.append(f"{'●' if not use_color else col + '●' + rst} {count_part}{name_part}{breakdown}{stats}")

    if not segs:
        return []
    line = f"  {MUTE}·{rst}  ".join(segs)
    if not use_color:
        return [_strip(line)]
    return [line]


# Per-effort gradient palettes — a MONOTONIC grey→blue→purple ladder matching
# Claude Code's own "Faster → Smarter" effort slider (low … max, then ultracode
# = xhigh+workflows as the distinct vivid-purple top). Each tier is visibly more
# saturated/purple than the one below, so the level reads as an ordered ladder
# (not the old rainbow, where coral `max` looked hotter than `ultracode`).
_EFFORT_GRADIENTS = {
    # DESATURATED cool→purple ladder — each tier sweeps toward the next hue so it
    # reads as a gradient, but the colours are dusty/low-saturation so the line
    # doesn't shout next to the rest of the (restrained) bar. ultracode is a touch
    # brighter as the top tier.
    "low":       [(120, 172, 168), (124, 158, 196)],             # dusty teal → blue
    "auto":      [(120, 172, 168), (124, 158, 196)],             # neutral, like low
    "medium":    [(122, 156, 198), (132, 146, 202)],             # dusty azure
    "high":      [(130, 144, 204), (156, 140, 202)],             # dusty blue → indigo
    "xhigh":     [(152, 138, 204), (176, 134, 200)],             # dusty indigo → violet
    "max":       [(176, 134, 200), (196, 138, 196)],             # dusty violet → mauve
    "ultracode": [(200, 138, 202), (220, 160, 208)],             # dusty magenta → pink (top)
}
# Fallback for unknown/future levels — the showcase vivid purple.
_MODE_GRADIENT_STOPS = _EFFORT_GRADIENTS["ultracode"]


def _effort_gradient_stops(level):
    return _EFFORT_GRADIENTS.get(str(level).strip().lower(), _MODE_GRADIENT_STOPS)


def _lerp_rgb(a, b, f):
    return tuple(int(round(a[i] + (b[i] - a[i]) * f)) for i in range(3))


def _grad_sample(stops, f):
    """Sample a NON-cyclic gradient at f∈[0,1] across `stops` (clamped ends)."""
    if f <= 0:
        return stops[0]
    if f >= 1:
        return stops[-1]
    x = f * (len(stops) - 1)
    i = int(x)
    return _lerp_rgb(stops[i], stops[i + 1], x - i)


def _gradient_text(text: str, stops=None) -> str:
    """A single STATIC gradient (palette `stops`) swept once across `text`, left
    to right. Not animated: the statusLine refreshes at ≤1 Hz (and event-driven
    in some builds), so any motion can only step ~1/s and reads as a flicker —
    a clean stable sweep is the right call. The per-effort palette tells the tier."""
    stops = stops or _MODE_GRADIENT_STOPS
    n = len(text)
    out = [
        _fg(_grad_sample(stops, i / max(1, n - 1))) + ch
        for i, ch in enumerate(text)
    ]
    return "".join(out) + RESET


_EFFORT_LEVELS = ["low", "auto", "medium", "high", "xhigh", "max", "ultracode"]
_EFFORT_BAR_FILLED = "█"
_EFFORT_BAR_EMPTY  = "░"

def _effort_bar(level: str) -> str:
    """6-cell visual bar showing intensity: low=1, auto=1, medium=2, high=3, xhigh=4, max=5, ultra=6."""
    lv = str(level).strip().lower()
    rank = {"low": 1, "auto": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5, "ultracode": 6}.get(lv, 2)
    return _EFFORT_BAR_FILLED * rank + _EFFORT_BAR_EMPTY * (6 - rank)


def _effort_label(level: str) -> str:
    """Human-readable label for effort tier."""
    lv = str(level).strip().lower()
    return {
        "low": "economy",
        "auto": "auto",
        "medium": "balanced",
        "high": "performance",
        "xhigh": "max+",
        "max": "max",
        "ultracode": "ultra",
    }.get(lv, level)


def _effort_color(level, theme):
    lv = str(level).strip().lower()
    if lv in ("xhigh", "max", "ultracode"):
        return _fg(theme.s_warn)
    if lv in ("low", "auto", ""):
        return _fg(theme.mute)
    return _fg(theme.ink)


def render_mode_line(*, effort: str = "", thinking=None, fast=None,
                     style: str = "", theme: Theme, use_color: bool = True,
                     gradient: bool = True) -> str:
    """Session-mode readout: ⚡low █░░░░░ · 🧠 · ⚡fast

    Effort shows name + visual bar. Thinking and fast always shown —
    icon lit when on, grayed when off.
    """
    segs_plain = []
    if effort:
        segs_plain.append(f"⚡{effort} {_effort_bar(effort)}")
    if thinking is not None:
        segs_plain.append("🧠" if thinking else "🧠✗")
    if fast:
        segs_plain.append("⚡fast")
    if style and style not in ("default", ""):
        segs_plain.append(f"📝{style}")
    if not segs_plain:
        return ""

    plain = " · ".join(segs_plain)
    if not use_color:
        return plain
    if gradient:
        return _gradient_text(plain, _effort_gradient_stops(effort))

    MUTE = _fg(theme.mute)
    parts = []
    if effort:
        parts.append(f"{_effort_color(effort, theme)}⚡{effort} {_effort_bar(effort)}{RESET}")
    if thinking is not None:
        c = _fg(theme.ink) if thinking else MUTE
        parts.append(f"{c}🧠{'  ' if thinking else '✗'}{RESET}")
    if fast:
        parts.append(f"{_fg(theme.s_warn)}⚡fast{RESET}")
    if style and style not in ("default", ""):
        parts.append(f"{MUTE}📝{RESET}{_fg(theme.ink)}{style}{RESET}")
    return f"{MUTE} · {RESET}".join(parts)


RENDERERS = {
    "classic":  render_classic,
    "capsule":  render_capsule,
    "hairline": render_hairline,
}


def is_known_style(style: str) -> bool:
    return style in RENDERERS


def render(style: str, **kwargs) -> str:
    """Render with the named style. Unknown style names fall back to classic.

    Unknown kwargs are absorbed by each renderer's **_ignored, so callers can
    freely pass style-specific args (density, countdown_emoji, ...) to whichever
    renderer is selected.

    The optional `show_project_branch`/`identity`/`identity_dirty` kwargs
    cause a second `⤷ <project> ⎇ <branch>` line to be appended after the
    style renderer returns. The optional `activity`/`activity_opts` kwargs
    append an 'activity' line (todos / active tool / session stats) plus one
    bottom line per running subagent. All extra lines are style-agnostic.
    """
    show_pb = kwargs.pop("show_project_branch", False)
    info = kwargs.pop("identity", None)
    dirty = kwargs.pop("identity_dirty", None)
    ahead = kwargs.pop("identity_ahead", None)
    behind = kwargs.pop("identity_behind", None)
    duration_text = kwargs.pop("identity_duration", "")
    lines_text = kwargs.pop("identity_lines", "")
    show_version = kwargs.pop("identity_show_version", False)
    show_mode = kwargs.pop("mode_show", False)
    mode_effort = kwargs.pop("mode_effort", "")
    mode_thinking = kwargs.pop("mode_thinking", None)
    mode_fast = kwargs.pop("mode_fast", None)
    mode_style = kwargs.pop("mode_style", "")
    mode_gradient = kwargs.pop("mode_gradient", True)
    kwargs.pop("mode_phase", None)   # accepted for back-compat; gradient is static
    activity = kwargs.pop("activity", None)
    activity_opts = kwargs.pop("activity_opts", None)
    theme = kwargs.get("theme") or get_theme("graphite")
    use_color = kwargs.get("use_color", True)

    fn = RENDERERS.get(style, render_classic)
    out = fn(**kwargs)

    if show_pb and info is not None:
        version_text = _statusbar_version() if show_version else ""
        update_text = _update_hint() if show_version else ""
        identity_line = render_identity_line(
            info, theme=theme, dirty=dirty, ahead=ahead, behind=behind,
            duration_text=duration_text, lines_text=lines_text,
            version_text=version_text, update_text=update_text,
            use_color=use_color,
        )
        # Mode content inline after identity stats.
        if show_mode:
            mode_suffix = render_mode_line(
                effort=mode_effort, thinking=mode_thinking, fast=mode_fast,
                style=mode_style, theme=theme, use_color=use_color,
                gradient=mode_gradient)
            if mode_suffix:
                MUTE = _fg(theme.mute) if use_color else ""
                RST  = RESET if use_color else ""
                identity_line += f" {MUTE}·{RST} {mode_suffix}"
        # Activity stats appended to the same line after mode.
        agent_lines = []
        if activity_opts:
            opts = dict(activity_opts)
            show_agents = opts.pop("show_agents", False)
            show_progress = opts.pop("show_agent_progress", False)
            act_line = render_activity_line(
                activity, theme=theme, use_color=use_color, **opts)
            if act_line:
                MUTE = _fg(theme.mute) if use_color else ""
                RST  = RESET if use_color else ""
                identity_line += f" {MUTE}│{RST} {act_line}"
            if show_progress and activity is not None:
                try:
                    agent_lines = render_agent_progress_lines(
                        activity.agents,
                        recently_finished=activity.recently_finished_agents,
                        agent_total=activity.agent_total,
                        theme=theme, use_color=use_color)
                except Exception:
                    agent_lines = []
            elif show_agents and activity is not None:
                agent_lines = render_agent_lines(
                    activity.agents,
                    recently_finished=activity.recently_finished_agents,
                    theme=theme, use_color=use_color)
        out = out + "\n" + identity_line
        for agline in agent_lines:
            out = out + "\n" + agline
    elif activity_opts:
        # No identity line — fall back to standalone activity line.
        opts = dict(activity_opts)
        opts.pop("show_agents", False)
        opts.pop("show_agent_progress", False)
        act_line = render_activity_line(
            activity, theme=theme, use_color=use_color, **opts)
        if act_line:
            out = out + "\n" + act_line
    return out


def list_styles() -> list[str]:
    return list(RENDERERS.keys())

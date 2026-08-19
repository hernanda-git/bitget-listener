"""Telegram message presentation — the operator-facing look and feel.

The command router holds no trading logic; this module holds no trading logic
EITHER. It only turns already-computed numbers into clean Telegram HTML.

Design rules (Telegram is not a terminal):
- No ``key=value`` dumps. Labelled rows, grouped into sections by a blank line.
- Money always carries an explicit sign and a fixed precision, so a column of
  figures reads straight down.
- Emoji are STATUS, never decoration: one leading glyph per section or row.
- Never invent a number. ``None`` renders as an em-space dash, not ``0.00``
  (BUG B discipline: a missing liquidation price is not a zero one).
- All timestamps render in Asia/Jakarta (UTC+7), the operator's timezone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# The operator reads every timestamp in Jakarta time.
JAKARTA = timezone(timedelta(hours=7))

DASH = "—"          # em dash: "no value", visually distinct from a zero
BULLET = "·"


# --------------------------------------------------------------------- numbers
def money(value: float | None, places: int = 4, signed: bool = True,
          suffix: str = "") -> str:
    """A money figure with an explicit sign, or a dash when unknown."""
    if value is None:
        return DASH
    fmt = f"{{:+.{places}f}}" if signed else f"{{:.{places}f}}"
    out = fmt.format(float(value))
    return f"{out}{suffix}"


def price(value: float | None) -> str:
    """A price at its natural precision — no trailing-zero noise."""
    if value is None:
        return DASH
    v = float(value)
    if v == 0:
        return DASH
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def qty(value: float | None) -> str:
    if value is None:
        return DASH
    v = float(value)
    return f"{v:.8f}".rstrip("0").rstrip(".") or "0"


def pct(value: float | None, places: int = 1) -> str:
    return DASH if value is None else f"{float(value):.{places}f}%"


def signed_glyph(value: float | None) -> str:
    """Profit/loss/flat indicator."""
    if value is None:
        return "▪️"
    if value > 0:
        return "🟢"
    if value < 0:
        return "🔴"
    return "▪️"


def side_glyph(side: str) -> str:
    return "▲" if str(side).upper() in ("BUY", "LONG") else "▼"


def ok_glyph(good: bool | None) -> str:
    """Tri-state: good / bad / unknown."""
    if good is None:
        return "❔"
    return "✅" if good else "⚠️"


# ------------------------------------------------------------------ timestamps
def when(value) -> str:
    """Render any timestamp shape in Jakarta time. Empty string if unparseable."""
    dt = None
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Heuristic: >1e11 means milliseconds.
        secs = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            dt = datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return ""
    else:
        txt = str(value).strip()
        try:
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except ValueError:
            return txt          # unknown shape: show it verbatim rather than lie
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JAKARTA).strftime("%d %b %H:%M")


# ------------------------------------------------------------------- structure
def title(text: str, glyph: str = "") -> str:
    head = f"{glyph} {text}" if glyph else text
    return f"<b>{head}</b>"


def row(label: str, value: str, indent: int = 0) -> str:
    """A labelled row. Telegram collapses runs of spaces, so we do not pad."""
    pad = "  " * indent
    return f"{pad}{label}  <code>{value}</code>"


def plain_row(label: str, value: str, indent: int = 0) -> str:
    pad = "  " * indent
    return f"{pad}{label}  {value}"


def rule() -> str:
    return "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"


def join(*blocks: object) -> str:
    """Join sections with a blank line, dropping empties."""
    parts = [str(b).strip() for b in blocks if b not in (None, "", [])]
    return "\n\n".join(p for p in parts if p)


def lines(*items: object) -> str:
    return "\n".join(str(i) for i in items if i not in (None, ""))


def hint(text: str) -> str:
    return f"<i>{text}</i>"

"""`RuntimeField` — the declaration of one runtime-editable parameter.

Lives in its own module so `bot/strategies/` can declare its parameters without
importing `bot.config`, which now builds `RUNTIME_FIELDS` *from* the strategy
registry. Anything else would be a cycle.

A field carries everything anyone needs to know about the parameter:

    parsing + range check   → POST /config and the `bot_config` loader
    persistence flag        → whether it survives a restart
    label / hint / step     → how /settings renders it

That last group is why the presentation metadata lives here instead of in the
template: adding a parameter should be one edit, not one edit plus a forgotten
`<input>` in `settings.html`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeField:
    """A parameter that can be changed at runtime from /settings.

    Single source of truth for parsing and range-checking, shared by the POST
    /config handler (JSON values) and the persisted-override loader (strings
    read back from `bot_config`).
    """

    name: str
    kind: str                              # "float" | "int" | "bool" | "choice" | "hours"
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    persist: bool = True                   # survives a restart via bot_config

    # ── Presentation (consumed by /settings, never by the trader) ─────────────
    label: str = ""                        # Spanish, like every UI string
    hint: str = ""                         # one line under the input
    step: float | None = None              # <input step="…">
    # Display scale: the value is stored as `x` and shown as `x * scale`.
    # `ss_trend_min_strength` is a fraction in the config and a percentage on
    # screen; putting the factor here keeps the conversion in one place instead
    # of in two hand-written spots in settings.js.
    scale: float = 1.0
    choice_labels: tuple[str, ...] = ()    # parallel to `choices`, for <option>

    def coerce(self, value: object) -> tuple[bool, object]:
        """Parse and validate. Returns (ok, parsed_value)."""
        try:
            if self.kind == "bool":
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
                        return False, None
                    return True, lowered in ("1", "true", "yes", "on")
                return True, bool(value)

            if self.kind == "choice":
                parsed = str(value).strip().lower()
                return (parsed in self.choices), parsed

            if self.kind == "hours":
                # Inline hours-spec validator. The original `is_valid_hours_spec`
                # lived in `bot.regime` (now removed). The format is a
                # comma-separated list of "lo-hi" UTC hour ranges ("13-21,22-2");
                # every non-empty fragment must parse to a valid range.
                def _is_valid(spec: str) -> bool:
                    fragments = [c.strip() for c in (spec or "").split(",") if c.strip()]
                    if not fragments:
                        return True
                    parsed = 0
                    for frag in fragments:
                        if "-" not in frag:
                            return False
                        lo_s, _, hi_s = frag.partition("-")
                        try:
                            lo, hi = int(lo_s), int(hi_s)
                        except ValueError:
                            return False
                        if not (0 <= lo <= 24 and 0 <= hi <= 24) or lo == hi:
                            return False
                        parsed += 1
                    return parsed == len(fragments)

                parsed = str(value).strip()
                return _is_valid(parsed), parsed

            parsed = int(value) if self.kind == "int" else float(value)
        except (TypeError, ValueError):
            return False, None

        if self.minimum is not None and parsed < self.minimum:
            return False, None
        if self.maximum is not None and parsed > self.maximum:
            return False, None
        return True, parsed

    def serialize(self, value: object) -> str:
        if self.kind == "bool":
            return "true" if value else "false"
        return str(value)

    def to_json(self) -> dict:
        """What /settings needs to render this field without knowing its name."""
        return {
            "name": self.name,
            "kind": self.kind,
            "min": self.minimum,
            "max": self.maximum,
            "choices": list(self.choices),
            "choice_labels": list(self.choice_labels or self.choices),
            "label": self.label or self.name,
            "hint": self.hint,
            "step": self.step,
            "scale": self.scale,
        }

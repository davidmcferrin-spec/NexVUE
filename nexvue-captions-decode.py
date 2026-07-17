#!/usr/bin/env python3
"""
nexvue-captions-decode.py — CEA-608/CC1 decoder for the NexVUE caption side channel.

Reads raw CEA-608 byte pairs from a FIFO (fed by the encode pipeline's
ccextractor → ccconverter branch) and atomically writes the latest display
text to /run/nexvue/captions/<channel>.json for nexvue-captions.php.

Stdlib only. No listening port.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Basic North American CEA-608 character set (null / control slots = None).
_BASIC = [
    None, "É", "á", "é", "í", "ó", "ú", "ç", "÷", "Ñ", "ñ", "█", "½", "¿", None, "☑",
    "®", "°", "½", "¿", "™", "¢", "£", "♪", "à", "\xa0", "è", "â", "ê", "î", "ô", "û",
    "Á", "É", "Ó", "Ú", "Ü", "ü", "'", "¡", "*", "'", "—", "©", "℠", "•", "“", "”",
    "À", "Â", "Ç", "È", "Ê", "Ë", "ë", "Î", "Ï", "ï", "Ô", "Ù", "ù", "Û", "«", "»",
    "Ã", "ã", "Í", "Ì", "ì", "Ò", "ò", "Õ", "õ", "{", "}", "\\", "^", "_", "|", "~",
    "Ä", "ä", "Ö", "ö", "ß", "¥", "¤", "|", "Å", "å", "Ø", "ø", "┌", "┐", "└", "┘",
]

# Standard charset for bytes 0x20-0x7f (with a few 608 substitutions).
def _char608(b: int) -> str | None:
    if b < 0x20:
        return None
    if b == 0x2A:
        return "á"
    if b == 0x5C:
        return "é"
    if b == 0x5E:
        return "í"
    if b == 0x5F:
        return "ó"
    if b == 0x60:
        return "ú"
    if b == 0x7B:
        return "ç"
    if b == 0x7C:
        return "÷"
    if b == 0x7D:
        return "Ñ"
    if b == 0x7E:
        return "ñ"
    if b == 0x7F:
        return "█"
    try:
        return chr(b)
    except ValueError:
        return None


class Cea608Cc1:
    """Minimal CC1 decoder: roll-up / paint-on / pop-on → visible lines."""

    ROWS = 15
    COLS = 32

    def __init__(self) -> None:
        self._displayed = [["" for _ in range(self.COLS)] for _ in range(self.ROWS)]
        self._written = [["" for _ in range(self.COLS)] for _ in range(self.ROWS)]
        self._row = 14  # 0-based; PAC row 15
        self._col = 0
        self._mode = "rollup"  # rollup | popon | paint
        self._rollup_rows = 2
        self._last_ctrl: tuple[int, int] | None = None
        self._text = ""

    def visible_text(self) -> str:
        lines = []
        for r in range(self.ROWS):
            line = "".join(self._displayed[r]).rstrip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    def feed_pair(self, b1: int, b2: int) -> str | None:
        """Process one CC pair. Returns new visible text if it changed, else None."""
        b1 &= 0x7F
        b2 &= 0x7F
        if b1 == 0 and b2 == 0:
            return None

        changed = False
        if self._is_control(b1, b2):
            if self._last_ctrl == (b1, b2):
                # Duplicate control codes are ignored (608 redundancy).
                return None
            self._last_ctrl = (b1, b2)
            changed = self._control(b1, b2)
        else:
            self._last_ctrl = None
            if 0x20 <= b1 <= 0x7F:
                changed = self._print_char(b1) or changed
            if 0x20 <= b2 <= 0x7F:
                changed = self._print_char(b2) or changed

        if not changed:
            return None
        text = self.visible_text()
        if text == self._text:
            return None
        self._text = text
        return text

    @staticmethod
    def _is_control(b1: int, b2: int) -> bool:
        return 0x10 <= b1 <= 0x1F and 0x20 <= b2 <= 0x7F

    def _print_char(self, b: int) -> bool:
        ch = _char608(b)
        if ch is None:
            return False
        buf = self._active_buffer()
        if self._col >= self.COLS:
            return False
        buf[self._row][self._col] = ch
        self._col += 1
        if self._mode in ("rollup", "paint"):
            self._displayed[self._row][self._col - 1] = ch
            return True
        return False

    def _active_buffer(self) -> list[list[str]]:
        return self._written if self._mode == "popon" else self._displayed

    def _control(self, b1: int, b2: int) -> bool:
        # PAC: b2 in 0x40-0x7F. Mid-row / misc use lower b2 ranges.
        if b2 >= 0x40:
            row = self._pac_row(b1, b2)
            if row is None:
                return False
            # CC2 PACs use the high channel nibble (0x18-0x1F family).
            if b1 >= 0x18:
                return False
            self._row = row
            self._col = 0
            # Indent PACs: 0x40|0x50|… carry column steps of 4.
            indent_n = (b2 >> 1) & 0x07
            if (b2 & 0x10) == 0x10 and (b2 & 0x0E) != 0:
                self._col = min(indent_n * 4, self.COLS - 1)
            return False

        # Mid-row codes (CC1): styling only for v1.
        if b1 == 0x11 and 0x20 <= b2 <= 0x2F:
            return False
        # Extended char sets (CC1).
        if b1 in (0x12, 0x13) and 0x20 <= b2 <= 0x3F:
            idx = (b2 - 0x20) + (0 if b1 == 0x12 else 32)
            if 0 <= idx < len(_BASIC) and _BASIC[idx]:
                return self._put_raw(_BASIC[idx])
            return False

        # CC1 miscellaneous control codes.
        if b1 != 0x14:
            return False
        code = b2
        if code == 0x20:  # RCL — resume caption loading (pop-on)
            self._mode = "popon"
            return False
        if code == 0x21:  # BS
            if self._col > 0:
                self._col -= 1
                buf = self._active_buffer()
                buf[self._row][self._col] = ""
                if self._mode != "popon":
                    self._displayed[self._row][self._col] = ""
                    return True
            return False
        if code == 0x24:  # DER — delete to end of row
            buf = self._active_buffer()
            for c in range(self._col, self.COLS):
                buf[self._row][c] = ""
            if self._mode != "popon":
                for c in range(self._col, self.COLS):
                    self._displayed[self._row][c] = ""
                return True
            return False
        if code == 0x25:  # RU2
            self._mode = "rollup"
            self._rollup_rows = 2
            return False
        if code == 0x26:  # RU3
            self._mode = "rollup"
            self._rollup_rows = 3
            return False
        if code == 0x27:  # RU4
            self._mode = "rollup"
            self._rollup_rows = 4
            return False
        if code == 0x29:  # RDC — paint-on
            self._mode = "paint"
            return False
        if code == 0x2C:  # EDM — erase displayed memory
            self._clear(self._displayed)
            return True
        if code == 0x2D:  # CR — carriage return (rollup)
            return self._rollup_cr()
        if code == 0x2E:  # ENM — erase non-displayed
            self._clear(self._written)
            return False
        if code == 0x2F:  # EOC — end of caption (swap)
            self._displayed = [row[:] for row in self._written]
            self._mode = "popon"
            return True
        return False

    def _put_raw(self, ch: str) -> bool:
        buf = self._active_buffer()
        if self._col >= self.COLS:
            return False
        buf[self._row][self._col] = ch
        self._col += 1
        if self._mode in ("rollup", "paint"):
            self._displayed[self._row][self._col - 1] = ch
            return True
        return False

    def _rollup_cr(self) -> bool:
        # Scroll up within the rollup window ending at current row.
        base = max(0, self._row - self._rollup_rows + 1)
        for r in range(base, self._row):
            self._displayed[r] = self._displayed[r + 1][:]
        self._displayed[self._row] = ["" for _ in range(self.COLS)]
        self._col = 0
        return True

    @staticmethod
    def _clear(buf: list[list[str]]) -> None:
        for r in range(len(buf)):
            for c in range(len(buf[r])):
                buf[r][c] = ""

    @staticmethod
    def _pac_row(b1: int, b2: int) -> int | None:
        # CEA-608 PAC → row (1-based in the table, return 0-based).
        # Channel-2 PACs are 0x18-0x1F; callers filter those out for CC1.
        pac_rows = {
            0x11: (1, 2),
            0x12: (3, 4),
            0x15: (5, 6),
            0x16: (7, 8),
            0x17: (9, 10),
            0x10: (11,),
            0x13: (12,),
            0x14: (13, 14),
        }
        rows = pac_rows.get(b1)
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0] - 1
        # Second row of the pair when b2 selects the odd-row PAC group.
        return (rows[1] if (b2 & 0x20) else rows[0]) - 1


def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def decode_stream(fifo: Path, state_path: Path, channel: str) -> None:
    dec = Cea608Cc1()
    seq = 0
    # Open blocks until the encode pipeline opens the write end.
    with open(fifo, "rb", buffering=0) as fh:
        buf = b""
        while True:
            chunk = fh.read(256)
            if not chunk:
                # Writer closed (encoder restart) — exit; systemd/encode respawns us.
                break
            buf += chunk
            while len(buf) >= 2:
                b1, b2 = buf[0], buf[1]
                buf = buf[2:]
                text = dec.feed_pair(b1, b2)
                if text is None:
                    continue
                seq += 1
                atomic_write_json(
                    state_path,
                    {
                        "channel": channel,
                        "text": text,
                        "clear": text == "",
                        "ts": time.time(),
                        "seq": seq,
                        "service": "CC1",
                    },
                )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="NexVUE CEA-608/CC1 caption decoder")
    p.add_argument("--channel", required=True, help="MediaMTX path, e.g. ch0")
    p.add_argument("--fifo", required=True, help="Path to raw CEA-608 FIFO")
    p.add_argument(
        "--state-dir",
        default="/run/nexvue/captions",
        help="Directory for <channel>.json state files",
    )
    args = p.parse_args(argv)

    channel = args.channel.strip()
    if not channel or not all(c.isalnum() or c in "-_" for c in channel):
        print("invalid --channel", file=sys.stderr)
        return 64

    fifo = Path(args.fifo)
    state_dir = Path(args.state_dir)
    state_path = state_dir / f"{channel}.json"

    # Initial empty state so PHP/SSE clients see a file immediately.
    atomic_write_json(
        state_path,
        {
            "channel": channel,
            "text": "",
            "clear": True,
            "ts": time.time(),
            "seq": 0,
            "service": "CC1",
        },
    )

    try:
        decode_stream(fifo, state_path, channel)
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        print(f"[nexvue-captions-decode] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

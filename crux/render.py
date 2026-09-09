"""
Render the live ledger into README.md and assets/ledger.svg.

The README between the CRUX:BEGIN and CRUX:END markers is generated; every
other word in it is hand-written and is never touched.
"""

import html
import json
import os

from .chain import ChainState, emitted_supply
from .consensus import (
    COINBASE_MATURITY,
    HALVING_INTERVAL,
    RETARGET_INTERVAL,
    TARGET_SPACING,
    block_subsidy,
    difficulty,
    format_amount,
)

REGISTRY = os.path.join("chain", "registry.json")

BEGIN = "<!-- CRUX:BEGIN -->"
END = "<!-- CRUX:END -->"
RECENT = 10


def _when(ts: int) -> str:
    """Absolute UTC time — README is static, so relative 'ago' would freeze."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_registry():
    """Handle -> address bindings. Display only; not consensus."""
    if not os.path.exists(REGISTRY):
        return {}
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _who(address: str, by_address) -> str:
    """Render an address as a GitHub handle when one has claimed it."""
    handle = by_address.get(address)
    if handle:
        return f"[@{handle}](https://github.com/{handle})"
    return f"`{address[:16]}…`"


def render_readme_section(state: ChainState) -> str:
    registry = load_registry()
    by_address = {v["address"]: h for h, v in registry.items()}
    tip = state.tip
    height = state.height
    emitted = emitted_supply(height)
    next_h = height + 1

    halving_in = HALVING_INTERVAL - (next_h % HALVING_INTERVAL)
    retarget_in = RETARGET_INTERVAL - (next_h % RETARGET_INTERVAL)

    out = []
    out.append(BEGIN)
    out.append("")
    out.append(
        f'<picture>'
        f'<source media="(prefers-color-scheme: dark)" '
        f'srcset="assets/ledger-dark.svg?v={height}">'
        f'<img src="assets/ledger-light.svg?v={height}" width="100%" '
        f'alt="CRUX ledger, height {height}">'
        f'</picture>'
    )
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| **height** | `{height}` |")
    out.append(f"| **tip** | `{state.tip_hash}` |")
    out.append(f"| **difficulty** | `{difficulty(tip.bits):,.1f}`  (bits `{tip.bits:#010x}`) |")
    out.append(f"| **chainwork** | `{state.chainwork:,}` expected hashes |")
    out.append(f"| **supply** | `{format_amount(emitted)} CRUX` in "
               f"`{len(state.utxos.utxos)}` unspent outputs |")
    out.append(f"| **next reward** | `{format_amount(block_subsidy(next_h))} CRUX` |")
    out.append(f"| **next retarget** | in `{retarget_in}` block(s) |")
    out.append(f"| **next halving** | in `{halving_in}` block(s) |")
    out.append(f"| **transactions** | `{state.tx_count}` |")
    out.append("")

    out.append("### Recent blocks")
    out.append("")
    out.append("| # | hash | miner | message | txs | reward | mined |")
    out.append("|--:|---|---|---|--:|--:|---|")
    for b in reversed(state.blocks[-RECENT:]):
        msg = html.escape(b.txs[0].coinbase or "")
        msg = f"`{msg}`" if msg else "&nbsp;"
        reward = format_amount(sum(o.value for o in b.txs[0].outputs))
        out.append(
            f"| `{b.height}` | `{b.block_hash()[:20]}…` | "
            f"[@{b.miner}](https://github.com/{b.miner}) | {msg} | "
            f"`{len(b.txs)}` | `{reward}` | {_when(b.timestamp)} |"
        )
    out.append("")

    if state.miners:
        out.append("### Miners")
        out.append("")
        out.append("| miner | blocks | share |")
        out.append("|---|--:|--:|")
        total = sum(state.miners.values())
        for handle, count in sorted(state.miners.items(), key=lambda p: (-p[1], p[0]))[:12]:
            out.append(
                f"| [@{handle}](https://github.com/{handle}) | `{count}` | "
                f"`{100 * count / total:.1f}%` |"
            )
        out.append("")

    balances = state.utxos.balances()
    if balances:
        out.append("### Balances")
        out.append("")
        out.append("_Find your own name here once you have run "
                   "`python3 wallet.py identity`._")
        out.append("")
        out.append("| holder | address | balance |")
        out.append("|---|---|--:|")
        for addr, val in sorted(balances.items(), key=lambda p: -p[1]):
            handle = by_address.get(addr)
            who = f"[@{handle}](https://github.com/{handle})" if handle else "_unclaimed_"
            out.append(f"| {who} | `{addr}` | `{format_amount(val)} CRUX` |")
        out.append("")

    transfers = []
    for b in reversed(state.blocks):
        for t in b.txs[1:]:
            transfers.append((b, t))
        if len(transfers) >= 8:
            break
    if transfers:
        out.append("### Recent transfers")
        out.append("")
        out.append("| block | from | to | amount | note |")
        out.append("|--:|---|---|--:|---|")
        for b, t in transfers[:8]:
            src = "&nbsp;"
            first = t.inputs[0] if t.inputs else None
            if first:
                try:
                    import binascii

                    from .crypto import pubkey_to_address

                    src = _who(pubkey_to_address(bytes.fromhex(first.pubkey)), by_address)
                except (ValueError, binascii.Error):
                    src = "&nbsp;"
            dest = t.outputs[0]
            memo = html.escape(t.memo) if t.memo else "&nbsp;"
            out.append(
                f"| `{b.height}` | {src} | {_who(dest.address, by_address)} | "
                f"`{format_amount(dest.value)}` | {memo} |"
            )
        out.append("")

    out.append(f"<sub>Rendered from `chain/blocks.jsonl` at height {height}. "
               f"Verify it yourself: <code>python3 verify.py</code></sub>")
    out.append("")
    out.append(END)
    return "\n".join(out)


def update_readme(state: ChainState, path: str = "README.md") -> None:
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    if BEGIN not in content or END not in content:
        raise RuntimeError(f"{path} is missing the CRUX:BEGIN/CRUX:END markers")
    head = content.split(BEGIN)[0]
    tail = content.split(END, 1)[1]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head + render_readme_section(state) + tail)


# --------------------------------------------------------------------------
# SVG ledger tape
# --------------------------------------------------------------------------

PALETTES = {
    "light": dict(bg="#f3f1ee", card="#ffffff", edge="#d8d2c8", ink="#1a1612",
                  dim="#6e675c", accent="#9a4a18", teal="#1c4f5b", link="#c4bdb3",
                  slot="#cfc8be"),
    "dark": dict(bg="#0e0c0a", card="#161310", edge="#2e2820", ink="#ebe6de",
                 dim="#8a8276", accent="#e08a45", teal="#5ca3b0", link="#3a332b",
                 slot="#2c261f"),
}

SVG_HEAD = """<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" \
viewBox="0 0 {w} {h}" font-family="ui-monospace,SFMono-Regular,Menlo,monospace">
<rect fill="{bg}" width="{w}" height="{h}" rx="6"/>
"""


def _svg(state: ChainState, pal: dict, slots: int) -> str:
    blocks = state.blocks[-slots:]
    bw, gap, pad = 148, 26, 18
    w = pad * 2 + slots * bw + (slots - 1) * gap
    h = 138
    top, cardh = 44, 76
    out = [SVG_HEAD.format(w=w, h=h, bg=pal["bg"])]

    out.append(
        f'<text x="{pad}" y="26" fill="{pal["ink"]}" font-size="15" font-weight="700">CRUX</text>'
        f'<text x="{pad + 58}" y="26" fill="{pal["dim"]}" font-size="12.5">'
        f'height {state.height} &#183; difficulty {difficulty(state.tip.bits):,.0f} &#183; '
        f'1 knapsack + hash target</text>'
    )

    for slot in range(slots):
        x = pad + slot * (bw + gap)
        idx = slot - (slots - len(blocks))
        if slot:
            out.append(
                f'<line x1="{x - gap}" y1="{top + cardh / 2}" x2="{x}" y2="{top + cardh / 2}" '
                f'stroke="{pal["link"]}" stroke-width="1.5" stroke-dasharray="3 3"/>'
            )
        if idx < 0:
            out.append(
                f'<rect x="{x}" y="{top}" width="{bw}" height="{cardh}" rx="4" fill="none" '
                f'stroke="{pal["slot"]}" stroke-width="1" stroke-dasharray="4 4"/>'
            )
            continue

        b = blocks[idx]
        out.append(f'<rect x="{x}" y="{top}" width="{bw}" height="{cardh}" rx="4" '
                   f'fill="{pal["card"]}" stroke="{pal["edge"]}" stroke-width="1"/>')
        out.append(f'<text x="{x + 11}" y="{top + 21}" fill="{pal["accent"]}" font-size="13" '
                   f'font-weight="700">#{b.height}</text>')
        out.append(f'<text x="{x + 11}" y="{top + 37}" fill="{pal["ink"]}" font-size="10">'
                   f'{b.block_hash()[:16]}&#8230;</text>')
        out.append(f'<text x="{x + 11}" y="{top + 52}" fill="{pal["teal"]}" font-size="10.5">'
                   f'@{html.escape(b.miner[:16])}</text>')
        raw = b.txs[0].coinbase or ""
        msg = html.escape(raw[:21]) + ("&#8230;" if len(raw) > 21 else "")
        out.append(f'<text x="{x + 11}" y="{top + 66}" fill="{pal["dim"]}" font-size="9">{msg}</text>')

    out.append("</svg>\n")
    return "".join(out)


def render_svg(state: ChainState, directory: str = "assets", slots: int = 6) -> None:
    """
    Write one tape per theme. GitHub selects between them with <picture>,
    which follows the site theme rather than the reader's operating system.
    """
    os.makedirs(directory, exist_ok=True)
    for name, pal in PALETTES.items():
        with open(os.path.join(directory, f"ledger-{name}.svg"), "w", encoding="utf-8") as fh:
            fh.write(_svg(state, pal, slots))

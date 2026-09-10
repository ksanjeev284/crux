# Launching CRUX

Five steps, about three minutes plus genesis mining. Do them in order —
genesis is immutable, so everything before the first push is the only
chance to change the chain's identity.

## 1. Create your key

```bash
python3 wallet.py new
```

Writes `crux-wallet.json` (gitignored — it holds a private key in plain text).
Note the address it prints.

## 2. Mine genesis

```bash
python3 make_genesis.py \
  --miner YOUR_GITHUB_HANDLE \
  --message "your genesis message, max 256 bytes" \
  --address crux1...your-address...
```

Takes a few minutes on the reference solver (n=44, work 64). This writes
`chain/blocks.jsonl`, renders the README and generates `assets/ledger.svg`.

**The message is permanent.** Bitcoin's genesis carries a newspaper headline
from the day it was mined, which is what proves the chain wasn't premined.
Yours should be something you'd want quoted back at you.

If this repository already has a genesis you did not mine, remine with
`--force` **before the first push**. After the chain is public, genesis is
history.

Check it:

```bash
python3 verify.py
python3 tests/test_chain.py
python3 tests/test_pow.py
```

## 3. Create the repo

```bash
gh repo create YOUR_USER/crux --public \
  --description "A Bitcoin-like chain whose ledger is a git repository"
git init && git add . && git commit -m "CRUX: genesis"
git branch -M main
git remote add origin https://github.com/YOUR_USER/crux.git
git push -u origin main
```

Edit the explorer's repo attribute if you publish under a different name:

```html
<html lang="en" data-repo="YOUR_USER/crux">
```

in `docs/index.html`.

## 4. Create the label, not two standing issues

CRUX does not use a "mine here" issue and a "mempool" issue. Those threads
grow without bound. Each submission is a **new issue** or an `inbox/` pull
request, processed and closed.

```bash
gh label create crux --description "CRUX chain submissions" --color 9A4A18
```

That is the only setup the node needs. Pin nothing. Miners run:

```bash
python3 miner.py --miner YOUR_HANDLE --repo YOUR_USER/crux --submit --message "gm"
```

which opens a new labelled issue per block. Transactions:

```bash
python3 wallet.py send --to crux1... --amount 1.5 --memo "gg"
```

then `gh pr create` adding the file it wrote under `inbox/`, or open a new
issue with the printed line.

## 5. Check Actions can write

Settings → Actions → General → Workflow permissions →
**Read and write permissions**. Without this the node can validate but not
commit, and every submission will be rejected at the push step.

## 6. Turn on the explorer

Settings → Pages → Source: **Deploy from a branch**, branch `main`, folder
**`/docs`**. A minute later the explorer is live at
`https://YOUR_USER.github.io/crux/`.

Until the chain exists it falls back to a bundled demonstration chain,
clearly labelled, so the page is never blank.

## Desktop GUI

After genesis, the same node can be driven without flags:

```bash
python3 gui.py
```

On Windows, `CRUX.bat`. The window talks to the same `chain/`, `inbox/` and
`crux-wallet.json` as the CLI. Submissions are still new `crux` issues or
inbox pull requests.

```bash
python3 tests/test_gui.py
```

## Then mine block 1

```bash
python3 miner.py --miner YOUR_HANDLE --message "block one" --repo YOUR_USER/crux --submit
```

Watch the README update itself.

---

## Tuning

Everything is at the top of `crux/consensus.py`. Change any of these
**before** genesis.

| Constant | Effect |
|---|---|
| `GENESIS_BITS` | starting hash target — `0x20040000` is work 64, minutes on CPU |
| `POW_LIMIT_BITS` | easiest the chain will ever go |
| `TARGET_SPACING` | what the retarget aims for, in seconds |
| `RETARGET_INTERVAL` | how often difficulty adjusts |
| `HALVING_INTERVAL` | blocks between subsidy halvings — `210000` gives a 21 million CRUX cap |
| `COINBASE_MATURITY` | confirmations before a reward can be spent |
| `MAX_TXS_PER_BLOCK` | raise it if validation time stays comfortable |
| `MAX_SUBMISSION_BYTES` | node-side cap, well under GitHub's 64 KB |
| `MAX_MEMO_BYTES` | how much text can ride along with a transfer |

And in `crux/pow.py`:

| Constant | Effect |
|---|---|
| `N` | puzzle size. 44 is 2²² meet-in-the-middle, ~0.5–1 GB in Python. Do not raise it to scale difficulty — the hash target does that, and the proof stays 8 bytes. |

Changing any of these after genesis invalidates the existing chain, and
`verify.py` will tell you so.

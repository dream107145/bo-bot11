# xshark

A **paper** bot for Polymarket's BTC 5-minute "Up or Down" markets that runs the
strategy of the trader **x-MoneyForWhiskas**, with a console in Bo-BOTT's UI.
It holds no key and sends no order: every fill is simulated against the live
Polymarket books and trade prints.

## What it copies

From the trader's full activity log (`data/x-MoneyForWhiskas_activity.csv`,
BTC 5m only, 2026-06-10 to 2026-09-30, 28,101 windows):

| | the trader |
|---|---|
| windows traded | ~276 of 288 a day, 24/7 |
| first / last fill | a median 17 s / 217 s into the window |
| fills per window | 31 (median) |
| resting bids / taker clips | 44 % / 56 % of fills |
| holds both sides | 96.6 % of windows, ~81 % hedged, ~$0.99 per Up+Down pair |
| sells | never; holds to settlement |
| bids: return on cost | +2.4 % (no fee) |
| clips: return on cost | +0.3 % after ~3 % of fees |
| trend | 1.8 % (June) to 0.3 % (September); rebates now most of the profit |

Per window the bot does the same: from `start_after_s` after the open it keeps
a resting BUY on Up and on Down under its fair value, fires fill-and-kill clips
when BTC moves (a 500 ms composite return of at least 1.5 sigma) and the ask is
under fair value by more than the taker fee, never sells, and settles on the
venue's own result.

**Fair value** = the Up book's mid `anchor_s` ago plus how far the TWAP model
moved since. The market sets the level (it beats every model on level, per the
60-second study); the spot feed supplies the move the book has not priced yet.
The model is troll-poly-bot's settlement-TWAP pricer: the market resolves Up iff
Chainlink's 60 s TWAP at the close >= its 60 s TWAP at the open.

## The paper venue (xshark/paper.py)

Measured on the live feed (2026-09-30): the Up and Down books are **mirrored**
(a bid for Up at b is an ask for Down at 1 - b), every trade prints once on the
taker's token with the taker's side, and the book channel arrives a median
13 ms after the venue's timestamp.

* **Clips** reach the book `paper_latency_ms` after the decision and buy the asks
  up to the limit; what is not there is dropped (fill-and-kill). Our own earlier
  clips' shares count as gone for 2 s. Fee 0.07 * p * (1 - p) per share.
* **Bids** are post-only (rejected if they would cross) and join the back of the
  queue at their price. They fill from trade prints that sell our outcome at or
  below the bid (or buy the other outcome at or above 1 - bid), eating the queue
  ahead first, and when the ask falls through the bid ("cross-through").
  Cancels land `paper_cancel_latency_ms` later; fills in between count.
* Makers pay no fee. The **maker rebate is only estimated** (20 % of the fee the
  counterparty paid) and is kept out of PnL.

### Calibrated on this VPS's real orders

`scripts/calibrate_fills.py` replays 844 real fill-or-kill / fill-and-kill buys
sent by the VPS's real-money bots (bo-bot11-real, troll-poly-bot spot-lag) against
the books troll-poly-bot recorded, and writes `data/bot/fill_calibration.json`,
which the bot loads (`xshark/calibration.py`):

* only **10.7 %** of those real orders filled; the book alone says 64 % at a
  150 ms delay and still 38 % at each order's own real latency: faster bots take
  the stale ask first;
* every paper order and cancel waits a delay drawn from the real ones
  (decision to venue reply: p50 271 ms, p90 534 ms, p99 ~1.2 s);
* a clip that finds its price is kept only at the odds real spot-triggered
  orders had: 7 % (ask depth under 3x our size), 11 % (3-20x), 30 % (20x+), 54 %
  if the ask came down by arrival; otherwise it is killed ("lost the race").

Resting bids have no real counterpart (the real bots never rested an order), so
they get the real latency only; their fills stay an **upper bound**. Re-run the
script as the real bots log more orders; switches `paper_latency_real` and
`paper_race` turn each part off.

## What earlier work on this VPS says (read before trusting any paper profit)

* troll-poly-bot `fastclip` (the trader's clips, 2026-09-30): +6 to +15 % of
  volume at zero latency, about break-even at this VPS's latency (test -9 %,
  forward +1.9 %). The clip edge is speed.
* troll-poly-bot open-microstructure study (2026-09-25): 85-95 % of resting-bid
  fills were the ask falling through the order, with -2 to -4 cents of markout.
  The console's **Fill quality** page measures exactly this for our bids.

## Run

```bash
/root/.local/bin/uv venv --python 3.12 .venv
/root/.local/bin/uv pip install --python .venv/bin/python -e '.[dev]'

pm2 start ecosystem.config.js          # xshark-paper ($100 shared) + xshark-web (:8795)
# or by hand:
.venv/bin/python -m xshark --balance 40
.venv/bin/python -m xshark.web --port 8795
```

The bot needs about a minute of spot data (volatility warm-up) and trades the
first window whose open it saw (it must see the strike print). Tests (run them
from outside the project so nothing touches `data/`):

```bash
cd /tmp && /root/xshark/.venv/bin/python -m pytest /root/xshark/tests -q --rootdir /root/xshark
```

## Paper: six coins

Polymarket lists 5-minute Up/Down markets for BTC, ETH, SOL, XRP, DOGE and BNB.
The paper runner starts one market worker per coin in one process (`--asset all`),
with each coin's strategy state in `data/paper/<coin>/` and one shared simulated
bankroll (`--balance` is the total across all six markets). Shared cash and open
order reserves are checked before every order, so the six workers cannot each
spend the full bankroll independently. The shared starting balance is persisted in
`data/paper/paper_bankroll.json`; settled PnL from all six coin ledgers is included
when the runner restarts. Distances from the strike are in bps so one setting fits
every coin.

The configured paper runner uses `--cross-pair` on the six markets (BTC, ETH,
SOL, XRP, DOGE, BNB — **HYPE excluded**). Priority order:

1. **PRIMARY — Rule 7 cascade (xlead):** each cycle walks all **15 pairs** in
   a **distributed** order (BTC×ETH, SOL×XRP, DOGE×BNB, BTC×SOL, …) so no
   market stacks early. One pair every 7 seconds at **10 tokens** (5+5);
   next cycle after the 15th. Cheaper orientation of Up(A)+Down(B) when sum
   ≤ max. Both legs same tick. Tagged `crosspair:xlead:A>B:…`, PnL under
   Rule 7. Paper only.
2. **Rules 1–6** (leader/hold/gap/deep/majority/sweep peer-book signals) run next,
   each with an independent slot.
3. **SECONDARY — same-market complete set (`cset`):** buy Up+Down in one market
   only when depth-adjusted all-in cost (asks + crypto taker fee
   `0.07·p·(1−p)` + slip reserve) locks a positive settlement edge vs $1.
   Tagged `crosspair:cset:…`, metrics under `cset` (not Rule 7). Never scanned
   before the cross-asset engine.

Partial fills are retried; simultaneous legs are not assumed atomic. Paper mode
is the default; live trading stays disabled unless explicitly armed. Thresholds
are editable in Bot Variables. Positions hold to settlement.

**Venue notes (official docs, 2026):** CLOB V2 at `https://clob.polymarket.com`;
makers pay 0 fees / takers pay the crypto fee curve; 5-minute crypto Up/Down
resolves via Chainlink TWAP (30 s lookback on 5m markets) — Up iff final TWAP ≥
open TWAP. Winning tokens redeem $1.

Favourite bid (`fav_*`, off by default): once a token trades above 0.90, rest a
5-share bid at 0.90 on it while the coin is at least 2.4 bps from the strike on its
side, pulled with 30 s left. Replay over ~2,000 windows per coin (troll-poly-bot
charts), both halves of the data: BTC +3.6 / +1.9 % and ETH +3.7 / +1.2 % per $
bought; BNB, XRP, SOL, HYPE and DOGE around zero or negative.

## Real account (xshark/live.py)

`xshark-real` (pm2) runs the same bot against the real Polymarket account, with
its own data in `data/real/` and its own console on `:8796`. The account comes
from `.env` (the same `TPB_POLY_*` names bo-real-bot uses), loaded by the bot
and never printed. Orders are made like troll-poly-bot's live adapter
(polymarket-client 0.10): clips are fill-and-kill market orders with a price
bound, bids are post-only GTC limits with a 240 s venue-side expiry, polled
every second.

Three locks:

1. `--live` alone is **SHADOW**: it logs in, reads the balance, prewarms each
   new market and **signs** every order the strategy wants, then posts nothing.
   Fills stay simulated. The console badge says SHADOW.
2. `--live --armed` posts **real orders**, and only when the environment also has
   `TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY`. Fills are what the venue reports.
   At start it cancels stray orders; on stop it cancels its resting bids.
3. Hard caps outside the strategy: `--max-order-usdc` (5), `--max-open-usdc`
   (20), `--max-daily-loss-usdc` (10), `--max-orders-per-hour` (300), the
   console's KILL file (re-read before every POST), a 1 s stale-decision
   cutoff, and a halt after 5 API errors in a row.

Winnings are redeemed through the relayer when `.env` has a relayer key;
otherwise redeem them in the Polymarket app.

## Saved charts

`xshark-charts` (pm2, `scripts/save_charts.py`) photographs every window a bot traded and
keeps its data for good: `data/real/charts/` and `data/paper/<coin>/charts/`, one
`<GMT+9 start>_<pnl>_<slug>.png` (the console's window card: prices, fills, and the coin
against the strike with a marker and reason for every order sent) plus `<slug>.json`.
The image is the console page `?print=<slug>` taken by a headless Chromium.

## Console

`http://127.0.0.1:8795/` (every time shown is GMT+9 with its date).

* **Live**: equity, the window's Up/Down prices with our fair value, resting bids
  and fills, BTC against the strike, the position, what each leg is doing and why,
  feeds.
* **Windows**: settled PnL (total, maker, taker), the ledger, each window's chart,
  and a benchmark table against the trader.
* **Fill quality**: per leg and per fill type, edge at the fill and markouts at
  1 s / 5 s / 30 s and at settlement.
* **Earnings**: per day / week / month.
* **Bot variables**: every tunable, applied within ~2 s; Halt orders. Writes need
  the key in `data/bot/console_key`.

## Files

`data/bot/`: `state.json` (live, 2 Hz), `windows.jsonl`, `fills.jsonl`,
`windows/<slug>.json` (7 days), `open.json` (unsettled positions, restored on
restart), `controls.json`, `KILL`, `console_key`.

## Known limits

* Paper only. Going live needs an order adapter (troll-poly-bot's
  `execution/polymarket.py` uses `polymarket-client` 0.10 with post-only GTC
  bids and FAK clips) and your decision.
* The strike comes from Polymarket's legacy live-data socket
  (`crypto_prices_twap_sixty`), which Polymarket plans to retire around late
  October 2026; the bot then falls back to reconstructing it from Chainlink ticks,
  and then to its own spot composite.
* A bot started mid-window sits that window out (it never saw the strike).

Sources: the Bo-BOTT console (UI; `styles.css` copied unchanged, additions in
`xshark.css`), troll-poly-bot (`spot.py`, `vol.py`, `twap.py` copied), Bo-BOTT
(`chainlink.py`, ported to websockets).

## Replay study and the current settings (2026-09-30)

`scripts/replay.py` runs the real `strategy.decide()` over 1,758 recorded BTC
windows (troll-poly-bot charts, 09-22..09-30; TRAIN = first 60 %, TEST = last
40 %) with real VPS latency and race odds. Resting bids lost in every variant
(edge 2-10 c, price bands, pull-on-adverse-move, pair cap, time limits):
-3.7 % to -7.7 % on TEST. Clips alone were positive on TRAIN but ~0 on TEST; the
only variant not negative on TEST was a 3 c minimum edge (TRAIN +6.6 %, t 3.5;
TEST +0.7 %, t 0.4 -- break-even). So the paper bot now runs with
`maker_enabled = false` and `taker_edge = 0.03` as console overrides
(data/bot/controls.json; "Clear overrides" restores the trader's playbook).
Earlier runs are archived in data/bot/archive-*.

## Pair completion (2026-10-01, at the user's request)

After one side fills, the bot buys the other side so each Up+Down pair costs at
most `pair_target` (0.97): it takes the other ask at once when that already
completes the pair, else rests a post-only bid at that price. Replay (1,812
windows, whole-clip race draws): pairing made results WORSE at every target --
TEST -4.6 % to -5.4 % against -2.1 % for clips alone -- because the other side
only gets cheap enough when the first side is winning (pairing caps winners and
leaves losers unhedged). It is on because the user asked for it; Bot variables
-> "Complete pairs" switches it off.

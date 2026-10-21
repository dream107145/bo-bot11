// pm2 process definitions for xshark, in the style of Bo-BOTT's and compare_bot's.
//
//   pm2 start ecosystem.config.js                 # both apps
//   pm2 start ecosystem.config.js --only xshark-paper
//   pm2 logs xshark-paper
//
// xshark-paper is a PAPER bot: it holds no key and sends no order. Fills are
// simulated against the live Polymarket books (xshark/paper.py). Halt it from
// the console (Bot variables -> Halt orders) or `touch data/bot/KILL`.
//
// Normal CPU priority on purpose: a bot started under nice on this server falls
// behind its feeds and trades stale bursts (troll-poly-bot, 2026-09-25).
//
// The console is opened like the other consoles on 149.33.42.112 (the user's
// choice, 2026-09-30). Reading is open to anyone who can reach the port; every
// change (tuning, halt) needs the key in data/bot/console_key. For loopback
// only, use --host 127.0.0.1.
//
// Do not `pm2 save` / `pm2 resurrect` without checking: the saved pm2 list on this
// VPS is stale (it predates the IP change).

const path = require('path');
const PY = path.join(__dirname, '.venv', 'bin', 'python');

// Paper: all six markets share one $100 simulated bankroll and the cross-market lag strategy.
const PAPER_COINS = 'all';

module.exports = {
  apps: [
    {
      name: 'xshark-paper',
      cwd: __dirname,
      script: PY,
      interpreter: 'none',
      args: `-m xshark --asset ${PAPER_COINS} --balance 100 --cross-pair --price-source composite --exchanges bybit --data-dir data/paper`,
      autorestart: true,
      min_uptime: '30s',
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 15000,           // six market workers write open.json and a last state on SIGINT
      merge_logs: true,
      time: true,
      env: { PYTHONUNBUFFERED: '1' },
      max_memory_restart: '1500M',
      out_file: path.join(__dirname, 'logs', 'xshark-paper.log'),
      error_file: path.join(__dirname, 'logs', 'xshark-paper.log'),
    },
    {
      name: 'xshark-web',
      cwd: __dirname,
      script: PY,
      interpreter: 'none',
      args: '-m xshark.web --host 149.33.42.112 --port 8795 --data-dir data/paper --only-on',   // public at the user's request, 2026-09-30; dashboard follows the shared six-market paper bot
      autorestart: true,
      min_uptime: '30s',
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 5000,
      merge_logs: true,
      time: true,
      env: { PYTHONUNBUFFERED: '1' },
      max_memory_restart: '400M',
      out_file: path.join(__dirname, 'logs', 'xshark-web.log'),
      error_file: path.join(__dirname, 'logs', 'xshark-web.log'),
    },
    {
      // Second view of the same shared six-market paper bot, on port 8796.
      name: 'xshark-paper-web-8796',
      cwd: __dirname,
      script: PY,
      interpreter: 'none',
      args: '-m xshark.web --host 149.33.42.112 --port 8796 --data-dir data/paper --only-on',
      autorestart: true,
      min_uptime: '30s',
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 5000,
      merge_logs: true,
      time: true,
      env: { PYTHONUNBUFFERED: '1' },
      max_memory_restart: '400M',
      out_file: path.join(__dirname, 'logs', 'xshark-paper-web-8796.log'),
      error_file: path.join(__dirname, 'logs', 'xshark-paper-web-8796.log'),
    },
    {
      // The Whiskas bot (user, 2026-10-10: "make a new bot ... imitate x-MoneyForWhiskas"): a second PAPER
      // bot, BTC only, running xshark/whisk.py (BTC-move buys of the rising token + balancing buys), with its
      // own data (data/whisk/btc) and console (:8797). The first paper bot (xshark-paper) keeps running.
      name: 'xshark-whisk',
      cwd: __dirname,
      script: PY,
      interpreter: 'none',
      args: '-m xshark --asset btc --balance 1000 --price-source composite --exchanges bybit --data-dir data/whisk',
      autorestart: true,
      min_uptime: '30s',
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 15000,
      merge_logs: true,
      time: true,
      env: { PYTHONUNBUFFERED: '1' },
      max_memory_restart: '1500M',
      out_file: path.join(__dirname, 'logs', 'xshark-whisk.log'),
      error_file: path.join(__dirname, 'logs', 'xshark-whisk.log'),
    },
    {
      name: 'xshark-whisk-web',
      cwd: __dirname,
      script: PY,
      interpreter: 'none',
      args: '-m xshark.web --host 149.33.42.112 --port 8797 --data-dir data/whisk --only-on',
      autorestart: true,
      min_uptime: '30s',
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 5000,
      merge_logs: true,
      time: true,
      env: { PYTHONUNBUFFERED: '1' },
      max_memory_restart: '400M',
      out_file: path.join(__dirname, 'logs', 'xshark-whisk-web.log'),
      error_file: path.join(__dirname, 'logs', 'xshark-whisk-web.log'),
    },
      {
        // ETH V6 paper bot: separate process, controls, and history from BTC.
        name: 'xshark-whisk-eth',
        cwd: __dirname,
        script: PY,
        interpreter: 'none',
        args: '-m xshark --asset eth --balance 1000 --price-source composite --exchanges bybit --data-dir data/whisk',
        autorestart: true,
        min_uptime: '30s',
        max_restarts: 10,
        restart_delay: 5000,
        kill_timeout: 15000,
        merge_logs: true,
        time: true,
        env: { PYTHONUNBUFFERED: '1' },
        max_memory_restart: '1500M',
        out_file: path.join(__dirname, 'logs', 'xshark-whisk-eth.log'),
        error_file: path.join(__dirname, 'logs', 'xshark-whisk-eth.log'),
      },
      {
        // Remaining five 5-minute paper markets shown in the port 8795 console.
        name: 'xshark-whisk-rest',
        cwd: __dirname,
        script: PY,
        interpreter: 'none',
        args: '-m xshark --asset sol,xrp,doge,bnb --balance 1000 --price-source composite --exchanges bybit --data-dir data/whisk',
        autorestart: true,
        min_uptime: '30s',
        max_restarts: 10,
        restart_delay: 5000,
        kill_timeout: 15000,
        merge_logs: true,
        time: true,
        env: { PYTHONUNBUFFERED: '1' },
        max_memory_restart: '1500M',
        out_file: path.join(__dirname, 'logs', 'xshark-whisk-rest.log'),
        error_file: path.join(__dirname, 'logs', 'xshark-whisk-rest.log'),
      },
    {
      // The REAL account (credentials in .env, loaded by the bot, never printed).
      // --live alone = SHADOW: logs in, reads the balance, signs every order and
      // POSTS NOTHING; fills stay simulated. Real orders need BOTH --armed here
      // AND TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY in env -- only on the user's word.
      // Hard caps (xshark/live.py LiveCaps) apply on top of the console settings.
      // ETH since 2026-10-06 (user: scalp cycles live on ETH; BTC's leader buy stopped).
      // Money moved outside the bot (deposits, trades by hand) is booked by the bot's account
      // sync into data/real/outside.jsonl, so --balance stays the first deposit.
      name: 'xshark-real',
      cwd: __dirname,
      script: PY,
      interpreter: 'none',
      args: '-m xshark --asset eth --live --armed --balance 26.33 --price-source composite --exchanges bybit --data-dir data/real '
          + '--max-order-usdc 5 --max-open-usdc 20 --max-daily-loss-usdc 10',
      autorestart: true,
      min_uptime: '30s',
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 30000,           // armed: time to cancel resting orders on the venue
      merge_logs: true,
      time: true,
      env: { PYTHONUNBUFFERED: '1', TPB_LIVE_ACK: 'I_UNDERSTAND_REAL_MONEY' },
      max_memory_restart: '600M',
      out_file: path.join(__dirname, 'logs', 'xshark-real.log'),
      error_file: path.join(__dirname, 'logs', 'xshark-real.log'),
    },
    {
      name: 'xshark-real-web',
      cwd: __dirname,
      script: PY,
      interpreter: 'none',
      args: '-m xshark.web --host 149.33.42.112 --port 8796 --data-dir data/real',
      autorestart: true,
      min_uptime: '30s',
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 5000,
      merge_logs: true,
      time: true,
      env: { PYTHONUNBUFFERED: '1' },
      max_memory_restart: '400M',
      out_file: path.join(__dirname, 'logs', 'xshark-real-web.log'),
      error_file: path.join(__dirname, 'logs', 'xshark-real-web.log'),
    },
    {
      // Saves a PNG of every window a bot traded (real + paper) and keeps its chart data for good:
      // data/real/charts/, data/paper/<coin>/charts/ (user, 2026-10-02). Not a bot: niced so a
      // headless Chromium shot never takes CPU from the trading processes.
      name: 'xshark-charts',
      cwd: __dirname,
      script: '/usr/bin/nice',
      interpreter: 'none',
      args: `-n 10 ${PY} scripts/save_charts.py`,
      autorestart: true,
      min_uptime: '30s',
      max_restarts: 10,
      restart_delay: 10000,
      kill_timeout: 5000,
      merge_logs: true,
      time: true,
      env: { PYTHONUNBUFFERED: '1' },
      max_memory_restart: '300M',
      out_file: path.join(__dirname, 'logs', 'xshark-charts.log'),
      error_file: path.join(__dirname, 'logs', 'xshark-charts.log'),
    },
  ],
};

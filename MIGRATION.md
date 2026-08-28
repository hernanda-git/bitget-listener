# Bitget-Listener — Migration Guide (Sera → new server)

> Written 2026-08-28. Verified live on Sera (PID 2445887, branch `main` @ `843ff69`).
> Goal: move the bot to a new host with ZERO loss of state, auth, or history.

## 0. What the bot actually depends on (the dependency map)

The bot is NOT a single repo. It is **three things on disk + one database + two auth files + one systemd unit + one nginx site**.

| # | Artifact | Path on Sera | In git? | Notes |
|---|----------|-------------|---------|-------|
| 1 | **bitget-listener repo** | `/opt/bots/bitget-listener` | YES (github.com/hernanda-git/bitget-listener) | Code only. Secrets/state are git-ignored. |
| 2 | **bot_common package** | `/opt/bots/bot_common` | **NO** (local dir, no remote) | Hard runtime dependency (`from bot_common import repos/db`). MUST be copied separately. |
| 3 | **`.env` secrets** | `/opt/bots/bitget-listener/.env` | NO (git-ignored) | Bitget keys, TG bot token, TG user API, PG password, DASH_PASSWORD. Paste on new host. |
| 4 | **Telethon session** | `/opt/bots/bitget-listener/sessions/bitget_listener.session` | NO (git-ignored) | The live Telegram USER login. Copy the FILE or the bot must re-auth (needs your phone + 2FA). |
| 5 | **Postgres DB** | `fatty_config` (role `app`) | N/A | All durable state: `bc_trades`, `bc_positions`, `bc_armed_stops`, `bc_events`, `bc_equity`, `bc_trace_events`, `bc_signal_receipts`, `fatty_config.params`. |
| 6 | **systemd unit** | `/etc/systemd/system/bitget-listener.service` | NO | Launch flags + env (PAPER=0, BITGET_PRODUCT_TYPE, DASH_PASSWORD, PGPASSWORD, ARM_SL_TP, etc.). |
| 7 | **nginx site** | `/etc/nginx/conf.d/bitget.warga-digital.conf` | NO | Proxies `:443` → `127.0.0.1:8092`. References a TLS cert. |
| 8 | **TLS cert** | `/etc/letsencrypt/live/tia.warga-digital.com/` | NO | Shared SAN cert. Either copy it or re-issue with certbot on the new host. |
| 9 | **Python venv** | `/opt/bots/bitget-listener/.venv` | NO | Python 3.11.15 + pinned deps. Rebuild on new host (`requirements.txt` is pinned). |
| 10 | **runtime lock** | `/opt/bots/bitget-listener/.audit.lock` | NO | Ephemeral. Ignore / let it regenerate. |

**Other bots on Sera are NOT your concern for this migration** — `learnernoearner-listener`, `mexc-listener`, `hyperliquid-fatty-trader`, `chart-signal-vision-plugin` are separate. The `fatty_config` DB on Sera currently contains ONLY `bitget-listener` rows (verified: `SELECT DISTINCT bot` → `bitget-listener` only), so dumping it for bitget is clean.

---

## 1. Collect everything on Sera (run NOW, before the VPS goes down)

```bash
# 1a. Code repos (git)
cd /opt/bots
git -C bitget-listener bundle create /root/mig/bitget-listener.bundle --all
# bot_common has no remote -> tar it
tar czf /root/mig/bot_common.tgz bot_common

# 1b. Secrets + session (COPY, never commit)
mkdir -p /root/mig/secrets
cp bitget-listener/.env /root/mig/secrets/bitget.env
cp bitget-listener/sessions/bitget_listener.session /root/mig/secrets/
# (BITGET keys are also in .env. TG_API_ID/HASH in BOTH .env and the unit — same values.)

# 1c. Postgres dump (bitget rows only)
export PGPASSWORD=$(cat ~/.pg_app_pw)
pg_dump -h 127.0.0.1 -U app -d fatty_config \
  --table=bc_trades --table=bc_positions --table=bc_armed_stops \
  --table=bc_events --table=bc_equity --table=bc_trace_events \
  --table=bc_signal_receipts --table=fatty_config \
  --data-only \
  --where="bot='bitget-listener'" > /root/mig/bitget_pg.sql 2>/dev/null
# NOTE: pg_dump --where on multiple tables filters each by bot='bitget-listener'.
# fatty_config has no bot col -> dump it fully (only one row anyway):
pg_dump -h 127.0.0.1 -U app -d fatty_config --table=fatty_config --data-only >> /root/mig/bitget_pg.sql

# 1d. Systemd + nginx + cert
mkdir -p /root/mig/sys
cp /etc/systemd/system/bitget-listener.service /root/mig/sys/
cp /etc/nginx/conf.d/bitget.warga-digital.conf /root/mig/sys/
tar czf /root/mig/sys/letsencrypt.tgz /etc/letsencrypt/live/tia.warga-digital.com

# 1e. Bundle it all
tar czf /root/bitget-migration-FULL.tgz -C /root/mig .
```

Transfer `/root/bitget-migration-FULL.tgz` off Sera (scp / rclone / download) BEFORE shutdown.

---

## 2. New server: OS prerequisites

```bash
# Debian/Ubuntu
apt update && apt install -y python3.11 python3.11-venv python3-pip \
  postgresql postgresql-contrib nginx certbot python3-certbot-nginx git curl
# Open firewall
ufw allow 22 80 443
```

## 3. Restore code

```bash
mkdir -p /opt/bots
cd /opt/bots
git clone https://github.com/hernanda-git/bitget-listener     # OR: git bundle clone bitget-listener.bundle
tar xzf bot_common.tgz                                        # restores /opt/bots/bot_common
cd bitget-listener
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt                     # pinned; rebuilds venv
```

## 4. Restore secrets + session

```bash
cp /root/mig/secrets/bitget.env /opt/bots/bitget-listener/.env
mkdir -p /opt/bots/bitget-listener/sessions
cp /root/mig/secrets/bitget_listener.session /opt/bots/bitget-listener/sessions/
chmod 600 /opt/bots/bitget-listener/.env
```

## 5. Restore Postgres

```bash
sudo -u postgres psql -c "CREATE ROLE app LOGIN PASSWORD 'SAME_AS_OLD_PGPASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE fatty_config OWNER app;"
sudo -u postgres psql -d fatty_config -c "ALTER SCHEMA public OWNER TO app;"
# load schema + data
PGPASSWORD=<app_pw> psql -h 127.0.0.1 -U app -d fatty_config -f /root/mig/bitget_pg.sql
# re-grant (bot_common creates bc_* tables as postgres; app needs rights)
sudo -u postgres psql -d fatty_config -c \
  "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO app;
   GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO app;"
```
> If `ensure_schema()` is preferred over the dump, the bot can recreate tables on first boot
> (`from bot_common import ensure_schema`), but the **data** (history, paper flag) only comes from the dump.
> Always load the dump.

## 6. Restore systemd + nginx

```bash
cp /root/mig/sys/bitget-listener.service /etc/systemd/system/
# nginx: either reuse the copied cert, or re-issue:
cp /root/mig/sys/bitget.warga-digital.conf /etc/nginx/conf.d/
# Option A (keep cert): tar xzf /root/mig/sys/letsencrypt.tgz -C /
# Option B (fresh cert): certbot --nginx -d bitget.warga-digital.com
systemctl daemon-reload
nginx -t && systemctl reload nginx
```

## 7. Wire DNS + Cloudflare

- Point `bitget.warga-digital.com` **A record** to the new server IP.
- Cloudflare proxy (orange cloud) can stay; it terminates TLS at Cloudflare, origin serves `:443` with the SAN cert.

## 8. Launch + verify

```bash
systemctl enable --now bitget-listener.service
sleep 4
systemctl is-active bitget-listener.service          # expect: active
curl -sS https://bitget.warga-digital.com/api/health   # expect: ok + mode
curl -sS https://bitget.warga-digital.com/api/state | python3 -m json.tool
# confirm: gate.paper matches old (false = LIVE), positions unchanged, no naked stops
journalctl -u bitget-listener.service -n 50 --no-pager
```

---

## 9. Gotchas (learned the hard way)

- **`PAPER=0` in the unit is a red herring for boot mode.** Boot reads `paper` from `fatty_config`.
  The dump carries `paper=f`, so the new host boots LIVE automatically. If you want paper first,
  `UPDATE fatty_config SET params=jsonb_set(params,'{paper}','true') WHERE bot_name='bitget-listener';`
  then restart. (Or use the `/api/paper` endpoint with `DASH_PASSWORD`.)
- **`bot_common` has no git remote** — it will NOT come with the clone. Forget it and every import
  (`from bot_common import repos`) dies with ModuleNotFoundError. Tar it (step 1b/3).
- **Telethon `.session` is the auth.** Without it the bot prompts for phone+code on first run and
  you must re-auth the user account. Copy the file (step 1b/4).
- **`ARM_SL_TP=ORDIUSDT:4.045:5.18`** in the unit re-arms ORDI bot-side SL/TP on restart (the venue
  `place-tpsl-order` silently drops plan orders on this account). Keep it; adjust the symbol if ORDI
  is no longer held.
- **HIGH risk profile is live** with only ~$8.57 real balance → every new entry is rejected
  (`40762` / "min notional 90.0 exceeds max_position_usd"). That is a sizing/config decision, not a
  migration break — it will behave identically on the new host. Decide funding or profile before going live.
- **venv rebuild**: the pinned `requirements.txt` is what ran on Sera. Do NOT `pip install` unpinned.
- **`PYTHONPATH=/opt/bots/bitget-listener:/opt/bots`** must be set (it is in the unit). Without it,
  `import bot_common` fails.
- **Journald retention is short** — all durable history is in Postgres (`bc_*`). The dump is the source
  of truth for "recent signals / activities."

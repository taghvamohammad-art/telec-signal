#!/usr/bin/env bash
# telec-signal installer for Ubuntu 24.04. Run from the repository root:
#   sudo bash deploy/install.sh
# Safe to run again after `git pull` (keeps .env and the database).
set -euo pipefail
trap 'printf "\n\033[1;31m!! install failed at line %s: %s\033[0m\n" "$LINENO" "$BASH_COMMAND"' ERR

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER=telec
DB_NAME=telec_signal
DB_USER=telec

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
[[ "$APP_DIR" != /root* && "$APP_DIR" != /home/* ]] || { echo "Clone the repo under /opt (e.g. /opt/telec-signal) so the service user can read it"; exit 1; }
. /etc/os-release
[[ "${VERSION_ID:-}" == "24.04" ]] || warn "Tested on Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}"

PG_VER=16

say "System packages"
apt-get update -y
apt-get install -y python3-venv python3-pip curl gnupg lsb-release git ca-certificates postgresql-common

# The unversioned "postgresql"/"postgresql-contrib" metapackages follow the newest major
# version once the PGDG repo is added (they pulled in PostgreSQL 19 and moved 16 to port 5433).
# Pin to postgresql-16 explicitly and drop the metapackages.
if dpkg -s postgresql >/dev/null 2>&1 || dpkg -s postgresql-contrib >/dev/null 2>&1; then
  apt-mark manual "postgresql-${PG_VER}" "postgresql-client-${PG_VER}" 2>/dev/null || true
  apt-get remove -y postgresql postgresql-contrib || true
fi

# Official PostgreSQL repo: Ubuntu's own build often lags the minor version TimescaleDB requires.
if ! grep -rqs apt.postgresql.org /etc/apt/sources.list.d/; then
  say "Adding PostgreSQL (PGDG) apt repository"
  /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y || warn "PGDG repo setup failed; using Ubuntu's PostgreSQL"
fi
apt-get update -y
apt-get install -y "postgresql-${PG_VER}" "postgresql-client-${PG_VER}"
systemctl enable --now postgresql
PORT16="$(pg_conftool "${PG_VER}" main show port 2>/dev/null | grep -oE '[0-9]+$' || true)"
if [[ "$PORT16" != "5432" ]]; then
  pg_lsclusters || true
  echo "PostgreSQL ${PG_VER}/main is on port '${PORT16}', expected 5432. Fix the clusters (see README) and re-run."
  exit 1
fi

say "TimescaleDB (optional; the app works without it)"
if ! dpkg -s "timescaledb-2-postgresql-${PG_VER}" >/dev/null 2>&1; then
  if curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey | gpg --dearmor --yes -o /etc/apt/trusted.gpg.d/timescaledb.gpg \
     && echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -cs) main" > /etc/apt/sources.list.d/timescaledb.list \
     && apt-get update -y && apt-get install -y "timescaledb-2-postgresql-${PG_VER}"; then
    :
  else
    warn "TimescaleDB install failed; continuing with plain PostgreSQL"
    rm -f /etc/apt/sources.list.d/timescaledb.list
  fi
fi
# The extension only loads if it is in shared_preload_libraries (checked on every run).
if dpkg -s "timescaledb-2-postgresql-${PG_VER}" >/dev/null 2>&1; then
  apt-get install -y timescaledb-tools || true   # provides timescaledb-tune
  PRELOAD="$(sudo -u postgres psql -tAc 'SHOW shared_preload_libraries')"
  if [[ "$PRELOAD" != *timescaledb* ]]; then
    if command -v timescaledb-tune >/dev/null 2>&1; then
      timescaledb-tune --quiet --yes --memory=768MB --cpus=2 \
        --pg-config="/usr/lib/postgresql/${PG_VER}/bin/pg_config" || warn "timescaledb-tune failed"
    fi
    PRELOAD="$(sudo -u postgres psql -tAc 'SHOW shared_preload_libraries')"
    if [[ "$PRELOAD" != *timescaledb* ]]; then
      # tune did not run; edit the file directly (takes effect on restart)
      sudo -u postgres psql -c "ALTER SYSTEM SET shared_preload_libraries = '${PRELOAD:+$PRELOAD,}timescaledb'"
    fi
    systemctl restart postgresql
  fi
fi

say "Swap (small servers)"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [[ $MEM_MB -lt 3000 && -z "$(swapon --show --noheadings)" && ! -e /swapfile ]]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null && echo 'vm.swappiness=10' > /etc/sysctl.d/99-telec.conf
fi

say "Service user"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

say "Database"
if [[ ! -f "$APP_DIR/.env" ]]; then
  # (tr </dev/urandom | head would be killed by SIGPIPE under pipefail and abort silently)
  DB_PASS="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  ELSE
    ALTER ROLE ${DB_USER} PASSWORD '${DB_PASS}';
  END IF;
END \$\$;
SQL
  DB_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'")"
  [[ "$DB_EXISTS" == "1" ]] || sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
  sed "s#^DATABASE_URL=.*#DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}#" \
    "$APP_DIR/.env.example" > "$APP_DIR/.env"
  chmod 640 "$APP_DIR/.env"
  say "Created $APP_DIR/.env (add ADMIN_BOT_TOKEN / ADMIN_CHAT_ID there for Telegram alerts)"
fi
# extension must be created by a superuser once
sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" 2>/dev/null \
  || warn "timescaledb extension not enabled (plain PostgreSQL mode)"
chown root:"$APP_USER" "$APP_DIR/.env"

say "Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-dev.txt" -q

say "Unit tests"
(cd "$APP_DIR" && .venv/bin/python -m pytest -q)

say "Connectivity check"
(cd "$APP_DIR" && .venv/bin/python scripts/check_connectivity.py) || warn "fix connectivity before relying on the engine"

say "Web assets (chart library, font) served from this server"
VENDOR="$APP_DIR/app/web/static/vendor"
mkdir -p "$VENDOR"
fetch() {  # fetch DEST URL... (first URL that works)
  local dest="$1"; shift
  [[ -s "$dest" ]] && return 0
  for url in "$@"; do curl -fsSL --max-time 60 "$url" -o "$dest.tmp" && mv "$dest.tmp" "$dest" && return 0; done
  rm -f "$dest.tmp"; return 1
}
fetch "$VENDOR/lightweight-charts.js" \
  "https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js" \
  "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js" \
  || warn "chart library download failed; the chart will not render"
fetch "$VENDOR/Vazirmatn.woff2" \
  "https://cdn.jsdelivr.net/npm/vazirmatn@33.0.3/fonts/webfonts/Vazirmatn%5Bwght%5D.woff2" \
  "https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/fonts/webfonts/Vazirmatn%5Bwght%5D.woff2" \
  || warn "font download failed; falling back to Tahoma"

say "systemd services"
SERVICES=(telec-data telec-analysis telec-web telec-bot telec-whales telec-ops)
for svc in "${SERVICES[@]}"; do
  sed "s#__APP_DIR__#${APP_DIR}#g" "$APP_DIR/deploy/${svc}.service" > "/etc/systemd/system/${svc}.service"
done
systemctl daemon-reload
for svc in "${SERVICES[@]}"; do
  systemctl enable "${svc}.service"
  systemctl restart "${svc}.service"
done
sleep 3
for svc in "${SERVICES[@]}"; do
  systemctl --no-pager --lines=5 status "${svc}.service" || true
done

say "Nginx and HTTPS"
grep -q '^DOMAIN=' "$APP_DIR/.env" || printf '\nDOMAIN=signal.telec.shop\n' >> "$APP_DIR/.env"
DOMAIN="$(grep '^DOMAIN=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
apt-get install -y nginx certbot python3-certbot-nginx
cat > /etc/nginx/sites-available/telec-signal <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    client_max_body_size 1m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/telec-signal /etc/nginx/sites-enabled/telec-signal
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 'Nginx Full' || true; fi
if [[ ! -d "/etc/letsencrypt/live/${DOMAIN}" ]]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect \
    || warn "HTTPS certificate failed: check that ${DOMAIN} points to this server, then run: sudo certbot --nginx -d ${DOMAIN}"
else
  certbot install --nginx --cert-name "$DOMAIN" --non-interactive --redirect >/dev/null 2>&1 || true
fi

USERS="$(sudo -u postgres psql -d "$DB_NAME" -tAc 'SELECT count(*) FROM users' 2>/dev/null || echo 0)"
if [[ "${USERS:-0}" == "0" ]]; then
  say "Create the first panel user:"
  echo "  cd $APP_DIR && sudo -u $APP_USER .venv/bin/python scripts/manage_users.py add admin --admin"
fi

say "Done. Panel: https://${DOMAIN}   Logs: journalctl -u telec-data -f | -u telec-analysis -f | -u telec-web -f"

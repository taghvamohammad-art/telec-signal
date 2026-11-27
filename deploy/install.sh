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

# Official PostgreSQL repo: Ubuntu's own build often lags the minor version TimescaleDB requires.
if ! grep -rqs apt.postgresql.org /etc/apt/sources.list.d/; then
  say "Adding PostgreSQL (PGDG) apt repository"
  /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y || warn "PGDG repo setup failed; using Ubuntu's PostgreSQL"
fi
apt-get update -y
apt-get install -y "postgresql-${PG_VER}" "postgresql-client-${PG_VER}"
systemctl enable --now postgresql

say "TimescaleDB (optional; the app works without it)"
if ! dpkg -s "timescaledb-2-postgresql-${PG_VER}" >/dev/null 2>&1; then
  if curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey | gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg \
     && echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -cs) main" > /etc/apt/sources.list.d/timescaledb.list \
     && apt-get update -y && apt-get install -y "timescaledb-2-postgresql-${PG_VER}"; then
    timescaledb-tune --quiet --yes --memory=768MB --cpus=2 || warn "timescaledb-tune failed; tune manually later"
    systemctl restart postgresql
  else
    warn "TimescaleDB install failed; continuing with plain PostgreSQL"
    rm -f /etc/apt/sources.list.d/timescaledb.list
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

say "systemd service"
sed "s#__APP_DIR__#${APP_DIR}#g" "$APP_DIR/deploy/telec-data.service" > /etc/systemd/system/telec-data.service
systemctl daemon-reload
systemctl enable telec-data.service
systemctl restart telec-data.service
sleep 3
systemctl --no-pager --lines=15 status telec-data.service || true

say "Done. Logs: journalctl -u telec-data -f"

#!/bin/bash
# Install the trading cycle cron job on the droplet.
#
# Replaces the in-app scheduler, which lives in the FastAPI process and so
# dies on every restart or redeploy without saying anything. Cron survives
# both, and reboots.
#
# Usage (on the droplet):  ./scripts/install_cron.sh
set -e

APP_DIR="/opt/fastapi"
LOG="/var/log/qqq-trading.log"
# -w /app and PYTHONPATH are both required: `docker compose exec` does not
# necessarily start in the image's WORKDIR, and without them run_cycle.py
# dies on "ModuleNotFoundError: No module named 'config'" every single minute
# while cron itself reports nothing wrong.
COMPOSE="docker compose -f $APP_DIR/docker-compose.yml -f $APP_DIR/docker-compose.prod.yml --project-directory $APP_DIR"
EXEC="$COMPOSE exec -T -w /app -e PYTHONPATH=/app app"

# Every minute. run_cycle.py decides for itself whether to act — it owns the
# market-hours and kill-switch checks, in Python, because market hours are
# America/New_York and shift with daylight saving. A UTC cron window would be
# wrong for half the year.
LINE="* * * * * cd $APP_DIR && $EXEC python scripts/run_cycle.py >> $LOG 2>&1"

touch "$LOG"

# Idempotent: strip any previous version before adding this one.
#
# The `|| true` matters. On an empty crontab `grep -v` gets no input and exits
# 1, which under `set -e` aborts the subshell before the echo ever runs --
# the result being a crontab installed with nothing in it, silently.
( crontab -l 2>/dev/null | grep -v "run_cycle.py" || true ; echo "$LINE" ) | crontab -

echo "installed:"
crontab -l | grep run_cycle.py

# Keep the log from growing without bound.
cat > /etc/logrotate.d/qqq-trading <<ROTATE
$LOG {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
ROTATE
echo "logrotate configured: $LOG (14 days)"

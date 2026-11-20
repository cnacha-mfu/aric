#!/usr/bin/env bash
# One-time install of ARIC OS on the ARIC VM. Run as:  sudo bash /var/www/aric/ops/deploy/install.sh
# Safe to run again: it keeps an existing /etc/aric-os.env and database, and backs up the nginx site first.
set -euo pipefail
REPO=/var/www/aric
DEP="$REPO/ops/deploy"
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo"; exit 1; }

echo "== 1/6 Pull the latest code"
sudo -u aric git -C "$REPO" pull --ff-only -q

echo "== 2/6 Data and backup folders"
install -d -o aric -g aric -m 750 /var/lib/aric-os /var/backups/aric-os

echo "== 3/6 Settings (/etc/aric-os.env)"
if [ ! -f /etc/aric-os.env ]; then install -o root -g aric -m 640 "$DEP/aric-os.env.example" /etc/aric-os.env; echo "   created"; else echo "   kept existing file"; fi

echo "== 4/6 nginx site"
BAK="/etc/nginx/sites-available/aric.bak-$(date +%Y%m%d-%H%M%S)"
cp /etc/nginx/sites-available/aric "$BAK"
install -m 644 "$DEP/nginx-aric.conf" /etc/nginx/sites-available/aric
if ! nginx -t; then cp "$BAK" /etc/nginx/sites-available/aric; echo "nginx test failed; old site restored from $BAK"; exit 1; fi

echo "== 5/6 Services"
install -m 644 "$DEP/aric-os.service" "$DEP/aric-os-backup.service" "$DEP/aric-os-backup.timer" /etc/systemd/system/
systemctl daemon-reload
sudo -u aric bash -c "set -a; . /etc/aric-os.env; set +a; python3 '$REPO/ops/server.py' --seed-initial"
systemctl enable --now aric-os.service aric-os-backup.timer
systemctl restart aric-os.service
systemctl reload nginx

echo "== 6/6 Check"
sleep 2
curl -fsS http://127.0.0.1:8090/os/api/health && echo
curl -fsS -o /dev/null -w "nginx -> /os/ : %{http_code}\n" http://127.0.0.1/os/
echo "Done. Open https://aric.mfu.ac.th/os/ and sign in with nacha.cho@mfu.ac.th"

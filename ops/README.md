# ARIC OS

The ARIC back office: partner pain points, kanban tasks, and people management with onboarding.
It runs on the ARIC VM behind nginx.

| URL | What it is |
|---|---|
| https://aric.mfu.ac.th/os/ | Back office (`web/app.html`, Thai first with an EN toggle) |
| https://aric.mfu.ac.th/os/api/... | JSON API (`server.py`) |
| https://aric.mfu.ac.th/challenge/ | Public stakeholder form (`../challenge/index.html`), which posts to `/os/api/public/challenge` |

## How it works

- **Stack:** Python 3 standard library and SQLite only, with no packages to install.
  - The database is `/var/lib/aric-os/aric.db`.
  - A nightly backup goes to `/var/backups/aric-os`, which keeps 14 copies.
- **Sign-in:** Firebase Authentication (Google provider, project `aric-os`, public config in `firebase_config.json`).
  - The server re-checks every token with Google and accepts only `@mfu.ac.th` and `@lamduan.mfu.ac.th`.
  - It then sets its own HttpOnly session cookie.
- **Roles:**
  - **Admins** (`ARIC_ADMIN_EMAILS`) see everything.
  - **Everyone else is staff.** Staff see the boards they belong to (with every task on them), tasks assigned to
    them, pain points assigned to them, and their own record and onboarding.
  - Every rule is enforced on the server.
- **Updates:** code is updated by the existing `aric-sync.timer` (`git pull` every 5 minutes). `server.py` exits when
  its own file changes, and systemd restarts it.

## Install (once, on the VM)

```
sudo bash /var/www/aric/ops/deploy/install.sh
```

The script does the following:
- creates the data and backup folders and `/etc/aric-os.env`;
- installs the nginx site, keeping a backup and restoring it if `nginx -t` fails;
- installs the systemd units;
- seeds the starting data, if the database is empty;
- starts the service.

## Local development

```
ARIC_DB=./data/aric.db ARIC_PORT=8091 ARIC_DEV_LOGIN=1 ARIC_SECURE_COOKIE=0 ARIC_DEV_STATIC=.. python3 server.py --seed-initial
ARIC_DB=./data/aric.db ARIC_PORT=8091 ARIC_DEV_LOGIN=1 ARIC_SECURE_COOKIE=0 ARIC_DEV_STATIC=.. python3 server.py
```

Then open http://127.0.0.1:8091/os/ and sign in with the "Dev" box. Dev login exists only when `ARIC_DEV_LOGIN=1`, which
production never sets.

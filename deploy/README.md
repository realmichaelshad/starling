# Deployment and backups

| File | Purpose |
|---|---|
| `Dockerfile` | The image. Targets: `runtime` (default, deployed) and `test` (runtime plus both test suites) |
| `docker-compose.yml` | Local runs and the clean-checkout test |
| `fly.toml` | Reference production deployment on Fly.io |

Every command below runs from the repository root.

## Build and test

1. Lock the Python dependencies (needs Docker; commit the result):
   `scripts/lock-requirements.sh` (Linux, macOS) or `scripts\lock-requirements.ps1` (Windows).
   Add `--upgrade` / `-Upgrade` to move to the newest versions `backend/requirements.in` allows.
2. Run both test suites in the image (as root, so the privilege-separation tests run too):
   `docker compose -f deploy/docker-compose.yml --profile test run --rm --build starling-test`
3. Run locally: `cp .env.example .env`, fill it in, then
   `docker compose -f deploy/docker-compose.yml up --build` and open http://127.0.0.1:8765.

## Production rules

- **One machine, one gateway worker.** Per-account slots and the engine pool live in the gateway's
  memory and SQLite has one writer. Never scale out without a documented redesign.
- **No access logs.** The supervisor starts uvicorn with `--no-access-log`. Do not add uvicorn
  options or `UVICORN_*` variables to the compose file, the Dockerfile, `fly.toml` or the machine's
  environment: the supervisor alone controls workers, proxy headers and logging.
- `/data` must be an encrypted volume. On Fly.io, volumes are encrypted by default but live on one
  host, so backups are mandatory.
- Secrets go in the platform's secret store (`fly secrets set ...`), never in `fly.toml` or the image.
- `BACKUP_PASSPHRASE`: a long random value (production requires at least 16 characters), with a
  copy kept outside the server. Without it no archive can be read; there is no recovery.

## Backups

What is saved: the control database (`/data/control/control.db`) and every customer folder
(`/data/tenants/<n>/`). SQLite databases are copied with SQLite's backup API, so archives are
consistent while the service runs. The archive is a compressed tar with a manifest (sizes and
SHA-256 of every file, schema and app version), encrypted with AES-256-GCM under a key derived from
the passphrase with scrypt. Tampering, truncation and a wrong passphrase are all detected.

The backup runs as root inside the supervisor, but root never opens a database in a folder another
user can write to: each SQLite file is read by a short helper process running as its owner (the
gateway user for the control database, the customer's engine user for customer data), and a customer
file not owned by that customer's engine user is left out of the archive and listed as skipped.

Nightly: the supervisor writes `BACKUP_DIR/starling-backup-<UTC time>.sbk` at 03:00 UTC, verifies it,
and keeps the newest archive of each of the last 7 days, 4 weeks and 3 months. If the machine was down
at 03:00, a catch-up backup runs 5 minutes after start. The outcome of every scheduled run is stored
in the control database (`service_state` key `backup:last`) for the owner dashboard.

Commands (inside the container: `docker compose -f deploy/docker-compose.yml exec starling ...` or
`fly ssh console -C "..."`):

    python -m starling_server.cli backup-status        last scheduled run and newest archives (exit 1 if it failed)
    python -m starling_server.cli backup
    python -m starling_server.cli backup-verify --archive /data/backups/<archive>.sbk
    python -m starling_server.cli backup-prune --dry-run

**Copy archives off the machine** at least weekly. An archive on the same volume does not survive
losing the volume:

- Docker: `docker compose -f deploy/docker-compose.yml cp starling:/data/backups ./offsite-archives`
- Fly.io: `fly ssh sftp get /data/backups/<archive>.sbk`

Check each off-site copy with `backup-verify` on a machine that has the passphrase.

## Restore

The restore verifies the whole archive first and refuses a target that already holds data unless
`--force` is given. With `--force`, existing `control/` and `tenants/` move aside (never deleted): to
`<target>.pre-restore-<time>`, or to `/data/.pre-restore-<time>` when the target is a volume root. Run
as root with `STARLING_ISOLATION=os` so owners and modes are reset to what the processes expect.

### Docker

1. `docker compose -f deploy/docker-compose.yml stop starling`
2. Restore with a one-off container on the same volume (mount the archive's folder if it is not
   already on the volume):

       docker compose -f deploy/docker-compose.yml run --rm --no-deps starling \
         python -m starling_server.cli restore --archive /data/backups/<archive>.sbk --target /data --force

3. `docker compose -f deploy/docker-compose.yml start starling`, check the site, then delete the
   moved-aside folder.

### Fly.io (maintenance mode)

With `STARLING_MAINTENANCE=1` the supervisor starts nothing (no gateway, services, engines or nightly
backup) and keeps the machine running, so the volume can be restored in place. The site is down until
step 4.

1. Enter maintenance mode: `fly secrets set STARLING_MAINTENANCE=1`. Setting a secret restarts the
   Machine ([secrets](https://fly.io/docs/apps/secrets/)).
2. If the archive is not already on the volume, upload it:
   `fly ssh sftp put ./<archive>.sbk /data/backups/<archive>.sbk` (check `fly ssh sftp put --help`).
3. Restore:
   `fly ssh console -C "python -m starling_server.cli restore --archive /data/backups/<archive>.sbk --target /data --force"`.
   The passphrase comes from the `BACKUP_PASSPHRASE` secret; if the session does not see it, open an
   interactive `fly ssh console` and run the command there, and it asks for the passphrase.
4. Leave maintenance mode: `fly secrets unset STARLING_MAINTENANCE`, which restarts the Machine
   normally (if it does not, run `fly machine restart`).
5. Check the site and `python -m starling_server.cli backup-status`, then delete
   `/data/.pre-restore-<time>` once the restore is confirmed.

## Client IP addresses on Fly.io (`STARLING_TRUSTED_PROXIES`)

What Fly.io's documentation says (checked 2026-09-14):

- `Fly-Client-IP` is "the IP address of the client from the perspective of Fly Proxy", and in
  `X-Forwarded-For` "the last address (rightmost) in this list will be a shared or dedicated IP address
  assigned to your app"; the header "must be treated with caution to avoid spoofing attempts"
  ([request headers](https://fly.io/docs/networking/request-headers/)).
- Neither the request headers page, the [public network services](https://fly.io/docs/networking/services/)
  page nor the [Fly Proxy reference](https://fly.io/docs/reference/fly-proxy/) says which address Fly Proxy
  connects to a Machine from. A community post suggests `172.16.0.0/16` but calls it unconfirmed, so it is
  not used here.
- Apps in the same organization can reach each other over the private 6PN network, whose addresses start
  with `fdaa` ([private networking](https://fly.io/docs/networking/private-networking/)).

How Starling uses the setting: the gateway reads `X-Forwarded-For` only when the TCP peer is listed in
`STARLING_TRUSTED_PROXIES` (addresses or CIDR ranges), and walks it from the right, skipping listed
addresses (the supervisor gives uvicorn the same list). The list therefore needs:

1. the address(es) Fly Proxy connects from, and
2. every public IP of the app (`fly ips list`). Fly adds the app's own IP as the rightmost entry; if it is
   missing, every request appears to come from that address and all customers share one rate limit.

Never list anything broader (`*`, `0.0.0.0/0` or all of `fdaa::/16`): other apps in the organization
could then forge client addresses.

Check on the first deploy:

1. Deploy with `STARLING_TRUSTED_PROXIES` unset. The header is ignored, so for this short check every
   request counts against the proxy's address.
2. `fly ips list` and note the app's IPv4 and IPv6 addresses.
3. Load the site a few times, then run `fly ssh console -C "python -m starling_server.cli proxy-peers"`.
   It lists the peer addresses connected to port 8080 (`127.0.0.1` is the container's own health check).
4. Set the peers (or their narrowest range) and the app's addresses, for example in `fly.toml` `[env]`:
   `STARLING_TRUSTED_PROXIES = "<peer addresses>,<app IPv4>,<app IPv6>"`, then `fly deploy`.
5. Sign in and open the session list under Account, Security: it must show your own network, not a Fly
   address. Repeat the check if the Machine moves to another host or region.

## Check on the first Docker and Fly.io run

- `backend/requirements.lock` exists and installs with `--require-hashes --only-binary=:all:` (every
  package has a Linux x86_64 wheel for CPython 3.11).
- The base image provides `tini` from apt and has no users or groups with ids 10001, 10002, 10010 or 10020.
- `compileall` succeeds over `backend/` and `web/`.
- The gateway listens on `0.0.0.0:8080` and `/healthz` answers 200 for `Host: 127.0.0.1:8080` (the Docker
  health check) and for Fly's checks.
- `no-new-privileges` in the compose file does not block the supervisor from switching users.
- The `test` target: the lock-to-constraints `sed` works on the real lock, both suites collect, and the
  privilege-separation tests run (they are skipped when not root on Linux).
- `/tmp` in the container has room for the largest customer database (the snapshot helper uses it).
- `docker compose` is 2.24 or newer (long `env_file` syntax).
- Fly.io: `fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile` finds the Dockerfile,
  `kill_timeout = "30s"` is accepted, `STARLING_TRUSTED_PROXIES` is set by the check above, and the
  maintenance-mode restore has been rehearsed once.
- The release gate passes on the unpacked image filesystem: `python scripts/check_release.py <unpacked image root>`.

# Deploying to the VPS

Stack: Docker image (app + GDAL wheels) + Caddy sidecar for automatic HTTPS.
Target: Hostinger VPS, Ubuntu 24.04 + Docker template.

## 0. One-time VPS + DNS setup (you)

1. Reimage the VPS to **Ubuntu 24.04 with Docker** (Hostinger template), or
   plain Ubuntu 24.04 then install Docker Engine + the compose plugin.
2. Point your domain at the VPS public IP:
   - DNS **A record**: `garmin.example.com  ->  <VPS_IP>`
   - Wait for it to resolve (`dig +short garmin.example.com`).
3. Open firewall ports **80** and **443** (Let's Encrypt + HTTPS).

## 1. Get the code onto the VPS

**Option A — git (recommended):** push this repo to a remote, then on the VPS:

```bash
git clone <REMOTE_URL> garmin_gui && cd garmin_gui
```

**Option B — copy from your dev box:**

```bash
rsync -av --exclude .venv --exclude data --exclude legacy/__pycache__ \
  ./ user@<VPS_IP>:~/garmin_gui/
```

## 2. Configure secrets

```bash
cd ~/garmin_gui
cp .env.example .env
python3 -c "import secrets;print('GARMIN_GUI_SECRET='+secrets.token_urlsafe(32))"
# edit .env: set DOMAIN, GARMIN_GUI_PASSWORD, paste the generated SECRET
nano .env
```

## 3. Build and start

```bash
docker compose up -d --build       # first build ~ several minutes
docker compose logs -f app         # watch startup
docker compose ps                  # app should be healthy
```

Caddy obtains a Let's Encrypt cert automatically on first HTTPS hit
(needs DNS resolving + ports 80/443 reachable).

## 4. Verify

```bash
curl -s https://garmin.example.com/healthz        # {"ok":true}
```

Open `https://garmin.example.com` → password prompt → empty map (no tracks
yet — that's expected on a fresh deploy).

## 5. Get survey data onto the server

The map is empty until an inventory exists. Until the upload UI (Phase 3b)
lands, stage RSD files into the data volume and run a tracks job:

```bash
# copy RSDs into the named volume's rsd dir
docker compose cp ./some_rsd_folder app:/data/rsd/
# build the inventory (authenticated; use the browser, or curl with a cookie)
```

Then trigger a `tracks` job over `/data/rsd` from the UI/API; the map will
populate. Mosaic runs land under `/data/runs/<job>/`.

## Backfilling metadata + weather

Older mosaic runs (pre-Phase 7) and historical imports don't have weather
or survey-metadata in their job results. Catch them up at any time:

```bash
docker compose exec app python -m server.backfill --dry-run   # plan only
docker compose exec app python -m server.backfill             # apply
docker compose exec app python -m server.backfill --force     # refetch all
```

Weather backfill needs the **track inventory geojson** uploaded (so each
RSD has a lat/lon centroid) and the survey date in the RSD filename.
Open-Meteo fetches are cached under `/data/weather/`.

## Operations

- **Update:** `git pull` (or rsync) then `docker compose up -d --build`.
- **Data/retention:** everything is in the `garmin_data` volume → `/data`
  (`rsd/`, `runs/`, `mosaics/`, `tracks/`, `garmin_gui.sqlite`). This is the
  one thing to back up and watch against the 400 GB disk.
- **Logs:** `docker compose logs -f app` / `caddy`.
- **Restart:** `docker compose restart app`.
- Sessions survive restarts only if `GARMIN_GUI_SECRET` is set in `.env`.
- Caddy works without `ACME_EMAIL`; set it only for expiry notices.

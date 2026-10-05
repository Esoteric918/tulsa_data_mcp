# tulsa_data_mcp (CountyLayer)

## Status: hosting shut down (October 2026)

CountyLayer's DigitalOcean hosting has been destroyed (droplet and managed Postgres).
No data was backed up; there were no real customers. The code in this repo is the only
copy that matters. The Stripe and Resend credentials used in production should be
treated as retired; create new ones when relaunching.

## Bringing it back up

How it was deployed:

- **App server:** one droplet (`s-1vcpu-2gb`, nyc1) running `server.py` as a systemd
  service under a non-root user. `server.py` listens on `MCP_HOST:MCP_PORT`
  (default `127.0.0.1:8000`) and was not exposed directly.
- **Reverse proxy / TLS:** Caddy in front of the app, with automatic Let's Encrypt
  certificates.
- **Database:** DigitalOcean Managed Postgres 18 (`db-s-1vcpu-1gb`, single node, nyc1),
  connected over SSL (`DB_SSLMODE`).
- **Firewall:** inbound allowed only for SSH (22), HTTP (80) and HTTPS (443).

To restore:

1. Create a droplet in nyc1 and a non-root user for the app. Install Python, create a
   venv, and `pip install -r requirements.txt`.
2. Create a managed Postgres 18 database in the same region, add the droplet as a
   trusted source, and apply `schema.sql`.
3. Copy `.env.example` to `.env` on the server (never commit it) and fill in new values
   for `DB_*`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `RESEND_API_KEY`,
   `PORTAL_LINK_SECRET`, `STRIPE_PORTAL_CONFIGURATION_ID` and `APP_BASE_URL`.
4. Re-run the INCOG ingestion (`python ingest.py`) to repopulate the `properties` table.
5. Create a systemd unit that runs `server.py` as the non-root user, and a Caddyfile
   that reverse-proxies your domain to `127.0.0.1:8000`.
6. Apply the firewall rules above (SSH, 80, 443 only), start the service, and point DNS
   at the droplet so Caddy can obtain a certificate.
7. Re-create the Stripe webhook endpoint and update `STRIPE_WEBHOOK_SECRET` to match.

**The Caddyfile and systemd unit were not saved** (they lived only on the droplet and
are not in this repo), so they have to be recreated.

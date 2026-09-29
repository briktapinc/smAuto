# Hostinger production deploy (Bubble Pod Studio)

This app is a **multi-tenant** FastAPI Studio: each member only sees **their own** jobs and topics. Admins see everything and own Settings / restart / audit.

## What Hostinger plan you need

Use a **VPS** (KVM / Docker), not plain shared PHP hosting. You need:

- Docker + Docker Compose, **or** Python 3.11+ with systemd
- Outbound HTTPS (OpenAI, fal, Stripe)
- A domain with HTTPS (Hostinger SSL or Cloudflare)

Gentle (lip-sync) is optional as a second container; set `GENTLE_URL` accordingly.

## Quick start (Docker)

```bash
cd /opt/bubblepod   # or your clone path
cp .env.production.example .env.production
# edit .env.production — set BUBBLEPOD_PUBLIC_BASE_URL, passwords, Stripe, API keys

docker compose -f docker-compose.prod.yml up -d --build
```

Point your Hostinger reverse proxy / Cloudflare tunnel to `http://127.0.0.1:7878`.

Stripe webhook endpoint:

`https://YOUR_DOMAIN/api/stripe/webhook`

## First login (required)

1. Open `https://YOUR_DOMAIN/`
2. Sign in with `BUBBLEPOD_USER` / `BUBBLEPOD_PASSWORD` (default was historically `admin` / `bubblepod`)
3. **Change the password** in Settings → Account (`must_change_password` is set when the default password is used)
4. Configure Stripe + membership in Settings (admin only)
5. Optional MCP: remote clients over ngrok use **HTTP Basic** from Settings → Ngrok (Basic only — not Basic+Bearer). Alternatively send a Studio JWT (`Authorization: Bearer …`) or MCP PIN (`?mcp_pin=` / `X-MCP-Pin`).

## Multi-tenant behavior

| Resource | Member | Admin |
|---|---|---|
| Jobs / library | Own `owner_id` only | All |
| Topics | Own only | All |
| Prompts | Per-user overrides | Same |
| Settings / restart / audit | Blocked | Allowed |
| Unsubscribed | Can delete & read; cannot create/edit/generate video or topics | Full |

Legacy jobs/topics without `owner_id` are assigned to the first admin on startup.

## Environment checklist

| Variable | Purpose |
|---|---|
| `BUBBLEPOD_PUBLIC_BASE_URL` | HTTPS origin → Secure cookies + Stripe return URLs |
| `BUBBLEPOD_COOKIE_SECURE=1` | Force Secure cookie flag |
| `BUBBLEPOD_CORS_ORIGINS` | Comma-separated allowed origins (required behind a custom domain) |
| `BUBBLEPOD_USER` / `PASSWORD` | Seed admin on empty members store |
| `STRIPE_*` | Membership billing |
| `OPENAI_API_KEY` / `FAL_KEY` | Production providers |
| `GENTLE_URL` | Align service |

## Security notes already in the build

- Admin-only: `PUT /api/settings`, `POST /api/admin/restart`, `GET /api/audit`, ngrok / MCP cards
- MCP HTTP auth: Studio JWT (per-member), MCP PIN, or ngrok Basic (edge + tunnel gate). Prefer Basic-only over the public URL — do not combine Basic + Bearer
- JWT `tv` (token_version) bumps on password change → old tokens die
- Signup rate-limited separately from login
- CORS no longer `*` when credentials are used

## Backups

Back up the Docker volume `bubblepod_data` (or `user_data/`) daily — it holds members, projects, topics, tokens, and settings.

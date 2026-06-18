# ARM64 Deployment Guide — SMS-First Agent for Kenya

**For:** Raspberry Pi 4/5, NVIDIA Jetson, or any ARM64 Linux device

---

## Docker on Raspberry Pi

### 1. Install Docker (if not present)
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### 2. Install Docker Compose
```bash
sudo apt-get update
sudo apt-get install -y docker-compose-plugin
```

### 3. Enable Docker on Boot
```bash
sudo systemctl enable docker
```

---

## Deploy SMS-First Agent

### 1. Clone the repo
```bash
git clone https://github.com/drwjkirkpatrick-web/SMS-first-agent.git
cd SMS-first-agent
```

### 2. Configure environment
```bash
cp .env.example .env
nano .env
# Fill in:
#   - AFRICAS_TALKING_USERNAME and API_KEY
#   - MPESA_CONSUMER_KEY and SECRET (from Safaricom Daraja portal)
#   - ADMIN_TOKEN (set a strong random string)
#   - DB_PASSWORD (change from default)
```

### 3. Build and start
```bash
docker compose up --build -d
```

### 4. Run database migrations
```bash
docker compose exec app alembic upgrade head
```

### 5. Verify
```bash
curl http://localhost:8000/health
```

---

## Raspberry Pi Performance Tuning

```bash
# Disable swap (prevents SD card wear)
sudo dphys-swapfile swapoff
sudo systemctl disable dphys-swapfile

# Mount /tmp as tmpfs (RAM disk)
echo "tmpfs /tmp tmpfs defaults,nosuid,size=512M 0 0" | sudo tee -a /etc/fstab

# Use NVMe SSD for database (if available via USB adapter)
# Mount the SSD and move Docker's data directory there:
sudo systemctl stop docker
sudo mv /var/lib/docker /mnt/ssd/docker
echo '{"data-root": "/mnt/ssd/docker"}' | sudo tee /etc/docker/daemon.json
sudo systemctl start docker
```

---

## Startup Script (auto-start on boot)

Create `/etc/systemd/system/sms-agent.service`:

```ini
[Unit]
Description=SMS-First Agent
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/pi/SMS-first-agent
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=pi

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo systemctl enable sms-agent
sudo systemctl start sms-agent
```

---

## Building ARM64 Images

The Dockerfile uses `python:3.11-slim-bookworm` which has ARM64 wheels.
No special build flags needed.

```bash
docker compose build --no-cache
```

Expected build time on Raspberry Pi 4:
- First build: ~8 minutes
- Subsequent builds (cached): ~30 seconds

---

## M-Pesa Webhook Setup (Safaricom Daraja API)

### Sandbox setup:
1. Register at [developer.safaricom.co.ke](https://developer.safaricom.co.ke)
2. Create an app → get Consumer Key + Consumer Secret
3. Go to "Apps" → your app → "M-Pesa Express" (STK Push) and "C2B"
4. Set confirmation/validation URLs:
   - Validation: `https://your-pi-ip:8000/webhooks/mpesa/c2b/validate`
   - Confirmation: `https://your-pi-ip:8000/webhooks/mpesa/c2b/confirm`
5. Set STK Push callback URL: `https://your-pi-ip:8000/webhooks/mpesa/stk/callback`

### Production:
1. Apply for M-Pesa production access via Safaricom
2. Use the same URLs with your production domain
3. Update `.env`: `MPESA_ENV=production`

---

## Africa's Talking Setup

1. Register at [africastalking.com](https://africastalking.com)
2. Create a new app → get API key
3. Set up an SMS short code or alphanumeric sender ID
4. Configure inbound SMS webhook:
   - URL: `https://your-pi-ip:8000/webhooks/africas-talking/sms`
5. Configure delivery reports webhook:
   - URL: `https://your-pi-ip:8000/webhooks/africas-talking/delivery`
6. Update `.env` with username, API key, and sender ID

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Docker won't start | `sudo systemctl start docker` |
| Database migration fails | Check `DATABASE_URL` in `.env` |
| No SMS going out | Check connectivity: `curl localhost:8000/health` |
| M-Pesa webhook not received | Check Safaricom Daraja URLs, ensure port 8000 is forwarded |
| Pi reboots during power cut | Normal — systemd auto-starts the agent on boot |
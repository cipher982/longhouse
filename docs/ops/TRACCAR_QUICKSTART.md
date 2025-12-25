# Traccar GPS Tracking - Quick Start

Your Traccar GPS server is now running on clifford! Follow these steps to complete the setup.

## Step 1: Access Web UI

Visit: **http://5.161.97.53:5055**

Login with:

- Username: `admin`
- Password: `admin`

**IMPORTANT**: Change this password immediately!

## Step 2: Add Your Device

1. In Traccar web UI, click "Devices" → "+" button
2. Name: `iPhone` (or whatever you prefer)
3. Click "Save"
4. **Note the Device ID** (you'll need this)

## Step 3: Install Tracking App

### iOS

Download "Traccar Client" from App Store

### Android

Download "Traccar Client" from Play Store

### Configure App

- **Server URL**: `http://5.161.97.53:5055`
- **Device ID**: (from Step 2)
- **Frequency**: 30 seconds (or your preference)

## Step 4: Update Credentials

Edit: `apps/zerg/backend/scripts/personal_credentials.local.json`

```json
{
  "traccar": {
    "url": "http://5.161.97.53:5055",
    "username": "admin",
    "password": "YOUR_NEW_PASSWORD",
    "device_id": "YOUR_DEVICE_ID"
  }
}
```

## Step 5: Test Connection

```bash
cd apps/zerg/backend
uv run scripts/test_traccar.py
```

You should see:

```
✅ Server reachable
✅ Devices endpoint working
✅ Positions endpoint working
✅ All tests passed!
```

## Step 6: Seed Credentials to Database

```bash
cd apps/zerg/backend
uv run scripts/seed_personal_credentials.py
```

## Step 7: Try It!

Start Jarvis and ask:

- "Where am I?"
- "What's my current location?"
- "Where am I right now?"

Jarvis will use the `get_current_location` tool to fetch your GPS position!

## Troubleshooting

**Can't login?**

- Default is admin/admin
- Try resetting via clifford SSH if needed

**No position data?**

- Make sure tracking app is running
- Check GPS permissions on your phone
- Wait 30-60 seconds for first position

**API errors?**

- Run `test_traccar.py` to diagnose
- Check credentials match web UI password
- Verify device_id is correct

## Server Management

**View logs:**

```bash
ssh clifford
sudo docker logs traccar-server
```

**Restart server:**

```bash
ssh clifford
cd /var/lib/docker/data/traccar
sudo docker compose restart
```

**Update Traccar:**

```bash
ssh clifford
cd /var/lib/docker/data/traccar
sudo docker compose pull
sudo docker compose up -d
```

## Full Documentation

See: `apps/zerg/backend/scripts/TRACCAR_SETUP.md`

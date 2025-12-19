# Traccar GPS Tracking Server Setup

**Status**: ✅ Operational on clifford
**Deployed**: 2025-12-19
**Version**: Traccar 6.11.1

## Overview

Traccar GPS tracking server is running on the clifford VPS to enable Jarvis personal tools to access location data. This replaces the previous unreliable local setup at 192.168.1.5:5055.

## Deployment Details

### Server Location

- **Host**: clifford (5.161.97.53)
- **Container**: `traccar-server`
- **Web UI**: http://5.161.97.53:5055
- **API Base**: http://5.161.97.53:5055/api

### Docker Configuration

```yaml
# Location: /tmp/traccar-compose.yml on clifford
services:
  traccar:
    image: traccar/traccar:latest
    container_name: traccar-server
    restart: unless-stopped
    ports:
      - "5055:8082" # Web UI
      - "5000-5010:5000-5010" # GPS device protocols
    volumes:
      - /var/lib/docker/data/traccar/data:/opt/traccar/data
      - /var/lib/docker/data/traccar/logs:/opt/traccar/logs
    environment:
      - TZ=America/New_York
```

### Data Storage

- **Database**: `/var/lib/docker/data/traccar/data/database.mv.db` (H2)
- **Logs**: `/var/lib/docker/data/traccar/logs/`
- **Backup Strategy**: Included in clifford's Kopia backups to cube server

### Ports

- **5055**: Web UI and REST API (publicly accessible)
- **5000-5010**: GPS device protocols (various)
  - 5000: Generic
  - 5001: OsmAnd
  - 5002: Traccar Client
  - etc. (see Traccar docs for full list)

## Initial Setup

### 1. Access Web UI

Visit http://5.161.97.53:5055

Default credentials:

- **Username**: admin
- **Password**: admin

**IMPORTANT**: Change the admin password immediately on first login!

### 2. Add Your Device

1. Click "Devices" → "+" (Add Device)
2. Name your device (e.g., "iPhone")
3. Note the Device ID (needed for configuration)
4. Click "Save"

### 3. Configure GPS Tracking App

**Recommended App**: Traccar Client (iOS/Android)

Settings:

- **Server URL**: http://5.161.97.53:5055
- **Device ID**: (from step 2)
- **Frequency**: 30 seconds (or as preferred)
- **Protocol**: HTTP (not HTTPS - no SSL configured yet)

Alternative apps that work:

- OsmAnd (use server port 5001)
- GPSLogger
- OverlandGPS

### 4. Test Position Reporting

After your device starts sending data:

```bash
# From laptop
curl -u admin:YOUR_PASSWORD http://5.161.97.53:5055/api/positions
```

You should see JSON with your latest position.

### 5. Update Jarvis Credentials

Edit: `/Users/davidrose/git/zerg/apps/zerg/backend/scripts/personal_credentials.local.json`

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

Then seed the credentials to the database:

```bash
cd /Users/davidrose/git/zerg/apps/zerg/backend
uv run scripts/seed_personal_credentials.py
```

## Validation

Test the connection:

```bash
cd /Users/davidrose/git/zerg/apps/zerg/backend
uv run scripts/test_traccar.py
```

Expected output:

```
✅ Server reachable
✅ Devices endpoint working
✅ Positions endpoint working
✅ All tests passed!
```

## API Usage

### Authentication

Traccar uses HTTP Basic Auth:

```python
import httpx

auth = ("admin", "password")
response = httpx.get("http://5.161.97.53:5055/api/positions", auth=auth)
```

### Key Endpoints

**Server Info** (no auth required):

```
GET /api/server
```

**List Devices**:

```
GET /api/devices
```

**Get Positions**:

```
GET /api/positions?deviceId=1
```

**Get Single Device Position**:

```
GET /api/positions?deviceId=1&limit=1
```

## Integration with Jarvis

The `get_current_location()` tool in `zerg/tools/builtin/personal_tools.py` uses:

1. Credentials from connector system (type: `traccar`)
2. HTTP Basic Auth (username:password)
3. Fetches latest position via `/api/positions`
4. Returns lat, lon, address, speed, battery

Example usage in Jarvis:

```
User: "Where am I?"
Jarvis: *calls get_current_location tool*
Jarvis: "You're at 123 Main St, San Francisco, CA"
```

## Troubleshooting

### Can't Login to Web UI

- Default credentials are `admin:admin`
- If changed and forgotten, need to reset via database:
  ```bash
  ssh clifford
  sudo docker exec -it traccar-server sh
  # Manual H2 database password reset (advanced)
  ```

### Device Not Reporting Positions

1. Check device is online and has GPS signal
2. Verify server URL in tracking app
3. Check Traccar logs:
   ```bash
   ssh clifford
   sudo docker logs traccar-server | tail -50
   ```
4. Test with curl from device's perspective

### API Returns 401 Unauthorized

- Credentials incorrect in `personal_credentials.local.json`
- Password not updated after changing in web UI
- Run `test_traccar.py` to diagnose

### No Position Data

- Device hasn't sent first position yet
- GPS signal weak/unavailable
- Check device battery saver settings
- Verify app has location permissions

## Security Considerations

### Current State

- HTTP only (no SSL/TLS)
- Public IP exposure on port 5055
- Basic Auth for API access

### Recommendations

1. **Change default admin password** (critical)
2. Create device-specific user accounts (not admin)
3. Consider setting up HTTPS via Coolify/Caddy proxy
4. Optionally restrict access to Tailscale network only
5. Enable rate limiting if experiencing abuse

### Future Improvements

- [ ] Set up HTTPS with Let's Encrypt
- [ ] Create dedicated API user (not admin)
- [ ] Add Cloudflare proxy for DDoS protection
- [ ] Configure geofencing and alerts
- [ ] Set up automatic device cleanup

## Maintenance

### Updating Traccar

```bash
ssh clifford
cd /tmp
sudo docker compose -f traccar-compose.yml pull
sudo docker compose -f traccar-compose.yml up -d
```

### Backup Verification

Database is backed up via clifford's Kopia schedule:

```bash
# Check backup status
ssh clifford
# Database location: /var/lib/docker/data/traccar/data/
```

### Logs Rotation

Traccar handles log rotation internally. Monitor disk usage:

```bash
ssh clifford
sudo du -sh /var/lib/docker/data/traccar/logs/
```

## Resources

- **Traccar Documentation**: https://www.traccar.org/documentation/
- **API Reference**: https://www.traccar.org/api-reference/
- **Client Apps**: https://www.traccar.org/client/
- **Device Protocols**: https://www.traccar.org/protocols/

## Change Log

### 2025-12-19

- Initial deployment on clifford
- Traccar 6.11.1 installed via Docker
- Public HTTP access on port 5055
- Persistent storage configured
- Integration with Jarvis personal tools
- Validation script created

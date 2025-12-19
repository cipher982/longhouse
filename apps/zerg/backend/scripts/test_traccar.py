#!/usr/bin/env python3
"""Test Traccar GPS server connection and API access.

This script validates:
1. Traccar server is reachable
2. Authentication works
3. Can fetch server info and positions
4. Device is configured (if device_id provided)

Usage:
    python test_traccar.py
    # Reads from personal_credentials.local.json

    # Or test with explicit credentials:
    python test_traccar.py --url http://REDACTED_IP:5055 --username admin --password admin
"""

import argparse
import json
import sys
from pathlib import Path

import httpx


def load_credentials():
    """Load Traccar credentials from personal_credentials.local.json."""
    creds_file = Path(__file__).parent / "personal_credentials.local.json"

    if not creds_file.exists():
        print(f"❌ Credentials file not found: {creds_file}")
        return None

    with open(creds_file) as f:
        data = json.load(f)

    if "traccar" not in data:
        print("❌ No 'traccar' section in credentials file")
        return None

    return data["traccar"]


def test_traccar(url: str, username: str, password: str, device_id: str = None):
    """Test Traccar server connection and API.

    Args:
        url: Traccar server URL (e.g. http://REDACTED_IP:5055)
        username: Traccar username
        password: Traccar password
        device_id: Optional device ID to check for

    Returns:
        True if all tests pass, False otherwise
    """
    print(f"🔍 Testing Traccar server at {url}\n")

    auth = (username, password)

    try:
        with httpx.Client(timeout=10.0) as client:
            # Test 1: Server info
            print("1️⃣  Testing server endpoint...")
            response = client.get(f"{url}/api/server", auth=auth)

            if response.status_code != 200:
                print(f"❌ Server endpoint failed: {response.status_code}")
                print(f"   Response: {response.text[:200]}")
                return False

            server_info = response.json()
            print(f"✅ Server reachable")
            print(f"   Version: {server_info.get('version')}")
            print(f"   Registration: {server_info.get('registration')}\n")

            # Test 2: Devices list
            print("2️⃣  Testing devices endpoint...")
            response = client.get(f"{url}/api/devices", auth=auth)

            if response.status_code == 401:
                print("❌ Authentication failed!")
                print("   Check username and password")
                return False

            if response.status_code != 200:
                print(f"❌ Devices endpoint failed: {response.status_code}")
                print(f"   Response: {response.text[:200]}")
                return False

            devices = response.json()
            print(f"✅ Devices endpoint working")
            print(f"   Found {len(devices)} device(s)")

            if devices:
                for device in devices:
                    print(f"   - Device ID: {device.get('id')}, Name: {device.get('name')}")
                    print(f"     Status: {device.get('status')}, Last Update: {device.get('lastUpdate')}")
            else:
                print("   ⚠️  No devices configured yet!")
                print("   Add a device in the Traccar web UI first\n")

            print()

            # Test 3: Positions
            print("3️⃣  Testing positions endpoint...")
            positions_url = f"{url}/api/positions"
            if device_id:
                positions_url += f"?deviceId={device_id}"

            response = client.get(positions_url, auth=auth)

            if response.status_code != 200:
                print(f"❌ Positions endpoint failed: {response.status_code}")
                print(f"   Response: {response.text[:200]}")
                return False

            positions = response.json()
            print(f"✅ Positions endpoint working")

            if positions:
                print(f"   Found {len(positions)} position(s)")
                for pos in positions[:3]:  # Show first 3
                    print(f"   - Device ID: {pos.get('deviceId')}")
                    print(f"     Lat: {pos.get('latitude')}, Lon: {pos.get('longitude')}")
                    print(f"     Speed: {pos.get('speed')} knots")
                    print(f"     Time: {pos.get('fixTime')}")
                    if pos.get('address'):
                        print(f"     Address: {pos.get('address')}")
            else:
                print("   ℹ️  No position data available yet")
                print("   Device needs to report its first position\n")

            print()

            # Test 4: Check specific device if provided
            if device_id:
                print(f"4️⃣  Checking device ID {device_id}...")
                device_ids = [d.get('id') for d in devices]

                if int(device_id) in device_ids:
                    print(f"✅ Device {device_id} found in system")
                else:
                    print(f"❌ Device {device_id} NOT found!")
                    print(f"   Available device IDs: {device_ids}")
                    return False

            print("\n✅ All tests passed!")
            return True

    except httpx.TimeoutException:
        print(f"❌ Connection timeout to {url}")
        print("   Is the server running? Check firewall/network access")
        return False

    except httpx.ConnectError as e:
        print(f"❌ Connection failed: {e}")
        print("   Is the server running? Check URL and port")
        return False

    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description="Test Traccar GPS server connection")
    parser.add_argument("--url", help="Traccar server URL")
    parser.add_argument("--username", help="Traccar username")
    parser.add_argument("--password", help="Traccar password")
    parser.add_argument("--device-id", help="Device ID to check")

    args = parser.parse_args()

    # Load credentials
    if args.url and args.username and args.password:
        # Use command line args
        url = args.url
        username = args.username
        password = args.password
        device_id = args.device_id
    else:
        # Load from file
        creds = load_credentials()
        if not creds:
            sys.exit(1)

        url = creds.get("url")
        username = creds.get("username", "admin")
        password = creds.get("password")
        device_id = creds.get("device_id")

        if not url or not password:
            print("❌ URL and password required in credentials file")
            sys.exit(1)

    # Run tests
    success = test_traccar(url, username, password, device_id)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

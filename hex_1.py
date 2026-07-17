#!/usr/bin/env python3
"""
Hexoskin BLE discovery scan.

Scans for BLE devices, connects to the chosen one, and prints a table of every
GATT service + characteristic with its properties (notify / read / write / etc.),
then writes the same table to gatt_map.csv.

Usage:
    python hexoskin_discover.py            # scans, lists devices, you pick one
    python hexoskin_discover.py --name Hexoskin   # auto-pick by name substring
    python hexoskin_discover.py --address AA:BB:CC:DD:EE:FF   # connect directly

Requires: pip install bleak
Works on Windows, macOS, Linux, and Raspberry Pi 5.
"""

import argparse
import asyncio
import csv
import sys

from bleak import BleakClient, BleakScanner


async def scan_and_choose(name_filter=None, address=None):
    if address:
        return address

    print("Scanning for BLE devices (5 s)...\n")
    # return_adv=True gives {address: (BLEDevice, AdvertisementData)}.
    # RSSI lives on AdvertisementData in modern bleak, not on BLEDevice.
    discovered = await BleakScanner.discover(timeout=5.0, return_adv=True)
    if not discovered:
        print("No BLE devices found. Is Bluetooth on? Is the shirt powered and worn?")
        sys.exit(1)

    # Build a list of (device, rssi), strongest signal first.
    pairs = [(dev, adv.rssi) for dev, adv in discovered.values()]
    pairs.sort(key=lambda p: p[1] if p[1] is not None else -999, reverse=True)

    if name_filter:
        for dev, _ in pairs:
            if dev.name and name_filter.lower() in dev.name.lower():
                print(f"Auto-selected: {dev.name} [{dev.address}]")
                return dev.address
        print(f"No device matched name '{name_filter}'. Falling back to manual pick.\n")

    print(f"{'#':>2}  {'NAME':<28}  {'ADDRESS':<20}  RSSI")
    print("-" * 62)
    for i, (dev, rssi) in enumerate(pairs):
        print(f"{i:>2}  {(dev.name or '(unknown)'):<28}  {dev.address:<20}  {rssi}")

    choice = input("\nEnter the number of the shirt: ").strip()
    try:
        return pairs[int(choice)][0].address
    except (ValueError, IndexError):
        print("Invalid choice.")
        sys.exit(1)


async def discover(address):
    print(f"\nConnecting to {address} ...")
    async with BleakClient(address) as client:
        if not client.is_connected:
            print("Failed to connect.")
            return
        print("Connected. Reading GATT table...\n")

        rows = []
        header = f"{'SERVICE':<38}  {'CHAR UUID':<38}  {'PROPERTIES'}"
        print(header)
        print("-" * len(header))

        for service in client.services:
            for char in service.characteristics:
                props = ",".join(char.properties)
                notify = "notify" in char.properties or "indicate" in char.properties
                print(f"{service.uuid:<38}  {char.uuid:<38}  {props}")
                rows.append({
                    "service_uuid": service.uuid,
                    "service_desc": service.description,
                    "char_uuid": char.uuid,
                    "char_desc": char.description,
                    "properties": props,
                    "notify_capable": notify,
                })

        with open("gatt_map.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        notify_chars = [r for r in rows if r["notify_capable"]]
        print(f"\nWrote {len(rows)} characteristics to gatt_map.csv")
        print(f"{len(notify_chars)} of them support notify/indicate (streamable):\n")
        for r in notify_chars:
            print(f"  {r['char_uuid']}  ({r['char_desc']})")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="substring of device name to auto-select")
    ap.add_argument("--address", help="connect directly to this BLE address")
    args = ap.parse_args()

    address = await scan_and_choose(args.name, args.address)
    await discover(address)


if __name__ == "__main__":
    asyncio.run(main())

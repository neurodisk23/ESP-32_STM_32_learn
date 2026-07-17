#!/usr/bin/env python3
"""
Hexoskin BLE stream sniffer.

Subscribes to Heart Rate + the three proprietary notify characteristics found
during discovery, and logs every notification with:
    timestamp, characteristic label, payload length, packet rate, raw hex

Goal: identify what each unknown stream carries, by correlating byte activity
with physical actions (breathe, hold breath, move, sit still).

It also OPTIONALLY writes a probe byte to the control characteristic
(1da084fc) to test whether that arms a heavier data stream (e.g. raw ECG).
Toggle with --arm.

Everything is logged to sniff_log.csv for offline analysis.

Usage:
    python hexoskin_sniff.py --address 00:A0:50:3C:55:9E
    python hexoskin_sniff.py --address 00:A0:50:3C:55:9E --arm
    python hexoskin_sniff.py --address 00:A0:50:3C:55:9E --seconds 120

Requires: pip install bleak
"""

import argparse
import asyncio
import csv
import time
from collections import defaultdict
from datetime import datetime

from bleak import BleakClient

# ---- Characteristics from your discovery scan -----------------------------
HR_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"      # Heart Rate Measurement
BATT_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"    # Battery Level

# Proprietary notify streams (the unknowns we're identifying)
PROP_A = "9bc730c3-8cc0-4d87-85bc-573d6304403c"        # service 3b55c581-...
PROP_B = "1da084f1-e765-4bba-ae78-ec747d0dabfa"        # service 1da084fd-... (has control char)
PROP_C = "75246a26-237a-4863-aca6-09b639344f43"        # service bdc750c7-...

# Control / write characteristic paired with PROP_B — may "arm" a data stream
CONTROL_CHAR = "1da084fc-e765-4bba-ae78-ec747d0dabfa"

# Human-readable labels for the log
LABELS = {
    HR_CHAR: "HR(0x2A37)",
    BATT_CHAR: "BATT(0x2A19)",
    PROP_A: "PROP_A(9bc730c3)",
    PROP_B: "PROP_B(1da084f1)",
    PROP_C: "PROP_C(75246a26)",
}

SUBSCRIBE = [HR_CHAR, BATT_CHAR, PROP_A, PROP_B, PROP_C]

# ---- Runtime stats --------------------------------------------------------
counts = defaultdict(int)          # packets per characteristic
last_len = {}                      # last payload length seen
first_ts = {}                      # first packet time per char
csv_rows = []                      # buffered rows for the CSV


def handler_factory(uuid):
    label = LABELS.get(uuid, uuid)

    def handler(_char, data: bytearray):
        now = time.time()
        counts[label] += 1
        last_len[label] = len(data)
        first_ts.setdefault(label, now)
        hexstr = data.hex()
        csv_rows.append({
            "wall_time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "epoch": f"{now:.3f}",
            "char": label,
            "len": len(data),
            "hex": hexstr,
        })
        # Live console: keep it light, one line per packet but truncated hex
        preview = hexstr if len(hexstr) <= 48 else hexstr[:48] + "..."
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {label:<18} "
              f"len={len(data):>3}  {preview}")

    return handler


async def try_arm(client):
    """Write a probe byte to the control characteristic to see if it
    unlocks a heavier stream. This is exploratory — safe single-byte writes."""
    print("\n--- ARM: probing control characteristic 1da084fc ---")
    for probe in (b"\x01", b"\x02", b"\xff"):
        try:
            await client.write_gatt_char(CONTROL_CHAR, probe, response=True)
            print(f"  wrote {probe.hex()} to control char OK")
            await asyncio.sleep(3)  # watch for stream to come alive
        except Exception as e:
            print(f"  write {probe.hex()} failed: {e}")
    print("--- ARM probe done ---\n")


async def run(address, seconds, arm):
    print(f"Connecting to {address} ...")
    async with BleakClient(address, timeout=30.0) as client:
        if not client.is_connected:
            print("Failed to connect.")
            return
        print("Connected. Subscribing to notify streams...\n")

        subscribed = []
        for uuid in SUBSCRIBE:
            try:
                await client.start_notify(uuid, handler_factory(uuid))
                subscribed.append(uuid)
                print(f"  subscribed: {LABELS.get(uuid, uuid)}")
            except Exception as e:
                print(f"  could NOT subscribe {LABELS.get(uuid, uuid)}: {e}")

        print(f"\nCollecting for {seconds}s. NOW DO THIS, in order:")
        print("  1. Sit completely still           (~15s)")
        print("  2. Breathe slow and deep           (~15s)")
        print("  3. Hold your breath                (~10s)")
        print("  4. Move / walk / tap the sensor    (~15s)")
        print("Correlate the timing with which stream's bytes change.\n")

        if arm:
            await try_arm(client)

        await asyncio.sleep(seconds)

        for uuid in subscribed:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass

    # ---- Summary ----
    print("\n" + "=" * 60)
    print("SUMMARY — packet counts, rate, and payload size per stream")
    print("=" * 60)
    print(f"{'STREAM':<20} {'PACKETS':>8} {'RATE(Hz)':>9} {'BYTES':>7}")
    print("-" * 60)
    for label in sorted(counts):
        n = counts[label]
        span = max(time.time() - first_ts[label], 1e-6)
        rate = n / span
        print(f"{label:<20} {n:>8} {rate:>9.1f} {last_len.get(label, 0):>7}")

    if csv_rows:
        with open("sniff_log.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            w.writeheader()
            w.writerows(csv_rows)
        print(f"\nWrote {len(csv_rows)} packets to sniff_log.csv")

    print("\nHOW TO READ THIS:")
    print("  * High rate (>50 Hz), large/steady payload  -> raw ECG")
    print("  * Mid rate, bytes swing when you breathe     -> respiration")
    print("  * Changes only when you move                 -> accelerometer")
    print("  * ~1 Hz, small payload                       -> a derived metric")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True, help="shirt BLE address")
    ap.add_argument("--seconds", type=int, default=75, help="capture duration")
    ap.add_argument("--arm", action="store_true",
                    help="probe the control characteristic to unlock streams")
    args = ap.parse_args()
    asyncio.run(run(args.address, args.seconds, args.arm))


if __name__ == "__main__":
    main()

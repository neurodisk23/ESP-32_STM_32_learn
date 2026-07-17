#!/usr/bin/env python3
"""
Hexoskin Smart — long-run resilient logger.

Designed for unattended multi-hour / overnight logging.

Robustness features:
  * Auto-reconnect: on any BLE dropout it retries forever (backoff) and
    resumes logging. The spec's 300 ms supervision timeout makes brief
    dropouts normal over long runs.
  * Fresh numbered file on every (re)connect:
        HEX_<YYYYMMDD>_seg<NNN>_<stream>.csv
    'seg' increments each time a new connection is established, so you can
    always see where a dropout happened.
  * Daily rotation: the date in the filename rolls over at midnight, and a
    new segment starts, so each calendar day is its own set of files.
  * Periodic flush (every FLUSH_SECS) so a power loss never costs more than
    a few seconds of buffered data.
  * Decoders per official Hexoskin Bluetooth Smart Spec (HR, RR/HRV,
    respiration insp/exp). PROP_B / PROP_C logged as structured raw.

Outputs go to ./logs/<YYYYMMDD>/ .

Usage:
    python hexoskin_logger_long.py --address 00:A0:50:3C:55:9E
    python hexoskin_logger_long.py --address 00:A0:50:3C:55:9E --hours 8
    # runs until Ctrl-C if --hours not given

Requires: pip install bleak
"""

import argparse
import asyncio
import csv
import os
import signal
import struct
import time
from datetime import datetime, timezone

from bleak import BleakClient, BleakScanner

# ---- Characteristics (discovery + official spec) --------------------------
HR_CHAR   = "00002a37-0000-1000-8000-00805f9b34fb"   # Heart Rate Measurement
BATT_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"   # Battery Level
RESP_CHAR = "9bc730c3-8cc0-4d87-85bc-573d6304403c"   # Respiration Rate (spec 2.4)
PROP_B    = "1da084f1-e765-4bba-ae78-ec747d0dabfa"   # activity/step/cadence (5.8 Hz)
PROP_C    = "75246a26-237a-4863-aca6-09b639344f43"   # derived metric

SUBS = [HR_CHAR, RESP_CHAR, PROP_B, PROP_C, BATT_CHAR]
LABELS = {
    HR_CHAR: "heartrate", RESP_CHAR: "respiration",
    PROP_B: "activity_B", PROP_C: "metric_C", BATT_CHAR: "battery",
}

FLUSH_SECS = 5           # flush all files at least this often
RECONNECT_BACKOFF = [2, 5, 10, 20, 30]   # seconds, then holds at last value


# ---------------------------------------------------------------------------
class SegmentWriter:
    """One set of CSV files for a single connection segment on a given day."""

    def __init__(self, date_str, seg_num):
        self.date_str = date_str
        self.seg = seg_num
        self.t0 = time.time()
        self.dir = os.path.join("logs", date_str)
        os.makedirs(self.dir, exist_ok=True)
        self.files, self.writers = {}, {}
        prefix = f"HEX_{date_str}_seg{seg_num:03d}"
        self._open("events", prefix, ["iso_time", "t_rel", "stream", "len", "hex", "decoded"])
        self._open("heartrate", prefix, ["iso_time", "t_rel", "hr_bpm", "rr_intervals_ms"])
        self._open("respiration", prefix, ["iso_time", "t_rel", "breathing_rate", "insp_s", "exp_s"])
        self._open("activity", prefix, ["iso_time", "t_rel", "stream", "tag", "values_dec", "hex"])
        self._open("battery", prefix, ["iso_time", "t_rel", "battery_pct"])
        self.last_flush = time.time()
        print(f"  -> writing segment files: {prefix}_*.csv")

    def _open(self, name, prefix, header):
        path = os.path.join(self.dir, f"{prefix}_{name}.csv")
        f = open(path, "w", newline="")
        w = csv.writer(f); w.writerow(header)
        self.files[name] = f; self.writers[name] = w

    def stamp(self):
        now = time.time()
        iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        return iso, round(now - self.t0, 3)

    def maybe_flush(self):
        if time.time() - self.last_flush >= FLUSH_SECS:
            for f in self.files.values():
                f.flush(); os.fsync(f.fileno())
            self.last_flush = time.time()

    def close(self):
        for f in self.files.values():
            try:
                f.flush(); os.fsync(f.fileno()); f.close()
            except Exception:
                pass


# ---- decoders (validated against spec worked examples) --------------------
def decode_hr(data):
    flags = data[0]
    hr16 = flags & 0x01
    rr_present = (flags >> 4) & 0x01
    idx = 1
    if hr16:
        hr = struct.unpack_from("<H", data, idx)[0]; idx += 2
    else:
        hr = data[idx]; idx += 1
    rr_ms = []
    if rr_present:
        while idx + 1 < len(data):
            rr = struct.unpack_from("<H", data, idx)[0]; idx += 2
            rr_ms.append(round(rr / 1024.0 * 1000.0, 1))
    return hr, rr_ms


def decode_resp(data):
    flags = data[0]
    rate16 = flags & 0x01
    ie_present = (flags >> 1) & 0x01
    first_is_exp = (flags >> 2) & 0x01
    idx = 1
    if rate16:
        rate = struct.unpack_from("<H", data, idx)[0]; idx += 2
    else:
        rate = data[idx]; idx += 1
    insp = exp = None
    if ie_present:
        vals = []
        while idx + 1 < len(data):
            v = struct.unpack_from("<H", data, idx)[0]; idx += 2
            vals.append(round(v / 32.0, 3))
        if vals:
            if first_is_exp:
                exp = vals[0]; insp = vals[1] if len(vals) > 1 else None
            else:
                insp = vals[0]; exp = vals[1] if len(vals) > 1 else None
    return rate, insp, exp


def handle_packet(seg, uuid, data):
    iso, trel = seg.stamp()
    label = LABELS.get(uuid, uuid)
    hexs = bytes(data).hex()
    decoded = ""
    try:
        if uuid == HR_CHAR:
            hr, rr = decode_hr(data)
            decoded = f"hr={hr} rr_ms={rr}"
            seg.writers["heartrate"].writerow([iso, trel, hr, ";".join(map(str, rr))])
        elif uuid == RESP_CHAR:
            rate, insp, exp = decode_resp(data)
            decoded = f"br={rate} insp={insp} exp={exp}"
            seg.writers["respiration"].writerow([iso, trel, rate, insp, exp])
        elif uuid == BATT_CHAR:
            pct = data[0]
            decoded = f"battery={pct}%"
            seg.writers["battery"].writerow([iso, trel, pct])
        elif uuid in (PROP_B, PROP_C):
            tag = data[0] if len(data) else None
            words = [struct.unpack_from("<H", data, i)[0]
                     for i in range(1, len(data) - 1, 2)]
            decoded = f"tag={tag} words={words}"
            seg.writers["activity"].writerow(
                [iso, trel, label, tag, ";".join(map(str, words)), hexs])
    except Exception as e:
        decoded = f"decode_error:{e}"
    seg.writers["events"].writerow([iso, trel, label, len(data), hexs, decoded])
    seg.maybe_flush()


# ---------------------------------------------------------------------------
async def connect_once(address, seg_holder, seg_counter, stop_evt):
    """One connection lifecycle. Returns when disconnected or stop set."""
    disconnected = asyncio.Event()

    def on_disc(_c):
        print("  ! device disconnected")
        disconnected.set()

    async with BleakClient(address, timeout=30.0, disconnected_callback=on_disc) as client:
        if not client.is_connected:
            return
        # New segment on every successful connect
        date_str = datetime.now().strftime("%Y%m%d")
        seg_counter[0] += 1
        seg = SegmentWriter(date_str, seg_counter[0])
        seg_holder[0] = seg
        print(f"Connected. Segment {seg_counter[0]:03d} started.")

        for uuid in SUBS:
            try:
                await client.start_notify(uuid, (lambda u: (lambda _c, d: handle_packet(seg_holder[0], u, d)))(uuid))
            except Exception as e:
                print(f"  subscribe failed {LABELS.get(uuid, uuid)}: {e}")
        try:
            b = await client.read_gatt_char(BATT_CHAR)
            handle_packet(seg, BATT_CHAR, b)
        except Exception:
            pass

        # Wait until disconnect, stop, or midnight (to force daily rotation)
        while not (disconnected.is_set() or stop_evt.is_set()):
            await asyncio.sleep(1)
            if datetime.now().strftime("%Y%m%d") != date_str:
                print("  midnight rollover -> new day/segment")
                break

        for uuid in SUBS:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass
        seg.close()
        print(f"  segment {seg_counter[0]:03d} closed.")


async def run(address, hours):
    stop_evt = asyncio.Event()

    def _sig(*_):
        print("\nStopping (Ctrl-C)...")
        stop_evt.set()
    signal.signal(signal.SIGINT, _sig)

    deadline = time.time() + hours * 3600 if hours else None
    seg_counter = [0]
    seg_holder = [None]
    backoff_i = 0

    print(f"Long-run logger. Target: {address}")
    print("Auto-reconnect ON. Daily rotation ON. Fresh numbered file per reconnect.\n")

    while not stop_evt.is_set():
        if deadline and time.time() >= deadline:
            print("Reached time limit.")
            break
        try:
            await connect_once(address, seg_holder, seg_counter, stop_evt)
            backoff_i = 0  # reset after a clean connection
        except Exception as e:
            print(f"  connect error: {e}")

        if stop_evt.is_set():
            break
        if deadline and time.time() >= deadline:
            break

        wait = RECONNECT_BACKOFF[min(backoff_i, len(RECONNECT_BACKOFF) - 1)]
        backoff_i += 1
        print(f"  reconnecting in {wait}s ...")
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass

    if seg_holder[0]:
        seg_holder[0].close()
    print("\nLogger stopped. Check ./logs/<date>/ for your files.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True, help="shirt BLE address")
    ap.add_argument("--hours", type=float, default=0,
                    help="stop after this many hours (0 = until Ctrl-C)")
    args = ap.parse_args()
    asyncio.run(run(args.address, args.hours))


if __name__ == "__main__":
    main()

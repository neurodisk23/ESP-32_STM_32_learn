#!/usr/bin/env python3
"""
Sleepa.ai — RPi UART Logger v2.0
═══════════════════════════════════════════════════════════
Receives JSON packets from Clock Hub via UART
Handles two packet types:
  audio       → 20Hz features, buf_id chunked → CSV 1
  environment → 10min sensor snapshot        → CSV 2
  pressure    → 60min pressure reading       → CSV 2

Additional responsibilities:
  → Sends NTP time sync to hub every hour via UART
  → Reassembles buf_id chunks before writing audio row
  → Handles partial/missing chunks gracefully
  → Auto-creates new CSV at midnight
  → Prints live stats every 60 seconds
═══════════════════════════════════════════════════════════
"""

import serial
import json
import csv
import os
import time
import ntplib
import threading
from datetime import datetime
from collections import defaultdict

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════
UART_PORT           = '/dev/ttyAMA0'
BAUD_RATE           = 115200
DATA_DIR            = '/home/hv/sleepa_data'
NTP_SERVER          = 'pool.ntp.org'
NTP_SYNC_INTERVAL_S = 3600          # sync every hour
STATS_INTERVAL_S    = 60            # print stats every 60s
CHUNK_TIMEOUT_S     = 5             # drop incomplete buf after 5s
LOG_RAW             = True          # log raw JSON to file

# ═══════════════════════════════════════════════════════════
# CSV COLUMNS
# ═══════════════════════════════════════════════════════════
AUDIO_COLUMNS = [
    'rpi_timestamp',
    'rtc_timestamp',
    'buf_id',
    'chunk',
    'sample_ts_ms',
    'rms_db',
    'zcr',
    'transient_score',
    'peak_freq_hz',
    'spectral_centroid_hz',
    'spectral_flux',
    'band_sublow_db',
    'band_low_db',
    'band_midlow_db',
    'band_midhigh_db',
    'band_high_db',
    'snore_score',
    'snore_confirmed',
    'calibrated',
    'session_date'
]

ENV_COLUMNS = [
    'rpi_timestamp',
    'rtc_timestamp',
    'event_type',
    'fw_version',          # NEW — firmware version that produced this row
    'lux',
    'temperature',
    'humidity',
    'pressure',
    'lux_valid',           # NEW — sensor actually responded
    'th_valid',            # NEW — temp/humidity sensor responded
    'pressure_valid',      # NEW
    'sensor_status',       # NEW — raw fault bitmask
    'sensor_faults',       # NEW — human-readable fault list
    'lux_alert',
    'temp_alert',
    'hum_alert',
    'heartbeat',
    'calib_valid',
    'calib_ts',
    'calib_noise_db',
    'calib_stage',
    'batt_v',              # NEW — explicit battery columns
    'batt_pct',
    'batt_state',
    'free_heap',           # NEW — heap monitoring
    'min_heap',            # NEW
    'session_date'
]

# ── Sensor fault bitmask decode (must match firmware #defines) ──
SENSOR_FAULT_BITS = {
    0x01: 'BH1750_FAIL',
    0x02: 'AHT10_FAIL',
    0x04: 'BMP280_FAIL',
}

def decode_sensor_faults(status: int) -> str:
    """Turn the firmware bitmask into a readable string."""
    if not status:
        return 'OK'
    faults = [name for bit, name in SENSOR_FAULT_BITS.items()
              if status & bit]
    return '|'.join(faults) if faults else f'UNKNOWN(0x{status:02X})'

RAW_COLUMNS = [
    'rpi_timestamp',
    'packet_type',
    'raw_json',
    'session_date'
]

# ═══════════════════════════════════════════════════════════
# CSV MANAGER
# ═══════════════════════════════════════════════════════════
class CSVManager:
    def __init__(self, session_date: str):
        self.session_date  = session_date
        self.audio_file    = None
        self.env_file      = None
        self.raw_file      = None
        self.audio_writer  = None
        self.env_writer    = None
        self.raw_writer    = None
        self._open_files(session_date)

    def _open_files(self, date: str):
        os.makedirs(DATA_DIR, exist_ok=True)
        self._open_audio(date)
        self._open_env(date)
        self._open_raw(date)

    def _open_audio(self, date: str):
        path = f"{DATA_DIR}/sleepa_audio_{date}.csv"
        new  = not os.path.exists(path)
        self.audio_file   = open(path, 'a', newline='')
        self.audio_writer = csv.DictWriter(
            self.audio_file, fieldnames=AUDIO_COLUMNS)
        if new:
            self.audio_writer.writeheader()
            self.audio_file.flush()
        print(f"Audio CSV  : {path} ({'new' if new else 'append'})")

    def _open_env(self, date: str):
        path = f"{DATA_DIR}/sleepa_env_{date}.csv"
        new  = not os.path.exists(path)
        self.env_file   = open(path, 'a', newline='')
        self.env_writer = csv.DictWriter(
            self.env_file, fieldnames=ENV_COLUMNS)
        if new:
            self.env_writer.writeheader()
            self.env_file.flush()
        print(f"Env CSV    : {path} ({'new' if new else 'append'})")

    def _open_raw(self, date: str):
        if not LOG_RAW:
            return
        path = f"{DATA_DIR}/sleepa_raw_{date}.jsonl"
        self.raw_file   = open(path, 'a')
        print(f"Raw log    : {path}")

    def check_date_rollover(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self.session_date:
            self.close()
            self.session_date = today
            self._open_files(today)
            print(f"\nDate rollover → new session: {today}")

    def write_audio(self, row: dict):
        self.audio_writer.writerow(row)
        self.audio_file.flush()

    def write_env(self, row: dict):
        self.env_writer.writerow(row)
        self.env_file.flush()

    def write_raw(self, packet_type: str, raw: str):
        if not LOG_RAW or not self.raw_file:
            return
        entry = json.dumps({
            'ts'   : datetime.now().isoformat(),
            'type' : packet_type,
            'raw'  : raw
        })
        self.raw_file.write(entry + '\n')
        self.raw_file.flush()

    def close(self):
        for f in [self.audio_file, self.env_file, self.raw_file]:
            if f:
                try:
                    f.close()
                except Exception:
                    pass

# ═══════════════════════════════════════════════════════════
# CHUNK REASSEMBLER
# ═══════════════════════════════════════════════════════════
class ChunkReassembler:
    """
    Collects chunks for each buf_id.
    When all chunks arrive → yields list of samples.
    Drops incomplete buf_ids after CHUNK_TIMEOUT_S.
    """
    def __init__(self):
        self.buffers  = {}   # buf_id → {'total': N, 'chunks': {}, 'ts': time}
        self.complete = []   # list of completed (buf_id, rtc, cal, samples)

    def add_chunk(self, data: dict):
        buf_id  = data.get('buf_id', -1)
        chunk   = data.get('chunk', 1)
        total   = data.get('total', 5)
        rtc     = data.get('rtc', '1970-01-01T00:00:00')
        cal     = data.get('cal', False)
        samples = data.get('s', [])

        if buf_id not in self.buffers:
            self.buffers[buf_id] = {
                'total'  : total,
                'chunks' : {},
                'rtc'    : rtc,
                'cal'    : cal,
                'ts'     : time.time()
            }

        self.buffers[buf_id]['chunks'][chunk] = samples

        # Check if complete
        buf = self.buffers[buf_id]
        if len(buf['chunks']) >= buf['total']:
            all_samples = []
            for i in range(1, buf['total'] + 1):
                all_samples.extend(buf['chunks'].get(i, []))
            self.complete.append((
                buf_id, buf['rtc'], buf['cal'], all_samples))
            del self.buffers[buf_id]

    def get_complete(self):
        done = self.complete[:]
        self.complete = []
        return done

    def expire_old(self):
        now     = time.time()
        expired = [k for k, v in self.buffers.items()
                   if now - v['ts'] > CHUNK_TIMEOUT_S]
        for k in expired:
            buf = self.buffers[k]
            print(f"  [WARN] buf_id:{k} expired "
                  f"({len(buf['chunks'])}/{buf['total']} chunks)")
            del self.buffers[k]

# ═══════════════════════════════════════════════════════════
# NTP TIME SYNC
# ═══════════════════════════════════════════════════════════
class NTPSync:
    def __init__(self, ser: serial.Serial):
        self.ser        = ser
        self.last_sync  = 0
        self.ntp_time   = None

    def sync_now(self):
        try:
            client   = ntplib.NTPClient()
            response = client.request(NTP_SERVER, version=3)
            t        = datetime.fromtimestamp(response.tx_time)
            self.ntp_time = t
            self.last_sync = time.time()

            # Build time sync packet for hub
            msg = json.dumps({
                "type"  : "ntp_sync",
                "year"  : t.year % 100,
                "month" : t.month,
                "day"   : t.day,
                "hour"  : t.hour,
                "min"   : t.minute,
                "sec"   : t.second
            }) + '\n'

            self.ser.write(msg.encode('utf-8'))
            print(f"\n[NTP] Synced: {t.isoformat()} → sent to hub")
            return True

        except Exception as e:
            print(f"\n[NTP] Sync failed: {e}")
            return False

    def check_and_sync(self):
        if time.time() - self.last_sync >= NTP_SYNC_INTERVAL_S:
            self.sync_now()

# ═══════════════════════════════════════════════════════════
# PACKET PARSER
# ═══════════════════════════════════════════════════════════
def parse_packet(raw: str) -> dict | None:
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        if raw.strip():
            print(f"  [WARN] Malformed: {raw[:60]}")
        return None

# ═══════════════════════════════════════════════════════════
# ROW BUILDERS
# ═══════════════════════════════════════════════════════════
def build_audio_rows(buf_id: int, rtc: str,
                     cal: bool, samples: list,
                     session_date: str) -> list:
    rows = []
    now  = datetime.now().isoformat()
    for i, s in enumerate(samples):
        rows.append({
            'rpi_timestamp'      : now,
            'rtc_timestamp'      : rtc,
            'buf_id'             : buf_id,
            'chunk'              : i // 4 + 1,
            'sample_ts_ms'       : s.get('t',   0),
            'rms_db'             : round(s.get('rms', 0), 2),
            'zcr'                : round(s.get('zcr', 0), 4),
            'transient_score'    : round(s.get('tr',  0), 3),
            'peak_freq_hz'       : round(s.get('pf',  0), 2),
            'spectral_centroid_hz': round(s.get('sc', 0), 2),
            'spectral_flux'      : round(s.get('sf',  0), 6),
            'band_sublow_db'     : round(s.get('bsl', 0), 2),
            'band_low_db'        : round(s.get('bl',  0), 2),
            'band_midlow_db'     : round(s.get('bml', 0), 2),
            'band_midhigh_db'    : round(s.get('bmh', 0), 2),
            'band_high_db'       : round(s.get('bh',  0), 2),
            'snore_score'        : s.get('ss',  0),
            'snore_confirmed'    : s.get('sn',  False),
            'calibrated'         : cal,
            'session_date'       : session_date
        })
    return rows

def build_env_row(data: dict, session_date: str) -> dict:
    now = datetime.now().isoformat()
    sensor_status = data.get('sensor_status', 0)
    return {
        'rpi_timestamp'  : now,
        'rtc_timestamp'  : data.get('rtc',          ''),
        'event_type'     : data.get('type',          'environment'),
        'fw_version'     : data.get('fw',            'unknown'),
        'lux'            : round(data.get('lux',     0), 2),
        'temperature'    : round(data.get('temp',    0), 2),
        'humidity'       : round(data.get('humidity',0), 2),
        'pressure'       : round(data.get('pressure',0), 2),
        'lux_valid'      : data.get('lux_valid',     True),
        'th_valid'       : data.get('th_valid',      True),
        'pressure_valid' : data.get('pressure_valid',True),
        'sensor_status'  : sensor_status,
        'sensor_faults'  : decode_sensor_faults(sensor_status),
        'lux_alert'      : data.get('lux_alert',     False),
        'temp_alert'     : data.get('temp_alert',    False),
        'hum_alert'      : data.get('hum_alert',     False),
        'heartbeat'      : data.get('heartbeat',     False),
        'calib_valid'    : data.get('calib_valid',   False),
        'calib_ts'       : data.get('calib_ts',      ''),
        'calib_noise_db' : round(data.get('calib_noise_db', 0), 1),
        'calib_stage'    : data.get('calib_stage',   0),
        'batt_v'         : round(data.get('batt_v',  0), 2),
        'batt_pct'       : round(data.get('batt_pct',0), 0),
        'batt_state'     : data.get('batt_state',    0),
        'free_heap'      : data.get('free_heap',     0),
        'min_heap'       : data.get('min_heap',      0),
        'session_date'   : session_date
    }

# ═══════════════════════════════════════════════════════════
# SESSION STATS
# ═══════════════════════════════════════════════════════════
class SessionStats:
    def __init__(self):
        self.audio_rows       = 0
        self.env_rows         = 0
        self.pressure_rows    = 0
        self.snore_confirmed  = 0
        self.lux_alerts       = 0
        self.temp_alerts      = 0
        self.chunks_received  = 0
        self.bufs_complete    = 0
        self.bufs_expired     = 0
        # NEW — data integrity tracking
        self.sensor_fault_events = 0
        self.last_sensor_faults  = 'OK'
        self.fw_version          = 'unknown'
        self.first_heap          = None
        self.last_heap           = None
        self.min_heap_seen       = None
        self.last_batt_v         = 0.0
        self.last_batt_pct       = 0.0
        self.last_print       = time.time()
        self.start_time       = time.time()

    def update_health(self, data: dict):
        """Track firmware/sensor/heap health from a heartbeat."""
        self.fw_version = data.get('fw', self.fw_version)

        status = data.get('sensor_status', 0)
        faults = decode_sensor_faults(status)
        if faults != 'OK' and faults != self.last_sensor_faults:
            self.sensor_fault_events += 1
            print(f"\n  ⚠️  SENSOR FAULT DETECTED: {faults} "
                  f"(status=0x{status:02X})")
        self.last_sensor_faults = faults

        heap = data.get('free_heap', 0)
        if heap:
            if self.first_heap is None:
                self.first_heap = heap
            self.last_heap = heap
        min_heap = data.get('min_heap', 0)
        if min_heap:
            self.min_heap_seen = min_heap

        self.last_batt_v   = data.get('batt_v', self.last_batt_v)
        self.last_batt_pct = data.get('batt_pct', self.last_batt_pct)

    def print_if_due(self, csv: CSVManager):
        if time.time() - self.last_print < STATS_INTERVAL_S:
            return
        elapsed = int(time.time() - self.start_time)
        hrs     = elapsed // 3600
        mins    = (elapsed % 3600) // 60
        secs    = elapsed % 60
        print(f"\n{'─'*50}")
        print(f"  Session: {hrs:02d}:{mins:02d}:{secs:02d} | "
              f"Date: {csv.session_date} | FW: {self.fw_version}")
        print(f"  Audio rows    : {self.audio_rows}")
        print(f"  Env rows      : {self.env_rows}")
        print(f"  Pressure rows : {self.pressure_rows}")
        print(f"  Bufs complete : {self.bufs_complete}")
        print(f"  Bufs expired  : {self.bufs_expired}")
        print(f"  Snore events  : {self.snore_confirmed}")
        print(f"  Lux alerts    : {self.lux_alerts}")
        print(f"  Temp alerts   : {self.temp_alerts}")
        # ── Data integrity block ──
        print(f"  {'·'*46}")
        sensor_line = self.last_sensor_faults
        flag = '' if sensor_line == 'OK' else '  ⚠️'
        print(f"  Sensors now   : {sensor_line}{flag}")
        print(f"  Fault events  : {self.sensor_fault_events}")
        # heap trend — flag a possible leak
        if self.first_heap and self.last_heap:
            drift = self.first_heap - self.last_heap
            trend = '↓ LEAK?' if drift > 5000 else 'stable'
            print(f"  Heap          : {self.last_heap} bytes "
                  f"(start {self.first_heap}, Δ{-drift:+d}, {trend})")
        if self.min_heap_seen:
            print(f"  Min heap ever : {self.min_heap_seen} bytes")
        if self.last_batt_v:
            print(f"  Battery       : {self.last_batt_v:.2f}V "
                  f"({self.last_batt_pct:.0f}%)")
        print(f"{'─'*50}\n")
        self.last_print = time.time()

# ═══════════════════════════════════════════════════════════
# SERIAL CONNECTION
# ═══════════════════════════════════════════════════════════
def connect_serial() -> serial.Serial:
    while True:
        try:
            ser = serial.Serial(
                port        = UART_PORT,
                baudrate    = BAUD_RATE,
                timeout     = 2
            )
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            print(f"UART       : {UART_PORT} @ {BAUD_RATE} baud ✓")
            return ser
        except Exception as e:
            print(f"UART failed: {e} — retrying in 3s...")
            time.sleep(3)

# ═══════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════╗")
    print("║   Sleepa.ai UART Logger v2.0         ║")
    print("║   Dual CSV | Chunk reassembly        ║")
    print("║   NTP sync | Live stats              ║")
    print("╚══════════════════════════════════════╝\n")

    session_date = datetime.now().strftime('%Y-%m-%d')
    csv_mgr      = CSVManager(session_date)
    reassembler  = ChunkReassembler()
    stats        = SessionStats()
    ser          = connect_serial()
    ntp          = NTPSync(ser)

    # Initial NTP sync
    print("\n[NTP] Attempting initial sync...")
    ntp.sync_now()

    print("\nWaiting for packets...\n")

    while True:
        try:
            # ── Read line from hub ─────────────────────────
            raw = ser.readline().decode('utf-8', errors='ignore')
            if not raw.strip():
                # Idle — do maintenance tasks
                reassembler.expire_old()
                stats.print_if_due(csv_mgr)
                ntp.check_and_sync()
                csv_mgr.check_date_rollover()
                continue

            # ── Parse JSON ─────────────────────────────────
            data = parse_packet(raw)
            if data is None:
                continue

            pkt_type = data.get('type', 'unknown')
            now_str  = datetime.now().isoformat()

            # ── Route by packet type ───────────────────────

            # AUDIO packet — chunk based
            if pkt_type == 'audio':
                stats.chunks_received += 1
                csv_mgr.write_raw('audio', raw.strip())
                reassembler.add_chunk(data)

                # Process completed buffers
                for buf_id, rtc, cal, samples in \
                        reassembler.get_complete():
                    stats.bufs_complete += 1
                    rows = build_audio_rows(
                        buf_id, rtc, cal, samples,
                        csv_mgr.session_date)
                    for row in rows:
                        csv_mgr.write_audio(row)
                        stats.audio_rows += 1
                        if row['snore_confirmed']:
                            stats.snore_confirmed += 1

                    # Print summary for this buffer
                    if samples:
                        avg_rms  = sum(s.get('rms',0)
                                       for s in samples) / len(samples)
                        avg_sc   = sum(s.get('sc', 0)
                                       for s in samples) / len(samples)
                        snores   = sum(1 for s in samples
                                       if s.get('sn', False))
                        print(f"[{now_str}] AUDIO "
                              f"buf:{buf_id:4d} "
                              f"samples:{len(samples):2d} "
                              f"rms:{avg_rms:5.1f}dB "
                              f"centroid:{avg_sc:6.1f}Hz "
                              f"snore:{snores}")

            # ENVIRONMENT + HEARTBEAT packet
            elif pkt_type == 'environment':
                row = build_env_row(data, csv_mgr.session_date)
                csv_mgr.write_env(row)
                csv_mgr.write_raw('environment', raw.strip())
                stats.env_rows += 1
                stats.update_health(data)          # NEW — health tracking
                if data.get('lux_alert'):  stats.lux_alerts  += 1
                if data.get('temp_alert'): stats.temp_alerts += 1

                # Sanity-check calibration noise floor.
                # With SPL calibration it should sit ~30–60 dB.
                # Values outside this hint at a bad calibration
                # or a mic/scaling issue worth investigating.
                noise = data.get('calib_noise_db', 0)
                if data.get('calib_valid') and (noise < 20 or noise > 90):
                    print(f"  ⚠️ Calibration noise {noise:.1f}dB looks "
                          f"unusual (expected ~30–60 dB SPL) — "
                          f"consider re-calibrating")

                # flag invalid sensor readings inline
                faults = row['sensor_faults']
                fault_str = '' if faults == 'OK' else f"  ⚠️ {faults}"
                lux_str  = f"{row['lux']:6.1f}" if row['lux_valid'] else "  DEAD"
                th_str   = (f"temp:{row['temperature']:5.1f}°C "
                            f"hum:{row['humidity']:5.1f}%"
                            if row['th_valid'] else "temp/hum: DEAD")

                print(f"[{now_str}] ENV "
                      f"lux:{lux_str} {th_str} "
                      f"batt:{row['batt_v']:.2f}V "
                      f"heap:{row['free_heap']} "
                      f"fw:{row['fw_version']}{fault_str}")

            # PRESSURE packet
            elif pkt_type == 'pressure':
                # Reuse the env-row builder so ALL columns exist,
                # then mark it as a pressure event.
                p_valid = data.get('pressure_valid', True)
                row = build_env_row(
                    {
                        'type'           : 'pressure',
                        'rtc'            : data.get('rtc', ''),
                        'fw'             : data.get('fw', 'unknown'),
                        'pressure'       : data.get('pressure', 0),
                        'pressure_valid' : p_valid,
                    },
                    csv_mgr.session_date)
                csv_mgr.write_env(row)
                csv_mgr.write_raw('pressure', raw.strip())
                stats.pressure_rows += 1
                p_str = (f"{row['pressure']} hPa"
                         if p_valid else "DEAD")
                print(f"[{now_str}] PRESSURE {p_str}")

            # AMBIENT BATTERY ALERT (sent on state change)
            elif pkt_type == 'ambient_battery':
                status = data.get('status', 'unknown')
                bv     = data.get('batt_v', 0)
                bp     = data.get('batt_pct', 0)
                csv_mgr.write_raw('ambient_battery', raw.strip())
                print(f"\n[{now_str}] 🔋 BATTERY {status.upper()}: "
                      f"{bv:.2f}V ({bp:.0f}%) — ambient unit\n")

            # HUB STATUS packet
            elif pkt_type == 'hub_status':
                rtc = data.get('rtc', '')
                print(f"[{now_str}] HUB_STATUS rtc:{rtc}")

            # PONG response
            elif pkt_type == 'pong':
                print(f"[{now_str}] PONG from hub "
                      f"rtc:{data.get('rtc','')}")

            else:
                print(f"[{now_str}] UNKNOWN type:{pkt_type}")
                csv_mgr.write_raw('unknown', raw.strip())

            # Periodic maintenance
            stats.print_if_due(csv_mgr)
            ntp.check_and_sync()
            csv_mgr.check_date_rollover()

        except serial.SerialException:
            print("\nUART disconnected — reconnecting...")
            ser.close()
            time.sleep(2)
            ser      = connect_serial()
            ntp.ser  = ser

        except KeyboardInterrupt:
            print(f"\n\nStopping logger...")
            print(f"Audio rows logged   : {stats.audio_rows}")
            print(f"Env rows logged     : {stats.env_rows}")
            print(f"Snore events        : {stats.snore_confirmed}")
            print(f"Buffers completed   : {stats.bufs_complete}")
            print(f"Data saved to       : {DATA_DIR}")
            ser.close()
            csv_mgr.close()
            break

        except Exception as e:
            print(f"Error: {e}")
            continue

if __name__ == '__main__':
    main()

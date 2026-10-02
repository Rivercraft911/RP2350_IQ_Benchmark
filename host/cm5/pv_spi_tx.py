#!/usr/bin/env python3
"""PV-SPI v1 reference sender for the CM5 / Pi 5 (docs/pv-spi-spec.md).

  pv_spi_tx.py --vector                          print the spec's test vector
  pv_spi_tx.py --pattern --count 10000 --hz 1e6  deterministic test stream
  pv_spi_tx.py --file capture.ts --hz 20e6       a TS file, 7 packets per message
  pv_spi_tx.py --udp 230.10.0.1:1234 --hz 20e6   forward the E200 TS feed
  pv_spi_tx.py --faults 200 --hz 20e6            malformed messages between good ones

Needs spidev (dtparam=spi=on) and gpiod v1 or v2 (or gpiozero) for READY. Prints a JSON summary
with crc_chain (CRC-32 over the CRC fields of all messages sent); the RP2350 reports the same value
over the messages it accepted, so equal values mean nothing was lost, altered or reordered.
"""
import argparse
import json
import socket
import struct
import sys
import time
import zlib

MAGIC, VERSION, NOP, TS_DATA = 0x5650, 1, 0, 1
MSG_BYTES, PAYLOAD = 1332, 1316
GAP_S = 10e-6                     # CS_N high time, and READY valid after CS_N rises (spec)


def message(seq: int, payload: bytes, mtype: int = TS_DATA) -> bytes:
    """One 1332-byte PV-SPI v1 message: 12-byte header, payload + zero pad, CRC-32."""
    if len(payload) % 188 or len(payload) > PAYLOAD:
        raise ValueError("payload must be n x 188 bytes, n <= 7")
    body = struct.pack("<HBBHHI", MAGIC, VERSION, mtype, seq & 0xFFFF, len(payload), 0)
    body += payload + bytes(PAYLOAD - len(payload))
    return body + struct.pack("<I", zlib.crc32(body))


def pattern_packet(k: int) -> bytes:
    """Test packet k: PID 0x100, CC = k mod 16, payload ((k mod 112) + j) mod 256.
    Repeats every 112 packets = 16 messages, as the firmware's on-chip emulator does."""
    return bytes([0x47, 0x01, 0x00, 0x10 | (k & 15)]) + bytes(((k % 112) + j) & 0xFF for j in range(184))


_PATTERN = {}


def pattern_payload(m: int) -> bytes:
    """Payload of pattern message m (period 16 messages, cached so the sender keeps up)."""
    if m % 16 not in _PATTERN:
        _PATTERN[m % 16] = b"".join(pattern_packet(7 * (m % 16) + i) for i in range(7))
    return _PATTERN[m % 16]


class Ready:
    """READY input via gpiod v2, gpiod v1 or gpiozero, whichever is installed."""

    def __init__(self, pin: int, chip: str | None):
        self.pin, self.get = pin, None
        try:
            import gpiod
            if hasattr(gpiod, "request_lines"):                       # libgpiod v2
                from gpiod.line import Direction, Value
                path = chip or self._find_chip(gpiod)
                req = gpiod.request_lines(path, consumer="pv_spi_tx",
                                          config={pin: gpiod.LineSettings(direction=Direction.INPUT)})
                self.get = lambda: req.get_value(pin) == Value.ACTIVE
            else:                                                     # libgpiod v1
                c = gpiod.Chip(chip or "gpiochip4")
                line = c.get_line(pin)
                line.request(consumer="pv_spi_tx", type=gpiod.LINE_REQ_DIR_IN)
                self.get = lambda: line.get_value() == 1
        except Exception:
            from gpiozero import DigitalInputDevice
            dev = DigitalInputDevice(pin, pull_up=None, active_state=True)
            self.get = lambda: dev.value == 1

    @staticmethod
    def _find_chip(gpiod):
        import glob
        for p in sorted(glob.glob("/dev/gpiochip*")):
            try:
                with gpiod.Chip(p) as c:
                    if "rp1" in c.get_info().label:
                        return p
            except OSError:
                pass
        return "/dev/gpiochip0"

    def wait(self, timeout: float) -> float:
        """Block until READY is high; return seconds waited (raise on timeout)."""
        if self.get():
            return 0.0
        t0 = time.monotonic()
        while not self.get():
            if time.monotonic() - t0 > timeout:
                raise TimeoutError("READY stayed low")
            time.sleep(20e-6)
        return time.monotonic() - t0


# --faults: each is sent once, between runs of good messages. Messages the RP2350 drops reuse the
# next sequence number, so only seq_skip counts as lost.
FAULTS = ("long+1", "long+4", "short-1", "bad_crc", "bad_hdr", "nop", "seq_skip")
FAULT_EXPECT = dict(bad_hdr=1, bad_crc=1, bad_sync=0, lost=1, short=1, long=1, overflows=0, nop=1)


def fault_frames(k: int):
    """Yield (bytes to send, accepted as TS): k good messages, then each fault and k more."""
    seq = m = 0
    for fault in (None,) + FAULTS:
        msg = message(seq, pattern_payload(m))
        if fault == "long+1":              # 8 extra bits: accepted, and must not shift later messages
            yield msg + bytes(1), True
            seq, m = seq + 1, m + 1
        elif fault == "long+4":
            yield msg + bytes(4), False
        elif fault == "short-1":
            yield msg[:-1], False
        elif fault == "bad_crc":
            yield msg[:20] + bytes([msg[20] ^ 1]) + msg[21:], False
        elif fault == "bad_hdr":
            yield bytes(2) + msg[2:], False
        elif fault == "nop":
            yield message(seq, b"", NOP), False
            seq += 1
        elif fault == "seq_skip":
            seq += 1
        for _ in range(k):
            yield message(seq, pattern_payload(m)), True
            seq, m = seq + 1, m + 1


def frames(a):
    """Yield (bytes to send, accepted as TS by the RP2350)."""
    if a.faults is not None:
        yield from fault_frames(a.faults)
        return
    seq = 0
    for payload in sources(a):
        if payload:
            yield message(seq, payload), True
            seq += 1


def sources(a):
    """Yield TS payloads of n x 188 bytes, n <= 7."""
    if a.pattern:
        for m in range(a.count):
            yield pattern_payload(m)
    elif a.file:
        with open(a.file, "rb") as f:
            while chunk := f.read(PAYLOAD):
                yield chunk[: len(chunk) // 188 * 188]
    elif a.udp:
        host, port = a.udp.split(":")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        s.bind(("", int(port)))
        mreq = struct.pack("4s4s", socket.inet_aton(host), socket.inet_aton("0.0.0.0"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        while True:
            d = s.recv(2048)
            for i in range(0, len(d) // 188 * 188, PAYLOAD):          # datagrams > 1316 B are split
                yield d[i:i + PAYLOAD][: (min(len(d) - i, PAYLOAD)) // 188 * 188]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--vector", action="store_true")
    src.add_argument("--pattern", action="store_true")
    src.add_argument("--file")
    src.add_argument("--udp", help="multicast group:port, e.g. 230.10.0.1:1234")
    src.add_argument("--faults", type=int, metavar="K", help="K good messages around each fault")
    ap.add_argument("--count", type=int, default=10000, help="pattern messages")
    ap.add_argument("--hz", type=float, default=1e6, help="SCK frequency")
    ap.add_argument("--bus", type=int, default=0)
    ap.add_argument("--cs", type=int, default=0)
    ap.add_argument("--ready", type=int, default=25, help="READY GPIO (BCM number)")
    ap.add_argument("--gpiochip", help="gpiochip path (default: the RP1 chip)")
    a = ap.parse_args()

    if a.vector:
        ts = bytes([0x47, 0x01, 0x00, 0x10]) + bytes(i & 0xFF for i in range(184))
        m = message(1, ts)
        print(m.hex(" "))
        print(f"crc32 = 0x{struct.unpack('<I', m[-4:])[0]:08x}", file=sys.stderr)
        return

    import spidev
    spi = spidev.SpiDev()
    spi.open(a.bus, a.cs)
    spi.mode, spi.bits_per_word, spi.max_speed_hz = 0, 8, int(a.hz)
    ready = Ready(a.ready, a.gpiochip)
    sent, ts, crc, waits, max_wait, t0 = 0, 0, 0, 0, 0.0, time.monotonic()
    t_end = 0.0
    try:
        for frame, accepted in frames(a):
            while time.perf_counter() - t_end < GAP_S:          # CS_N high and READY valid
                pass
            w = ready.wait(1.0)
            waits += w > 0
            max_wait = max(max_wait, w)
            spi.writebytes2(frame)
            t_end = time.perf_counter()
            if accepted:
                crc = zlib.crc32(frame[MSG_BYTES - 4:MSG_BYTES], crc)
                sent, ts = sent + 1, ts + struct.unpack_from("<H", frame, 6)[0] // 188
    except KeyboardInterrupt:
        pass
    dt = time.monotonic() - t0
    print(json.dumps(dict(messages=sent, ts_packets=ts, seconds=round(dt, 3), msg_per_s=round(sent / dt, 1),
                          ts_mbps=round(ts * 188 * 8 / dt / 1e6, 3),
                          ready_waits=waits, max_ready_wait_ms=round(max_wait * 1e3, 3),
                          crc_chain=f"0x{crc:08x}", sck_hz=int(a.hz),
                          expect=FAULT_EXPECT if a.faults is not None else None)))


if __name__ == "__main__":
    main()

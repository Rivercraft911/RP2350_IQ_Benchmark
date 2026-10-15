"""Random-access TS/GCS references for the on-chip PV-SPI pattern transmitter."""
from __future__ import annotations

import functools
import sys
import zlib

import numpy as np

import iqbench as iq

sys.path[:0] = [str(iq.ROOT / "reference/dvbs2"), str(iq.ROOT / "host/cm5")]
import dvbs2 as ref
import pv_spi_tx as pv

DATA_BYTES = (43040 - 80) // 8
FRAME_SYMBOLS = 33282
PACKET_PERIOD = 16 * 7


@functools.lru_cache(maxsize=1)
def packets() -> tuple[bytes, ...]:
    raw = [pv.pattern_packet(k) for k in range(PACKET_PERIOD)]
    return tuple(bytes([ref.crc8(np.unpackbits(np.frombuffer(raw[k - 1][1:], np.uint8)))])
                 + packet[1:] for k, packet in enumerate(raw))


def bbframe(index: int, *, gcs: bool = False, rate: str = "2/3") -> np.ndarray:
    """Construct just one BBFRAME, without encoding all preceding frames."""
    data_bytes = (ref.code(rate, False).kbch - 80) // 8
    first, offset = divmod(index * data_bytes, 188)
    count = (offset + data_bytes + 187) // 188
    raw = (tuple(pv.pattern_packet(k) for k in range(PACKET_PERIOD)) if gcs else packets())
    data = bytearray(b"".join(raw[k % PACKET_PERIOD] for k in range(first, first + count)))
    if first == 0 and not gcs:
        data[0] = 0  # the first packet has no predecessor
    header = (ref.bbheader(ref.matype1(ref.GCS, 0.20), data_bytes * 8, sync=0xB9) if gcs else
              ref.bbheader(ref.matype1(ref.TS, 0.20), data_bytes * 8, 1504, 0x47,
                           (-index * data_bytes * 8) % 1504))
    return np.concatenate([header, np.unpackbits(np.frombuffer(data[offset:offset + data_bytes], np.uint8))])


def first_crcs(*, gcs: bool = False, rate: str = "2/3") -> list[int]:
    return [zlib.crc32(np.packbits(bbframe(k, gcs=gcs, rate=rate)).tobytes()) for k in range(4)]


def reference_window(symbol_cursor: int, *, span: int = 12, beta: float = 1.0,
                     gcs: bool = False, rate: str = "2/3") -> tuple[int, np.ndarray]:
    """A previous frame supplies FIR history; three more cover the sparse capture."""
    first = max(0, symbol_cursor // FRAME_SYMBOLS - 1)
    frames = [ref.plframe(ref.fecframe(bbframe(k, gcs=gcs, rate=rate), rate), rate, pilots=True)
              for k in range(first, first + 4)]
    bi, bq = (np.concatenate([frame[axis] for frame in frames]) for axis in (0, 1))
    lut = iq.m.Lut(iq.gen_coeffs.ALPHA, 4, span, beta, iq.gen_coeffs.HEADROOM_DB)
    words = iq.m.generate(iq.pack_symbols(bi, bq), lut)
    return first, np.asarray(words, dtype="<u4").view("<u2")


def packet_faults(result: dict) -> list[str]:
    stats = result.get("pv_spi", {})
    gcs = result.get("source") == 6
    rate = result.get("s2_code", "normal 2/3").split()[-1]
    data_bytes = (ref.code(rate, False).kbch - 80) // 8
    faults = [f"pv_spi.{name}" for name in
              ("lost", "bad_hdr", "bad_crc", "bad_sync", "null_packets",
               "short_msgs", "long_msgs", "overflows") if stats.get(name, -1) != 0]
    faults += [f"pv_spi.{name}" for name in ("drain_timeouts", "late_closes")
               if stats.get(name, 0) != 0]
    if stats.get("ok", 0) <= 24 or stats.get("gcs_packets" if gcs else "ts_packets", 0) <= 168:
        faults.append("PV-SPI did not consume runtime traffic")
    if gcs:
        if stats.get("bb_mode") != "gcs":
            faults.append("PV-SPI did not select generic-continuous mode")
        if stats.get("gcs_underflows", -1) != 0:
            faults.append("pv_spi.gcs_underflows")
        if stats.get("gcs_bytes") != stats.get("bbframes", 0) * data_bytes:
            faults.append("generic stream byte count differs from complete BBFRAMEs")
        if stats.get("gcs_packets") != (stats.get("gcs_bytes", 0) + 187) // 188:
            faults.append("generic stream container count differs from emitted bytes")
        if stats.get("ts_packets", -1) != 0:
            faults.append("generic stream unexpectedly used TS adaptation")
    if stats.get("runtime_bbframes", 0) <= 0 or stats.get("runtime_messages", 0) <= 0:
        faults.append("no runtime SPI arrivals and BB construction")
    crc_fields = b"".join(pv.message(k, pv.pattern_payload(k))[-4:] for k in range(16))
    accepted = int(stats.get("ok", 0))
    chain = crc_fields * (accepted // 16) + crc_fields[:4 * (accepted % 16)]
    if stats.get("crc_chain") != zlib.crc32(chain):
        faults.append("accepted SPI CRC chain differs from pattern source")
    if stats.get("bb_crc") != first_crcs(gcs=gcs, rate=rate):
        faults.append(f"{'GCS' if gcs else 'TS'} BBFRAME CRCs differ from independent reference")
    return faults

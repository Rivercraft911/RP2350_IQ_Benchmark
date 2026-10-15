"""Random-access reference for the on-chip PV-SPI pattern transmitter."""
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


def bbframe(index: int) -> np.ndarray:
    """Construct just one BBFRAME, without encoding all preceding frames."""
    first, offset = divmod(index * DATA_BYTES, 188)
    count = (offset + DATA_BYTES + 187) // 188
    data = bytearray(b"".join(packets()[k % PACKET_PERIOD] for k in range(first, first + count)))
    if first == 0:
        data[0] = 0  # the first packet has no predecessor
    header = ref.bbheader(ref.matype1(ref.TS, 0.20), DATA_BYTES * 8, 1504, 0x47,
                          (-index * DATA_BYTES * 8) % 1504)
    return np.concatenate([header, np.unpackbits(np.frombuffer(data[offset:offset + DATA_BYTES], np.uint8))])


def first_crcs() -> list[int]:
    return [zlib.crc32(np.packbits(bbframe(k)).tobytes()) for k in range(4)]


def reference_window(symbol_cursor: int) -> tuple[int, np.ndarray]:
    """A previous frame supplies FIR history; three more cover the sparse capture."""
    first = max(0, symbol_cursor // FRAME_SYMBOLS - 1)
    frames = [ref.plframe(ref.fecframe(bbframe(k), "2/3"), "2/3", pilots=True)
              for k in range(first, first + 4)]
    bi, bq = (np.concatenate([frame[axis] for frame in frames]) for axis in (0, 1))
    lut = iq.m.Lut(iq.gen_coeffs.ALPHA, 4, 12, iq.gen_coeffs.KAISER_BETA,
                   iq.gen_coeffs.HEADROOM_DB)
    words = iq.m.generate(iq.pack_symbols(bi, bq), lut)
    return first, np.asarray(words, dtype="<u4").view("<u2")


def packet_faults(result: dict) -> list[str]:
    stats = result.get("pv_spi", {})
    faults = [f"pv_spi.{name}" for name in
              ("lost", "bad_hdr", "bad_crc", "bad_sync", "null_packets",
               "short_msgs", "long_msgs", "overflows") if stats.get(name, -1) != 0]
    if stats.get("ok", 0) <= 24 or stats.get("ts_packets", 0) <= 168:
        faults.append("PV-SPI did not consume runtime traffic")
    if stats.get("runtime_bbframes", 0) <= 0 or stats.get("runtime_messages", 0) <= 0:
        faults.append("no runtime SPI arrivals and BB construction")
    crc_fields = b"".join(pv.message(k, pv.pattern_payload(k))[-4:] for k in range(16))
    accepted = int(stats.get("ok", 0))
    chain = crc_fields * (accepted // 16) + crc_fields[:4 * (accepted % 16)]
    if stats.get("crc_chain") != zlib.crc32(chain):
        faults.append("accepted SPI CRC chain differs from pattern source")
    if stats.get("bb_crc") != first_crcs():
        faults.append("TS BBFRAME CRCs differ from independent reference")
    return faults

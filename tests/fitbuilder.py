"""Write small synthetic FIT files for tests (fitparse can only read).

Only what the tests need: little-endian definition and data messages with fixed-size numeric
fields, a correct header and file CRC. Values are given already encoded (scaled and offset as
the FIT profile requires); ``None`` writes the base type's "invalid" value.

Example::

    b = FitBuilder()
    b.message(RECORD, {253: 1_000_000_000, 3: 120})
    data = b.build()
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)

# global message numbers
SESSION, LAP, RECORD = 18, 19, 20

# base type id -> (struct format, invalid value)
BASE_TYPES = {
    0x00: ("B", 0xFF),  # enum
    0x01: ("b", 0x7F),  # sint8
    0x02: ("B", 0xFF),  # uint8
    0x83: ("h", 0x7FFF),  # sint16
    0x84: ("H", 0xFFFF),  # uint16
    0x85: ("i", 0x7FFFFFFF),  # sint32
    0x86: ("I", 0xFFFFFFFF),  # uint32
}
ENUM, SINT8, UINT8, SINT16, UINT16, SINT32, UINT32 = 0x00, 0x01, 0x02, 0x83, 0x84, 0x85, 0x86

# field number -> base type, per message (only fields used in tests)
FIELD_TYPES: dict[int, dict[int, int]] = {
    RECORD: {
        253: UINT32,  # timestamp
        0: SINT32,  # position_lat (semicircles)
        1: SINT32,  # position_long
        2: UINT16,  # altitude (scale 5, offset 500)
        5: UINT32,  # distance (scale 100)
        6: UINT16,  # speed (scale 1000)
        73: UINT32,  # enhanced_speed (scale 1000)
        78: UINT32,  # enhanced_altitude (scale 5, offset 500)
        3: UINT8,  # heart_rate
        4: UINT8,  # cadence
        53: UINT8,  # fractional_cadence (scale 128)
        7: UINT16,  # power
        13: SINT8,  # temperature
        108: UINT16,  # respiration rate * 100 (unknown to fitparse 1.2)
    },
    LAP: {
        254: UINT16,  # message_index
        253: UINT32,  # timestamp
        2: UINT32,  # start_time
        7: UINT32,  # total_elapsed_time (scale 1000)
        8: UINT32,  # total_timer_time (scale 1000)
        9: UINT32,  # total_distance (scale 100)
        110: UINT32,  # enhanced_avg_speed (scale 1000)
        111: UINT32,  # enhanced_max_speed (scale 1000)
        15: UINT8,  # avg_heart_rate
        16: UINT8,  # max_heart_rate
        17: UINT8,  # avg_cadence / avg_running_cadence when sport is running
        80: UINT8,  # avg_fractional_cadence (scale 128)
        19: UINT16,  # avg_power
        21: UINT16,  # total_ascent
        22: UINT16,  # total_descent
        23: ENUM,  # intensity
        24: ENUM,  # lap_trigger
        25: ENUM,  # sport
        50: SINT8,  # avg_temperature
        71: UINT16,  # wkt_step_index
    },
    SESSION: {
        253: UINT32,  # timestamp
        2: UINT32,  # start_time
        5: ENUM,  # sport (1 running, 2 cycling)
        6: ENUM,  # sub_sport
    },
}

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)  # fmt: skip


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def fit_time(dt: datetime) -> int:
    """Seconds since the FIT epoch (1989-12-31 UTC)."""
    return int((dt - FIT_EPOCH).total_seconds())


def deg_to_semicircles(deg: float) -> int:
    return round(deg * 2**31 / 180)


class FitBuilder:
    def __init__(self) -> None:
        self._body = bytearray()
        self._definitions: dict[int, tuple[int, tuple[int, ...]]] = {}  # local -> (global, fields)

    def message(self, global_num: int, values: dict[int, int | None]) -> None:
        """Append one data message (with a definition message when its layout is new)."""
        field_nums = tuple(values)
        local = 0
        for local_num, definition in self._definitions.items():
            if definition == (global_num, field_nums):
                local = local_num
                break
        else:
            local = len(self._definitions) % 16
            self._definitions[local] = (global_num, field_nums)
            types = FIELD_TYPES[global_num]
            self._body += struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(field_nums))
            for num in field_nums:
                base = types[num]
                size = struct.calcsize(BASE_TYPES[base][0])
                self._body += struct.pack("<BBB", num, size, base)
        self._body += struct.pack("<B", local)
        types = FIELD_TYPES[global_num]
        for num, value in values.items():
            fmt, invalid = BASE_TYPES[types[num]]
            self._body += struct.pack("<" + fmt, invalid if value is None else value)

    def build(self) -> bytes:
        header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(self._body), b".FIT")
        header += struct.pack("<H", fit_crc(header))
        data = header + bytes(self._body)
        return data + struct.pack("<H", fit_crc(data))

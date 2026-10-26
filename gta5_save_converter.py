#!/usr/bin/env python3
import argparse
import re
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Tuple

PC_KEY = "46ed8d3f9435e4ec122cb2e2af97c57e4c5a8c3092c7844e11c686ff41df410f"
PS5_KEY = "1685ffa38d010f0dfe661cf9b5572c500d802648db37b9ed0f48c57342c022f5"
TITLE_SEED = 0x009DBEE6
CHKS_SEED = 0x3FAC7125

TITLE = slice(0x004, 0x104)
TITLE_HASH = slice(0x104, 0x108)
PS5_DATA_LENGTH = slice(0x108, 0x10C)
PS5_TIMESTAMP = slice(0x10C, 0x114)
PC_HEADER_SIZE = 0x108
PS5_HEADER_SIZE = 0x114
CHKS_SIZE = 0x14
MEMORY_DAT_SIZE = 0x100000
M32 = 0xFFFFFFFF


def joaat(data: bytes, seed: int) -> int:
    h = seed
    for c in data:
        h = (h + (c - 256 if c >= 128 else c)) & M32
        h = (h + (h << 10)) & M32
        h ^= h >> 6
    h = (h + (h << 3)) & M32
    h ^= h >> 11
    return (h + (h << 15)) & M32


def aes_ecb(data: bytes, key: str, decrypt: bool) -> bytes:
    if len(data) % 16:
        raise ValueError(f"AES input length {len(data):#x} is not a multiple of 16")
    args = ["openssl", "enc", "-aes-256-ecb", "-nopad", "-K", key] + (["-d"] if decrypt else [])
    return subprocess.run(args, input=data, capture_output=True, check=True).stdout


def check_title_hash(raw: bytes) -> bytes:
    title = raw[TITLE]
    if len(raw) < PS5_HEADER_SIZE or struct.unpack(">I", raw[TITLE_HASH])[0] != joaat(title, TITLE_SEED):
        raise ValueError("not a GTA V save: title hash at 0x104 does not match")
    return title


def title_text(title: bytes) -> str:
    return title.decode("utf-16le", errors="replace").split("\0")[0]


def encode_title(text: str) -> bytes:
    raw = text.encode("utf-16le")
    size = TITLE.stop - TITLE.start
    if len(raw) >= size:
        raise ValueError("title too long")
    return raw + b"\0" * (size - len(raw))


def chks(data: bytes) -> Tuple[int, int]:
    pos = data.rfind(b"CHKS")
    if pos < 0 or struct.unpack(">I", data[pos + 4:pos + 8])[0] != CHKS_SIZE:
        raise ValueError("no CHKS block in the decrypted data")
    end = pos + CHKS_SIZE
    body = bytearray(data[:end])
    body[pos + 8:pos + 16] = bytes(8)
    return end, joaat(bytes(body), CHKS_SEED)


def chks_valid(data: bytes) -> bool:
    end, checksum = chks(data)
    return data[end - 12:end - 4] == struct.pack(">II", end, checksum)


def with_chks(data: bytes) -> bytes:
    end, checksum = chks(data)
    return data[:end - 12] + struct.pack(">II", end, checksum) + data[end - 4:end]


def decrypt_pso(encrypted: bytes, key: str) -> bytes:
    data = aes_ecb(encrypted, key, decrypt=True)
    if not data.startswith(b"PSIN"):
        raise ValueError("save did not decrypt to PSO data (unsupported version, e.g. PC Legacy)")
    return data[:chks(data)[0]]


def is_pc_save(raw: bytes) -> bool:
    return len(raw) != MEMORY_DAT_SIZE and (len(raw) - PC_HEADER_SIZE) % 16 == 0


def read_pc(raw: bytes) -> Tuple[bytes, bytes]:
    title = check_title_hash(raw)
    if not is_pc_save(raw):
        raise ValueError("not a PC save: unexpected size")
    return title, decrypt_pso(raw[PC_HEADER_SIZE:], PC_KEY)


def read_ps5(raw: bytes) -> Tuple[bytes, bytes, int]:
    title = check_title_hash(raw)
    length = struct.unpack(">I", raw[PS5_DATA_LENGTH])[0] or len(raw[PS5_HEADER_SIZE:].rstrip(b"\0")) // 16 * 16
    data = decrypt_pso(raw[PS5_HEADER_SIZE:PS5_HEADER_SIZE + length], PS5_KEY)
    return title, data, struct.unpack("<Q", raw[PS5_TIMESTAMP])[0]


def build_ps5(title: bytes, data: bytes, timestamp: int) -> bytes:
    data = with_chks(data)
    data += b"p" * (-len(data) % 16)
    header = struct.pack(">I", 1) + title
    header += struct.pack(">II", joaat(title, TITLE_SEED), len(data)) + struct.pack("<Q", timestamp)
    out = header + aes_ecb(data, PS5_KEY, decrypt=False)
    if len(out) > MEMORY_DAT_SIZE:
        raise ValueError("converted save does not fit in memory.dat")
    return out + bytes(MEMORY_DAT_SIZE - len(out))


def cmd_convert(args: argparse.Namespace) -> None:
    title, data = read_pc(Path(args.pc_save).read_bytes())
    text = args.title or re.sub(r"\s+-\s+\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}$", "", title_text(title))
    Path(args.output).write_bytes(build_ps5(encode_title(text), data, int(time.time())))
    print(f"wrote {args.output}: {text!r}, {len(data):#x} bytes of save data")


def cmd_inspect(args: argparse.Namespace) -> None:
    raw = Path(args.save).read_bytes()
    if is_pc_save(raw):
        kind, (title, data), saved = "PC", read_pc(raw), ""
    else:
        title, data, timestamp = read_ps5(raw)
        kind = "PS5"
        saved = f", saved {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(timestamp))} UTC" if timestamp else ""
    print(f"{kind} save: {title_text(title)!r}{saved}")
    print(f"  data {len(data):#x} bytes, CHKS {'valid' if chks_valid(data) else 'INVALID'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert GTA V Enhanced PC story saves to PS5, or inspect a save.")
    sub = parser.add_subparsers(dest="command", required=True)
    convert = sub.add_parser("convert", help="PC SGTA5xxxx -> PS5 memory.dat")
    convert.add_argument("pc_save")
    convert.add_argument("output")
    convert.add_argument("--title", help="Load Game title (default: the PC title without its date)")
    convert.set_defaults(func=cmd_convert)
    inspect = sub.add_parser("inspect", help="show the title and check the hashes of a PC or PS5 save")
    inspect.add_argument("save")
    inspect.set_defaults(func=cmd_inspect)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()

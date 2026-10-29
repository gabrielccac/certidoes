"""Extract the pinned Chrome APKs from the official API 36 r07 SDK image, offline."""
import argparse
import gzip
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

EXPECTED = {
    "Chrome": "04052228ab8204e643cc7af6c7c53ada529fd98479d3ce6e5ff872a918e7a349",
    "TrichromeLibrary": "2a58eda6dd7239f1fcff7efeb8835f973d52539c237fc5c65aacfa833259069d",
}


def product_extents(image):
    image.seek(512)
    header = image.read(92)
    if header[:8] != b"EFI PART":
        raise ValueError("Expected the GPT SDK system.img")
    lba, count, size = struct.unpack_from("<QII", header, 72)
    if not (0 < count <= 4096 and 128 <= size <= 4096):
        raise ValueError("Invalid GPT table")
    image.seek(lba * 512)
    for _ in range(count):
        entry = image.read(size)
        if entry[56:128].decode("utf-16-le").split("\0")[0] == "super":
            super_offset = struct.unpack_from("<Q", entry, 32)[0] * 512
            break
    else:
        raise ValueError("SDK super partition not found")
    image.seek(super_offset + 4096)
    geometry = image.read(52)
    if struct.unpack_from("<I", geometry)[0] != 0x616C4467:
        raise ValueError("Invalid super geometry")
    maximum = struct.unpack_from("<I", geometry, 40)[0]
    if not 128 <= maximum <= 16 * 1024 * 1024:
        raise ValueError("Invalid metadata size")
    image.seek(super_offset + 3 * 4096)
    metadata = image.read(maximum)
    if struct.unpack_from("<I", metadata)[0] != 0x414C5030:
        raise ValueError("Invalid logical partition metadata")
    header_size = struct.unpack_from("<I", metadata, 8)[0]
    po, pn, ps = struct.unpack_from("<III", metadata, 80)
    eo, en, es = struct.unpack_from("<III", metadata, 92)
    if not (ps == 52 and es == 24 and header_size + po + pn * ps <= len(metadata)
            and header_size + eo + en * es <= len(metadata)):
        raise ValueError("Invalid logical partition tables")
    for i in range(pn):
        entry = metadata[header_size + po + i * ps:header_size + po + (i + 1) * ps]
        if entry[:36].split(b"\0")[0] != b"product":
            continue
        first, number = struct.unpack_from("<II", entry, 40)
        if first + number > en:
            raise ValueError("Invalid product extents")
        extents = []
        for j in range(number):
            sectors, kind, target, source = struct.unpack_from("<QIQI", metadata, header_size + eo + (first + j) * es)
            if kind != 0 or source != 0:
                raise ValueError("Only linear SDK extents are supported")
            extents.append((super_offset + target * 512, sectors * 512))
        return extents
    raise ValueError("SDK product partition not found")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-image", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "apks")
    args = parser.parse_args()
    if not shutil.which("debugfs"):
        raise SystemExit("Install e2fsprogs (debugfs) before extracting the SDK APKs")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="receita-sdk-") as temporary:
        temporary = Path(temporary)
        product = temporary / "product.img"
        with args.system_image.open("rb") as source:
            extents = product_extents(source)
            with product.open("wb") as target:
                for offset, size in extents:
                    source.seek(offset)
                    while size:
                        data = source.read(min(size, 8 * 1024 * 1024))
                        if not data:
                            raise ValueError("Truncated SDK image")
                        target.write(data)
                        size -= len(data)
        for name, expected in EXPECTED.items():
            packed = temporary / (name + ".apk.gz")
            subprocess.run(["debugfs", "-R", f"dump /app/{name}/{name}.apk.gz {packed}", str(product)],
                           check=True, capture_output=True)
            unpacked = temporary / (name + ".apk")
            with gzip.open(packed, "rb") as source, unpacked.open("wb") as target:
                shutil.copyfileobj(source, target)
            if hashlib.sha256(unpacked.read_bytes()).hexdigest() != expected:
                raise ValueError(f"{name} differs from the validated Chrome 133 SDK APK")
            shutil.copyfile(unpacked, args.output / unpacked.name)
            print(f"{name}.apk: SHA-256 verified")


if __name__ == "__main__":
    main()

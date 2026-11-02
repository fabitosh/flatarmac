"""Extract single members from a large remote zip using HTTP range requests."""

import io
import struct
import zipfile
import zlib
from pathlib import Path

import httpx

_LOCAL_HEADER = struct.Struct("<4s5H3L2H")


class _HttpRangeFile(io.RawIOBase):
    """Seekable read-only view on a remote file, enough for zipfile to read the central directory."""

    def __init__(self, client: httpx.Client, url: str):
        self.client = client
        self.url = url
        self.size = int(client.head(url).headers["content-length"])
        self.pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def read_range(self, start: int, length: int) -> bytes:
        response = self.client.get(
            self.url, headers={"Range": f"bytes={start}-{start + length - 1}"}
        )
        response.raise_for_status()
        return response.content

    def readinto(self, buffer) -> int:
        n = min(len(buffer), self.size - self.pos)
        if n <= 0:
            return 0
        data = self.read_range(self.pos, n)
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)


def extract_members(url: str, member_names: list[str], out_dir: Path) -> list[Path]:
    """Download and inflate the given members (matched by basename) into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(member_names)
    written = []
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        remote = _HttpRangeFile(client, url)
        with zipfile.ZipFile(io.BufferedReader(remote, buffer_size=1 << 20)) as archive:
            infos = [
                i
                for i in archive.infolist()
                if i.filename.replace("\\", "/").split("/")[-1] in wanted
            ]
        missing = wanted - {i.filename.replace("\\", "/").split("/")[-1] for i in infos}
        if missing:
            raise FileNotFoundError(f"not in archive: {sorted(missing)}")

        for info in infos:
            target = out_dir / info.filename.replace("\\", "/").split("/")[-1]
            if target.exists() and target.stat().st_size == info.file_size:
                written.append(target)
                continue
            header = _LOCAL_HEADER.unpack(remote.read_range(info.header_offset, _LOCAL_HEADER.size))
            name_len, extra_len = header[-2], header[-1]
            data_start = info.header_offset + _LOCAL_HEADER.size + name_len + extra_len
            _stream_member(client, url, data_start, info, target)
            written.append(target)
    return written


def _stream_member(
    client: httpx.Client, url: str, start: int, info: zipfile.ZipInfo, target: Path
) -> None:
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ValueError(f"unsupported compression {info.compress_type} for {info.filename}")
    inflater = zlib.decompressobj(-zlib.MAX_WBITS) if info.compress_type else None
    partial = target.with_suffix(target.suffix + ".part")
    end = start + info.compress_size - 1
    with client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as response:
        response.raise_for_status()
        with partial.open("wb") as out:
            for chunk in response.iter_bytes(1 << 20):
                out.write(inflater.decompress(chunk) if inflater else chunk)
            if inflater:
                out.write(inflater.flush())
    if partial.stat().st_size != info.file_size:
        raise OSError(f"size mismatch for {info.filename}")
    partial.rename(target)

"""原始文件存储：按 SHA-256 命名，写入后不再修改。

采集到的网页、附件，用户上传的招标文件，都存在这里。数据库里只存 SHA-256。
文件名只由内容决定，网页或上传文件里的任何内容（文件名、路径）都不会被拿来当路径。

顺序上先写文件、再提交数据库。进程中途退出可能留下没被引用的文件，但不会出现"数据库有记录、文件却不存在"。
读取时重新校验 SHA-256：文件被改过、损坏了，读的时候就能发现。
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

DIGEST = re.compile(r"[0-9a-f]{64}")


class BlobError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise BlobError("blob_root_symlink")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise BlobError("invalid_blob_id")
        # 按前两位分目录，一个目录里的文件不会多到让文件系统变慢。
        return self.root / digest[:2] / (digest + ".bin")

    def put(self, data: bytes) -> str:
        """保存文件，返回 SHA-256。同样的内容只存一份；已存在的文件不会被覆盖。"""
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        if target.exists():
            self.read(digest)
            return digest
        target.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        try:
            # 先写临时文件，再用硬链接放到正式位置：目标已存在时链接会失败，不会覆盖。
            # 别的进程同时写了同一个文件时，核对已有内容是否一致。
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read(digest)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def read(self, digest: str) -> bytes:
        """按 SHA-256 读取文件，并重新校验内容没有被改过。"""
        path = self._path(digest)
        if path.is_symlink():
            raise BlobError("blob_symlink")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BlobError("blob_missing") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise BlobError("blob_corrupt")
        return data

    def exists(self, digest: str) -> bool:
        return self._path(digest).is_file()

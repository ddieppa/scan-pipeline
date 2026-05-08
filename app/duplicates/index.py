from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.utils import fingerprint_prefix, sha256_file


@dataclass(frozen=True)
class IndexedFile:
    path: Path
    size: int
    prefix_hash: str


class DuplicateIndex:
    def __init__(self, root: Path, allowed_extensions: set[str]) -> None:
        self.root = root
        self.allowed_extensions = allowed_extensions
        self.by_size: dict[int, list[IndexedFile]] = {}

    def build(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self.allowed_extensions:
                continue
            indexed = IndexedFile(path=path, size=path.stat().st_size, prefix_hash=fingerprint_prefix(path))
            self.by_size.setdefault(indexed.size, []).append(indexed)

    def find_exact_duplicates(self, source: Path, restrict_to: Path | None = None) -> list[dict]:
        size = source.stat().st_size
        candidates = self.by_size.get(size, [])
        if not candidates:
            return []
        source_prefix = fingerprint_prefix(source)
        narrowed = [candidate for candidate in candidates if candidate.prefix_hash == source_prefix]
        if not narrowed:
            return []
        source_hash = sha256_file(source)
        results: list[dict] = []
        for candidate in narrowed:
            if candidate.path.resolve() == source.resolve():
                continue
            if restrict_to is not None and not self._is_relative_to(candidate.path, restrict_to):
                continue
            full_hash = sha256_file(candidate.path)
            if full_hash == source_hash:
                results.append(
                    {
                        "path": str(candidate.path),
                        "relativePath": str(candidate.path.relative_to(self.root)),
                        "size": size,
                        "hash": full_hash,
                    }
                )
        return results

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

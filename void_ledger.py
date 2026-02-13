#!/usr/bin/env python3
"""
void_ledger.py
Layer 1: The Void — JSONL (append-only) SHA-256 hash-chained ledger.

STRICT 10/10 Requirements:
1) SHA-256 HASH CHAINING: Every entry includes prev_hash; entry_hash is SHA-256 over a canonical payload.
2) FILE STORAGE: Ledger stored locally as JSON Lines file: void_ledger.jsonl
3) TAMPER EVIDENT: verify_integrity() recomputes chain; any manual edit breaks verification and identifies broken link.
4) SAFE WRITES: Each appended line is written under lock, flushed, and fsynced.
   If a crash causes partial/corrupt last line, verify_integrity() returns False and flags the corrupt line.
"""

from __future__ import annotations

import os
import json
import time
import uuid
import hashlib
from typing import Any, Dict, Optional, Tuple, List

LEDGER_FILE = "void_ledger.jsonl"
GENESIS_HASH = "0" * 64


# -------------------------
# Cross-platform advisory lock
# -------------------------
class FileLock:
    """
    Best-effort cross-platform advisory file lock.
    - Windows: msvcrt.locking on 1 byte
    - Unix: fcntl.flock
    """

    def __init__(self, file_obj):
        self.f = file_obj

    def __enter__(self):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        except Exception:
            # If lock fails, single-process usage still works.
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        return False


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _canonical_payload(entry: Dict[str, Any]) -> bytes:
    """
    Canonical bytes for hashing.
    Excludes 'entry_hash' itself.
    """
    payload = {
        "id": entry.get("id"),
        "ts_utc": entry.get("ts_utc"),
        "type": entry.get("type"),
        "data": entry.get("data"),
        "prev_hash": entry.get("prev_hash"),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def _read_last_entry_hash(path: str) -> str:
    """
    Read last valid entry_hash by scanning from the end (fast).
    If the last line is corrupt/partial, returns GENESIS_HASH; verify_integrity() will detect the corruption.
    """
    if not os.path.exists(path):
        return GENESIS_HASH

    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return GENESIS_HASH

            # Read last 8KB chunk
            chunk = 8192
            start = max(0, size - chunk)
            f.seek(start)
            data = f.read()
            lines = data.splitlines()

            # Skip empties; try parse last non-empty line
            for raw in reversed(lines):
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                    h = obj.get("entry_hash")
                    if isinstance(h, str) and len(h) == 64:
                        return h
                    return GENESIS_HASH
                except Exception:
                    return GENESIS_HASH
    except Exception:
        return GENESIS_HASH


def append_entry(entry_type: str, data: Any, path: str = LEDGER_FILE) -> Dict[str, Any]:
    """
    Append a single entry as one JSON line (JSONL).
    """
    prev_hash = _read_last_entry_hash(path)

    entry: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts_utc": _utc_iso(),
        "type": str(entry_type),
        "data": data,
        "prev_hash": prev_hash,
    }
    entry["entry_hash"] = _sha256_hex(_canonical_payload(entry))

    line = (json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

    with open(path, "ab") as f:
        with FileLock(f):
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    return entry


def verify_integrity(path: str = LEDGER_FILE) -> Tuple[bool, Dict[str, Any]]:
    """
    Recalculate and verify the full SHA-256 chain.

    Returns:
      (True, {"count": N, "status": "OK"}) on success
      (False, {...details...}) on failure with broken line index and reason.

    Reasons:
      - CORRUPT_LINE_OR_PARTIAL_WRITE
      - PREV_HASH_MISMATCH
      - ENTRY_HASH_MISMATCH
    """
    if not os.path.exists(path):
        return True, {"count": 0, "status": "EMPTY_LEDGER"}

    prev = GENESIS_HASH
    count = 0

    with open(path, "rb") as f:
        with FileLock(f):
            for line_no, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue

                try:
                    entry = json.loads(raw.decode("utf-8"))
                except Exception as e:
                    return False, {
                        "line": line_no,
                        "reason": "CORRUPT_LINE_OR_PARTIAL_WRITE",
                        "detail": str(e),
                    }

                found_prev = entry.get("prev_hash")
                if found_prev != prev:
                    return False, {
                        "line": line_no,
                        "reason": "PREV_HASH_MISMATCH",
                        "expected_prev": prev,
                        "found_prev": found_prev,
                        "found_entry_hash": entry.get("entry_hash"),
                    }

                expected_hash = _sha256_hex(_canonical_payload(entry))
                found_hash = entry.get("entry_hash")

                if found_hash != expected_hash:
                    return False, {
                        "line": line_no,
                        "reason": "ENTRY_HASH_MISMATCH",
                        "expected_hash": expected_hash,
                        "found_hash": found_hash,
                    }

                prev = found_hash
                count += 1

    return True, {"count": count, "status": "OK"}


def read_entries(path: str = LEDGER_FILE, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Read entries into memory.
    For huge ledgers, pass limit to return only the last N entries.
    """
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    if limit is not None and limit > 0:
        return out[-limit:]
    return out


if __name__ == "__main__":
    # Genesis test: append one entry and verify
    e = append_entry("genesis", {"msg": "Void ledger initialized"})
    ok, info = verify_integrity()
    print(json.dumps({"appended_id": e["id"], "ok": ok, "info": info}, ensure_ascii=False, indent=2))

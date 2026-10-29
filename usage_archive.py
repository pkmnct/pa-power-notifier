"""
Local accumulating archives for utility usage and bills.

Design:
  - Merge-only: new keys are added; existing records are never deleted.
  - Optional fill-in of blank fields (e.g. cost was 0) without changing kWh/MCF.
  - Survives utility APIs dropping older history from their rolling windows.

Env (optional overrides; defaults under repo ./data or /data in Docker):
  DAILY_USAGE_FILE
  GAS_USAGE_FILE
  BILL_ARCHIVE_DIR
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent


def _default_data_path(name: str) -> str:
    return str(_REPO_ROOT / "data" / name)


def daily_usage_path() -> str:
    return os.getenv("DAILY_USAGE_FILE", _default_data_path("daily_usage.json"))


def gas_usage_path() -> str:
    return os.getenv("GAS_USAGE_FILE", _default_data_path("gas_usage.json"))


def bill_archive_dir() -> str:
    return os.getenv("BILL_ARCHIVE_DIR", _default_data_path("bills"))


@dataclass
class DailyUsageRecord:
    date: str  # YYYY-MM-DD
    kwh: float
    cost: float = 0.0
    effective_rate: float = 0.0


class DailyElectricUsageStore:
    """Accumulate Duquesne daily kWh/cost, keyed by date."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or daily_usage_path()
        self._records: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            self._records = {r["date"]: r for r in data.get("records", []) if r.get("date")}
            logger.info(
                "Loaded %d daily electric records from %s (%s → %s)",
                len(self._records),
                self.path,
                data.get("earliest", "?"),
                data.get("latest", "?"),
            )
        except Exception as e:
            logger.warning("Could not load daily usage store %s: %s", self.path, e)

    def merge(self, records: Iterable[Any]) -> int:
        """
        Merge daily rows. Never removes history.
        Fills cost/rate when an existing day had kWh but cost was 0.
        """
        added = 0
        filled = 0
        for r in records:
            if hasattr(r, "date"):
                date = str(r.date)
                kwh = float(getattr(r, "kwh", 0) or 0)
                cost = float(getattr(r, "cost", 0) or 0)
                rate = float(getattr(r, "effective_rate", 0) or 0)
            else:
                date = str(r.get("date") or "")
                kwh = float(r.get("kwh") or 0)
                cost = float(r.get("cost") or 0)
                rate = float(r.get("effective_rate") or 0)
            if not date or kwh < 0:
                continue
            if rate <= 0 and kwh > 0 and cost > 0:
                rate = cost / kwh
            if date not in self._records:
                self._records[date] = {
                    "date": date,
                    "kwh": kwh,
                    "cost": cost,
                    "effective_rate": rate,
                }
                added += 1
            else:
                existing = self._records[date]
                # Prefer keeping first-seen kWh; fill blank cost only.
                if (existing.get("cost") or 0) <= 0 and cost > 0:
                    existing["cost"] = cost
                    existing["effective_rate"] = rate or (
                        cost / existing["kwh"] if existing.get("kwh") else 0
                    )
                    filled += 1
        if added or filled:
            logger.info(
                "Daily electric store: +%d new, filled cost on %d (%d total)",
                added,
                filled,
                len(self._records),
            )
        return added

    def save(self) -> None:
        if not self._records:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            dates = sorted(self._records.keys())
            data = {
                "records": [self._records[d] for d in dates],
                "record_count": len(self._records),
                "earliest": dates[0],
                "latest": dates[-1],
                "last_updated": datetime.now().isoformat(timespec="seconds"),
            }
            with open(self.path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("Saved %d daily electric records to %s", len(self._records), self.path)
        except Exception as e:
            logger.error("Could not save daily usage store: %s", e)

    def all_records(self) -> List[DailyUsageRecord]:
        return [DailyUsageRecord(**self._records[d]) for d in sorted(self._records.keys())]

    def __len__(self) -> int:
        return len(self._records)


class GasUsageStore:
    """Accumulate People's Gas monthly cycles, keyed by year-month (+ to_date)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or gas_usage_path()
        self._records: Dict[str, dict] = {}
        self._load()

    @staticmethod
    def _key(record: Any) -> str:
        if hasattr(record, "year"):
            year = int(record.year)
            month = int(record.month)
            to_date = getattr(record, "to_date", "") or getattr(record, "date", "") or ""
        else:
            year = int(record.get("year") or 0)
            month = int(record.get("month") or 0)
            to_date = record.get("to_date") or record.get("date") or ""
        return f"{year:04d}-{month:02d}:{to_date}"

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            self._records = {}
            for r in data.get("records", []):
                self._records[self._key(r)] = r
            logger.info(
                "Loaded %d gas usage cycles from %s",
                len(self._records),
                self.path,
            )
        except Exception as e:
            logger.warning("Could not load gas usage store %s: %s", self.path, e)

    def merge(self, records: Iterable[Any]) -> int:
        added = 0
        filled = 0
        for r in records:
            if hasattr(r, "usage_mcf"):
                row = {
                    "date": getattr(r, "date", "") or "",
                    "usage_mcf": float(r.usage_mcf or 0),
                    "month": int(r.month),
                    "year": int(r.year),
                    "from_date": getattr(r, "from_date", "") or "",
                    "to_date": getattr(r, "to_date", "") or "",
                    "meter_read_type": getattr(r, "meter_read_type", "") or "",
                    "bill_amount": getattr(r, "bill_amount", None),
                    "effective_rate": getattr(r, "effective_rate", None),
                }
            else:
                row = dict(r)
            key = self._key(row)
            if row.get("usage_mcf", 0) <= 0:
                continue
            if key not in self._records:
                self._records[key] = row
                added += 1
            else:
                existing = self._records[key]
                # Fill bill fields if previously missing
                if existing.get("bill_amount") is None and row.get("bill_amount") is not None:
                    existing["bill_amount"] = row["bill_amount"]
                    existing["effective_rate"] = row.get("effective_rate")
                    filled += 1
        if added or filled:
            logger.info(
                "Gas usage store: +%d new, filled bill on %d (%d total)",
                added,
                filled,
                len(self._records),
            )
        return added

    def save(self) -> None:
        if not self._records:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            rows = sorted(
                self._records.values(),
                key=lambda r: (r.get("year", 0), r.get("month", 0), r.get("to_date") or ""),
            )
            data = {
                "records": rows,
                "record_count": len(rows),
                "last_updated": datetime.now().isoformat(timespec="seconds"),
            }
            with open(self.path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("Saved %d gas usage cycles to %s", len(rows), self.path)
        except Exception as e:
            logger.error("Could not save gas usage store: %s", e)

    def all_records_as_gas_usage_data(self) -> List[Any]:
        """Return archived rows as peoples_gas_analyzer.GasUsageData when available."""
        try:
            from peoples_gas_analyzer import GasUsageData
        except ImportError:
            return list(self._records.values())

        out: List[Any] = []
        for r in sorted(
            self._records.values(),
            key=lambda x: (x.get("year", 0), x.get("month", 0), x.get("to_date") or ""),
        ):
            out.append(
                GasUsageData(
                    date=r.get("date") or "",
                    usage_mcf=float(r.get("usage_mcf") or 0),
                    month=int(r.get("month") or 0),
                    year=int(r.get("year") or 0),
                    from_date=r.get("from_date") or "",
                    to_date=r.get("to_date") or "",
                    meter_read_type=r.get("meter_read_type") or "",
                    bill_amount=r.get("bill_amount"),
                    effective_rate=r.get("effective_rate"),
                )
            )
        return out

    def __len__(self) -> int:
        return len(self._records)


class BillArchive:
    """Keep every fetched Duquesne bill PDF + extracted EGS metadata by bill id."""

    def __init__(self, directory: Optional[str] = None):
        self.directory = Path(directory or bill_archive_dir())
        self.index_path = self.directory / "index.json"
        self._index: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            data = json.loads(self.index_path.read_text())
            self._index = {str(k): v for k, v in (data.get("bills") or {}).items()}
        except Exception as e:
            logger.warning("Could not load bill archive index: %s", e)

    def _save_index(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "bills": self._index,
            "bill_count": len(self._index),
            "last_updated": datetime.now().isoformat(timespec="seconds"),
        }
        self.index_path.write_text(json.dumps(payload, indent=2))

    def archive_pdf(
        self,
        src_pdf: str,
        bill_id: str,
        egs: Optional[Any] = None,
    ) -> Optional[str]:
        """
        Copy src_pdf into the archive as {bill_id}.pdf (no overwrite if present).
        Updates index with EGS metadata when provided.
        """
        if not bill_id or not src_pdf or not os.path.exists(src_pdf):
            return None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            dest = self.directory / f"{bill_id}.pdf"
            if not dest.exists():
                shutil.copy2(src_pdf, dest)
                logger.info("Archived bill PDF → %s", dest)
            else:
                logger.debug("Bill PDF already archived: %s", dest)

            entry = dict(self._index.get(bill_id) or {})
            entry.update(
                {
                    "bill_id": bill_id,
                    "pdf": str(dest),
                    "archived_at": entry.get("archived_at")
                    or datetime.now().isoformat(timespec="seconds"),
                    "last_seen_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            if egs is not None:
                if hasattr(egs, "__dataclass_fields__"):
                    entry["egs"] = asdict(egs)
                elif isinstance(egs, dict):
                    entry["egs"] = egs
            self._index[bill_id] = entry
            self._save_index()
            # Also keep a convenience pointer to the newest PDF
            latest = self.directory / "latest_bill.pdf"
            try:
                shutil.copy2(dest, latest)
            except Exception:
                pass
            return str(dest)
        except Exception as e:
            logger.warning("Could not archive bill PDF: %s", e)
            return None

    def latest_egs(self) -> Optional[dict]:
        if not self._index:
            return None
        # Prefer last_seen_at
        def sort_key(item: tuple) -> str:
            return (item[1] or {}).get("last_seen_at") or ""

        _bid, entry = max(self._index.items(), key=sort_key)
        return (entry or {}).get("egs")

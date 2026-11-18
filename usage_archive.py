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
import re
import logging
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

from env_bootstrap import data_dir
from energy_shopper_core.tou import HourlyUsageRecord
from energy_shopper_core.utilities.peoples_gas import GasUsageData


def _default_data_path(name: str) -> str:
    return str(data_dir() / name)


def daily_usage_path() -> str:
    return os.getenv("DAILY_USAGE_FILE", _default_data_path("daily_usage.json"))


def gas_usage_path() -> str:
    return os.getenv("GAS_USAGE_FILE", _default_data_path("gas_usage.json"))


def bill_archive_dir() -> str:
    return os.getenv("BILL_ARCHIVE_DIR", _default_data_path("bills"))


def gas_bill_archive_dir() -> str:
    return os.getenv("GAS_BILL_ARCHIVE_DIR", _default_data_path("gas_bills"))


def market_archive_dir() -> str:
    return os.getenv("MARKET_ARCHIVE_DIR", _default_data_path("market"))


def report_archive_dir() -> str:
    return os.getenv("REPORT_ARCHIVE_DIR", _default_data_path("reports"))


@dataclass
class DailyUsageRecord:
    date: str  # YYYY-MM-DD
    kwh: float
    cost: float = 0.0
    effective_rate: float = 0.0


def write_text_atomic(path: Any, text: str) -> None:
    """
    Write a file through a temp file and an atomic rename, so a reader (the collector API,
    or the notifier and collector sharing data/) sees the old file or the new one, never
    half of one. The temp name is per process, so two writers can't collide on it.

    The data is flushed to disk before the rename, and the rename after it: otherwise a
    power cut soon after a save can leave the renamed file empty on filesystems that
    delay writing data, and an archive file that can't be read is never saved over.
    """
    path = str(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    _fsync_directory(directory)


def _fsync_directory(directory: str) -> None:
    """Make a rename in `directory` durable. Best effort: not every platform allows it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(path: Any, data: Any) -> None:
    """JSON through write_text_atomic (same layout as json.dump with indent=2)."""
    write_text_atomic(path, json.dumps(data, indent=2))


REFUSED_SAVE = "Not saving %s: it couldn't be read when loaded, and saving would replace the history in it"

# Why a save didn't happen. The remedies differ: an unreadable file needs repairing or
# restoring; a failed write (disk full, permissions, a read-only mount) needs the disk
# fixed, and the file itself is fine: moving it aside would throw its history away.
UNREADABLE = "unreadable"
WRITE_FAILED = "write failed"
# Neither: a bug stopped the save (say, a value JSON can't encode). Not a disk problem.
INTERNAL_ERROR = "internal error"

# Files whose save was refused or failed since take_failed_saves() last ran, so a caller
# that saves in many places (the notifier) can report them once, without checking each save.
_failed_saves: Dict[str, str] = {}


def _refuse_save(path: Any) -> None:
    logger.error(REFUSED_SAVE, path)
    _failed_saves[str(path)] = UNREADABLE


def _save_failed(path: Any, error: Exception) -> None:
    """Record a save that raised: an OSError is a failed write; anything else is a bug."""
    if isinstance(error, OSError):
        logger.error("Could not save %s: %s", path, error)
        _failed_saves[str(path)] = WRITE_FAILED
    else:
        logger.exception("Could not save %s: internal error", path, exc_info=error)
        _failed_saves[str(path)] = INTERNAL_ERROR


def take_failed_saves() -> List[Tuple[str, str]]:
    """(path, cause) for saves that didn't happen since the last call; clears the list."""
    failed = sorted(_failed_saves.items())
    _failed_saves.clear()
    return failed


def save_failure_advice(path: str, cause: str) -> str:
    """What to tell the user about a save that didn't happen, by cause."""
    if cause == UNREADABLE:
        return (
            f"Couldn't save {path}: it couldn't be read, so it isn't saved over and nothing new "
            "was archived to it. Repair or restore it from a backup; moving it aside starts it "
            "again from what the utilities still have."
        )
    if cause == WRITE_FAILED:
        return (
            f"Couldn't save {path}: writing it failed (disk full, permissions, or a read-only "
            "mount?), so nothing new was archived to it. The file itself is intact: fix the disk "
            "or permissions, and don't move the file aside, or its history goes with it."
        )
    return (
        f"Couldn't save {path}: an internal error (a bug) stopped the save, so nothing new was "
        "archived to it. The file itself is intact; the log has the details. Please report it."
    )


class DailyElectricUsageStore:
    """Accumulate Duquesne daily kWh/cost, keyed by date."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or daily_usage_path()
        self._records: Dict[str, dict] = {}
        self._load_failed = False
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
            self._load_failed = True
            logger.error("Could not load daily usage store %s (%s); it won't be saved over", self.path, e)

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

    @property
    def load_failed(self) -> bool:
        """The file exists but couldn't be read, so this store won't save over it."""
        return self._load_failed

    def save(self) -> bool:
        """Save the merged records. False when it refused (see load_failed) or failed."""
        if self._load_failed:
            _refuse_save(self.path)  # never replace a file we couldn't read
            return False
        if not self._records:
            return True
        try:
            dates = sorted(self._records.keys())
            data = {
                "records": [self._records[d] for d in dates],
                "record_count": len(self._records),
                "earliest": dates[0],
                "latest": dates[-1],
                "last_updated": datetime.now().isoformat(timespec="seconds"),
            }
            write_json_atomic(self.path, data)
            logger.info("Saved %d daily electric records to %s", len(self._records), self.path)
            return True
        except Exception as e:
            _save_failed(self.path, e)
            return False

    def all_records(self) -> List[DailyUsageRecord]:
        return [DailyUsageRecord(**self._records[d]) for d in sorted(self._records.keys())]

    def __len__(self) -> int:
        return len(self._records)


class GasUsageStore:
    """Accumulate People's Gas monthly cycles, keyed by year-month (+ to_date)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or gas_usage_path()
        self._records: Dict[str, dict] = {}
        self._load_failed = False
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
            self._load_failed = True
            logger.error("Could not load gas usage store %s (%s); it won't be saved over", self.path, e)

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

    @property
    def load_failed(self) -> bool:
        """The file exists but couldn't be read, so this store won't save over it."""
        return self._load_failed

    def save(self) -> bool:
        """Save the merged records. False when it refused (see load_failed) or failed."""
        if self._load_failed:
            _refuse_save(self.path)  # never replace a file we couldn't read
            return False
        if not self._records:
            return True
        try:
            rows = sorted(
                self._records.values(),
                key=lambda r: (r.get("year", 0), r.get("month", 0), r.get("to_date") or ""),
            )
            data = {
                "records": rows,
                "record_count": len(rows),
                "last_updated": datetime.now().isoformat(timespec="seconds"),
            }
            write_json_atomic(self.path, data)
            logger.info("Saved %d gas usage cycles to %s", len(rows), self.path)
            return True
        except Exception as e:
            _save_failed(self.path, e)
            return False

    def all_records_as_gas_usage_data(self) -> List[Any]:
        """Return archived rows as GasUsageData."""
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
        self._load_failed = False
        self._load()

    def _load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            data = json.loads(self.index_path.read_text())
            self._index = {str(k): v for k, v in (data.get("bills") or {}).items()}
            # "pdf" is a filename beside index.json; older entries held absolute paths,
            # which break when data/ moves between machines or into a container.
            for entry in self._index.values():
                if entry.get("pdf"):
                    entry["pdf"] = Path(entry["pdf"]).name
        except Exception as e:
            self._load_failed = True
            logger.error("Could not load bill archive index %s (%s); it won't be saved over", self.index_path, e)

    @property
    def load_failed(self) -> bool:
        """The index exists but couldn't be read, so it won't be saved over."""
        return self._load_failed

    def _save_index(self) -> bool:
        """Write the index. False when it refused (see load_failed) or the write failed (recorded)."""
        if self._load_failed:
            _refuse_save(self.index_path)  # it lists bills we don't
            return False
        payload = {
            "bills": self._index,
            "bill_count": len(self._index),
            "last_updated": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            write_json_atomic(self.index_path, payload)
            return True
        except Exception as e:  # recorded, so the notifier reports it and the collector's run fails
            _save_failed(self.index_path, e)
            return False

    def archive_pdf(
        self,
        src_pdf: str,
        bill_id: str,
        egs: Optional[Any] = None,
        bill_date: str = "",
        supplier: Optional[str] = None,
        details: Optional[dict] = None,
        update_latest: bool = True,
    ) -> Optional[str]:
        """
        Copy src_pdf into the archive as {bill_id}.pdf (no overwrite if present).
        Updates index with EGS metadata when provided. supplier is the EGS on that bill
        ("" for Duquesne default service); it defaults to egs.supplier when egs is given.
        details holds any other parsed fields (People's Gas rates, Mcf, amounts).
        update_latest=False when backfilling older bills, so latest_bill.pdf stays current.
        """
        if not bill_id or not src_pdf or not os.path.exists(src_pdf):
            return None
        dest = self.directory / f"{bill_id}.pdf"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src_pdf, dest)
                logger.info("Archived bill PDF → %s", dest)
            else:
                logger.debug("Bill PDF already archived: %s", dest)

            entry = dict(self._index.get(bill_id) or {})
            entry.update(
                {
                    "bill_id": bill_id,
                    "pdf": dest.name,
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
            egs_dict = entry.get("egs") or {}
            if supplier is None and egs is not None:
                supplier = egs_dict.get("supplier") or ""
            if supplier is not None:
                entry["supplier"] = supplier
            if details:
                entry["details"] = {**(entry.get("details") or {}), **details}
            bill_date = bill_date or egs_dict.get("bill_date") or ""
            if bill_date and not entry.get("bill_date"):
                entry["bill_date"] = bill_date
            self._index[bill_id] = entry
            if not self._save_index():
                # The PDF is there, but its supplier, rate and date live in the index: the
                # bill isn't archived until that's saved (a backfill mustn't count it, and
                # the next run, reloading the old index, tries it again).
                return None
            if update_latest:
                # Also keep a convenience pointer to the newest PDF
                latest = self.directory / "latest_bill.pdf"
                try:
                    shutil.copy2(dest, latest)
                except Exception:
                    pass
            return str(dest)
        except Exception as e:
            # Recorded, not just logged: callers swallow this, and a bill that isn't archived
            # (the only record of its supplier and supply rate) must still be reported.
            _save_failed(dest, e)
            return None

    def known_ids(self) -> List[str]:
        return sorted(self._index)

    def entries(self) -> List[dict]:
        """Copies of every index entry, oldest bill date first (undated bills first)."""
        return [dict(self._index[k]) for k in sorted(self._index, key=lambda k: (self._index[k].get("bill_date") or "", k))]

    def fill_bill_meta(self, bill_id: str, bill_date: str, details: Optional[dict] = None) -> None:
        """Fill in a bill's date and details (from the bill history) where not recorded yet."""
        entry = self._index.get(bill_id)
        if entry is None:
            return
        missing = {k: v for k, v in (details or {}).items() if v is not None and k not in (entry.get("details") or {})}
        if (not bill_date or entry.get("bill_date")) and not missing:
            return
        if bill_date and not entry.get("bill_date"):
            entry["bill_date"] = bill_date
        if missing:
            entry["details"] = {**(entry.get("details") or {}), **missing}
        # Bills archived before supplier was recorded carry it inside egs
        if "supplier" not in entry and (entry.get("egs") or {}).get("supplier"):
            entry["supplier"] = entry["egs"]["supplier"]
        self._save_index()

    def supply_rate_history(self) -> List[dict]:
        """[{"bill_id", "bill_date", "supplier", "rate"}] for bills with an EGS supply rate, oldest first."""
        rows = []
        for bill_id, entry in self._index.items():
            egs = entry.get("egs") or {}
            if entry.get("bill_date") and egs.get("supply_rate_per_kwh"):
                rows.append(
                    {
                        "bill_id": bill_id,
                        "bill_date": entry["bill_date"],
                        "supplier": entry.get("supplier") or egs.get("supplier") or "",
                        "rate": float(egs["supply_rate_per_kwh"]),
                    }
                )
        return sorted(rows, key=lambda r: r["bill_date"])

    def supplier_history(self) -> List[dict]:
        """
        Electric suppliers seen on archived bills, oldest first:
        [{"supplier", "first_bill", "last_bill"}]. Bills on Duquesne default service
        (supplier "") are left out; bills without a date are ignored. A name cut short by
        the bill's letter-spacing ("AMERICAN P O W E R") merges into the full name.
        """
        by_supplier: Dict[str, dict] = {}
        for entry in self._index.values():
            supplier = entry.get("supplier")
            if supplier is None:
                supplier = (entry.get("egs") or {}).get("supplier") or ""
            when = entry.get("bill_date") or ""
            if not supplier or not when:
                continue
            row = by_supplier.setdefault(
                supplier, {"supplier": supplier, "first_bill": when, "last_bill": when}
            )
            row["first_bill"] = min(row["first_bill"], when)
            row["last_bill"] = max(row["last_bill"], when)

        def squash(name: str) -> str:
            return re.sub(r"[^a-z0-9]", "", name.lower().replace("&", "and"))

        for short in sorted(by_supplier, key=len):
            for full in by_supplier:
                if full != short and len(squash(short)) >= 8 and squash(full).startswith(squash(short)):
                    row = by_supplier.pop(short)
                    by_supplier[full]["first_bill"] = min(by_supplier[full]["first_bill"], row["first_bill"])
                    by_supplier[full]["last_bill"] = max(by_supplier[full]["last_bill"], row["last_bill"])
                    break
        return sorted(by_supplier.values(), key=lambda r: r["first_bill"])

    def latest_egs(self) -> Optional[dict]:
        if not self._index:
            return None
        # Prefer last_seen_at
        def sort_key(item: tuple) -> str:
            return (item[1] or {}).get("last_seen_at") or ""

        _bid, entry = max(self._index.items(), key=sort_key)
        return (entry or {}).get("egs")


class MarketArchive:
    """
    Daily copies of the PA Power/Gas Switch offer CSVs plus a Price to Compare log, for
    mining how offers and default-service prices move over time.

      market/electric/YYYY-MM-DD.csv, market/gas/YYYY-MM-DD.csv  (last run of the day wins)
      market/price_to_compare.jsonl  (one line per utility per day)
    """

    def __init__(self, directory: Optional[str] = None):
        self.directory = Path(directory or market_archive_dir())

    def save_offers_csv(self, utility: str, csv_text: str, day: Optional[str] = None) -> Optional[str]:
        if not csv_text:
            return None
        day = day or datetime.now().date().isoformat()
        dest = self.directory / utility / f"{day}.csv"
        try:
            # Atomic: a run killed mid-write mustn't leave the day's only snapshot truncated.
            write_text_atomic(dest, csv_text)
            return str(dest)
        except Exception as e:  # recorded, so the notifier's report says so
            _save_failed(dest, e)
            return None

    def log_price_to_compare(
        self, utility: str, price: float, unit: str, source: str, day: Optional[str] = None
    ) -> None:
        day = day or datetime.now().date().isoformat()
        path = self.directory / "price_to_compare.jsonl"
        rows = []
        try:
            if path.is_file():
                rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            # A history that can't be read isn't saved over: rewriting it from the lines that
            # parse would drop the rest. Recorded, so the report says so (and how to fix it).
            _refuse_save(path)
            return
        try:
            rows = [r for r in rows if not (r.get("date") == day and r.get("utility") == utility)]
            rows.append({"date": day, "utility": utility, "price": price, "unit": unit, "source": source})
            rows.sort(key=lambda r: (r.get("date", ""), r.get("utility", "")))
            write_text_atomic(path, "".join(json.dumps(r) + "\n" for r in rows))
        except Exception as e:  # recorded, so the notifier's report says so
            _save_failed(path, e)


# ---------------------------------------------------------------------------
# Hourly usage persistence
# ---------------------------------------------------------------------------

class HourlyUsageStore:
    """
    Persistent store for hourly usage records.

    Records are keyed by their ISO datetime string. Merging new data never
    overwrites existing records, so historical data accumulates over time
    even as the API's rolling window advances.
    """

    def __init__(self, path: str):
        self.path = path
        self._records: Dict[str, dict] = {}
        self._load_failed = False
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            self._records = {r['dt']: r for r in data.get('records', [])}
            logger.info(
                "Loaded %d hourly records from %s (range: %s to %s)",
                len(self._records), self.path,
                data.get('earliest', '?'), data.get('latest', '?'),
            )
        except Exception as e:
            self._load_failed = True
            logger.error("Could not load hourly usage store %s (%s); it won't be saved over", self.path, e)

    def merge(self, records: List[HourlyUsageRecord]) -> int:
        """
        Merge new records without deleting history.

        Existing hours keep their first-seen kWh. If an existing hour had
        cost=0 and the new fetch has cost>0, fill in the cost (utilities
        often backfill $ after the kWh read).
        Returns the count of genuinely new records added.
        """
        added = 0
        filled = 0
        for r in records:
            if r.dt not in self._records:
                self._records[r.dt] = asdict(r)
                added += 1
            else:
                existing = self._records[r.dt]
                if (existing.get("cost") or 0) <= 0 and (r.cost or 0) > 0:
                    existing["cost"] = float(r.cost)
                    filled += 1
        if added or filled:
            logger.info(
                "Merged %d new hourly records, filled cost on %d (%d total)",
                added,
                filled,
                len(self._records),
            )
        return added

    @property
    def load_failed(self) -> bool:
        """The file exists but couldn't be read, so this store won't save over it."""
        return self._load_failed

    def save(self) -> bool:
        """Save the merged records. False when it refused (see load_failed) or failed."""
        if self._load_failed:
            _refuse_save(self.path)  # never replace a file we couldn't read
            return False
        if not self._records:
            return True
        try:
            sorted_dts = sorted(self._records.keys())
            data = {
                'records': [self._records[dt] for dt in sorted_dts],
                'record_count': len(self._records),
                'earliest': sorted_dts[0],
                'latest': sorted_dts[-1],
                'last_updated': datetime.now().isoformat(),
            }
            write_json_atomic(self.path, data)
            logger.info("Saved %d hourly records to %s", len(self._records), self.path)
            return True
        except Exception as e:
            _save_failed(self.path, e)
            return False

    def all_records(self) -> List[HourlyUsageRecord]:
        return [HourlyUsageRecord(**r) for r in self._records.values()]

    def __len__(self) -> int:
        return len(self._records)


def save_report(text: str, html: str, directory: Optional[str] = None) -> Optional[str]:
    """Keep each run's report as reports/YYYY-MM-DDTHH-MM-SS.{txt,html}."""
    base = Path(directory or report_archive_dir())
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / f"{stamp}.txt").write_text(text)
        (base / f"{stamp}.html").write_text(html)
        return str(base / f"{stamp}.txt")
    except Exception as e:
        logger.warning("Could not archive report: %s", e)
        return None

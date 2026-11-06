#!/usr/bin/env python3
"""Download Brønnøysund's open subunit CSV and build a local parent-company index."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError
from pathlib import Path
from urllib.request import Request, urlopen


SOURCE_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv"
USER_AGENT = "SignalpostLocalCache/1.0"


def first(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value.strip()
    return ""


def download(path: Path, timeout: float, etag: str | None) -> tuple[str | None, str | None, bool]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.brreg.enhetsregisteret.underenhet.v2+gzip"}
    if etag:
        headers["If-None-Match"] = etag
    request = Request(SOURCE_URL, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response, path.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
            return response.headers.get("Last-Modified"), response.headers.get("ETag"), False
    except HTTPError as exc:
        if exc.code == 304:
            return None, etag, True
        raise


def read_metadata(database: Path) -> dict[str, str]:
    if not database.is_file():
        return {}
    try:
        with sqlite3.connect(database) as connection:
            return {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
    except (sqlite3.Error, json.JSONDecodeError):
        return {}


def build_index(source: Path, database: Path, *, source_modified: str | None, source_etag: str | None) -> dict[str, int | str]:
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    snapshot_hash = digest.hexdigest()

    database.parent.mkdir(parents=True, exist_ok=True)
    temp_db = database.with_suffix(database.suffix + ".tmp")
    temp_db.unlink(missing_ok=True)
    counts = {"rows_read": 0, "subunits_indexed": 0, "parent_companies": 0}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temp_db)
        with connection:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("""
                CREATE TABLE subunits (
                    subunit_org TEXT PRIMARY KEY,
                    parent_org TEXT NOT NULL,
                    name TEXT,
                    legal_form TEXT,
                    employees INTEGER,
                    municipality TEXT,
                    municipality_number TEXT,
                    industry_code TEXT,
                    industry_label TEXT,
                    website TEXT
                )
            """)
            reader = None
            with gzip.open(source, "rt", encoding="utf-8-sig", newline="") as handle:
                sample = handle.read(8192)
                handle.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
                reader = csv.DictReader(handle, dialect=dialect)
                columns = (
                    "subunit_org", "parent_org", "name", "legal_form", "employees",
                    "municipality", "municipality_number", "industry_code", "industry_label", "website",
                )
                batch = []
                for row in reader:
                    counts["rows_read"] += 1
                    subunit = first(row, "organisasjonsnummer", "Organisasjonsnummer")
                    parent = first(row, "overordnetEnhet", "Overordnet enhet", "overordnetEnhet.organisasjonsnummer")
                    if len(subunit) != 9 or not subunit.isdigit() or len(parent) != 9 or not parent.isdigit():
                        continue
                    employees_raw = first(row, "antallAnsatte", "Antall ansatte")
                    try:
                        employees = int(employees_raw) if employees_raw else None
                    except ValueError:
                        employees = None
                    batch.append((
                        subunit,
                        parent,
                        first(row, "navn", "Navn"),
                        first(row, "organisasjonsform.kode", "Organisasjonsform.kode"),
                        employees,
                        first(row, "beliggenhetsadresse.kommune", "forretningsadresse.kommune", "Beliggenhetsadresse kommune"),
                        first(row, "beliggenhetsadresse.kommunenummer", "forretningsadresse.kommunenummer", "Beliggenhetsadresse kommunenummer"),
                        first(row, "naeringskode1.kode", "Næringskode1.kode"),
                        first(row, "naeringskode1.beskrivelse", "Næringskode1.beskrivelse"),
                        first(row, "hjemmeside", "Hjemmeside"),
                    ))
                    if len(batch) >= 10_000:
                        connection.executemany(
                            f"INSERT OR REPLACE INTO subunits ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                            batch,
                        )
                        counts["subunits_indexed"] += len(batch)
                        batch.clear()
                if batch:
                    connection.executemany(
                        f"INSERT OR REPLACE INTO subunits ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        batch,
                    )
                    counts["subunits_indexed"] += len(batch)
            connection.execute("CREATE INDEX idx_subunits_parent ON subunits(parent_org)")
            connection.execute("CREATE INDEX idx_subunits_municipality ON subunits(municipality_number)")
            counts["parent_companies"] = connection.execute("SELECT COUNT(DISTINCT parent_org) FROM subunits").fetchone()[0]
            metadata = {
                "source_url": SOURCE_URL,
                "source_license": "Norsk lisens for offentlige data (NLOD)",
                "source_last_modified": source_modified,
                "source_etag": source_etag,
                "source_sha256": snapshot_hash,
                "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                **counts,
            }
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", [(k, json.dumps(v, ensure_ascii=False)) for k, v in metadata.items()])
        connection.close()
        connection = None
        os.replace(temp_db, database)
    except Exception:
        if connection is not None:
            connection.close()
        temp_db.unlink(missing_ok=True)
        raise
    return {**counts, "source_sha256": snapshot_hash}


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the local Brønnøysund subunit cache and index it by parent organisation number.")
    parser.add_argument("--cache-dir", default="data/local-cache")
    parser.add_argument("--source-file", help="Use a previously downloaded Brønnøysund CSV.GZ instead of downloading it")
    parser.add_argument("--keep-snapshot", action="store_true", help="Retain the downloaded CSV.GZ beside the SQLite index")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    database = cache_dir / "brreg_subunits.sqlite"
    if args.source_file:
        metadata = build_index(Path(args.source_file), database, source_modified=None, source_etag=None)
    else:
        fd, temp_name = tempfile.mkstemp(prefix="brreg-subunits-", suffix=".csv.gz", dir=cache_dir)
        os.close(fd)
        temp_source = Path(temp_name)
        try:
            old_metadata = read_metadata(database)
            modified, etag, unchanged = download(temp_source, args.timeout, old_metadata.get("source_etag"))
            if unchanged:
                print(json.dumps({"database": str(database), "unchanged": True, "source_etag": etag}, indent=2))
                return
            metadata = build_index(temp_source, database, source_modified=modified, source_etag=etag)
            if args.keep_snapshot:
                shutil.copyfile(temp_source, cache_dir / "brreg_subunits_source.csv.gz")
        finally:
            temp_source.unlink(missing_ok=True)
    print(json.dumps({"database": str(database), **metadata}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

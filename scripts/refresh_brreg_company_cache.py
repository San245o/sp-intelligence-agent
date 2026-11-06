#!/usr/bin/env python3
"""Download Brønnøysund's open entity CSV and build a minimal local SQLite index."""

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
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SOURCE_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
USER_AGENT = "SignalpostLocalCache/1.0"


def first(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value.strip()
    return ""


def read_metadata(database: Path) -> dict[str, str]:
    if not database.is_file():
        return {}
    connection = sqlite3.connect(database)
    try:
        return {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
    except (sqlite3.Error, json.JSONDecodeError):
        return {}
    finally:
        connection.close()


def download(path: Path, timeout: float, etag: str | None) -> tuple[str | None, str | None, bool]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.brreg.enhetsregisteret.enhet.v2+gzip"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urlopen(Request(SOURCE_URL, headers=headers), timeout=timeout) as response, path.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
            return response.headers.get("Last-Modified"), response.headers.get("ETag"), False
    except HTTPError as exc:
        if exc.code == 304:
            return None, etag, True
        raise


def build_index(source: Path, database: Path, *, source_modified: str | None, source_etag: str | None) -> dict[str, int | str]:
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    snapshot_hash = digest.hexdigest()
    temp_db = database.with_suffix(database.suffix + ".tmp")
    temp_db.unlink(missing_ok=True)
    counts = {"rows_read": 0, "companies_indexed": 0}
    connection = sqlite3.connect(temp_db)
    try:
        with connection:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("""
                CREATE TABLE companies (
                    organisation_number TEXT PRIMARY KEY,
                    name TEXT,
                    legal_form TEXT,
                    employees INTEGER,
                    bankrupt INTEGER,
                    liquidating INTEGER,
                    municipality TEXT,
                    municipality_number TEXT,
                    industry_code TEXT,
                    industry_label TEXT,
                    website TEXT,
                    latest_submitted_accounts TEXT,
                    registered_in_vat INTEGER,
                    registered_in_business_register INTEGER
                )
            """)
            columns = (
                "organisation_number", "name", "legal_form", "employees", "bankrupt", "liquidating",
                "municipality", "municipality_number", "industry_code", "industry_label", "website",
                "latest_submitted_accounts", "registered_in_vat", "registered_in_business_register",
            )
            with gzip.open(source, "rt", encoding="utf-8-sig", newline="") as handle:
                sample = handle.read(8192)
                handle.seek(0)
                reader = csv.DictReader(handle, dialect=csv.Sniffer().sniff(sample, delimiters=";,\t"))
                batch = []
                for row in reader:
                    counts["rows_read"] += 1
                    org = first(row, "organisasjonsnummer", "Organisasjonsnummer")
                    if len(org) != 9 or not org.isdigit():
                        continue
                    employees_raw = first(row, "antallAnsatte", "Antall ansatte")
                    try:
                        employees = int(employees_raw) if employees_raw else None
                    except ValueError:
                        employees = None
                    batch.append((
                        org,
                        first(row, "navn", "Navn"),
                        first(row, "organisasjonsform.kode", "Organisasjonsform.kode"),
                        employees,
                        int(first(row, "konkurs", "Konkurs").lower() == "true"),
                        int(first(row, "underAvvikling", "Under avvikling").lower() == "true"),
                        first(row, "forretningsadresse.kommune", "Forretningsadresse.kommune"),
                        first(row, "forretningsadresse.kommunenummer", "Forretningsadresse.kommunenummer"),
                        first(row, "naeringskode1.kode", "Næringskode1.kode"),
                        first(row, "naeringskode1.beskrivelse", "Næringskode1.beskrivelse"),
                        first(row, "hjemmeside", "Hjemmeside"),
                        first(row, "sisteInnsendteAarsregnskap", "Siste innsendte årsregnskap"),
                        int(first(row, "registrertIMvaregisteret", "Registrert i Mva-registeret").lower() == "true"),
                        int(first(row, "registrertIForetaksregisteret", "Registrert i Foretaksregisteret").lower() == "true"),
                    ))
                    if len(batch) >= 10_000:
                        connection.executemany(
                            f"INSERT OR REPLACE INTO companies ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                            batch,
                        )
                        counts["companies_indexed"] += len(batch)
                        batch.clear()
                if batch:
                    connection.executemany(
                        f"INSERT OR REPLACE INTO companies ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        batch,
                    )
                    counts["companies_indexed"] += len(batch)
            connection.execute("CREATE INDEX idx_companies_name ON companies(name)")
            connection.execute("CREATE INDEX idx_companies_municipality ON companies(municipality_number)")
            connection.execute("CREATE INDEX idx_companies_industry ON companies(industry_code)")
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
        os.replace(temp_db, database)
    except Exception:
        connection.close()
        temp_db.unlink(missing_ok=True)
        raise
    return {**counts, "source_sha256": snapshot_hash}


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the full Brønnøysund entity cache and index it by organisation number.")
    parser.add_argument("--cache-dir", default="data/local-cache")
    parser.add_argument("--source-file", help="Use an existing Brønnøysund CSV.GZ instead of downloading it")
    parser.add_argument("--keep-snapshot", action="store_true", help="Retain the compressed CSV.GZ beside the SQLite index")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    database = cache_dir / "brreg_companies.sqlite"
    if args.source_file:
        result = build_index(Path(args.source_file), database, source_modified=None, source_etag=None)
    else:
        fd, temp_name = tempfile.mkstemp(prefix="brreg-entities-", suffix=".csv.gz", dir=cache_dir)
        os.close(fd)
        temp_source = Path(temp_name)
        try:
            old_metadata = read_metadata(database)
            modified, etag, unchanged = download(temp_source, args.timeout, old_metadata.get("source_etag"))
            if unchanged:
                print(json.dumps({"database": str(database), "unchanged": True, "source_etag": etag}, indent=2))
                return
            result = build_index(temp_source, database, source_modified=modified, source_etag=etag)
            if args.keep_snapshot:
                shutil.copyfile(temp_source, cache_dir / "brreg_entities_source.csv.gz")
        finally:
            temp_source.unlink(missing_ok=True)
    print(json.dumps({"database": str(database), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

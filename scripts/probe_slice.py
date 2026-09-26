#!/usr/bin/env python3
import json
import socket
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.identity import assess_identity, registrable_domain
from signalpost.extract.contact import page_signals
from signalpost.extract.structured import parse_structured

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

manifest_lines = [json.loads(l) for l in open(ROOT / "data/eval-manifest-100.jsonl", encoding="utf-8")][92:100]

def inspect_company(m):
    org = m["organisation_number"]
    name = m["name"]
    emp = m.get("employees")
    mun = m.get("municipality")
    reg_site = (m.get("website") or "").strip()
    
    out = [f"\n[{org}] {name} (emp: {emp}, mun: {mun}, reg: {reg_site})"]
    
    # candidate domains to check
    base_name = name.lower()
    for drop in (" as", " nuf", " ab", " sti", " legat", " og marte aarsæthers legat"):
        base_name = base_name.replace(drop, "")
    base_name = base_name.strip()
    words = base_name.replace("-", " ").replace("&", " ").split()
    
    candidates = []
    if reg_site:
        candidates.append(reg_site if "://" in reg_site else "https://" + reg_site)
    
    candidates.append(f"https://{''.join(words)}.no")
    candidates.append(f"https://{'-'.join(words)}.no")
    if len(words) > 1:
        candidates.append(f"https://{words[0]}.no")
        candidates.append(f"https://{words[0]}.com")
    if "incluso" in base_name:
        candidates.extend(["https://incluso.se", "https://incluso.no", "https://incluso.com"])
    if "skorstad" in base_name:
        candidates.extend(["https://skorstad.no", "https://skorstadfisk.no", "https://skorstad-settefisk.no"])
    if "kvamma" in base_name:
        candidates.extend(["https://kvammabygg.no", "https://kvamma.no"])

    seen_hosts = set()
    found_any = False
    for url in candidates:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        
        # Check DNS
        try:
            socket.setdefaulttimeout(2.5)
            socket.gethostbyname(host)
        except Exception:
            continue
        
        # Fetch HTTP
        try:
            req = urllib.request.Request(f"https://{host}", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=3.0, context=ctx) as resp:
                html = resp.read(65536).decode("utf-8", errors="ignore")
                final_url = resp.geturl()
        except Exception:
            try:
                req = urllib.request.Request(f"http://{host}", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=3.0, context=ctx) as resp:
                    html = resp.read(65536).decode("utf-8", errors="ignore")
                    final_url = resp.geturl()
            except Exception as e:
                out.append(f"  * {host} resolved DNS but HTTP failed: {e}")
                continue
                
        # Run Identity assessment
        domain = registrable_domain(final_url)
        structured = parse_structured(html, final_url)
        names = structured.get("legal_names", []) + structured.get("names", [])
        signals = page_signals(html, hostname=domain, structured_names=names)
        verdict = assess_identity(m, signals, source_url=final_url)
        
        out.append(f"  * {final_url} -> Score: {verdict.score:.2f} | Publishable: {verdict.publishable}")
        out.append(f"    Reasons: {verdict.reasons}")
        found_any = True
        
    if not found_any:
        out.append("  -> NO live domain found across candidates.")
    return "\n".join(out)

with ThreadPoolExecutor(max_workers=8) as pool:
    results = pool.map(inspect_company, manifest_lines)
    for r in results:
        print(r, flush=True)

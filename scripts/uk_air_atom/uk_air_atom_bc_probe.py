#!/usr/bin/env python3

import argparse
import gzip
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests


ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

KEYWORDS = [
    "black carbon",
    "blackcarbon",
    "black-carbon",
    "aethalometer",
    "aeth",
    "uv component",
    "370 nm",
    "370nm",
    "880 nm",
    "880nm",
    "ukbsn",
    "equivalent black carbon",
    "bc concentration",
]

BASE = "https://uk-air.defra.gov.uk/data/atom-dls"


def feed_url(kind: str, year: int) -> str:
    return f"{BASE}/{kind}/{year}/atom.en.xml"


def fetch_text(session: requests.Session, url: str, timeout: int = 60) -> str:
    print(f"Fetching feed: {url}", file=sys.stderr)
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def entry_text(entry: ET.Element) -> str:
    bits = []
    for tag in ["title", "id", "updated", "summary", "content"]:
        el = entry.find(f"atom:{tag}", ATOM_NS)
        if el is not None and el.text:
            bits.append(el.text)
    for link in entry.findall("atom:link", ATOM_NS):
        bits.append(link.attrib.get("href", ""))
        bits.append(link.attrib.get("title", ""))
        bits.append(link.attrib.get("type", ""))
    return "\n".join(bits)


def entry_links(entry: ET.Element, feed: str, include_related: bool = False) -> list[dict]:
    links = []

    for link in entry.findall("atom:link", ATOM_NS):
        href = link.attrib.get("href")
        if not href:
            continue

        rel = link.attrib.get("rel", "")
        link_type = link.attrib.get("type", "")
        title = link.attrib.get("title", "")

        # related links are pollutant vocabulary pages, not downloadable data files
        if rel == "related" and not include_related:
            continue

        links.append({
            "url": urljoin(feed, href),
            "rel": rel,
            "type": link_type,
            "title": title,
        })

    return links

def find_candidates(feed_xml: str, feed: str) -> list[dict]:
    root = ET.fromstring(feed_xml)
    entries = root.findall("atom:entry", ATOM_NS)
    print(f"Entries in feed: {len(entries)}")

    candidates = []
    for entry in entries:
        text = entry_text(entry)
        lower = text.lower()
        hits = [kw for kw in KEYWORDS if kw in lower]
        if not hits:
            continue

        title_el = entry.find("atom:title", ATOM_NS)
        id_el = entry.find("atom:id", ATOM_NS)
        updated_el = entry.find("atom:updated", ATOM_NS)

        candidates.append({
            "title": title_el.text if title_el is not None else "",
            "id": id_el.text if id_el is not None else "",
            "updated": updated_el.text if updated_el is not None else "",
            "hits": hits,
            "links": entry_links(entry, feed, include_related=False),
        })

    return candidates


def head_size(session: requests.Session, url: str) -> int | None:
    try:
        r = session.head(url, allow_redirects=True, timeout=30)
        if not r.ok:
            return None
        raw = r.headers.get("content-length")
        return int(raw) if raw and raw.isdigit() else None
    except Exception:
        return None


def download(session: requests.Session, url: str, out_dir: Path, max_mb: int) -> Path | None:
    size = head_size(session, url)
    if size is not None:
        mb = size / 1024 / 1024
        print(f"  HEAD size: {mb:.1f} MB")
        if mb > max_mb:
            print(f"  Skipping download, over --max-mb={max_mb}")
            return None

    name = url.rstrip("/").split("/")[-1] or "download.bin"
    if "?" in name:
        name = name.split("?", 1)[0]
    out = out_dir / name

    print(f"  Downloading: {url}")
    with session.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = 0
        limit = max_mb * 1024 * 1024
        with out.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    print(f"  Stopping, exceeded --max-mb={max_mb}")
                    return None
                f.write(chunk)

    print(f"  Saved: {out} ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    return out


def sniff_file(path: Path, max_chars: int = 500_000) -> None:
    print(f"\nInspecting: {path}")

    texts = []

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            print("  ZIP members:")
            for name in z.namelist()[:50]:
                print(f"    {name}")
            for name in z.namelist():
                if not name.lower().endswith((".xml", ".gml", ".txt", ".csv")):
                    continue
                with z.open(name) as f:
                    data = f.read(max_chars)
                texts.append((name, data.decode("utf-8", errors="replace")))
                if len(texts) >= 10:
                    break
    elif path.suffix.lower() == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            texts.append((path.name, f.read(max_chars)))
    else:
        texts.append((path.name, path.read_text(encoding="utf-8", errors="replace")[:max_chars]))

    for name, text in texts:
        lower = text.lower()
        hits = [kw for kw in KEYWORDS if kw in lower]
        print(f"  {name}: keyword hits = {hits or 'none'}")

        # Show likely observed-property / pollutant fragments.
        for pat in [
            r".{0,80}black.{0,80}",
            r".{0,80}carbon.{0,80}",
            r".{0,80}aeth.{0,80}",
            r".{0,80}370.{0,80}",
            r".{0,80}880.{0,80}",
            r".{0,80}uv.{0,80}",
            r".{0,80}observedProperty.{0,80}",
        ]:
            matches = re.findall(pat, text, flags=re.IGNORECASE)
            for m in matches[:5]:
                print("   ", " ".join(m.split()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--kind", choices=["non-auto", "auto", "both"], default="non-auto")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--max-mb", type=int, default=200)
    parser.add_argument("--out-dir", default="tmp/ukair_atom_bc")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    kinds = ["non-auto", "auto"] if args.kind == "both" else [args.kind]

    session = requests.Session()
    session.headers.update({
        "User-Agent": "uk-aq-bc-uv-probe/0.1",
        "Accept": "application/atom+xml, application/xml, text/xml, */*",
    })

    total_candidates = 0

    for kind in kinds:
        feed = feed_url(kind, args.year)
        xml = fetch_text(session, feed)
        feed_path = out_dir / f"{kind}_{args.year}_atom.en.xml"
        feed_path.write_text(xml, encoding="utf-8")
        print(f"\nSaved feed: {feed_path}")

        candidates = find_candidates(xml, feed)
        total_candidates += len(candidates)

        print(f"Candidate entries for {kind} {args.year}: {len(candidates)}")

        for i, c in enumerate(candidates, start=1):
            print("\n---")
            print(f"Candidate {i}")
            print(f"title: {c['title']}")
            print(f"id: {c['id']}")
            print(f"updated: {c['updated']}")
            print(f"hits: {', '.join(c['hits'])}")
            print("links:")
            for link in c["links"]:
                print(f"  [{link['rel']}] {link['type']} {link['url']} ({link['title']})")

            if args.download:
                for link in c["links"]:
                    path = download(session, link["url"], out_dir, args.max_mb)
                    if path:
                        sniff_file(path)

    if total_candidates == 0:
        print("\nNo obvious BC/UV candidates found at Atom entry level.")
        print("Next step: inspect linked feed downloads or use the UK-AIR Data Selector path.")


if __name__ == "__main__":
    main()
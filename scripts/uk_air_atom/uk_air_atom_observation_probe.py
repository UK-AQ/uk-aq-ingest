#!/usr/bin/env python3

import argparse
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


POLLUTANT_RE = re.compile(
    r"http://dd\.eionet\.europa\.eu/vocabulary/aq/pollutant/([0-9]+)"
)

KEYWORDS = [
    "black carbon",
    "880",
    "370",
    "uv",
    "observedproperty",
    "result",
    "values",
    "dataarray",
    "uom",
]


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("xml_file")
    parser.add_argument("--max-snippets", type=int, default=40)
    args = parser.parse_args()

    path = Path(args.xml_file)
    text = path.read_text(encoding="utf-8", errors="replace")

    print(f"File: {path}")
    print(f"Size: {path.stat().st_size:,} bytes")

    pollutants = Counter(POLLUTANT_RE.findall(text))
    print("\nPollutant vocabulary IDs found:")
    for pollutant, count in pollutants.most_common():
        print(f"  {pollutant}: {count}")

    print("\nKeyword hits:")
    lower = text.lower()
    for kw in KEYWORDS:
        print(f"  {kw}: {lower.count(kw.lower())}")

    print("\nLines/snippets around likely BC/UV terms:")
    terms = ["black carbon", "880", "370", "uv", "pollutant/391", "observedProperty"]
    snippets = []

    for term in terms:
        for m in re.finditer(re.escape(term), text, flags=re.IGNORECASE):
            start = max(0, m.start() - 220)
            end = min(len(text), m.end() + 220)
            snippet = " ".join(text[start:end].split())
            snippets.append((term, snippet))
            if len(snippets) >= args.max_snippets:
                break
        if len(snippets) >= args.max_snippets:
            break

    for term, snippet in snippets:
        print(f"\n[{term}]")
        print(snippet)

    print("\nXML element counts:")
    try:
        root = ET.fromstring(text)
        counts = Counter(strip_ns(el.tag) for el in root.iter())
        for name, count in counts.most_common(40):
            print(f"  {name}: {count}")
    except ET.ParseError as exc:
        print(f"  XML parse failed: {exc}")


if __name__ == "__main__":
    main()
"""Build the local ima store from a JSON dump of ima search results.

The container has no access to ima's MCP server, so an agent (or the user, via
the ima client) produces a JSON dump and this script normalises it into
``data/ima/store.jsonl``.

Accepted input shapes (auto-detected):
  - the raw MCP ``search_knowledge`` payload: {"searched_knowledge_list": [...]}
  - a list of those payloads (results from several queries concatenated)
  - a flat list of {"title", "text"|"introduction", "source", "published"}

De-duplication is by normalised title: the same article is routinely filed in
several ima folders, and duplicates crowd out other sources in the prompt.

Usage:
    python tools/ima_sync.py data/ima/inbox.json
    python tools/ima_sync.py data/ima/inbox.json --replace
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

_CN_DATE = re.compile(r"发布时间[:：]\s*(\d{4})年(\d{1,2})月(\d{1,2})日")
_ISO_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


def _store_path() -> str:
    # Share the resolver with the reader so sync and retrieval never disagree
    # about where the corpus lives (host ./data vs the container's mount).
    from tradingagents.dataflows.vendors.ima import _data_root

    return os.path.join(_data_root(), "ima", "store.jsonl")


def _norm_title(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", (title or "").lower())


def _published(intro: str, fallback: str = "") -> str:
    m = _CN_DATE.search(intro or "")
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = _ISO_DATE.search(intro or "")
    if m:
        return "-".join(m.groups())
    return fallback


def _clean_text(intro: str, title: str) -> str:
    """Drop the duplicated '# title' head and the publisher-byline noise."""
    text = (intro or "").strip()
    text = re.sub(r"^#\s*", "", text)
    if title and text.startswith(title):
        text = text[len(title):]
    text = re.sub(r"作者[:：].{0,30}?发布时间[:：]", " ", text)
    text = re.sub(r"发布地点[:：]\s*\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _iter_records(payload) -> list[dict]:
    """Flatten any accepted input shape into (title, intro, source, published)."""
    records: list[dict] = []

    def take_knowledge(entry: dict) -> None:
        k = entry.get("knowledge") if isinstance(entry, dict) and "knowledge" in entry else entry
        if not isinstance(k, dict):
            return
        title = (k.get("title") or "").strip()
        intro = k.get("introduction") or k.get("text") or ""
        if not title and not intro:
            return
        source = (entry.get("parent_folder_name") if isinstance(entry, dict) else "") \
            or k.get("source") or k.get("folder_name") or "ima"
        records.append({
            "title": title,
            "text": _clean_text(intro, title),
            "source": source,
            "published": k.get("published") or _published(intro, (k.get("time_wording") or "")[:10]),
            "media_id": k.get("media_id"),
            "media_type": (k.get("media_type_info") or {}).get("name"),
        })

    def walk(node) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            if "searched_knowledge_list" in node:
                for e in node["searched_knowledge_list"]:
                    take_knowledge(e)
            elif "knowledge_list" in node:
                for e in node["knowledge_list"]:
                    take_knowledge(e)
            elif "title" in node and ("introduction" in node or "text" in node):
                take_knowledge(node)

    walk(payload)
    return records


def sync(inbox: str, replace: bool = False, since: str | None = None) -> dict:
    """Merge an ima dump into the store.

    Safe to run repeatedly: items are keyed by normalised title, so the same
    article pushed into ima twice (or filed in two folders) is stored once. The
    first-seen timestamp is preserved, so the store doubles as a record of when
    a story entered the corpus.

    ``since`` (YYYY-MM-DD) skips older items — use it for routine incremental
    pulls so a refresh only costs tokens on genuinely new content.
    """
    with open(inbox, encoding="utf-8") as fh:
        payload = json.load(fh)
    incoming = _iter_records(payload)
    if since:
        incoming = [r for r in incoming if (r.get("published") or "") >= since]

    path = _store_path()
    existing: list[dict] = []
    if not replace and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        existing.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    by_title = {_norm_title(r.get("title") or ""): r for r in existing}
    added = dup = 0
    now = datetime.now().isoformat(timespec="seconds")
    for rec in incoming:
        key = _norm_title(rec["title"])
        if not key:
            continue
        if key in by_title:
            dup += 1
            # Keep the longer text: one copy often carries a fuller excerpt.
            if len(rec.get("text") or "") > len(by_title[key].get("text") or ""):
                by_title[key].update({"text": rec["text"], "source": rec["source"]})
            continue
        rec.setdefault("first_seen", now)
        rec["last_synced"] = now
        if "ingested_at" not in rec:
            rec["ingested_at"] = now
        by_title[key] = rec
        added += 1

    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = sorted(by_title.values(), key=lambda r: r.get("published") or "", reverse=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in rows:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)

    sources: dict[str, int] = {}
    for rec in rows:
        sources[rec.get("source") or "?"] = sources.get(rec.get("source") or "?", 0) + 1
    return {
        "incoming": len(incoming),
        "added": added,
        "duplicates": dup,
        "total_in_store": len(rows),
        "by_source": dict(sorted(sources.items(), key=lambda kv: -kv[1])),
        "store": path,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="sync ima search results into the local store")
    ap.add_argument("inbox", help="JSON file with ima search results")
    ap.add_argument("--replace", action="store_true", help="rebuild instead of merging")
    ap.add_argument("--since", help="only items published on/after YYYY-MM-DD (incremental pull)")
    args = ap.parse_args(argv)
    print(json.dumps(sync(args.inbox, args.replace, args.since), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

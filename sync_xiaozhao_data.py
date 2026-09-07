"""Generate the expanded source pool for 秋招雷达 from xiaozhao-radar.

The runtime server never depends on the external repository: it only reads the
two generated JSON files in ``data/``. Run this script occasionally (or weekly)
to pull in newly added sites / aggregated campus batches:

    python sync_xiaozhao_data.py                 # local clone next to this repo
    python sync_xiaozhao_data.py --remote        # fetch latest from GitHub
    python sync_xiaozhao_data.py --source DIR    # custom clone location

This only extends the *data source pool*. All filtering, scoring and matching
continue to live in server.py and are not touched by this script.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_REPO = ROOT.parents[1] / "xiaozhao-radar"
REMOTE_BASE = "https://raw.githubusercontent.com/jiabaobei/xiaozhao-radar/main/"

AGGREGATOR_HOST_HINTS = (
    "51job.com", "yingjiesheng.com", "iguopin.com", "zhipin.com", "liepin.com",
    "zhaopin.com", "shixiseng.com", "lagou.com", "nowcoder.com", "bysjy.com.cn",
    "ncss.cn",
)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "QiuzhaoRadar-sync/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def read_source_file(path: Path, remote_name: str, remote: bool) -> str:
    if remote:
        return fetch_text(REMOTE_BASE + remote_name)
    if not path.is_file():
        raise FileNotFoundError(f"找不到 {path}；请先克隆仓库或用 --remote 拉取")
    return path.read_text(encoding="utf-8", errors="ignore")


def extract_js_array(text: str, marker: str) -> list:
    """Extract a top-level JavaScript array literal and parse it as JSON."""
    token = f"{marker} = ["
    start = text.find(token)
    if start < 0:
        raise ValueError(f"在页面中找不到 {token}")
    start += len(token) - 1
    depth = 0
    quote = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in ("\"", "'", "`"):
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                break
        index += 1
    if depth != 0:
        raise ValueError(f"未找到 {marker} 数组的结尾")
    chunk = text[start:index]
    # The embedded list is hand-written JS: drop full-line comments and any
    # trailing commas so the strict JSON parser can consume it.
    chunk = re.sub(r"(?m)^\s*//.*$", "", chunk)
    chunk = re.sub(r"(?s)/\*.*?\*/", "", chunk)
    chunk = re.sub(r",\s*([}\]])", r"\1", chunk)
    chunk = chunk.rstrip().rstrip(",") + "]"
    # Hand-written JS objects use unquoted keys like {cat:"腾讯", url:"..."}.
    # Quote keys only when they follow a comma / brace so values stay intact.
    chunk = re.sub(r"([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)", r'\1"\2"\3', chunk)
    try:
        parsed = json.loads(chunk)
    except json.JSONDecodeError as exc:
        lines = chunk.splitlines()
        start = max(0, exc.lineno - 5)
        end = min(len(lines), exc.lineno + 8)
        context = "\n".join(f"{index + 1}: {line[:200]}" for index, line in enumerate(lines[start:end], start))
        raise ValueError(f"{marker} JSON 解析失败：{exc}\n{context}") from None
    if not isinstance(parsed, list):
        raise ValueError(f"{marker} 不是数组")
    return parsed


def url_key(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{parts.path.rstrip('/').lower()}"


def kind_for(url: str) -> str:
    key = url_key(url)
    if any(hint in key for hint in AGGREGATOR_HOST_HINTS):
        return "aggregator"
    return "official"


def source_id(prefix: str, key: str) -> str:
    import hashlib

    return f"{prefix}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}"


def build_sites(raw_sites: list) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for raw in raw_sites:
        if not isinstance(raw, dict):
            continue
        company = str(raw.get("cat") or raw.get("company") or "").strip()
        url = str(raw.get("url") or "").strip()
        industry = str(raw.get("ind") or raw.get("industry") or "").strip()
        key = url_key(url)
        if not company or not key or key in seen:
            continue
        seen.add(key)
        result.append({
            "id": source_id("xz", key),
            "name": f"{company}（校招雷达源）",
            "company": company,
            "url": url,
            "kind": kind_for(url),
            "priority": 20,
            "industry": industry,
            "campus": True,
            "rendered": True,
            "pool": "xiaozhao-sites",
        })
    return result


def clean_batch_label(value: str, max_len: int = 60) -> str:
    label = re.sub(r"\s+", " ", str(value or "")).strip()
    label = re.sub(r"^批次[:：]\s*", "", label)
    return label[:max_len] or "校招批次"


def build_batches(raw_jobs: list) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        company = str(raw.get("c") or "").strip()
        url = str(raw.get("u") or "").strip()
        key = url_key(url)
        if not company or not key or key in seen:
            continue
        seen.add(key)
        batch = clean_batch_label(raw.get("w"))
        result.append({
            "id": source_id("xb", key),
            "name": f"{company}（聚合·{batch}）",
            "company": company,
            "url": url,
            "kind": kind_for(url),
            "priority": 15,
            "industry": str(raw.get("ind") or raw.get("t") or "").strip(),
            "categories": str(raw.get("p") or "").strip()[:400],
            "locations": str(raw.get("l") or "").strip()[:200],
            "deadline": str(raw.get("d") or "").strip()[:80],
            "batch": batch,
            "campus": True,
            "rendered": True,
            "pool": "xiaozhao-batches",
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="同步校招雷达开源信源池")
    parser.add_argument("--source", default=str(DEFAULT_REPO), help="校招雷达仓库目录")
    parser.add_argument("--remote", action="store_true", help="直接从 GitHub 拉取最新数据")
    args = parser.parse_args()

    repo = Path(args.source)
    index_text = read_source_file(repo / "index.html", "index.html", args.remote)
    jobs_payload = json.loads(read_source_file(repo / "jobs.json", "jobs.json", args.remote))
    raw_jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else jobs_payload

    sites = build_sites(extract_js_array(index_text, "const CRAWL_SITES"))
    batches = build_batches(raw_jobs or [])

    # A company batch URL that already exists in the curated site pool would be
    # redundant (same portal crawled twice); the curated site wins.
    site_keys = {url_key(item["url"]) for item in sites}
    batches = [item for item in batches if url_key(item["url"]) not in site_keys]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "xiaozhao_sites.json").write_text(
        json.dumps(sites, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (DATA_DIR / "xiaozhao_batches.json").write_text(
        json.dumps(batches, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"站点池: {len(sites)} 个（CRAWL_SITES）")
    print(f"聚合批次: {len(batches)} 个（jobs.json 去重后）")
    print(f"已写入 {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

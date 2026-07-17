#!/usr/bin/env python3
"""Amazon exit-sign occupancy collector â Python port of refresh-dashboard.mjs.
Zero dependencies beyond the Python standard library."""

import json, re, time, copy, sys, os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote_plus

CST = timezone(timedelta(hours=8))

# ââ paths ââââââââââââââââââââââââââââââââââââââââââââââ
SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE  = SCRIPT_DIR.parent.parent
DASHBOARD  = WORKSPACE / "outputs" / "occupancy-dashboard"
SETTINGS   = SCRIPT_DIR / "settings.json"
RAW_HTML   = SCRIPT_DIR / "raw-html"

# ââ regex (ported from mjs) ââââââââââââââââââââââââââââ
ENTRY_RE    = re.compile(
    r'<div\b(?=[^>]*\bdata-component-type=(["\'])s-search-result\1)'
    r'(?=[^>]*\bdata-asin=(["\'])([^"\']*)\2)[^>]*>',
    re.IGNORECASE
)
SPONSORED_RE = re.compile(
    r'data-cy=(["\'])sp-sponsored-result\1|puis-sponsored-label-text|'
    r's-sponsored-label-text|aria-label=(["\'])Sponsored\2|>Sponsored<|'
    r'sp-sponsored-result|s-widget-sponsored-product',
    re.IGNORECASE
)
BLOCKED_RE = re.compile(
    r'robot check|sorry, we just need to make sure you.re not a robot|'
    r'validateCaptcha|enter the characters you see below',
    re.IGNORECASE
)
PRICE_SYMBOL_RE = re.compile(r'\$[\d,]+\.?\d*')


def detect_page_state(html, status):
    if not html:
        return "empty"
    if status == 503 or BLOCKED_RE.search(html):
        return "blocked"
    if "s-search-result" not in html and "data-asin" not in html:
        return "unexpected"
    return "results"


def extract_organic_asins(html, max_slots):
    matches = [(m.group(3).strip(), m.start()) for m in ENTRY_RE.finditer(html) if m.group(3).strip()]
    if not matches:
        return [], [], {}

    organic, sponsored, details, seen = [], [], {}, set()

    for i, (asin, start) in enumerate(matches):
        end = matches[i + 1][1] if i + 1 < len(matches) else len(html)
        chunk = html[start:end]
        is_sp = bool(SPONSORED_RE.search(chunk))

        price = None
        pm = PRICE_SYMBOL_RE.search(chunk)
        if pm:
            try:
                price = float(pm.group().replace("$", "").replace(",", ""))
            except ValueError:
                price = None

        review_count = None
        rm = re.search(r'(?:>\(?(\d[\d,]*)\)?\s*(?:ratings?|reviews?)|aria-label="(\d[\d,]*) ratings?")', chunk, re.IGNORECASE)
        if rm:
            try:
                review_count = int((rm.group(1) or rm.group(2) or "").replace(",", ""))
            except ValueError:
                review_count = None

        rating = None
        ratm = re.search(r'(?:a-star-(\d[\d.-]*)|aria-label="([\d.]*)\s*out of\s*5\s*stars")', chunk, re.IGNORECASE)
        if ratm:
            try:
                rating = float(ratm.group(1) or ratm.group(2))
            except ValueError:
                rating = None

        details[asin] = {"price": price, "reviewCount": review_count, "rating": rating, "isSponsored": is_sp}

        if is_sp:
            if asin not in seen:
                seen.add(asin)
                sponsored.append(asin)
            continue
        if asin in seen:
            continue
        seen.add(asin)
        organic.append(asin)
        if len(organic) >= max_slots:
            break

    return organic, sponsored, details


def format_error(err):
    msg = str(err)
    if isinstance(err, HTTPError):
        return f"HTTP {err.code} {err.reason}"
    if isinstance(err, URLError):
        return f"fetch failed | {err.reason}"
    return f"fetch failed | {msg}"


def to_date_str(dt):
    return dt.astimezone(CST).strftime("%Y-%m-%d")


def to_timestamp(dt):
    return dt.astimezone(CST).strftime("%Y-%m-%dT%H:%M:%S")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json_if_exists(path):
    try:
        return load_json(path)
    except FileNotFoundError:
        return None


def load_dashboard_data(js_path):
    text = Path(js_path).read_text(encoding="utf-8")
    # extract JSON from "window.dashboardData = {...};"
    m = re.search(r'window\.dashboardData\s*=\s*', text)
    if not m:
        raise ValueError("dashboard-data.js has unexpected format")
    start = m.end()
    return json.loads(text[start:].rstrip().rstrip(";"))


def write_dashboard_data(js_path, json_path, data):
    s = json.dumps(data, ensure_ascii=False, indent=2)
    Path(js_path).write_text(f"window.dashboardData = {s};\n", encoding="utf-8")
    Path(json_path).write_text(f"{s}\n", encoding="utf-8")


def write_browser_data(js_path, global_name, data):
    s = json.dumps(data, ensure_ascii=False, indent=2)
    Path(js_path).write_text(f"window.{global_name} = {s};\n", encoding="utf-8")


def build_keyword_targets(data, settings, keyword_filter=None):
    chosen = set(keyword_filter or [])
    targets = []
    for kw in data.get("keywords", []):
        if chosen and kw["key"] not in chosen:
            continue
        override = (settings.get("keywordOverrides") or {}).get(kw["key"], {})
        if override.get("searchUrl"):
            targets.append({"key": kw["key"], "label": kw["label"], "url": override["searchUrl"]})
            continue
        domain = settings["amazonDomain"]
        base = settings["searchBasePath"]
        params = dict(settings.get("defaultQueryParams", {}))
        params["k"] = quote_plus(kw["label"])
        params.update(override.get("queryParams", {}))
        qs = "&".join(f"{quote_plus(str(k))}={quote_plus(str(v))}" for k, v in params.items() if v)
        url = f"https://{domain}{base}?{qs}"
        targets.append({"key": kw["key"], "label": kw["label"], "url": url})
    return targets


def fetch_keyword_html(target, settings):
    headers = {
        "accept": settings["requestHeaders"]["accept"],
        "accept-language": settings["requestHeaders"]["acceptLanguage"],
        "cache-control": settings["requestHeaders"]["cacheControl"],
        "pragma": settings["requestHeaders"]["pragma"],
        "upgrade-insecure-requests": "1",
        "user-agent": settings["requestHeaders"]["userAgent"],
    }
    if settings.get("cookie"):
        headers["cookie"] = settings["cookie"]

    req = Request(target["url"], headers=headers)
    timeout = settings.get("requestTimeoutMs", 30000) / 1000.0
    resp = urlopen(req, timeout=timeout)
    html = resp.read().decode("utf-8", errors="replace")
    return {"finalUrl": resp.geturl(), "status": resp.status, "ok": 200 <= resp.status < 300, "html": html}


def build_empty_snapshot(data, date_str):
    snap = {"date": date_str, "brands": {}}
    for brand in data.get("brands", []):
        snap["brands"][brand["id"]] = {}
        for listing in brand.get("listings", []):
            snap["brands"][brand["id"]][listing["asin"]] = {}
    return snap


def merge_keyword_subset(base, patch, keyword_keys):
    merged = copy.deepcopy(base)
    for brand_id in merged["brands"]:
        for asin in merged["brands"][brand_id]:
            for k in keyword_keys:
                merged["brands"][brand_id][asin].pop(k, None)
            for k, rank in patch["brands"].get(brand_id, {}).get(asin, {}).items():
                merged["brands"][brand_id][asin][k] = rank
    return merged


def apply_snapshot_to_listings(data, snapshot, keyword_keys):
    for brand in data.get("brands", []):
        for listing in brand.get("listings", []):
            for k in keyword_keys:
                listing.setdefault("positions", {}).pop(k, None)
            for k, rank in snapshot["brands"].get(brand["id"], {}).get(listing["asin"], {}).items():
                listing.setdefault("positions", {})[k] = rank


def merge_snapshot_history(data, snapshot):
    history = list(data.get("history", []))
    idx = next((i for i, h in enumerate(history) if h.get("date") == snapshot["date"]), None)
    if idx is not None:
        history[idx] = snapshot
    else:
        history.append(snapshot)
    history.sort(key=lambda h: h["date"])
    return history


def build_snapshot_from_listings(data, date_str):
    snap = build_empty_snapshot(data, date_str)
    for brand in data.get("brands", []):
        for listing in brand.get("listings", []):
            pos = listing.get("positions", {})
            if pos:
                snap["brands"][brand["id"]][listing["asin"]] = dict(pos)
    return snap


def summarize_run(report):
    failed = [r["label"] for r in report.get("keywordResults", []) if r.get("status") != "success"]
    return {
        "status": report["status"],
        "snapshotDate": report["snapshotDate"],
        "startedAt": report["startedAt"],
        "finishedAt": report["finishedAt"],
        "successfulKeywords": report["successfulKeywords"],
        "totalKeywords": report["totalKeywords"],
        "failedKeywords": report["failedKeywords"],
        "dashboardUpdated": report["dashboardUpdated"],
        "failedKeywordLabels": failed,
    }


def merge_recent_runs(prev_report, cur_report, limit=3):
    prior = []
    if prev_report:
        prior = prev_report.get("recentRuns", [])
        if not prior:
            prior = [summarize_run(prev_report)]
    combined = [summarize_run(cur_report)] + prior
    seen, out = set(), []
    for r in combined:
        key = r.get("finishedAt")
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out[:limit]


def run_collection(opts=None):
    opts = opts or {}
    settings = load_json(SETTINGS)
    data = load_dashboard_data(DASHBOARD / "dashboard-data.js")
    prev_report = load_json_if_exists(DASHBOARD / "auto-collection-report.json") if not opts.get("dry_run") else None

    now = datetime.now(CST)
    snapshot_date = opts.get("date") or to_date_str(now)
    started_at = datetime.now(CST)
    tracked_asins = set()
    for brand in data.get("brands", []):
        for listing in brand.get("listings", []):
            tracked_asins.add(listing["asin"])

    keyword_targets = build_keyword_targets(data, settings, opts.get("keywords"))
    selected_keys = [t["key"] for t in keyword_targets]
    snapshot = build_empty_snapshot(data, snapshot_date)
    keyword_results = []

    if not keyword_targets:
        raise SystemExit("No keywords selected for collection.")

    for idx, target in enumerate(keyword_targets):
        if idx > 0:
            time.sleep(settings.get("requestDelayMs", 1500) / 1000.0)

        try:
            resp = fetch_keyword_html(target, settings)
            page_state = detect_page_state(resp["html"], resp["status"])

            if page_state != "results":
                keyword_results.append({
                    "key": target["key"], "label": target["label"], "url": target["url"],
                    "finalUrl": resp["finalUrl"], "status": "failed",
                    "httpStatus": resp["status"], "pageState": page_state,
                    "organicSlotsSeen": 0, "trackedHits": 0,
                    "note": "Amazon blocked the request" if page_state == "blocked"
                            else "Unexpected HTML structure or empty response."
                })
                continue

            organic, sponsored, details = extract_organic_asins(resp["html"], settings.get("maxOrganicSlots", 48))
            positions = {asin: i + 1 for i, asin in enumerate(organic)}

            for brand in data.get("brands", []):
                for listing in brand.get("listings", []):
                    rank = positions.get(listing["asin"])
                    if rank:
                        snapshot["brands"][brand["id"]][listing["asin"]][target["key"]] = rank

            new_entrants = []
            for asin in organic[:10]:
                if asin not in tracked_asins:
                    d = details.get(asin, {})
                    new_entrants.append({
                        "asin": asin, "position": organic.index(asin) + 1,
                        "price": d.get("price"), "reviewCount": d.get("reviewCount"),
                        "rating": d.get("rating")
                    })

            sponsored_tracked = [a for a in sponsored if a in tracked_asins]
            tracked_positions = {asin: i + 1 for i, asin in enumerate(organic) if asin in tracked_asins}
            tracked_hits = sum(1 for a in organic if a in tracked_asins)

            tracked_listings = {}
            for asin in organic:
                if asin in tracked_asins and asin in details:
                    tracked_listings[asin] = details[asin]

            keyword_results.append({
                "key": target["key"], "label": target["label"], "url": target["url"],
                "finalUrl": resp["finalUrl"], "status": "success",
                "httpStatus": resp["status"], "pageState": page_state,
                "organicSlotsSeen": len(organic), "trackedHits": tracked_hits,
                "trackedPositions": tracked_positions,
                "sponsoredSlots": len(sponsored), "sponsoredTracked": sponsored_tracked,
                "newEntrants": new_entrants, "trackedListings": tracked_listings,
            })
        except Exception as exc:
            keyword_results.append({
                "key": target["key"], "label": target["label"], "url": target["url"],
                "status": "failed", "pageState": "exception",
                "organicSlotsSeen": 0, "trackedHits": 0,
                "note": format_error(exc)
            })

    successful = [r for r in keyword_results if r["status"] == "success"]
    failed = [r for r in keyword_results if r["status"] != "success"]
    append_partial = settings.get("appendPartialSnapshots", False)
    min_success = settings.get("minimumSuccessfulKeywords", len(keyword_targets))
    can_append = (len(successful) >= min_success) if append_partial else (len(failed) == 0)

    finished_at = datetime.now(CST)
    report = {
        "status": "success" if can_append else "failed",
        "snapshotDate": snapshot_date,
        "startedAt": to_timestamp(started_at),
        "finishedAt": to_timestamp(finished_at),
        "collector": "amazon-search-html",
        "scheduleLabel": settings.get("scheduleLabel", ""),
        "timeZone": settings.get("timeZone", "Asia/Shanghai"),
        "successfulKeywords": len(successful),
        "totalKeywords": len(keyword_targets),
        "failedKeywords": len(failed),
        "appendPartialSnapshots": append_partial,
        "minimumSuccessfulKeywords": min_success,
        "dashboardUpdated": can_append and not opts.get("dry_run"),
        "keywordResults": keyword_results,
    }
    report["recentRuns"] = merge_recent_runs(prev_report, report)

    next_data = copy.deepcopy(data)
    next_data["automation"] = {
        "collector": "amazon-search-html",
        "scheduleLabel": settings.get("scheduleLabel", ""),
        "timeZone": settings.get("timeZone", "Asia/Shanghai"),
        "lastRunAt": report["finishedAt"],
        "lastRunStatus": report["status"],
        "lastRunSnapshotDate": snapshot_date,
        "lastSuccessDate": snapshot_date if can_append else next_data.get("automation", {}).get("lastSuccessDate") or next_data.get("generatedAt"),
        "successfulKeywords": len(successful),
        "totalKeywords": len(keyword_targets),
        "reportFile": "auto-collection-report.json",
    }

    if can_append:
        existing = next((h for h in next_data.get("history", []) if h.get("date") == snapshot_date), None)
        fallback = build_snapshot_from_listings(next_data, snapshot_date) if snapshot_date == next_data.get("generatedAt") else build_empty_snapshot(next_data, snapshot_date)
        merged = merge_keyword_subset(existing or fallback, snapshot, selected_keys)
        next_data["generatedAt"] = snapshot_date
        next_data["history"] = merge_snapshot_history(next_data, merged)
        apply_snapshot_to_listings(next_data, merged, selected_keys)

    if not opts.get("dry_run"):
        DASHBOARD.mkdir(parents=True, exist_ok=True)
        report_json = DASHBOARD / "auto-collection-report.json"
        report_js = DASHBOARD / "auto-collection-report.js"
        report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_browser_data(str(report_js), "autoCollectionReport", report)
        write_dashboard_data(str(DASHBOARD / "dashboard-data.js"), str(DASHBOARD / "dashboard-data.json"), next_data)

    return report


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--save-raw-html", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--keyword", action="append", default=[])
    args = ap.parse_args()

    opts = {"dry_run": args.dry_run, "date": args.date, "keywords": args.keyword}
    report = run_collection(opts)
    summary = f"{report['status'].upper()} {report['successfulKeywords']}/{report['totalKeywords']} keywords"
    print(summary)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
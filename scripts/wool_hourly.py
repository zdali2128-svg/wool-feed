#!/usr/bin/env python3
"""Hourly GitHub Actions watchdog for the WoolCollector feed.

What it does (and does not do), by design:
- Reads wool.json, preserves every existing item's discoveredAt and all
  other fields untouched, except for one maintenance flip (see below).
- Polls ONE public, no-login source: the LowEndBox blog RSS feed, which
  is where the already-listed HostNamaste Diwali deal came from. It only
  looks for foreign VPS deals, because that is what this feed can judge
  from a title + link alone. It does NOT cover AI subscriptions, takeout
  codes, memberships, hardware "meat" or phone plans -- those need human
  or agent verification and must not be guessed from an RSS title.
- New finds are appended as postOnly, clearly marked "script-discovered,
  not human-verified", capped at 3 per run, and only when the RSS item is
  at most 7 days old, names a dollar price, and is not a domestic cloud
  (Alibaba/Tencent/Huawei etc. are skipped outright).
- Maintenance: if an existing item's deadlineText contains an explicit
  YYYY-MM-DD end date that has already passed, isTodayValid is flipped
  to False (the item stays in the list, it just leaves "today valid",
  the same treatment as the EDIS Black Friday item before it starts).
- Writes wool.json only when something actually changed, sorted by
  discoveredAt descending (newest first, stable within a day).

Stdlib only, no secrets, no logins, no account data.
"""

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
FEED_PATH = "wool.json"
RSS_URL = "https://lowendbox.com/feed/"
MAX_NEW_PER_RUN = 3
MAX_AGE_DAYS = 7

DOMESTIC = ("alibaba", "tencent", "huawei", "阿里", "腾讯", "华为", "国内云")
# Giveaway/contest posts and non-US-West locations are noise for this user,
# even when their titles also mention a VPS price. They must not enter the feed.
EXCLUDE_TITLE = ("giveaway", "win a", "contest", "raffle", "bonus entries", "抽奖")
EXCLUDE_PLACE = ("toronto", "canada", "montreal", "vancouver", "london", "frankfurt",
                 "amsterdam", "singapore", "tokyo", "sydney", "paris", "berlin")
PRICE_RE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)\s*/\s*(year|yr|mo|month)", re.I)
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
TAG_RE = re.compile(r"<[^>]+>")


def today_cst():
    return datetime.now(CST).date()


def strip_tags(text):
    return TAG_RE.sub("", text or "").strip()


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "deal"


def parse_rss_date(raw):
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CST).date()
        except (ValueError, AttributeError):
            continue
    return None


def fetch_rss_items():
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "wool-feed-watchdog/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    items = []
    for el in root.iter("item"):
        title = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip()
        desc = strip_tags(el.findtext("description") or "")
        pub = parse_rss_date(el.findtext("pubDate") or "")
        items.append({"title": title, "link": link, "desc": desc, "pub": pub})
    return items


def main():
    today = today_cst()
    with open(FEED_PATH, encoding="utf-8") as f:
        feed = json.load(f)
    items = feed.get("items", [])
    seen_links = {it.get("originLink") for it in items}
    seen_ids = {it.get("id") for it in items}
    changed = False

    # Maintenance: explicit deadline passed -> no longer today-valid.
    for it in items:
        m = DATE_RE.search(it.get("deadlineText") or "")
        if m and it.get("isTodayValid"):
            end = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            if end < today:
                it["isTodayValid"] = False
                changed = True

    # Discovery: LowEndBox RSS, foreign VPS only, price required.
    added = 0
    try:
        rss_items = fetch_rss_items()
    except Exception as exc:  # noqa: BLE001 - watchdog must not fail the run on a feed outage
        print(f"RSS fetch failed, keeping existing feed: {exc}", file=sys.stderr)
        rss_items = []
    for rss in rss_items:
        if added >= MAX_NEW_PER_RUN:
            break
        title, link, desc = rss["title"], rss["link"], rss["desc"]
        if not title or not link or link in seen_links:
            continue
        low = (title + " " + desc).lower()
        if any(d in low for d in DOMESTIC):
            continue
        if any(d in title.lower() for d in EXCLUDE_TITLE):
            continue
        if any(d in low for d in EXCLUDE_PLACE):
            continue
        if "vps" not in low and "dedicated" not in low and "kvm" not in low:
            continue
        price = PRICE_RE.search(title + " " + desc)
        if not price:
            continue
        pub = rss["pub"]
        if pub is not None and (today - pub).days > MAX_AGE_DAYS:
            continue
        amount, period = price.group(1), price.group(2).lower()
        annual = float(amount) * (12 if period.startswith("mo") else 1)
        if annual > 60:
            continue
        item_id = "lowendbox-" + slugify(link.rstrip("/").split("/")[-1] or title)
        if item_id in seen_ids:
            continue
        unit = "/年" if period.startswith(("y", "yr")) else "/月"
        new_item = {
            "id": item_id,
            "title": title,
            "category": "vps",
            "isSample": False,
            "headline": (desc[:90] + "…") if len(desc) > 90 else desc,
            "what": desc or title,
            "savingDetail": f"标题价约 ${amount}{unit}，以原链接结算页为准",
            "deadlineText": f"RSS 发布时间 {pub.isoformat() if pub else '未知'}，以原链接为准",
            "isTodayValid": True,
            "threshold": "国外 VPS，以原链接配置与付款周期为准",
            "pitfalls": "脚本每小时巡检自动发现，未经人工核实：价格、配置、库存、续费价均以原链接为准，不要只按标题价下单；国内 AI、国内云不在本脚本覆盖范围",
            "sourceChannel": "脚本巡检：LowEndBox 公开 RSS",
            "credibility": "postOnly",
            "originLink": link,
            "lastVerified": today.isoformat(),
            "discoveredAt": today.isoformat(),
            "originalPrice": None,
            "currentPrice": None,
            "discountText": None,
            "historyLowText": None,
            "secondhandPrice": None,
            "shippingFee": None,
            "configText": None,
            "salesChannel": None,
            "warrantyText": None,
            "xianyuPrice": None,
            "upstreamActivity": None,
            "upstreamEntry": None,
            "riskLevel": None,
            "takeoutPlatform": None,
            "takeoutRegion": None,
        }
        items.append(new_item)
        seen_links.add(link)
        seen_ids.add(item_id)
        added += 1
        changed = True

    if not changed:
        print("no change, not rewriting")
        return 0
    # Newest discovery first, stable within a day; never touch discoveredAt.
    items.sort(key=lambda it: it.get("discoveredAt") or "", reverse=True)
    feed["items"] = items
    feed["updatedAt"] = datetime.now(CST).strftime("%Y-%m-%dT%H:%M+08:00")
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"updated: added={added}, total={len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

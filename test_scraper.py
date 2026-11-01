#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import re
import json
import traceback
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup
from cloud_monitor import get_latest, CHAPTER_RE, is_paid, norm, fix_chap_url

FLARESOLVERR_URL = "http://localhost:8191/v1"


def fetch_via_flaresolverr(url: str) -> str:
    """لایه ۱: باز کردن سایت با مرورگر واقعی FlareSolverr برای حل چالش کلودفلر"""
    print("[1/3] 🛡️ تلاش با مرورگر ضد کلودفلر (FlareSolverr)...")
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000,
    }
    r = requests.post(FLARESOLVERR_URL, json=payload, timeout=70)
    if r.status_code == 200:
        data = r.json()
        if data.get("status") == "ok":
            html = data.get("solution", {}).get("response", "")
            if html and "Just a moment..." not in html:
                print("  ✅ کلودفلر با موفقیت توسط FlareSolverr دور زده شد!")
                return html
    raise ValueError("FlareSolverr نتوانست صفحه را باز کند")


def fetch_via_jina(url: str) -> str:
    """لایه ۲: استفاده از رندر ابری Jina Reader (با آی‌پی خارج از گیت‌هاب)"""
    print("[2/3] 🌐 تلاش با رندر ابری Jina Reader...")
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "text/html,application/xhtml+xml,*/*",
        "X-Return-Format": "html",
    }
    r = requests.get(jina_url, headers=headers, impersonate="chrome124", timeout=35)
    if r.status_code == 200 and len(r.text) > 300 and "403 Forbidden" not in r.text:
        print("  ✅ صفحه با موفقیت از طریق Jina Reader دریافت شد!")
        return r.text

    # حالت مارک‌داون Jina
    r2 = requests.get(jina_url, impersonate="chrome124", timeout=35)
    if r2.status_code == 200 and len(r2.text) > 200:
        print("  ✅ متن صفحه از طریق Jina Markdown دریافت شد!")
        return r2.text
    raise ValueError("Jina Reader نتوانست صفحه را دریافت کند")


def parse_anti_cf_content(content: str, base_url: str, include_paid: bool = False):
    """استخراج آخرین چپتر رایگان و رد کردن چپترهای سکه‌دار (مثل 10 🪙 در LuaComic)"""
    free_only = not include_paid
    raw = norm(content)
    soup = BeautifulSoup(raw, "html.parser")

    slug = urlparse(base_url).path.rstrip("/").split("/")[-1].lower()
    cands = []

    # ۱. جستجو در تگ‌های لینک HTML
    for a in soup.find_all("a"):
        href = a.get("href", "") or ""
        if slug and len(slug) >= 4 and slug not in href.lower() and "chapter" not in href.lower():
            continue
        row_text = norm(a.get_text(" ", strip=True) + " " + (a.parent.get_text(" ", strip=True) if a.parent else ""))
        m = CHAPTER_RE.search(row_text) or CHAPTER_RE.search(href)
        if not m:
            continue
        ch_num = m.group(1)

        # تشخیص قفل یا سکه (مثلاً "10" کنار آیکون سکه/قفل یا svg در ردیف چپتر)
        row_html = str(a) + " " + (str(a.parent) if a.parent else "")
        paid_flag = is_paid(a) or bool(
            re.search(r"coin|lock|premium|paywall|🪙|🔒|\b\d+\s*<svg", row_html, re.I)
        )
        cands.append((float(ch_num), ch_num, fix_chap_url(base_url, href), paid_flag))

    # ۲. اگر محتوا به صورت Markdown (از Jina) برگشته باشد
    if not cands:
        for line in raw.splitlines():
            m = CHAPTER_RE.search(line)
            if m:
                ch_num = m.group(1)
                # در LuaComic چپتر پولی بعد از نام چپتر یک عدد سکه مثل "Chapter 44 10" دارد
                after_chap = line[m.end():].strip()
                paid_flag = bool(
                    re.search(r"^(?:\d{1,3}\b(?!\s*(?:days?|hours?|mins?|seconds?|ago|/)))|coin|lock|🪙|🔒", after_chap, re.I)
                )
                m_url = re.search(r"\((https?://[^\s\)]+)\)", line)
                ch_link = m_url.group(1) if m_url else f"{base_url.rstrip('/')}/chapter-{ch_num}"
                cands.append((float(ch_num), ch_num, ch_link, paid_flag))

    if not cands:
        raise ValueError("هیچ چپتری در محتوای دریافتی پیدا نشد")

    if free_only and any(not c[3] for c in cands):
        cands = [c for c in cands if not c[3]]

    best = max(cands, key=lambda x: x[0])
    return best[1], best[2]


def smart_get_latest(url: str, selector: str = "", include_paid: bool = False):
    # ابتدا تلاش با موتور اصلی
    try:
        print("[0/3] ⚡ تلاش اول با موتور مستقیم...")
        return get_latest(url, selector=selector, include_paid=include_paid)
    except Exception as e:
        print(f"  ⚠️ موتور مستقیم رد نشد ({e}) -> فعال‌سازی سیستم ضد کلودفلر...")

    # لایه ۱: FlareSolverr
    try:
        html = fetch_via_flaresolverr(url)
        return parse_anti_cf_content(html, url, include_paid=include_paid)
    except Exception as e1:
        print(f"  ⚠️ لایه ۱ رد نشد: {e1}")

    # لایه ۲: Jina Cloud Reader
    html_or_md = fetch_via_jina(url)
    return parse_anti_cf_content(html_or_md, url, include_paid=include_paid)


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("❌ لطفا یک لینک برای تست وارد کنید.")
        sys.exit(1)

    url = sys.argv[1].strip()
    include_paid = (len(sys.argv) > 2 and sys.argv[2].strip().lower() == "true")
    selector = sys.argv[3].strip() if len(sys.argv) > 3 else ""

    print("=" * 55)
    print("🔍 شروع تست لینک با موتور ۳ لایه ضد کلودفلر:")
    print(f"🔗 آدرس: {url}")
    print(f"🔓 شامل چپترهای قفل/پولی: {include_paid}")
    print("=" * 55)

    try:
        ch, link = smart_get_latest(url, selector=selector, include_paid=include_paid)
        print("\n✅ نتیجه تست موفقیت‌آمیز بود!")
        print(f"📖 آخرین چپتر تشخیص داده شده: {ch}")
        print(f"🔗 لینک چپتر: {link}")
        print("=" * 55)
    except Exception as e:
        print(f"\n❌ خطا در تشخیص چپتر: {e}")
        print("-" * 55)
        traceback.print_exc()
        print("=" * 55)
        sys.exit(1)


if __name__ == "__main__":
    main()

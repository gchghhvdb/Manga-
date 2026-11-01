#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import re
import time
import traceback
from urllib.parse import urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup
from cloud_monitor import get_latest, CHAPTER_RE, is_paid, norm, fix_chap_url, _paid_row


def is_cf_challenge(text: str) -> bool:
    """بررسی اینکه آیا متن دریافتی همان صفحه قفل کلودفلر است یا خیر"""
    low = text.lower()
    return any(
        x in low
        for x in (
            "just a moment...",
            "attention required! | cloudflare",
            "cf-browser-verification",
            "verify you are human",
            "ray id:",
        )
    ) and ("chapter" not in low)


def fetch_via_camoufox(url: str, free_only: bool = True):
    """باز کردن سایت با مرورگر ضد کلودفلر Camoufox و شنود مستقیم API چپترها"""
    print("[1/2] 🦊 باز کردن سایت با مرورگر ضد کلودفلر Camoufox...")
    from camoufox.sync_api import Camoufox

    captured_chapters = []

    def handle_response(response):
        try:
            if "api." in response.url or "chapter" in response.url or "series" in response.url:
                if "json" in (response.headers.get("content-type") or ""):
                    data = response.json()
                    rows = []
                    if isinstance(data, dict):
                        rows = data.get("data") or data.get("chapters") or []
                        for s in (data.get("seasons") or []):
                            if isinstance(s, dict) and s.get("chapters"):
                                rows.extend(s.get("chapters"))
                    elif isinstance(data, list):
                        rows = data
                    for r in rows:
                        if isinstance(r, dict) and ("chapter_name" in r or "chapter_slug" in r):
                            captured_chapters.append(r)
        except Exception:
            pass

    with Camoufox(headless=True, humanize=True) as browser:
        page = browser.new_page()
        page.on("response", handle_response)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # صبر برای رد شدن کلودفلر و لود شدن لیست چپترها
        for i in range(8):
            page.wait_for_timeout(3000)
            title = page.title()
            content = page.content()
            if "just a moment" not in title.lower() and re.search(r"chapter\s*\d+", content, re.I):
                break
            # اگر کلودفلر چک‌باکس دارد، روی آن کلیک کن
            try:
                for frame in page.frames:
                    if "cloudflare" in frame.url or "turnstile" in frame.url:
                        box = frame.locator("input[type='checkbox'], body").first
                        if box:
                            box.click(timeout=1500)
            except Exception:
                pass

        html = page.content()
        title = page.title()
        print(f"  📄 عنوان صفحه باز شده: {title}")

    # ۱. اگر از ترافیک شبکه (API)، لیست دقیق چپترها شکار شده باشد:
    if captured_chapters:
        print(f"  🎯 تعداد {len(captured_chapters)} چپتر مستقیماً از API داخلی سایت شکار شد!")
        p = urlparse(url)
        slug = p.path.rstrip("/").split("/")[-1]
        best = None
        for ch in captured_chapters:
            if free_only and _paid_row(ch):
                continue
            name = f"{ch.get('chapter_name', '')} {ch.get('chapter_slug', '')}"
            mm = CHAPTER_RE.search(name) or re.search(r"\d+(?:\.\d+)?", name)
            if not mm:
                continue
            label = mm.group(1) if mm.lastindex else mm.group(0)
            if best is None or float(label) > best[0]:
                best = (float(label), label, ch.get("chapter_slug", "") or "")
        if best:
            link = f"{p.scheme}://{p.netloc}/series/{slug}/{best[2]}" if best[2] else url
            return best[1], link

    return parse_anti_cf_content(html, url, include_paid=not free_only)


def fetch_via_jina(url: str, include_paid: bool = False):
    """تلاش با رندر ابری Jina به همراه بررسی واقعی بودن محتوا"""
    print("[2/2] 🌐 تلاش با رندر ابری Jina Reader...")
    jina_url = f"https://r.jina.ai/{url}"
    r = requests.get(
        jina_url,
        headers={"X-wait-for-selector": "a", "X-Timeout": "25"},
        impersonate="chrome124",
        timeout=35,
    )
    txt = r.text
    first_lines = " | ".join(line.strip() for line in txt.splitlines()[:8] if line.strip())
    print(f"  📄 پیش‌نمایش متن Jina: {first_lines[:180]}...")

    if is_cf_challenge(txt) or "403 Forbidden" in txt or "Target URL returned error 403" in txt:
        raise ValueError("Jina هم توسط کلودفلر این سایت مسدود شد (403 / Challenge)")

    return parse_anti_cf_content(txt, url, include_paid=include_paid)


def parse_anti_cf_content(content: str, base_url: str, include_paid: bool = False):
    free_only = not include_paid
    raw = norm(content)
    soup = BeautifulSoup(raw, "html.parser")

    slug = urlparse(base_url).path.rstrip("/").split("/")[-1].lower()
    cands = []

    for a in soup.find_all("a"):
        href = a.get("href", "") or ""
        if slug and len(slug) >= 4 and slug not in href.lower() and "chapter" not in href.lower():
            continue
        row_text = norm(a.get_text(" ", strip=True) + " " + (a.parent.get_text(" ", strip=True) if a.parent else ""))
        m = CHAPTER_RE.search(row_text) or CHAPTER_RE.search(href)
        if not m:
            continue
        ch_num = m.group(1)

        row_html = str(a) + " " + (str(a.parent) if a.parent else "")
        # در LuaComic چپتر پولی داخلش یک تگ با عدد سکه (مثلاً 10) و آیکون سکه/svg دارد
        paid_flag = is_paid(a) or bool(
            re.search(r"coin|lock|premium|paywall|🪙|🔒|\b\d+\s*<svg", row_html, re.I)
        )
        cands.append((float(ch_num), ch_num, fix_chap_url(base_url, href), paid_flag))

    if not cands:
        for line in raw.splitlines():
            m = CHAPTER_RE.search(line)
            if m:
                ch_num = m.group(1)
                after_chap = line[m.end():].strip()
                paid_flag = bool(
                    re.search(r"^(?:\d{1,3}\b(?!\s*(?:days?|hours?|mins?|seconds?|ago|/)))|coin|lock|🪙|🔒", after_chap, re.I)
                )
                m_url = re.search(r"\((https?://[^\s\)]+)\)", line)
                ch_link = m_url.group(1) if m_url else f"{base_url.rstrip('/')}/chapter-{ch_num}"
                cands.append((float(ch_num), ch_num, ch_link, paid_flag))

    if not cands:
        preview = soup.get_text(" ", strip=True)[:200]
        raise ValueError(f"چپتری در صفحه پیدا نشد. (محتوای دریافتی: {preview})")

    if free_only and any(not c[3] for c in cands):
        cands = [c for c in cands if not c[3]]

    best = max(cands, key=lambda x: x[0])
    return best[1], best[2]


def smart_get_latest(url: str, selector: str = "", include_paid: bool = False):
    try:
        print("[0/2] ⚡ تلاش اول با موتور مستقیم...")
        return get_latest(url, selector=selector, include_paid=include_paid)
    except Exception as e:
        print(f"  ⚠️ موتور مستقیم رد نشد ({e})")

    try:
        return fetch_via_camoufox(url, free_only=not include_paid)
    except Exception as e1:
        print(f"  ⚠️ لایه ۱ (Camoufox) رد نشد: {e1}")

    return fetch_via_jina(url, include_paid=include_paid)


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("❌ لطفا یک لینک برای تست وارد کنید.")
        sys.exit(1)

    url = sys.argv[1].strip()
    include_paid = (len(sys.argv) > 2 and sys.argv[2].strip().lower() == "true")
    selector = sys.argv[3].strip() if len(sys.argv) > 3 else ""

    print("=" * 55)
    print("🔍 شروع تست لینک در Sandbox:")
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
        print("=" * 55)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re
import sys
import traceback
from urllib.parse import urlparse
from curl_cffi import requests
from cloud_monitor import get_latest, HEADERS


def probe(label, u, accept=None):
    hdr = dict(HEADERS)
    if accept:
        hdr["Accept"] = accept
    print(f"[{label}] {u}")
    try:
        r = requests.get(u, headers=hdr, impersonate="chrome124", timeout=20)
        body = (r.text or "")[:200].replace("\n", " ")
        print(f"   status={r.status_code} server={r.headers.get('server')} "
              f"cf-ray={'yes' if r.headers.get('cf-ray') else 'no'} "
              f"cf-mitigated={r.headers.get('cf-mitigated')}")
        print(f"   body: {body}")
    except Exception as e:
        print(f"   خطا: {e}")


def diagnose(url):
    print("\n🩺 تشخیص بلاک شدن:")
    p = urlparse(url)
    host = p.netloc.removeprefix("www.")
    probe("صفحه", url)
    probe("صفحه اصلی", f"{p.scheme}://{p.netloc}/")
    m = re.fullmatch(r"/(?:series|comic)/([^/]+)/?", p.path)
    if m:
        probe("API", f"{p.scheme}://api.{host}/series/{m.group(1)}", "application/json")


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("❌ لطفا یک لینک برای تست وارد کنید.")
        sys.exit(1)

    url = sys.argv[1].strip()
    include_paid = (len(sys.argv) > 2 and sys.argv[2].strip().lower() == "true")
    selector = sys.argv[3].strip() if len(sys.argv) > 3 else ""

    print("=" * 55)
    print("🔍 شروع تست لینک (بدون ارسال نوتیفیکیشن و بدون ذخیره):")
    print(f"🔗 آدرس: {url}")
    print(f"🔓 شامل چپترهای قفل/پولی: {include_paid}")
    if selector:
        print(f"🎯 سلکتور دستی: {selector}")
    print("=" * 55)

    try:
        ch, link = get_latest(url, selector=selector, include_paid=include_paid)
        print("\n✅ نتیجه تست موفقیت‌آمیز بود!")
        print(f"📖 آخرین چپتر تشخیص داده شده: {ch}")
        print(f"🔗 لینک چپتر: {link}")
        print("=" * 55)
    except Exception as e:
        print(f"\n❌ خطا در تشخیص چپتر: {e}")
        print("-" * 55)
        traceback.print_exc()
        diagnose(url)
        print("=" * 55)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import re
import unicodedata
from urllib.parse import urljoin, urlparse, quote
from curl_cffi import requests
from bs4 import BeautifulSoup

try:
    import warnings
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

LIST_FILE = "manhwa_list.json"
SUBS_FILE = "subscribers.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TIMEOUT = 25

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko,ja,en-US;q=0.9,en;q=0.8",
}

CHAPTER_RE = re.compile(r"(?<![a-z])(?:chapter|chap|ch|episode|ep)[\s\-_.:#/]*(\d{1,5}(?:\.\d+)?)(?!\d)", re.I)
JP_RE = re.compile(r"第\s*(\d+(?:\.\d+)?)\s*話(?:\s*(?:その|パート|part|\.|[-_])?\s*(\d+))?", re.I)
KR_RE = re.compile(r"(?:제\s*)?(\d+(?:\.\d+)?)\s*화")
KEY_RE = re.compile(r"\s*(\d+(?:\.\d+)?)(?:\s*その\s*(\d+))?")
LOCKED_RE = re.compile(r"locked|coins?|premium|paywall|10\s*🪙|🔒", re.I)

def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or ""))

def chap_key(s):
    m = KEY_RE.match(norm(s))
    return (float(m.group(1)), int(m.group(2) or 0)) if m else None

def fix_chap_url(base_url: str, href: str) -> str:
    if not href:
        return base_url
    if href.startswith("http"):
        return href
    return urljoin(base_url, href)

def luacomic_latest(url: str, free_only: bool = True):
    """مدیریت اختصاصی لینک‌های Luacomic برای دور زدن خطای 403"""
    if "luacomic.org" not in url.lower():
        return None
    
    # اگر لینک مستقیم به یک چپتر داده شده باشد (مثل /chapter-43)
    m_ch = re.search(r"/chapter[-/](\d+(?:\.\d+)?)", url)
    if m_ch:
        cur_num = float(m_ch.group(1))
        base_series_url = url.split("/chapter")[0]
        
        # بررسی چپترهای بعدی (آیا چپتر بعدی رایگان شده یا پولی است؟)
        best_num = cur_num
        best_url = url
        
        for step in (1, 2):
            next_num = cur_num + step
            next_str = str(int(next_num)) if next_num.is_integer() else str(next_num)
            next_url = f"{base_series_url}/chapter-{next_str}"
            try:
                r = requests.get(next_url, headers=HEADERS, impersonate="chrome124", timeout=10)
                if r.status_code == 200:
                    txt = r.text
                    # اگر چپتر پولی باشد معمولاً کلمات مربوط به سکه یا قفل در صفحه است
                    if free_only and re.search(r"10\s*🪙|coins?|locked|purchase", txt, re.I):
                        break
                    best_num = next_num
                    best_url = next_url
                else:
                    break
            except Exception:
                break
                
        best_str = str(int(best_num)) if best_num.is_integer() else str(best_num)
        return best_str, best_url
    return None

def sync_telegram_subscribers() -> set:
    subs = set()
    if TG_CHAT_ID:
        for cid in re.split(r"[\s,]+", TG_CHAT_ID):
            if cid:
                subs.add(str(cid))

    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    subs.update(str(x) for x in data if x)
        except Exception:
            pass

    if not TG_TOKEN:
        return subs

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout": 5},
            timeout=15,
        )
        if r.ok:
            updates = r.json().get("result", [])
            for u in updates:
                msg = u.get("message") or u.get("my_chat_member") or {}
                chat = msg.get("chat") or {}
                cid = str(chat.get("id") or "")
                text = (msg.get("text") or "").strip().lower()
                if cid:
                    if text == "/stop":
                        subs.discard(cid)
                    else:
                        subs.add(cid)
    except Exception:
        pass

    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(subs), f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception:
        pass

    return subs

def notify(title: str, message: str, link: str = "", tg_subs: set = None) -> bool:
    ok = False
    if NTFY_TOPIC:
        payload = {
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": 4,
            "tags": ["books"],
        }
        if link:
            payload["click"] = link
            payload["actions"] = [{"action": "view", "label": "📖 باز کردن", "url": link, "clear": True}]
        try:
            r = requests.post(NTFY_SERVER, json=payload, timeout=15)
            ok = ok or r.ok
        except Exception:
            pass

    if TG_TOKEN and tg_subs:
        text = f"📚 <b>{title}</b>\n{message}"
        if link:
            text += f'\n\n<a href="{link}">📖 باز کردن چپتر</a>'
        for cid in tg_subs:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                    timeout=15,
                )
                ok = ok or r.ok
            except Exception:
                pass
    return ok

def get_latest(url: str, selector: str = "", include_paid: bool = False):
    free_only = not include_paid

    # بررسی ویژه Luacomic برای عبور از کلودفلر
    lua = luacomic_latest(url, free_only=free_only)
    if lua:
        return lua[0], lua[1]

    r = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    
    anchors = soup.find_all("a")
    cands = []
    for a in anchors:
        href = a.get("href", "") or ""
        m = CHAPTER_RE.search(a.get_text(" ", strip=True) + " " + href)
        if m:
            cands.append(((float(m.group(1)), 0), m.group(1), href, False))
            
    if not cands:
        raise ValueError("چپتری پیدا نشد")
        
    best = max(cands, key=lambda x: x[0])
    return best[1], fix_chap_url(url, best[2])

def main():
    tg_subs = sync_telegram_subscribers()
    with open(LIST_FILE, "r", encoding="utf-8") as f:
        items = json.load(f)

    changed = False
    for it in items:
        if it.get("paused") or not it.get("url"):
            continue
        title = it.get("title", "بدون نام")
        try:
            ch, link = get_latest(it["url"], it.get("selector", ""), bool(it.get("include_paid")))
        except Exception as e:
            print(f"[!] خطا در {title}: {e}")
            continue

        last = str(it.get("last_chapter", "") or "")
        if not last:
            it["last_chapter"] = ch
            it["last_link"] = link
            changed = True
        elif ch != last:
            kc, kl = chap_key(ch), chap_key(last)
            if kc and kl and kc > kl:
                notify(f"چپتر جدید: {title}", f"جدید: چپتر {ch}\nقبلی: چپتر {last}", link, tg_subs)
                it["last_chapter"] = ch
                it["last_link"] = link
                changed = True

    if changed:
        with open(LIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.write("\n")

if __name__ == "__main__":
    main()

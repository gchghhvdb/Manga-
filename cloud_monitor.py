#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import re
from urllib.parse import urljoin
from curl_cffi import requests
from bs4 import BeautifulSoup

LIST_FILE = "manhwa_list.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TIMEOUT = 25

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CHAPTER_RE = re.compile(r"(?<![a-z])(?:chapter|chap|ch|episode|ep)[\s\-_.:#/]*(\d{1,5}(?:\.\d+)?)(?!\d)", re.I)
JP_RE = re.compile(r"第\s*(\d+)\s*話(?:\s*その\s*(\d+))?")
KEY_RE = re.compile(r"\s*(\d+(?:\.\d+)?)(?:\s*その\s*(\d+))?")
PAID_RE = re.compile(r"\b(?:lock(?:ed)?|coins?|premium|paywall)\b|lucide-lock|fa-lock|🔒|🪙", re.I)
OPEN_RE = re.compile(r"[\w-]*(?:unlock|lock[\w-]*open)[\w-]*", re.I)

def chap_key(s):
    m = KEY_RE.match(str(s or ""))
    return (float(m.group(1)), int(m.group(2) or 0)) if m else None

def is_paid(tag) -> bool:
    if PAID_RE.search(OPEN_RE.sub(" ", str(tag))):
        return True
    parent = tag.parent
    if parent and PAID_RE.search(OPEN_RE.sub(" ", str(parent))):
        return True
    return False

def notify(title: str, message: str, link: str = "") -> bool:
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

    if TG_TOKEN and TG_CHAT_ID:
        text = f"📚 <b>{title}</b>\n{message}"
        if link:
            text += f'\n\n<a href="{link}">📖 باز کردن چپتر</a>'
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
            ok = ok or r.ok
        except Exception:
            pass
    return ok

def get_latest(url: str, selector: str = "", include_paid: bool = False):
    resp = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    if selector.strip():
        el = soup.select_one(selector)
        if not el:
            raise ValueError("Selector پیدا نشد")
        m = re.search(r"\d+(?:\.\d+)?", el.get_text(" ", strip=True))
        if not m:
            raise ValueError("عددی در متن نبود")
        a = el if el.name == "a" else (el.find("a") or el.find_parent("a"))
        href = a.get("href", "") if a else ""
        return m.group(0), (urljoin(url, href) if href else url)

    cands = []
    for a in soup.find_all("a"):
        href = a.get("href", "") or ""
        text = a.get_text(" ", strip=True) + " " + href
        m = JP_RE.search(text)
        if m:
            n, sub = int(m.group(1)), int(m.group(2) or 0)
            label = f"{n} その{sub}" if m.group(2) else str(n)
            cands.append(((float(n), sub), label, href, is_paid(a)))
            continue
        m = CHAPTER_RE.search(text)
        if m:
            cands.append(((float(m.group(1)), 0), m.group(1), href, is_paid(a)))

    if not include_paid and any(not c[3] for c in cands):
        cands = [c for c in cands if not c[3]]

    if not cands:
        raise ValueError("چپتری پیدا نشد")

    best = max(cands, key=lambda x: x[0])
    return best[1], (urljoin(url, best[2]) if best[2] else url)

def main():
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
            print(f"[*] مقدار اولیه برای {title}: چپتر {ch}")
        elif ch != last:
            kc, kl = chap_key(ch), chap_key(last)
            if kc and kl and kc > kl and (kc[0] - kl[0] <= 50):
                print(f"[+] چپتر جدید برای {title}: {last} -> {ch}")
                notify(f"چپتر جدید: {title}", f"جدید: چپتر {ch}\nقبلی: چپتر {last}", link)
                it["last_chapter"] = ch
                it["last_link"] = link
                changed = True

    if changed:
        with open(LIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.write("\n")

if __name__ == "__main__":
    main()

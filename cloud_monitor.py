#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import re
import unicodedata
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup

try:
    import warnings
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

LIST_FILE = "manhwa_list.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TIMEOUT = 25

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

CHAPTER_RE = re.compile(r"(?<![a-z])(?:chapter|chap|ch|episode|ep)[\s\-_.:#/]*(\d{1,5}(?:\.\d+)?)(?!\d)", re.I)
# پشتیبانی از شماره‌های ژاپنی مثل 第145話، 第98.5話، 第44話その2، [150話]
JP_RE = re.compile(r"第\s*(\d+(?:\.\d+)?)\s*話(?:\s*(?:その|パート|part)?\s*(\d+))?", re.I)
RSS_N_RE = re.compile(r"(?:^|[\[\(【\s])(\d+(?:\.\d+)?)\s*話(?:\s*(?:その|パート)?\s*(\d+))?")
CW_JSON_RE = re.compile(r'"(?:title|episodeTitle|subTitle|name)"\s*:\s*"[^"]*?第\s*(\d+(?:\.\d+)?)\s*話(?:\s*(?:その)?\s*(\d+))?')
KEY_RE = re.compile(r"\s*(\d+(?:\.\d+)?)(?:\s*その\s*(\d+))?")
RECO_RE = re.compile(r"recommend|related|similar|you may also|more like|trending|popular", re.I)
PAID_RE = re.compile(r"\b(?:lock(?:ed)?|coins?|premium|paywall)\b|lucide-lock|fa-lock|🔒|🪙", re.I)
OPEN_RE = re.compile(r"[\w-]*(?:unlock|lock[\w-]*open)[\w-]*", re.I)
EMB_RE = re.compile(r"\{[^{}]*?chapter_(?:name|slug)[^{}]*\}")
LOCKED_RE = re.compile(
    r"locked chapter|waiting to be unlocked|chapter is locked|login to unlock chapter"
    r"|cost to unlock|sign in to unlock|unlock this (?:exciting )?chapter", re.I
)

def norm(s: str) -> str:
    """تبدیل اعداد و حروف تمام‌عرض ژاپنی (مثل １２３) به استاندارد (123)"""
    return unicodedata.normalize("NFKC", str(s or ""))

def _jp(m):
    num_str = m.group(1)
    n = float(num_str)
    sub = int(m.group(2) or 0)
    base = str(int(n)) if n.is_integer() else num_str
    return (n, sub), (f"{base} その{sub}" if sub else base)

def chap_key(s):
    m = KEY_RE.match(norm(s))
    return (float(m.group(1)), int(m.group(2) or 0)) if m else None

def _paid_text(x) -> bool:
    return bool(PAID_RE.search(OPEN_RE.sub(" ", str(x))))

def _chap_anchor(x) -> bool:
    if getattr(x, "name", None) != "a":
        return False
    text = norm(x.get_text(" ", strip=True) + " " + (x.get("href") or ""))
    return bool(CHAPTER_RE.search(text) or JP_RE.search(text))

def is_paid(tag) -> bool:
    if _paid_text(tag):
        return True
    row, node = tag, tag.parent
    while node is not None and getattr(node, "name", None) not in (None, "body", "html", "[document]"):
        if sum(1 for x in node.find_all("a") if _chap_anchor(x)) > 1:
            break
        row, node = node, node.parent
    if row is not tag and _paid_text(row):
        return True
    for n, sib in enumerate(tag.next_siblings):
        if n >= 3 or (getattr(sib, "name", None) and (_chap_anchor(sib) or any(_chap_anchor(x) for x in sib.find_all("a")))):
            break
        if _paid_text(sib):
            return True
    return False

def _paid_row(ch: dict) -> bool:
    for k, v in ch.items():
        key = str(k).lower()
        if "price" in key or "coin" in key or "cost" in key:
            try:
                if float(v or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        elif key in ("is_premium", "premium", "locked", "is_locked", "paid", "is_paid", "is_early_access"):
            if v is True or str(v).lower() in ("1", "true", "yes"):
                return True
    return False

def chapter_locked(chap_url: str):
    try:
        r = requests.get(chap_url, headers=HEADERS, impersonate="chrome124", timeout=15)
        if r.status_code != 200:
            return None
        return bool(LOCKED_RE.search(BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)))
    except Exception:
        return None

def heancms_latest(url: str, free_only: bool = True):
    p = urlparse(url)
    m = re.fullmatch(r"/(?:series|comic)/([^/]+)/?", p.path)
    if not m:
        return None
    slug = m.group(1)
    api = f"{p.scheme}://api.{p.netloc.removeprefix('www.')}"
    hdr = {**HEADERS, "Accept": "application/json"}
    try:
        r_series = requests.get(f"{api}/series/{slug}", headers=hdr, impersonate="chrome124", timeout=15)
        if r_series.status_code != 200:
            return None
        sid = r_series.json().get("id")
        if not sid:
            return None

        best = None
        for page in range(1, 6):
            r_ch = requests.get(
                f"{api}/chapter/query",
                headers=hdr,
                params={"page": page, "perPage": 100, "series_id": sid},
                impersonate="chrome124",
                timeout=15,
            )
            if r_ch.status_code != 200:
                break
            j = r_ch.json()
            rows = j.get("data", []) if isinstance(j, dict) else j
            for ch in rows:
                if not isinstance(ch, dict):
                    continue
                if free_only and _paid_row(ch):
                    continue
                name = f"{ch.get('chapter_name', '')} {ch.get('chapter_slug', '')}"
                mm = CHAPTER_RE.search(name) or re.search(r"\d+(?:\.\d+)?", name)
                if not mm:
                    continue
                label = mm.group(1) if mm.lastindex else mm.group(0)
                if best is None or float(label) > best[0]:
                    best = (float(label), label, ch.get("chapter_slug", "") or "")
            last_page = ((j.get("meta") or {}).get("last_page", 1) if isinstance(j, dict) else 1) or 1
            if page >= last_page:
                break
        if best:
            link = f"{p.scheme}://{p.netloc}/series/{slug}/{best[2]}" if best[2] else url
            return best[1], link
    except Exception as e:
        print(f"HeanCMS API info ({p.netloc}): {e}")
    return None

def parse_rss_items(soup, base_url: str):
    """استخراج چپتر و لینک از فیدهای RSS/Atom (مثل ShonenJumpPlus و GigaViewer)"""
    cands = []
    for item in soup.find_all(["item", "entry"]):
        title_tag = item.find("title")
        if not title_tag:
            continue
        text = norm(title_tag.get_text(" ", strip=True))
        link = ""
        link_tag = item.find("link")
        if link_tag:
            link = (link_tag.get("href") or link_tag.get_text(strip=True) or "").strip()
            if not link and link_tag.next_sibling and isinstance(link_tag.next_sibling, str):
                link = link_tag.next_sibling.strip()
        if not link:
            guid = item.find("guid")
            if guid and guid.get_text(strip=True).startswith("http"):
                link = guid.get_text(strip=True)

        m = JP_RE.search(text) or RSS_N_RE.search(text)
        if m:
            key, label = _jp(m)
            cands.append((key, label, link or base_url, False))
            continue
        m = CHAPTER_RE.search(text)
        if m:
            cands.append(((float(m.group(1)), 0), m.group(1), link or base_url, False))
    return cands

def embedded_chapters(raw: str, free_only: bool):
    text = raw.replace('\\"', '"')
    clusters, cur, last_end = [], {}, None
    for m in EMB_RE.finditer(text):
        if last_end is not None and m.start() - last_end > 12:
            clusters.append(cur)
            cur = {}
        last_end = m.end()
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict) or (free_only and _paid_row(obj)):
            continue
        mm = CHAPTER_RE.search(f"{obj.get('chapter_name', '')} {obj.get('chapter_slug', '')}")
        if mm:
            cur[float(mm.group(1))] = mm.group(1)
    clusters.append(cur)
    best = max(clusters, key=len) if clusters else {}
    return best[max(best)] if len(best) >= 2 else None

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
    free_only = not include_paid

    # ۱. امتحان کردن API سایت‌های HeanCMS (مثل OmegaScans)
    if not selector.strip():
        hc = heancms_latest(url, free_only=free_only)
        if hc:
            return hc[0], hc[1]

    # ۲. دریافت صفحه یا فید RSS
    resp = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=TIMEOUT)
    resp.raise_for_status()
    raw = norm(resp.text)
    soup = BeautifulSoup(raw, "html.parser")

    if selector.strip():
        el = soup.select_one(selector)
        if not el:
            raise ValueError("Selector پیدا نشد")
        m = re.search(r"\d+(?:\.\d+)?", norm(el.get_text(" ", strip=True)))
        if not m:
            raise ValueError("عددی در متن نبود")
        a = el if el.name == "a" else (el.find("a") or el.find_parent("a"))
        href = a.get("href", "") if a else ""
        return m.group(0), (urljoin(url, href) if href else url)

    # ۳. اگر خود لینک یک فید RSS/Atom است (مثل shonenjumpplus.com/rss/series/...)
    rss_cands = parse_rss_items(soup, url)
    if rss_cands:
        best = max(rss_cands, key=lambda x: x[0])
        return best[1], best[2]

    # ۴. اگر صفحه لینک به فید RSS مخصوص اثر دارد (سایت‌های ژاپنی GigaViewer)
    for a in soup.select('a[href*="/rss/series/"], link[type*="rss"], link[type*="atom"]'):
        feed_href = a.get("href")
        if feed_href:
            try:
                rf = requests.get(urljoin(url, feed_href), headers=HEADERS, impersonate="chrome124", timeout=15)
                if rf.status_code == 200:
                    fc = parse_rss_items(BeautifulSoup(norm(rf.text), "html.parser"), url)
                    if fc:
                        best = max(fc, key=lambda x: x[0])
                        return best[1], best[2]
            except Exception:
                pass

    # ۵. بررسی لینک‌های HTML صفحه
    anchors = soup.find_all("a")
    paths = [urlparse(resp.url).path.rstrip("/"), urlparse(url).path.rstrip("/")]
    paths = [p for p in dict.fromkeys(paths) if len(p) > 1]
    own_found = False
    if paths:
        slugs = {p.rstrip("/").split("/")[-1].lower() for p in paths if len(p.rstrip("/").split("/")[-1]) >= 3}
        def is_own(a):
            href = a.get("href", "") or ""
            low = href.lower()
            return (any(p + "/chapter" in href for p in paths)
                    or (any(f"/{sl}/" in low for sl in slugs) and bool(CHAPTER_RE.search(href))))
        own = [a for a in anchors if is_own(a)]
        if own:
            anchors, own_found = own, True
        else:
            skip, in_reco = set(), False
            for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a"]):
                if el.name != "a":
                    in_reco = in_reco or bool(RECO_RE.search(el.get_text(" ", strip=True)))
                elif in_reco:
                    skip.add(id(el))
            anchors = [a for a in anchors if id(a) not in skip]

    cands = []
    for a in anchors:
        href = a.get("href", "") or ""
        for text in (norm(a.get_text(" ", strip=True)), href):
            m = JP_RE.search(text or "")
            if m:
                key, label = _jp(m)
                cands.append((key, label, href, is_paid(a)))
                break
            m = CHAPTER_RE.search(text or "")
            if m:
                cands.append(((float(m.group(1)), 0), m.group(1), href, is_paid(a)))
                break

    # ۶. همیشه داده‌های JSON داخل صفحه (مثل Comic-Walker / Next.js) را هم بررسی کن
    # چون Comic-Walker در تگ‌های <a> فقط ۳ چپتر اول را می‌گذارد اما در JSON آخرین چپتر هست!
    if not own_found:
        for m in CW_JSON_RE.finditer(raw):
            key, label = _jp(m)
            cands.append((key, label, "", False))
        for m in JP_RE.finditer(raw):
            key, label = _jp(m)
            cands.append((key, label, "", False))

    if free_only and any(not c[3] for c in cands):
        cands = [c for c in cands if not c[3]]

    if free_only and cands:
        ordered = sorted(cands, key=lambda c: c[0], reverse=True)
        keep = None
        for c in ordered[:3]:
            if not c[2]:
                keep = c
                break
            if chapter_locked(urljoin(url, c[2])) is not True:
                keep = c
                break
        if keep is not None and keep is not ordered[0]:
            cands = [c for c in cands if c[0] <= keep[0]]

    if not cands and not own_found:
        emb = embedded_chapters(raw, free_only)
        if emb:
            return emb, url

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

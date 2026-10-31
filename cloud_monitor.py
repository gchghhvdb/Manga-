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
SUBS_FILE = "subscribers.json"
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
JP_RE = re.compile(r"第\s*(\d+(?:\.\d+)?)\s*話(?:\s*(?:その|パート|part|\.|[-_])?\s*(\d+))?", re.I)
RSS_N_RE = re.compile(r"(?:^|[\[\(【\s])(\d+(?:\.\d+)?)\s*話(?:\s*(?:その|パート)?\s*(\d+))?")
KEY_RE = re.compile(r"\s*(\d+(?:\.\d+)?)(?:\s*その\s*(\d+))?")
RECO_RE = re.compile(r"recommend|related|similar|you may also|more like|trending|popular|weekly|monthly", re.I)
PAID_RE = re.compile(
    r"\b(?:lock(?:ed)?|coins?|premium|paywall|early\s*access)\b"
    r"|asura\+|lucide-lock|fa-lock|🔒|🪙",
    re.I,
)
OPEN_RE = re.compile(r"[\w-]*(?:unlock|lock[\w-]*open)[\w-]*", re.I)
EMB_RE = re.compile(r"\{[^{}]*?chapter_(?:name|slug)[^{}]*\}")
LOCKED_RE = re.compile(
    r"locked chapter|waiting to be unlocked|chapter is locked|login to unlock chapter"
    r"|cost to unlock|sign in to unlock|unlock this (?:exciting )?chapter"
    r"|early access|asura\+|subscribe to read|purchase this chapter|unlock with"
    r"|this chapter is locked|premium chapter|read early with",
    re.I,
)

def norm(s: str) -> str:
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

def fix_chap_url(base_url: str, href: str) -> str:
    if not href:
        return base_url
    if href.startswith("http"):
        return href
    if "asurascans" in base_url.lower() and not href.startswith("/"):
        if not href.startswith("comics/"):
            href = "/comics/" + href.lstrip("/")
        else:
            href = "/" + href
    return urljoin(base_url, href)

def chapter_locked(chap_url: str):
    try:
        r = requests.get(chap_url, headers=HEADERS, impersonate="chrome124", timeout=15)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        return bool(LOCKED_RE.search(soup.get_text(" ", strip=True)))
    except Exception:
        return None

def asurascans_latest(url: str, free_only: bool = True):
    """تشخیص اختصاصی و دقیق آخرین چپتر رایگان در AsuraScans"""
    if "asurascans" not in url.lower():
        return None
    try:
        resp = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        raw = norm(resp.text)
        soup = BeautifulSoup(raw, "html.parser")

        p_path = urlparse(resp.url).path.rstrip("/")
        sl = p_path.split("/")[-1].lower()
        base_sl = re.sub(r"-[0-9a-f]{6,10}$", "", sl)

        # جمع‌آوری لینک‌های چپتر همین مانهوا
        chap_map = {}  # num_float -> (label_str, full_url, has_svg_or_paid_in_row)
        for a in soup.find_all("a"):
            href = a.get("href", "") or ""
            low_href = href.lower()
            if "/chapter/" not in low_href:
                continue
            if base_sl and len(base_sl) >= 3 and base_sl not in low_href:
                continue

            m = re.search(r"/chapter/(\d+(?:\.\d+)?)", low_href) or CHAPTER_RE.search(norm(a.get_text(" ", strip=True)))
            if not m:
                continue
            label = m.group(1)
            key = float(label)
            full_u = fix_chap_url(url, href)

            # بررسی وجود آیکون قفل (svg) یا متن قفل داخل ردیفِ جدول چپترها (و نه دکمه‌های بالای صفحه)
            in_first_last_btn = bool(re.search(r"first\s*chapter|new\s*chapter|latest\s*chapter", norm(a.get_text(" ", strip=True)), re.I))
            row_paid = False
            if not in_first_last_btn:
                row_html = str(a) + " " + (str(a.parent) if a.parent else "")
                if (
                    a.find("svg")
                    or (a.parent and a.parent.find("svg"))
                    or re.search(r"lock|coin|premium|early|asura\+|<svg", row_html, re.I)
                ):
                    row_paid = True

            if key not in chap_map:
                chap_map[key] = [label, full_u, row_paid]
            else:
                if row_paid:
                    chap_map[key][2] = True

        if not chap_map:
            return None

        ordered = sorted(chap_map.keys(), reverse=True)
        if not free_only or len(ordered) == 1:
            top = chap_map[ordered[0]]
            return top[0], top[1]

        # ۱. اگر در جدول چپترها، بعضی چپترها آیکون SVG/قفل دارند و بعضی ندارند:
        paid_flags = [chap_map[k][2] for k in ordered]
        if any(paid_flags) and not all(paid_flags):
            for k in ordered:
                if not chap_map[k][2]:
                    print(f"[Asura] چپترهای قفل‌دار رد شدند؛ آخرین چپتر رایگان: {chap_map[k][0]}")
                    return chap_map[k][0], chap_map[k][1]

        # ۲. اگر در HTML جدول، همه یکسان بودند، با باز کردنِ صفحه چپترِ اول و مقایسه با یک چپترِ قدیمی‌تر (که مطمئناً رایگان است) چک کن:
        ref_key = ordered[min(len(ordered) - 1, 3)]  # مثلاً ۳ چپتر پایین‌تر که حتماً رایگان است
        r_ref = requests.get(chap_map[ref_key][1], headers=HEADERS, impersonate="chrome124", timeout=15)
        ref_imgs = len(re.findall(r'<img\b[^>]+src=["\']https?://[^"\']+(?:media|chapter|uploads|comic)[^"\']*["\']', r_ref.text, re.I))
        if ref_imgs < 3:
            ref_imgs = len(re.findall(r'https?://[^"\'\s\\]+/storage/media/\d+/', r_ref.text, re.I))

        for k in ordered[:3]:
            lbl, ch_url, _ = chap_map[k]
            r_ch = requests.get(ch_url, headers=HEADERS, impersonate="chrome124", timeout=15)
            txt = r_ch.text
            soup_ch = BeautifulSoup(txt, "html.parser")
            visible_txt = soup_ch.get_text(" ", strip=True)

            # اگر متن قفل در صفحه چپتر بود
            if LOCKED_RE.search(visible_txt):
                print(f"[Asura] چپتر {lbl} دارای متن قفل است؛ رد شد.")
                continue

            # شمارش عکس‌های داخل صفحه چپتر
            ch_imgs = len(re.findall(r'<img\b[^>]+src=["\']https?://[^"\']+(?:media|chapter|uploads|comic)[^"\']*["\']', txt, re.I))
            if ch_imgs < 3:
                ch_imgs = len(re.findall(r'https?://[^"\'\s\\]+/storage/media/\d+/', txt, re.I))

            # اگر چپتر مرجع (قدیمی‌تر) عکس‌های داستان را داشت ولی این چپتر جدید نداشت، یعنی قفل است!
            if ref_imgs >= 4 and ch_imgs < 3:
                print(f"[Asura] چپتر {lbl} عکس‌های داستان را ندارد ({ch_imgs} در برابر {ref_imgs}) -> قفل است!")
                continue

            return lbl, ch_url

        # اگر همه قفل تشخیص داده شدند، اولین چپتر غیر از بالاترین را برگردان
        top = chap_map[ordered[0]]
        return top[0], top[1]
    except Exception as e:
        print(f"Asura check error: {e}")
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
            last_update_id = None
            new_users = []
            for u in updates:
                last_update_id = u.get("update_id")
                msg = u.get("message") or u.get("my_chat_member") or {}
                chat = msg.get("chat") or {}
                cid = str(chat.get("id") or "")
                text = (msg.get("text") or "").strip().lower()
                if not cid:
                    continue
                if text == "/stop":
                    subs.discard(cid)
                else:
                    if cid not in subs:
                        subs.add(cid)
                        new_users.append(cid)

            for cid in new_users:
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                        json={
                            "chat_id": cid,
                            "text": "✅ شما با موفقیت در ربات اطلاع‌رسانی مانهوا عضو شدید!\nهر زمان چپتر جدیدی منتشر شود، اینجا برایتان ارسال خواهد شد.",
                        },
                        timeout=10,
                    )
                except Exception:
                    pass

            if last_update_id is not None:
                requests.get(
                    f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                    params={"offset": last_update_id + 1},
                    timeout=10,
                )
    except Exception as e:
        print(f"Telegram sync error: {e}")

    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(subs), f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception:
        pass

    return subs

def comic_walker_latest(url: str):
    if "comic-walker.com" not in url:
        return None
    m_code = re.search(r"(KC_\d+_S)", url)
    if not m_code:
        return None
    work_code = m_code.group(1)
    detail_url = f"https://comic-walker.com/detail/{work_code}"

    try:
        api_url = f"https://comic-walker.com/api/contents/details/episode?workCode={work_code}&episodeType=latest"
        r_api = requests.get(api_url, headers={**HEADERS, "Accept": "application/json"}, impersonate="chrome124", timeout=15)
        if r_api.status_code == 200 and "json" in (r_api.headers.get("content-type") or ""):
            raw_api = norm(r_api.text)
            cands = []
            for m in JP_RE.finditer(raw_api):
                key, label = _jp(m)
                cands.append((key, label))
            if cands:
                best = max(cands, key=lambda x: x[0])
                return best[1], detail_url
    except Exception:
        pass

    try:
        r = requests.get(detail_url, headers=HEADERS, impersonate="chrome124", timeout=20)
        if r.status_code != 200:
            return None
        raw = norm(r.text)
        soup = BeautifulSoup(raw, "html.parser")
        cands = []

        next_tag = soup.find("script", id="__NEXT_DATA__")
        search_text = norm(next_tag.string) if (next_tag and next_tag.string) else raw

        for m in JP_RE.finditer(search_text):
            key, label = _jp(m)
            cands.append((key, label, detail_url))

        prefix = work_code[:-2]
        for m_ep in re.finditer(rf"({prefix}(\d{{5}})(\d{{3}})\d{{2}}_E)", search_text):
            ep_code = m_ep.group(1)
            ch_num = int(m_ep.group(2))
            sub_num = int(m_ep.group(3))
            if 0 < ch_num < 2000:
                label = f"{ch_num} その{sub_num}" if sub_num > 0 else str(ch_num)
                ep_link = f"https://comic-walker.com/detail/{work_code}/episodes/{ep_code}?episodeType=latest"
                cands.append(((float(ch_num), sub_num), label, ep_link))

        if cands:
            best = max(cands, key=lambda x: x[0])
            return best[1], best[2]
    except Exception as e:
        print(f"Comic-Walker error: {e}")
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

    if not selector.strip():
        # ۰. بررسی اختصاصی AsuraScans
        asu = asurascans_latest(url, free_only=free_only)
        if asu:
            return asu[0], asu[1]

        # ۱. بررسی اختصاصی Comic-Walker
        cw = comic_walker_latest(url)
        if cw:
            return cw[0], cw[1]

        # ۲. بررسی اختصاصی HeanCMS (مثل OmegaScans)
        hc = heancms_latest(url, free_only=free_only)
        if hc:
            return hc[0], hc[1]

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
        return m.group(0), fix_chap_url(url, href)

    rss_cands = parse_rss_items(soup, url)
    if rss_cands:
        best = max(rss_cands, key=lambda x: x[0])
        return best[1], best[2]

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

    anchors = soup.find_all("a")
    paths = [urlparse(resp.url).path.rstrip("/"), urlparse(url).path.rstrip("/")]
    paths = [p for p in dict.fromkeys(paths) if len(p) > 1]
    own_found = False
    if paths:
        slugs = set()
        for p in paths:
            sl = p.rstrip("/").split("/")[-1].lower()
            if len(sl) >= 3:
                slugs.add(sl)
                base_sl = re.sub(r"-[0-9a-f]{6,10}$", "", sl)
                if len(base_sl) >= 3:
                    slugs.add(base_sl)

        def is_own(a):
            href = (a.get("href", "") or "").lower()
            return (any(p.lower() + "/chapter" in href for p in paths)
                    or (any(sl in href for sl in slugs) and bool(CHAPTER_RE.search(href))))

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

    if free_only and any(not c[3] for c in cands):
        cands = [c for c in cands if not c[3]]

    if free_only and cands:
        uniq = {}
        for c in cands:
            if c[0] not in uniq or (not uniq[c[0]][2] and c[2]):
                uniq[c[0]] = c
        ordered = sorted(uniq.values(), key=lambda c: c[0], reverse=True)
        keep = None
        for c in ordered[:3]:
            if not c[2]:
                keep = c
                break
            if chapter_locked(fix_chap_url(url, c[2])) is not True:
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
    return best[1], fix_chap_url(url, best[2])

def main():
    tg_subs = sync_telegram_subscribers()
    print(f"[*] تعداد اعضای فعال تلگرام: {len(tg_subs)}")

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

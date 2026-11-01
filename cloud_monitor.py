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
    warnings.filterwarnings("ignore")
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
RSS_N_RE = re.compile(r"(?:^|[\[\(【\s])(\d+(?:\.\d+)?)\s*話(?:\s*(?:その\vert{}パート)?\s*(\d+))?") KEY_RE = re.compile(r"\s*(\d+(?:\.\d+)?)(?:\s*その\s*(\d+))?") RECO_RE = re.compile(r"recommend\vert{}related\vert{}similar\vert{}you may also\vert{}more like\vert{}trending\vert{}popular\vert{}weekly\vert{}monthly", re.I) PAID_RE = re.compile( r"\b(?:lock(?:ed)?\vert{}coins?\vert{}premium\vert{}paywall\vert{}early\s*access)\b" r"\vert{}asura\+\vert{}lucide-lock\vert{}fa-lock\vert{}🔒\vert{}🪙", re.I, ) OPEN_RE = re.compile(r"[\w-]*(?:unlock\vert{}lock[\w-]*open)[\w-]*", re.I) EMB_RE = re.compile(r"\{[^{}]*?chapter_(?:name\vert{}slug)[^{}]*\}") LOCKED_RE = re.compile( r"locked chapter\vert{}waiting to be unlocked\vert{}chapter is locked\vert{}login to unlock chapter" r"\vert{}cost to unlock\vert{}sign in to unlock\vert{}unlock this (?:exciting )?chapter" r"\vert{}early access\vert{}asura\+\vert{}subscribe to read\vert{}purchase this chapter\vert{}unlock with" r"\vert{}this chapter is locked\vert{}premium chapter\vert{}read early with", re.I, )  def norm(s: str) -> str: return unicodedata.normalize("NFKC", str(s or ""))  def _jp(m): num_str = m.group(1) n = float(num_str) sub = int(m.group(2) or 0) base = str(int(n)) if n.is_integer() else num_str return (n, sub), (f"{base} その{sub}" if sub else base)  def chap_key(s): m = KEY_RE.match(norm(s)) return (float(m.group(1)), int(m.group(2) or 0)) if m else None  def _paid_text(x) -> bool: return bool(PAID_RE.search(OPEN_RE.sub(" ", str(x))))  def _chap_anchor(x) -> bool: if getattr(x, "name", None) != "a": return False text = norm(x.get_text(" ", strip=True) + " " + (x.get("href") or "")) return bool(CHAPTER_RE.search(text) or JP_RE.search(text))  def is_paid(tag) -> bool: if _paid_text(tag): return True row, node = tag, tag.parent while node is not None and getattr(node, "name", None) not in (None, "body", "html", "[document]"): if sum(1 for x in node.find_all("a") if _chap_anchor(x)) > 1: break row, node = node, node.parent if row is not tag and _paid_text(row): return True for n, sib in enumerate(tag.next_siblings): if n >= 3 or (getattr(sib, "name", None) and (_chap_anchor(sib) or any(_chap_anchor(x) for x in sib.find_all("a")))): break if _paid_text(sib): return True return False  def _paid_row(ch: dict) -> bool: for k, v in ch.items(): key = str(k).lower() if "price" in key or "coin" in key or "cost" in key: try: if float(v or 0) > 0: return True except (TypeError, ValueError): pass elif key in ("is_premium", "premium", "locked", "is_locked", "paid", "is_paid", "is_early_access"): if v is True or str(v).lower() in ("1", "true", "yes"): return True return False  def fix_chap_url(base_url: str, href: str) -> str: if not href: return base_url if href.startswith("http"): return href if "asurascans" in base_url.lower() and not href.startswith("/"): if not href.startswith("comics/"): href = "/comics/" + href.lstrip("/") else: href = "/" + href return urljoin(base_url, href)  def chapter_locked(chap_url: str): try: r = requests.get(chap_url, headers=HEADERS, impersonate="chrome124", timeout=15) if r.status_code != 200: return None soup = BeautifulSoup(r.text, "html.parser") return bool(LOCKED_RE.search(soup.get_text(" ", strip=True))) except Exception: return None  def camoufox_latest(url: str, free_only: bool = True): """پشتیبان ضد کلودفلر با مرورگر Camoufox (مخصوص سایت‌هایی مثل LuaComic که 403 می‌دهند)""" try: from camoufox.sync_api import Camoufox except ImportError: print("Camoufox نصب نیست؛ در ورک‌فلو نصب شود.") return None      print(f"[*] 🦊 فعال‌سازی Camoufox برای عبور از کلودفلر: {url}") p = urlparse(url) slug = p.path.rstrip("/").split("/")[-1] api_host = f"{p.scheme}://api.{p.netloc.removeprefix('www.')}"      for attempt in range(1, 3): captured_chapters = []          def handle_response(response): try: if "api." in response.url or "chapter" in response.url or "series" in response.url: if "json" in (response.headers.get("content-type") or ""): data = response.json() rows = [] if isinstance(data, dict): rows = data.get("data") or data.get("chapters") or [] for s in (data.get("seasons") or []): if isinstance(s, dict) and s.get("chapters"): rows.extend(s.get("chapters")) elif isinstance(data, list): rows = data for r in rows: if isinstance(r, dict) and ("chapter_name" in r or "chapter_slug" in r): captured_chapters.append(r) except Exception: pass          try: with Camoufox(headless=True, humanize=True, i_know_what_im_doing=True) as browser: page = browser.new_page() page.on("response", handle_response) page.goto(url, wait_until="domcontentloaded", timeout=45000)                  for _ in range(10): page.wait_for_timeout(3000) title = page.title() content = page.content() if captured_chapters or ("just a moment" not in title.lower() and re.search(r"chapter\s*\d+", content, re.I)): break try: for frame in page.frames: if "cloudflare" in frame.url or "turnstile" in frame.url: box = frame.locator("input[type='checkbox'], body").first if box: box.click(timeout=1500) except Exception: pass                  # اگر هنوز API شکار نشده، از داخل خود مرورگر به API سایت درخواست بزن if not captured_chapters and ("luacomic" in url.lower() or "/series/" in url.lower()): try: js_data = page.evaluate( f"""async () => {{ try {{ const r1 = await fetch("{api_host}/series/{slug}"); if (!r1.ok) return null; const s = await r1.json(); if (s && s.id) {{ const r2 = await fetch("{api_host}/chapter/query?page=1&perPage=100&series_id=" + s.id); if (r2.ok) return await r2.json(); }} return s; }} catch (e) {{ return null; }} }}""" ) if isinstance(js_data, dict): rows = js_data.get("data") or js_data.get("chapters") or [] for s in (js_data.get("seasons") or []): if isinstance(s, dict) and s.get("chapters"): rows.extend(s.get("chapters")) for r in rows: if isinstance(r, dict) and ("chapter_name" in r or "chapter_slug" in r): captured_chapters.append(r) except Exception: pass                  html = page.content()              # ۱. اگر از API داخلی سایت شکار شده باشد if captured_chapters: best = None for ch in captured_chapters: if free_only and _paid_row(ch): continue name = f"{ch.get('chapter_name', '')} {ch.get('chapter_slug', '')}" mm = CHAPTER_RE.search(name) or re.search(r"\d+(?:\.\d+)?", name) if not mm: continue label = mm.group(1) if mm.lastindex else mm.group(0) if best is None or float(label) > best[0]: best = (float(label), label, ch.get("chapter_slug", "") or "") if best: link = f"{p.scheme}://{p.netloc}/series/{slug}/{best[2]}" if best[2] else url print(f"[*] ✅ Camoufox چپتر {best[1]} را از API شکار کرد!") return best[1], link              # ۲. استخراج از HTML رندرشده توسط Camoufox raw = norm(html) soup = BeautifulSoup(raw, "html.parser") low_slug = slug.lower() cands = [] for a in soup.find_all("a"): href = a.get("href", "") or "" if low_slug and len(low_slug) >= 4 and low_slug not in href.lower() and "chapter" not in href.lower(): continue row_text = norm(a.get_text(" ", strip=True) + " " + (a.parent.get_text(" ", strip=True) if a.parent else "")) m = CHAPTER_RE.search(row_text) or CHAPTER_RE.search(href) if not m: continue ch_num = m.group(1) row_html = str(a) + " " + (str(a.parent) if a.parent else "") paid_flag = is_paid(a) or bool(re.search(r"coin\vert{}lock\vert{}premium\vert{}paywall\vert{}🪙\vert{}🔒\vert{}\b\d+\s*<svg", row_html, re.I)) cands.append((float(ch_num), ch_num, fix_chap_url(url, href), paid_flag))              if cands: if free_only and any(not c[3] for c in cands): cands = [c for c in cands if not c[3]] best = max(cands, key=lambda x: x[0]) print(f"[*] ✅ Camoufox چپتر {best[1]} را از صفحه استخراج کرد!") return best[1], best[2] except Exception as e: print(f"Camoufox attempt {attempt} info: {e}")      return None  def naver_webtoon_latest(url: str, free_only: bool = True): """پشتیبانی اختصاصی از سایت کره‌ای Naver Webtoon (comic.naver.com / m.comic.naver.com)""" if "comic.naver.com" not in url.lower(): return None m_id = re.search(r"titleId=(\d+)", url, re.I) if not m_id: return None title_id = m_id.group(1)      try: api_url = f"https://comic.naver.com/api/article/list?titleId={title_id}&page=1" r = requests.get( api_url, headers={**HEADERS, "Accept": "application/json", "Referer": f"https://comic.naver.com/webtoon/list?titleId={title_id}"}, impersonate="chrome124", timeout=15, ) if r.status_code == 200: data = r.json() articles = list(data.get("articleList") or []) if not free_only and data.get("chargeFolderArticleList"): articles = list(data.get("chargeFolderArticleList")) + articles              cands = [] for art in articles: if free_only and art.get("charge"): continue no = art.get("no") subtitle = norm(art.get("subtitle") or "") m_kr = KR_RE.search(subtitle) label = m_kr.group(1) if m_kr else str(no) if not label: continue key = float(label) link = f"https://m.comic.naver.com/webtoon/detail?titleId={title_id}&no={no}" cands.append((key, label, link))              if cands: best = max(cands, key=lambda x: x[0]) return best[1], best[2] except Exception as e: print(f"Naver API info: {e}")      try: mob_url = f"https://m.comic.naver.com/webtoon/list?titleId={title_id}" r = requests.get(mob_url, headers=HEADERS, impersonate="chrome124", timeout=15) if r.status_code == 200: soup = BeautifulSoup(norm(r.text), "html.parser") cands = [] for a in soup.find_all("a"): href = a.get("href", "") or "" if "/webtoon/detail" not in href or f"titleId={title_id}" not in href: continue row_txt = norm(a.get_text(" ", strip=True) + " " + str(a.parent or "")) if free_only and re.search(r"유료\vert{}쿠키\vert{}미리보기\vert{}lock\vert{}cookie", row_txt, re.I): continue m_kr = KR_RE.search(norm(a.get_text(" ", strip=True))) m_no = re.search(r"[?&]no=(\d+)", href) label = m_kr.group(1) if m_kr else (m_no.group(1) if m_no else "") if label: cands.append((float(label), label, urljoin("https://m.comic.naver.com", href))) if cands: best = max(cands, key=lambda x: x[0]) return best[1], best[2] except Exception as e: print(f"Naver HTML error: {e}")      return None  def asurascans_latest(url: str, free_only: bool = True): """نسخه‌ی اصلی و تست‌شده‌ی AsuraScans (بدون هیچ تغییری)""" if "asurascans" not in url.lower(): return None try: resp = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=TIMEOUT) if resp.status_code != 200: return None raw = norm(resp.text) soup = BeautifulSoup(raw, "html.parser")          p_path = urlparse(resp.url).path.rstrip("/") sl = p_path.split("/")[-1].lower() base_sl = re.sub(r"-[0-9a-f]{6,10}$", "", sl)          chap_map = {} for a in soup.find_all("a"): href = a.get("href", "") or "" low_href = href.lower() if "/chapter/" not in low_href: continue if base_sl and len(base_sl) >= 3 and base_sl not in low_href: continue              m = re.search(r"/chapter/(\d+(?:\.\d+)?)", low_href) or CHAPTER_RE.search(norm(a.get_text(" ", strip=True))) if not m: continue label = m.group(1) key = float(label) full_u = fix_chap_url(url, href)              in_first_last_btn = bool(re.search(r"first\s*chapter\vert{}new\s*chapter\vert{}latest\s*chapter", norm(a.get_text(" ", strip=True)), re.I)) row_paid = False if not in_first_last_btn: row_html = str(a) + " " + (str(a.parent) if a.parent else "") if ( a.find("svg") or (a.parent and a.parent.find("svg")) or re.search(r"lock\vert{}coin\vert{}premium\vert{}early\vert{}asura\+\vert{}<svg", row_html, re.I) ): row_paid = True              if key not in chap_map: chap_map[key] = [label, full_u, row_paid] else: if row_paid: chap_map[key][2] = True          if not chap_map: return None          ordered = sorted(chap_map.keys(), reverse=True) if not free_only or len(ordered) == 1: top = chap_map[ordered[0]] return top[0], top[1]          paid_flags = [chap_map[k][2] for k in ordered] if any(paid_flags) and not all(paid_flags): for k in ordered: if not chap_map[k][2]: print(f"[Asura] چپترهای قفل‌دار رد شدند؛ آخرین چپتر رایگان: {chap_map[k][0]}") return chap_map[k][0], chap_map[k][1]          ref_key = ordered[min(len(ordered) - 1, 3)] r_ref = requests.get(chap_map[ref_key][1], headers=HEADERS, impersonate="chrome124", timeout=15) ref_imgs = len(re.findall(r'<img\b[^>]+src=["\']https?://[^"\']+(?:media\vert{}chapter\vert{}uploads\vert{}comic)[^"\']*["\']', r_ref.text, re.I)) if ref_imgs < 3: ref_imgs = len(re.findall(r'https?://[^"\'\s\\]+/storage/media/\d+/', r_ref.text, re.I))

        for k in ordered[:3]:
            lbl, ch_url, _ = chap_map[k]
            r_ch = requests.get(ch_url, headers=HEADERS, impersonate="chrome124", timeout=15)
            txt = r_ch.text
            soup_ch = BeautifulSoup(txt, "html.parser")
            visible_txt = soup_ch.get_text(" ", strip=True)

            if LOCKED_RE.search(visible_txt):
                print(f"[Asura] چپتر {lbl} دارای متن قفل است؛ رد شد.")
                continue

            ch_imgs = len(re.findall(r'<img\b[^>]+src=["\']https?://[^"\']+(?:media|chapter|uploads|comic)[^"\']*["\']', txt, re.I))
            if ch_imgs < 3:
                ch_imgs = len(re.findall(r'https?://[^"\'\s\\]+/storage/media/\d+/', txt, re.I))

            if ref_imgs >= 4 and ch_imgs < 3:
                print(f"[Asura] چپتر {lbl} عکس‌های داستان را ندارد ({ch_imgs} در برابر {ref_imgs}) -> قفل است!")
                continue

            return lbl, ch_url

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
                f"{api

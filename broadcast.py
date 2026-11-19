import json, os, re, sys
from curl_cffi import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
text = sys.argv[1] if len(sys.argv) > 1 else "🔧 ربات در دست تعمیر است"

subs = {c for c in re.split(r"[\s,]+", CHAT_IDS) if c}
try:
    with open("subscribers.json", encoding="utf-8") as f:
        subs.update(str(x) for x in json.load(f) if x)
except Exception:
    pass

sent = 0
for cid in sorted(subs):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text},
            timeout=15,
        )
        sent += 1 if r.ok else 0
    except Exception as e:
        print(f"خطا برای {cid}: {e}")

print(f"ارسال شد به {sent} از {len(subs)} نفر")

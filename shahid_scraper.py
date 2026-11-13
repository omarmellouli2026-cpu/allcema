#!/usr/bin/env python3
"""shahid_scraper.py — جلب العناوين وقوائم الحلقات والصور لمسلسلات شاهد بإذن مكتوب من MBC.
الهوية صريحة (allcema-bot)، بطيء ومهذّب، ويحترم robots.txt ما لم يُصرَّح بالإذن عبر SHAHID_PERMISSION.
لا يغيّر أي عمل من elcinema: أرقام الأعمال هنا تبدأ من 8e9."""
from __future__ import annotations
import argparse, glob, hashlib, json, os, re, sqlite3, time
from datetime import datetime, timezone
from urllib import robotparser
import requests

UA = os.environ.get("SHAHID_UA", "allcema-bot/1.0 (+https://allcema.com)")
COUNTRY = "السعودية"
BASE_ID = 8_000_000_000
KEY = "سعود"                      # كلمة تدل على البلد المطلوب (السعودية/سعودي)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def series_id(url: str):
    m = re.search(r"series-(\d+)", url) or re.search(r"/(\d{10,})", url)
    return int(m.group(1)) if m else None


def work_id_for(sid: int) -> int:
    return BASE_ID + sid % 1_000_000_000


def next_data(html: str):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


def img(u, w=600, h=900):
    if not u:
        return None
    return u.replace("{height}", str(h)).replace("{width}", str(w)).replace("{croppingPoint}", "mc")


def released_count(show: dict) -> int:
    season = show.get("season") or {}
    best = 0
    for pl in season.get("playlists") or []:
        if pl.get("productSubType") == "EPISODE" and not pl.get("playlistType"):
            best = max(best, int(pl.get("count") or 0))
    if not best:
        best = int(season.get("numberOfAssets") or 0) - int(season.get("numberOfclips") or 0)
    return max(best, 0)


def names(items):
    return [(p.get("fullName") or p.get("firstName") or "").strip() for p in items or [] if isinstance(p, dict)]


def parse(html: str, url: str):
    nd = next_data(html)
    pm = ((nd or {}).get("props", {}).get("pageProps", {}).get("response") or {}).get("productModel") or {}
    show = pm.get("show") if isinstance(pm.get("show"), dict) else (pm if pm.get("productType") == "SHOW" else None)
    if not show or not show.get("title"):
        return None
    season = show.get("season") or {}
    year = int(str(show.get("productionDate") or show.get("sortDate") or "0")[:4] or 0)
    image = show.get("image") or {}
    return {
        "sid": series_id(url) or int(show.get("id") or 0),
        "title": show["title"].strip(),
        "year": year,
        "desc": (season.get("description") or show.get("description") or "").strip(),
        "genres": [g.get("title") for g in show.get("genres") or [] if g.get("title")],
        "cast": names(show.get("persons")),
        "cast_ids": [p.get("id") for p in show.get("persons") or []],
        "directors": names(show.get("directors")),
        "poster": img(image.get("posterImage") or season.get("posterImage") or image.get("thumbnailImage")),
        "released": released_count(show),
        "total": int(show.get("totalNumberOfEpisodes") or 0),
        "country": ((show.get("dialect") or {}).get("title")) or COUNTRY,
        "url": url,
    }


def upsert(db: sqlite3.Connection, d: dict) -> int:
    wid = work_id_for(d["sid"])
    ts = now()
    j = lambda v: json.dumps(v, ensure_ascii=False)
    prev = {}
    row = db.execute("SELECT extra FROM works WHERE work_id=?", (wid,)).fetchone()
    try:
        prev = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        prev = {}
    extra = j({**prev, "platform": "شاهد", "source": "shahid", "shahid_id": d["sid"], "total_episodes": d["total"],
               **d.get("verified", {})})
    chash = hashlib.sha1(j([d["title"], d["desc"], d["cast"], d["released"], d["total"]]).encode()).hexdigest()
    old = db.execute("SELECT episodes_count FROM works WHERE work_id=?", (wid,)).fetchone()
    count = max(int(old[0] or 0) if old else 0, d["released"])
    db.execute("""INSERT INTO works(work_id,url,title_ar,year,category_label,is_series,status,poster_url,synopsis,countries,
                  languages,genres,directors,cast_top,episodes_count,watch_links,extra,first_seen_at,fetched_at,content_hash)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(work_id) DO UPDATE SET url=excluded.url,title_ar=excluded.title_ar,year=excluded.year,
                  poster_url=COALESCE(excluded.poster_url,works.poster_url),synopsis=excluded.synopsis,
                  genres=excluded.genres,directors=excluded.directors,cast_top=excluded.cast_top,
                  episodes_count=excluded.episodes_count,extra=excluded.extra,fetched_at=excluded.fetched_at,
                  content_hash=excluded.content_hash""",
               (wid, d["url"], d["title"], d["year"], "مسلسل", 1, "يعرض على شاهد", d["poster"], d["desc"],
                j([d["country"]]), j(["العربية"]), j(d["genres"]), j(d["directors"]), j(d["cast"]), count,
                j([{"name": "شاهد", "url": d["url"]}]), extra, ts, ts, chash))
    db.execute("DELETE FROM credits WHERE work_id=?", (wid,))
    for pid, name in zip(d["cast_ids"], d["cast"]):
        if name:
            db.execute("INSERT OR IGNORE INTO credits VALUES (?,?,?,?,?)", (wid, int(pid or 0), name, "cast", None))
    for n in range(1, count + 1):                     # لا نستبدل ملخصًا موجودًا (يدويًا أو من بحث سابق)
        db.execute("INSERT OR IGNORE INTO episodes VALUES (?,?,?)", (wid, n, ""))
    db.commit()
    return wid


VERIFY_SYS = """أنت مدقق معلومات لموقع دراما. تُعطى نتائج بحث ويب عن مسلسل. حدّد بلد إنتاج المسلسل اعتمادًا على نتائج البحث وحدها ولا تخمّن.
أعد JSON فقط: {"country": "اسم البلد بالعربية أو null", "support": [أرقام النتائج التي تؤكد ذلك], "note": "جملة قصيرة"}.
إن لم تذكر النتائج البلد صراحةً أو تضاربت فاجعل country=null."""


def verify_country(d: dict, want: str, session=requests):
    """يتأكد عبر بحث DeepSeek أن بلد المسلسل هو want. يعيد (ok, سبب، نطاقات). لا يضيف إلا إذا أكدته ≥2 نطاقات مستقلة."""
    import elcinema_publish as P
    import elcinema_recaps as R
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return False, "لا مفتاح DeepSeek؛ لم أتحقق", []
    queries = [f"مسلسل {d['title']} {d['year']} إنتاج", f"مسلسل {d['title']} {' '.join(d['cast'][:2])} دولة الإنتاج"]
    results, seen = [], set()
    for q in queries:
        for r in R.deepseek_search(q, key, session):
            if r["url"] not in seen:
                seen.add(r["url"])
                results.append(r)
    results = results[:12]
    if not results:
        return False, "لا نتائج بحث للتحقق", []
    lines = []
    for n, r in enumerate(results, 1):
        text = re.sub(r"\s+", " ", str(r.get("summary") or r.get("snippet") or "")).strip()[:600]
        lines.append(f"[{n}] {R.domain(r['url'])} — {str(r.get('title') or '')[:120]}\n{text}")
    prompt = (f"المسلسل: {d['title']} ({d['year']})، من بطولة: {'، '.join(d['cast'][:4])}.\n"
              "نتائج البحث:\n" + "\n\n".join(lines) + "\n\nأعد JSON فقط.")
    obj = R._parse_obj(P.call_deepseek(VERIFY_SYS, prompt, P.llm_config()[2], key, session))
    country = str(obj.get("country") or "")
    sup = obj.get("support") or []
    doms = sorted({R.domain(results[n - 1]["url"]) for n in sup if isinstance(n, int) and 1 <= n <= len(results)})
    doms = [x for x in doms if x and "shahid" not in x]
    if KEY not in country:
        return False, f"البحث يشير إلى بلد آخر أو غير مؤكد ({country or 'غير مذكور'})", doms
    if len(doms) < 2:
        return False, "أقل من مصدرين مستقلين يؤكدان البلد", doms
    return True, country, doms


def review_log(args, d: dict, reason: str, doms):
    p = os.path.join(args.data_dir, "shahid_review.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "title": d["title"], "url": d["url"], "dialect": d["country"],
                            "reason": reason, "domains": doms}, ensure_ascii=False) + "\n")


def already_verified(db: sqlite3.Connection, wid: int) -> bool:
    row = db.execute("SELECT extra FROM works WHERE work_id=?", (wid,)).fetchone()
    try:
        return bool(row and json.loads(row[0] or "{}").get("country_verified"))
    except ValueError:
        return False


def allowed(url: str) -> bool:
    if os.environ.get("SHAHID_PERMISSION"):          # إذن مكتوب من MBC يُصرَّح به في الـworkflow
        return True
    rp = robotparser.RobotFileParser("https://shahid.mbc.net/robots.txt")
    try:
        rp.read()
    except Exception:
        return False
    return rp.can_fetch(UA, url)


def fetch(args) -> list[int]:
    urls = [l.strip() for l in open(args.urls, encoding="utf-8") if l.strip() and not l.startswith("#")]
    db = sqlite3.connect(glob.glob(os.path.join(args.data_dir, "*.sqlite"))[0])
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ar"})
    ids = []
    for i, u in enumerate(urls):
        if not allowed(u):
            print("robots.txt لا يسمح بهذه الهوية ولم يُصرَّح بإذن؛ توقفت:", u)
            break
        try:
            r = s.get(u, timeout=30)
        except requests.RequestException as e:
            print("! تعذر الجلب:", u, e)
            continue
        if r.status_code != 200:
            print(f"! {r.status_code} من شاهد: {u} (لا أحاول التحايل)")
            continue
        d = parse(r.text, u)
        if not d:
            print("! لم أجد بيانات المسلسل في الصفحة:", u)
            continue
        wid = work_id_for(d["sid"])
        if not already_verified(db, wid):
            if not (KEY in (d["country"] or "") or any("سعود" in g for g in d["genres"])):
                print(f"✗ {d['title']}: لهجة/وسم شاهد لا يشير إلى {COUNTRY} ({d['country']}) — لم يُضَف")
                review_log(args, d, "لهجة شاهد غير مطابقة", [])
                continue
            ok, why, doms = verify_country(d, COUNTRY, s)
            if not ok:
                print(f"✗ {d['title']}: لم يُضَف — {why} {doms}")
                review_log(args, d, why, doms)
                continue
            d["verified"] = {"country_verified": COUNTRY, "country_domains": doms}
        wid = upsert(db, d)
        ids.append(wid)
        print(f"✓ {d['title']} ({d['year']}) id={wid} حلقات متاحة={d['released']}/{d['total']}")
        if i < len(urls) - 1:
            time.sleep(args.sleep)
    env = os.environ.get("GITHUB_ENV")
    if env:
        allids = [r[0] for r in db.execute("SELECT work_id FROM works WHERE work_id>=? AND work_id<? ORDER BY work_id",
                                           (BASE_ID, BASE_ID + 1_000_000_000))]
        open(env, "a").write("SA=%s\n" % " ".join(map(str, allids)))
    return ids


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch"])
    ap.add_argument("--urls", default="shahid_series.txt")
    ap.add_argument("--data-dir", default="elcinema_data")
    ap.add_argument("--sleep", type=float, default=5.0)
    fetch(ap.parse_args())

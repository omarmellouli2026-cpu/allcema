"""اكتشاف مسلسلات شاهد الجديدة (بإذن مكتوب من MBC، بهوية allcema-bot).
discover: يقرأ كاروسيل «الخليجي» و«قريباً» و«الإصدارات الجديدة» من واجهة شاهد العامة ويكتب الروابط الجديدة في shahid_candidates.txt
commit: يضيف إلى shahid_series.txt ما قُبل فعلاً في قاعدة البيانات (بعد التحقق من الدولة)
"""
import glob, json, os, sqlite3, sys, urllib.parse
import requests
import shahid_scraper as S

CAROUSELS = ["NA-Series-Mood-GulfSeries", "NA-Series-Mood-ComingSoon", "NA-Series-Mood-NewReleases"]
API = "https://api3.shahid.net/proxy/v2.1/editorial/carousel"
DATA = os.environ.get("DATA_DIR", "elcinema_data")


def sids_in_file(path):
    out = set()
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                s = S.series_id(urllib.parse.unquote(line))
                if s:
                    out.add(int(s))
    return out


def review_sids():
    out = set()
    p = os.path.join(DATA, "shahid_review.jsonl")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                s = S.series_id(urllib.parse.unquote(json.loads(line).get("url", "")))
            except ValueError:
                continue
            if s:
                out.add(int(s))
    return out


def discover():
    s = requests.Session()
    s.headers.update({"User-Agent": S.UA, "Accept-Language": "ar"})
    known = sids_in_file("shahid_series.txt") | review_sids()
    found = {}
    for cid in CAROUSELS:
        req = {"displayedItems": 0, "id": f"Main/NORTHAFRICA/series/{cid}~Guest", "itemsRequestedStatic": True,
               "pageNumber": 0, "pageSize": 50}
        try:
            r = s.get(API, params={"request": json.dumps(req, separators=(",", ":")), "country": "SA"}, timeout=30)
            j = r.json()
        except Exception as e:
            print("! تعذر قراءة", cid, e)
            continue
        for e in j.get("editorialItems") or []:
            it = e.get("item") or {}
            u = (it.get("productUrl") or {}).get("url", "")
            if "/series/" not in u:
                continue
            sid = S.series_id(urllib.parse.unquote(u))
            if not sid or int(sid) in known or int(sid) in found:
                continue
            found[int(sid)] = (it.get("title", ""), urllib.parse.quote(urllib.parse.unquote(u), safe=":/"))
        print(cid, "->", len(j.get("editorialItems") or []), "عنصرًا")
    with open("shahid_candidates.txt", "w", encoding="utf-8") as f:
        for sid, (t, u) in found.items():
            f.write(u + "\n")
            print("مرشّح جديد:", t, sid)
    print("مرشّحون جدد:", len(found))


def commit():
    if not os.path.exists("shahid_candidates.txt"):
        return
    db = sqlite3.connect(glob.glob(os.path.join(DATA, "*.sqlite"))[0])
    add = []
    for line in open("shahid_candidates.txt", encoding="utf-8"):
        u = line.strip()
        sid = S.series_id(urllib.parse.unquote(u)) if u else None
        if sid and db.execute("SELECT 1 FROM works WHERE work_id=?", (S.work_id_for(int(sid)),)).fetchone():
            add.append(u)
    if add:
        with open("shahid_series.txt", "a", encoding="utf-8") as f:
            for u in add:
                f.write(u + "\n")
    print("أُضيف إلى القائمة اليومية:", len(add))


if __name__ == "__main__":
    {"discover": discover, "commit": commit}[sys.argv[1]]()

"""يتحقق عبر DeepSeek من أن مسلسلات elcinema في saudi_elcinema_ids.txt سعودية وسنتها 2026،
ثم يصدّر المعتمد منها في SA2 (GITHUB_ENV). ما لم يتأكد يُسجَّل في elcinema_data/saudi_review.jsonl ولا يُنشر."""
import argparse
import glob
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import shahid_scraper as S


def read_ids(path):
    out = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.split("#")[0].strip()
        if ln.isdigit():
            out.append(int(ln))
    return out


def pick_col(db, table, options):
    cols = [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]
    return next((c for c in options if c in cols), None)


def cast_names(db, wid):
    col = pick_col(db, "credits", ("person_name", "name", "person_ar", "person"))
    if not col:
        return []
    out = []
    for (v,) in db.execute("SELECT %s FROM credits WHERE work_id=? LIMIT 12" % col, (wid,)):
        if isinstance(v, str) and v.strip() and not v.strip().isdigit() and v not in out:
            out.append(v.strip())
    return out[:4]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", default="saudi_elcinema_ids.txt")
    ap.add_argument("--data-dir", default="elcinema_data")
    ap.add_argument("--sleep", type=float, default=3.0)
    ap.add_argument("--year", type=int, default=2026)
    a = ap.parse_args()
    db = sqlite3.connect(glob.glob(os.path.join(a.data_dir, "*.sqlite"))[0])
    tcol = pick_col(db, "works", ("title", "title_ar", "name_ar", "name"))
    ok, review = [], os.path.join(a.data_dir, "saudi_review.jsonl")

    def log(wid, title, reason, doms):
        with open(review, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "work_id": wid,
                                "title": title, "reason": reason, "domains": doms}, ensure_ascii=False) + "\n")
        print("استبعد", wid, title, "-", reason)

    for wid in read_ids(a.ids_file):
        row = db.execute("SELECT %s, year, extra FROM works WHERE work_id=?" % tcol, (wid,)).fetchone()
        if not row:
            print("غير مجلوب بعد", wid)
            continue
        title, year, extra = row
        if year != a.year:
            log(wid, title, "السنة %s وليست %s" % (year, a.year), [])
            continue
        if S.already_verified(db, wid):
            ok.append(wid)
            continue
        d = {"title": title, "year": year, "cast": cast_names(db, wid)}
        try:
            good, reason, doms = S.verify_country(d, "السعودية")
        except Exception as e:
            good, reason, doms = False, "تعذّر التحقق: %s" % e, []
        if good:
            try:
                ex = json.loads(extra or "{}")
            except ValueError:
                ex = {}
            ex["country_verified"] = reason
            ex["country_sources"] = doms
            db.execute("UPDATE works SET extra=? WHERE work_id=?", (json.dumps(ex, ensure_ascii=False), wid))
            db.commit()
            ok.append(wid)
            print("اعتمد", wid, title, reason, doms)
        else:
            log(wid, title, reason, doms)
        time.sleep(a.sleep)
    print("المعتمد:", len(ok))
    env = os.environ.get("GITHUB_ENV")
    if env:
        with open(env, "a") as f:
            f.write("SA2=%s\n" % " ".join(str(x) for x in ok))


if __name__ == "__main__":
    main()

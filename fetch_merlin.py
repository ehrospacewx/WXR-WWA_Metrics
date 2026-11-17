#!/usr/bin/env python3
"""Archive MERLIN lightning from the KSC Spaceport Weather Archive
(https://kscweather.ksc.nasa.gov/wxarchive) into monthly JSON files that the
WWA verification page reads.

Pulls both MERLIN Cloud-to-Ground (C-G) and MERLIN Cloud-to-Cloud (C-C) for each
UTC day using the archive's own search + CSV export (the same thing the
"Search MERLIN C-C / C-G" pages do). Standard library only.

    python scripts/fetch_merlin.py                      # last 3 days (daily run)
    python scripts/fetch_merlin.py --start 2026-10-01 --end 2026-11-01

--end is exclusive. Re-fetching a day replaces what was stored for it.
"""
import argparse, csv, html.parser, http.cookiejar, io, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "merlin"
BASE = os.environ.get("KSC_WXARCHIVE", "https://kscweather.ksc.nasa.gov/wxarchive").rstrip("/")
PAGES = {"cg": "MerlinCloudToGround", "cc": "MerlinCloudToCloud"}
# Box around every Eastern Range warning location plus margin (~20 NM).
BOX = {"lat_min": 27.90, "lat_max": 28.95, "lon_min": -81.15, "lon_max": -80.20}
PAUSE = float(os.environ.get("KSC_PAUSE", "1.0"))  # seconds between requests, to be polite
UTC = timezone.utc


class FormParser(html.parser.HTMLParser):
    """Collects the fields the browser would submit from <form id="SearchForm">."""
    def __init__(self):
        super().__init__()
        self.inside, self.action, self.fields = False, None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "SearchForm":
            self.inside, self.action = True, a.get("action")
        elif self.inside and tag == "input" and a.get("name"):
            t = (a.get("type") or "text").lower()
            if t in ("radio", "checkbox") and "checked" not in a:
                return
            if t in ("submit", "button", "image", "file"):
                return
            self.fields.append((a["name"], a.get("value", "")))

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


class Archive:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.op.addheaders = [("User-Agent", "ER-WWA-Verification/1.0 (GitHub Actions; 45 WS verification)")]

    def _open(self, url, data=None, timeout=180):
        for attempt in range(4):
            try:
                with self.op.open(url, data=data, timeout=timeout) as r:
                    return r.geturl(), r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 3:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise
            time.sleep(10 * (attempt + 1))

    def day(self, kind, day):
        """All detections of one kind for one UTC day, as (epoch_s, lat, lon)."""
        page = PAGES[kind]
        _, form_html = self._open(f"{BASE}/{page}")
        fp = FormParser(); fp.feed(form_html)
        if not fp.action:
            raise RuntimeError(f"{page}: search form not found (site layout may have changed)")
        nxt = day + timedelta(days=1)
        values = {"startDate": day.strftime("%m/%d/%Y"), "startTime": "00:00:00",
                  "endDate": nxt.strftime("%m/%d/%Y"), "endTime": "00:00:00"}
        fields = [(k, values.get(k, v)) for k, v in fp.fields]
        time.sleep(PAUSE)
        url, result = self._open(urllib.parse.urljoin(BASE + "/", fp.action), urllib.parse.urlencode(fields).encode())
        m = re.search(r'href="([^"]*/%s/Export/[^"]+)"' % page, result)
        if not m:  # empty searches still offer an export, so a missing link means something went wrong
            err = re.findall(r'class="[^"]*(?:validation|alert|error)[^"]*"[^>]*>([^<]+)', result)
            raise RuntimeError(f"{page}: no export link ({'; '.join(e.strip() for e in err if e.strip())[:200] or 'unexpected page'})")
        time.sleep(PAUSE)
        _, text = self._open(urllib.parse.urljoin(url, m.group(1).replace("&amp;", "&")))
        return parse_csv(text)


def coord(s):
    s = str(s).strip()
    if ":" in s:  # D:M:S, sign on degrees (C-C export)
        neg = s.startswith("-")
        d, mnt, sec = (abs(float(x)) for x in (s.lstrip("+-").split(":") + ["0", "0"])[:3])
        v = d + mnt / 60 + sec / 3600
        return -v if neg else v
    return float(s)


def parse_csv(text):
    rows = []
    rdr = csv.reader(io.StringIO(text))
    head = next(rdr, None)
    if not head:
        return rows
    h = [c.strip().lower() for c in head]
    try:
        iD, iT, iLa, iLo = h.index("date"), h.index("time"), h.index("latitude"), h.index("longitude")
    except ValueError:
        raise RuntimeError("export columns changed: " + ", ".join(head))
    for r in rdr:
        if len(r) <= max(iD, iT, iLa, iLo):
            continue
        try:
            mo, dd, yy = (int(x) for x in r[iD].split("/"))
            hh, mi, ss = r[iT].split(":")
            sec = float(ss)
            t = datetime(yy, mo, dd, int(hh), int(mi), int(sec), tzinfo=UTC).timestamp() + (sec - int(sec))
            la, lo = coord(r[iLa]), coord(r[iLo])
        except (ValueError, IndexError):
            continue
        if BOX["lat_min"] <= la <= BOX["lat_max"] and BOX["lon_min"] <= lo <= BOX["lon_max"]:
            rows.append((round(t, 1), round(la * 1e4), round(lo * 1e4)))
    return rows


def month_key(d):
    return d.strftime("%Y-%m")


def load_month(key):
    p = OUT / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"month": key, "box": BOX, "coverage": [], "cg": {"t": [], "lat": [], "lon": []}, "cc": {"t": [], "lat": [], "lon": []}}


def merge_cov(cov):
    out = []
    for s, e in sorted([list(c) for c in cov]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def replace_window(month, kind, s, e, rows):
    col = month[kind]
    keep = [(t, la, lo) for t, la, lo in zip(col["t"], col["lat"], col["lon"]) if not (s <= t < e)]
    keep += [r for r in rows if s <= r[0] < e]
    keep = sorted(set(keep))
    month[kind] = {"t": [k[0] for k in keep], "lat": [k[1] for k in keep], "lon": [k[2] for k in keep]}


def parse_day(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=UTC)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="UTC date YYYY-MM-DD, inclusive")
    ap.add_argument("--end", help="UTC date YYYY-MM-DD, exclusive (default: tomorrow)")
    ap.add_argument("--days", type=int, default=3, help="when --start is omitted, fetch this many days back")
    a = ap.parse_args()
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = parse_day(a.end) if a.end else today + timedelta(days=1)
    start = parse_day(a.start) if a.start else end - timedelta(days=a.days + 1)
    if end <= start:
        sys.exit("Nothing to fetch: end is not after start")
    OUT.mkdir(parents=True, exist_ok=True)
    arc, months, failed = Archive(), {}, []
    d = start
    while d < end:
        key = month_key(d)
        m = months.setdefault(key, load_month(key))
        s, e = int(d.timestamp()), int((d + timedelta(days=1)).timestamp())
        ok, counts = True, {}
        for kind in ("cg", "cc"):
            try:
                rows = arc.day(kind, d)
                replace_window(m, kind, s, e, rows)
                counts[kind] = len(rows)
            except Exception as ex:  # keep going; this day stays out of coverage
                ok = False
                counts[kind] = "FAILED"
                failed.append(f"{d:%Y-%m-%d} {kind.upper()}: {ex}")
            time.sleep(PAUSE)
        complete_until = min(e, int(datetime.now(UTC).timestamp()) - 3600)  # archive lags; don't claim the last hour
        if ok and complete_until > s:
            m["coverage"] = merge_cov(m["coverage"] + [[s, complete_until]])
        print(f"{d:%Y-%m-%d}  CG {counts.get('cg')!s:>7}  CC {counts.get('cc')!s:>8}", flush=True)
        d += timedelta(days=1)
    for key, m in months.items():
        m["updated"] = datetime.now(UTC).isoformat(timespec="seconds")
        m["source"] = "KSC Spaceport Weather Archive: MERLIN C-G and C-C"
        m["counts"] = {"cg": len(m["cg"]["t"]), "cc": len(m["cc"]["t"])}
        (OUT / f"{key}.json").write_text(json.dumps(m, separators=(",", ":")))
    index = []
    for p in sorted(OUT.glob("????-??.json")):
        mm = json.loads(p.read_text())
        index.append({"month": mm["month"], "file": p.name, "updated": mm.get("updated"), "coverage": mm["coverage"], "counts": mm.get("counts", {})})
    (OUT / "index.json").write_text(json.dumps({"source": "KSC Spaceport Weather Archive: MERLIN C-G and C-C", "box": BOX, "months": index}, indent=1))
    if failed:
        print("\nDays not fetched (left out of coverage; re-run for these dates):\n  " + "\n  ".join(failed[:60]))


if __name__ == "__main__":
    main()

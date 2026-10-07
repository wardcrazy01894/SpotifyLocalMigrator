#!/usr/bin/env python3
"""Rebuild a Spotify "local files" playlist as a proper Spotify playlist.

Pipeline (each step is resumable and writes to ./work/<slug>/):
  auth              one-time browser login (PKCE, no client secret needed)
  find   <name>     locate one of your playlists by name, print its id/url
  export <ref>      dump the source playlist (incl. local files) -> source.json
  match             search Spotify for each local track; strict matching -> matches.json
  build             create the new playlist, add matched tracks -> result.json
  all    <ref>      export + match + build

<ref> is a playlist URL/URI/id, or a playlist name prefixed with "name:".
Only the Python standard library is used. The Web API cannot create playlist
folders, so the folder step stays manual in the desktop app.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK_ROOT = HERE / "work"
TOKEN_FILE = HERE / "token.json"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public"
)
API = "https://api.spotify.com/v1"
DUR_TOL_MS = 2000          # title+artist+duration within this => added
DUR_FLAG_MS = 10000        # beyond this we call it a different recording


# ----------------------------------------------------------------------------
# Auth (Authorization Code with PKCE)
# ----------------------------------------------------------------------------
def _client_id() -> str:
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    if not cid:
        sys.exit("Set SPOTIFY_CLIENT_ID (from https://developer.spotify.com/dashboard).")
    return cid


def _token_request(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as r:
        tok = json.loads(r.read())
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    if "refresh_token" not in tok and TOKEN_FILE.exists():
        tok["refresh_token"] = json.loads(TOKEN_FILE.read_text()).get("refresh_token")
    TOKEN_FILE.write_text(json.dumps(tok))
    return tok


def cmd_auth(_args) -> None:
    cid = _client_id()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Done. You can close this tab.</h2>")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()

    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    print("Opening browser for Spotify login. If it does not open, visit:\n", url)
    webbrowser.open(url)
    deadline = time.time() + 300
    while "code" not in got and "error" not in got and time.time() < deadline:
        time.sleep(0.2)
    srv.server_close()
    if "error" in got:
        sys.exit(f"Spotify returned error: {got['error']}")
    if "code" not in got:
        sys.exit("Timed out waiting for the browser redirect.")
    if got.get("state") != state:
        sys.exit("State mismatch; aborting.")
    _token_request(
        {
            "grant_type": "authorization_code",
            "code": got["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": cid,
            "code_verifier": verifier,
        }
    )
    me = api_get("/me")
    print(f"Logged in as {me.get('display_name')} ({me.get('id')}). Token saved to {TOKEN_FILE.name}.")


def access_token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit("Not logged in. Run: python3 migrate.py auth")
    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() >= tok.get("expires_at", 0):
        tok = _token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": _client_id(),
            }
        )
    return tok["access_token"]


# ----------------------------------------------------------------------------
# HTTP helpers with 429 handling
# ----------------------------------------------------------------------------
def _call(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = path if path.startswith("http") else API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for attempt in range(8):
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {access_token()}")
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "2")) + 1
                if wait > 120:
                    raise RateLimited(wait) from None
                print(f"\n  rate limited; sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {url} -> {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Giving up on {method} {url}")


class RateLimited(Exception):
    def __init__(self, wait_s: int):
        super().__init__(f"Spotify rate limit; retry after {wait_s / 3600:.1f} h")
        self.wait_s = wait_s


def api_get(path: str, **params) -> dict:
    return _call("GET", path, params or None)


def api_post(path: str, body: dict) -> dict:
    return _call("POST", path, body=body)


def paged(url: str, params: dict | None) -> list[dict]:
    out: list[dict] = []
    first = True
    while url:
        page = _call("GET", url, params if first else None)
        first = False
        out.extend(page.get("items", []))
        url = page.get("next")
    return out


# ----------------------------------------------------------------------------
# Locate / export
# ----------------------------------------------------------------------------
def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower() or "playlist"


def workdir(name: str) -> Path:
    d = WORK_ROOT / slug(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


WORK_OVERRIDE: str | None = None


def current_work() -> Path:
    if WORK_OVERRIDE:
        d = WORK_ROOT / slug(WORK_OVERRIDE)
        if not (d / "source.json").exists():
            sys.exit(f"No export found at {d}. Run: python3 migrate.py export \"name:{WORK_OVERRIDE}\"")
        return d
    marker = WORK_ROOT / "current"
    if not marker.exists():
        sys.exit("Nothing exported yet. Run: python3 migrate.py export <ref>")
    return Path(marker.read_text().strip())


def _total(p: dict) -> int:
    return ((p.get('items') or p.get('tracks') or {}).get('total', 0))


def my_playlists() -> list[dict]:
    return paged(f"{API}/me/playlists", {"limit": 50})


def resolve_playlist(ref: str) -> dict:
    if ref.startswith("name:"):
        want = ref[5:].strip().lower()
        hits = [p for p in my_playlists() if (p.get("name") or "").strip().lower() == want]
        if not hits:
            sys.exit(f"No playlist named '{ref[5:]}' in your library.")
        if len(hits) > 1:
            print("Multiple playlists share that name; using the first:", file=sys.stderr)
            for p in hits:
                print(f"  {p['name']}  {_total(p)} items  {p['external_urls']['spotify']}", file=sys.stderr)
        return hits[0]
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", ref) or re.fullmatch(r"([A-Za-z0-9]{22})", ref)
    if not m:
        sys.exit(f"Could not parse playlist reference: {ref}")
    return api_get(f"/playlists/{m.group(1)}", fields="id,name,external_urls,items.total,tracks.total")


def cmd_find(args) -> None:
    want = args.name.strip().lower()
    for p in my_playlists():
        if want in (p.get("name") or "").lower():
            print(f"{p['name']!r}  {_total(p)} items  {p['external_urls']['spotify']}")


def cmd_export(args) -> Path:
    pl = resolve_playlist(args.playlist)
    pid, name = pl["id"], pl["name"]
    wd = workdir(name)
    (WORK_ROOT / "current").write_text(str(wd))
    print(f"Source playlist: {name} ({_total(pl)} items)")
    raw = paged(
        f"{API}/playlists/{pid}/items",
        {
            "limit": 50,
            "additional_types": "track",
            "fields": "next,items(is_local,item(uri,id,name,duration_ms,artists(name),album(name)))",
        },
    )
    items: list[dict] = []
    for it in raw:
        t = it.get("item") or it.get("track") or {}
        if not t:
            continue
        items.append(
            {
                "pos": len(items),
                "is_local": bool(it.get("is_local")),
                "uri": t.get("uri"),
                "id": t.get("id"),
                "title": t.get("name") or "",
                "artist": ", ".join(a.get("name", "") for a in t.get("artists") or []),
                "album": (t.get("album") or {}).get("name") or "",
                "duration_ms": t.get("duration_ms") or 0,
            }
        )
    local = sum(1 for i in items if i["is_local"])
    (wd / "source.json").write_text(json.dumps({"name": name, "id": pid, "items": items}, indent=1))
    print(f"Exported {len(items)} items ({local} local files, {len(items) - local} Spotify tracks) -> {wd / 'source.json'}")
    return wd


# ----------------------------------------------------------------------------
# Matching (strict: title + artist + duration)
# ----------------------------------------------------------------------------
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_TRACKNO = re.compile(r"^\s*\d{1,3}\s*[-._)\]]*\s+")
_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b.*$", re.IGNORECASE)
_SPLIT_ARTISTS = re.compile(
    r"\s*(?:,|;|/|&|\+|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bvs\.?\b|\bx\b)\s*", re.IGNORECASE
)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ").replace("’", "'")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def norm_title(s: str) -> str:
    return norm(_TRACKNO.sub("", s or ""))


def core_title(s: str) -> str:
    s = _TRACKNO.sub("", s or "")
    s = _PAREN.sub(" ", s)
    s = _FEAT.sub("", s)
    s = re.sub(
        r"\s*-\s*(remaster(ed)?|album version|single version|explicit|clean|radio edit|live|acoustic|\d{4}\b).*$",
        "", s, flags=re.I,
    )
    return norm(s)


def artist_set(s: str) -> set[str]:
    return {norm(a) for a in _SPLIT_ARTISTS.split(s or "") if norm(a)}


def artists_match(local_artist: str, cand_artists: list[str]) -> bool:
    la = norm(local_artist)
    if not la or not cand_artists:
        return False
    cand = [norm(a) for a in cand_artists]
    if la in cand or la == norm(", ".join(cand_artists)) or la == " ".join(cand):
        return True
    ls, cs = artist_set(local_artist), set(cand)
    if ls and (ls <= cs or cs <= ls):
        return True
    return norm(_FEAT.sub("", local_artist)) == cand[0]


def evaluate(src: dict, c: dict) -> dict:
    """Score one search candidate against the local track.

    verdict: match | duration_mismatch | title_variant | artist_mismatch | no_match
    Only 'match' is added to the playlist.
    """
    cand_artists = [a["name"] for a in c.get("artists", [])]
    title_exact = norm_title(src["title"]) == norm_title(c["name"])
    title_core = core_title(src["title"]) == core_title(c["name"])
    artist_ok = artists_match(src["artist"], cand_artists)
    dur_src, dur_c = src["duration_ms"] or 0, c.get("duration_ms", 0)
    dur_diff = abs(dur_src - dur_c) if dur_src else None
    dur_ok = dur_diff is not None and dur_diff <= DUR_TOL_MS

    score = (40 if title_exact else 20 if title_core else 0) + (30 if artist_ok else 0)
    if dur_diff is not None:
        score += 30 if dur_ok else max(0, 20 - dur_diff // 1000)

    if title_exact and artist_ok and dur_ok:
        verdict = "match"
    elif title_exact and artist_ok:
        verdict = "duration_mismatch" if dur_diff is not None else "no_duration"
    elif title_core and artist_ok:
        verdict = "title_variant"
    elif (title_exact or title_core) and not artist_ok:
        verdict = "artist_mismatch"
    else:
        verdict = "no_match"
    return {
        "verdict": verdict,
        "score": score,
        "uri": c["uri"],
        "title": c["name"],
        "artist": ", ".join(cand_artists),
        "album": c.get("album", {}).get("name", ""),
        "duration_ms": dur_c,
        "dur_diff_ms": dur_diff,
    }


def search_candidates(src: dict) -> list[dict]:
    title = _TRACKNO.sub("", src["title"]).strip()
    primary = next(iter(_SPLIT_ARTISTS.split(src["artist"] or "")), "").strip()
    queries: list[str] = []
    if title and primary:
        queries.append(f'track:"{title}" artist:"{primary}"')
        queries.append(f"{title} {primary}")
    ct = _PAREN.sub("", title).strip()
    if ct and ct != title and primary:
        queries.append(f'track:"{ct}" artist:"{primary}"')
    if title and not primary:
        queries.append(f'track:"{title}"')
    seen: dict[str, dict] = {}
    for q in queries:
        res = api_get("/search", q=q, type="track", limit=10, market="US")
        for t in res.get("tracks", {}).get("items", []):
            seen.setdefault(t["id"], t)
        if any(evaluate(src, t)["verdict"] == "match" for t in seen.values()):
            break
    return list(seen.values())


def cmd_match(args) -> None:
    wd = current_work()
    src = json.loads((wd / "source.json").read_text())
    cache_path = wd / "matches.json"
    cache: dict[str, dict] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    items = src["items"]
    todo = [i for i in items if i["is_local"] and str(i["pos"]) not in cache]
    limit = getattr(args, "limit", None)
    if limit:
        todo = todo[:limit]
    remaining = sum(1 for i in items if i["is_local"] and str(i["pos"]) not in cache)
    batch = datetime.date.today().isoformat()
    print(f"{len(items)} items; {remaining} local files still to match; doing {len(todo)} now (batch {batch})")
    t0 = time.time()
    stopped: str | None = None
    for n, it in enumerate(todo, 1):
        try:
            cands = search_candidates(it)
        except RateLimited as e:
            stopped = str(e)
            break
        except RuntimeError as e:
            print(f"\n  search failed for #{it['pos']} {it['title']}: {e}", file=sys.stderr)
            continue
        if not cands:
            cache[str(it["pos"])] = {"verdict": "no_candidates", "score": 0, "uri": None, "batch": batch}
        else:
            order = {"match": 5, "duration_mismatch": 4, "no_duration": 4, "title_variant": 3, "artist_mismatch": 2, "no_match": 1}
            best = max((evaluate(it, c) for c in cands), key=lambda r: (order[r["verdict"]], r["score"]))
            best["batch"] = batch
            cache[str(it["pos"])] = best
        if n % 10 == 0 or n == len(todo):
            cache_path.write_text(json.dumps(cache, indent=1))
            rate = n / max(time.time() - t0, 1e-6)
            print(f"  {n}/{len(todo)}  ({rate:.1f}/s)", end="\r", flush=True)
        time.sleep(0.05)
    print()
    carried = apply_global(src, cache)
    if carried:
        print(f"  {carried} decision(s) carried over from earlier playlists")
    cache_path.write_text(json.dumps(cache, indent=1))
    if stopped:
        print(f"STOPPED EARLY: {stopped}. Progress is saved; rerun match later.", file=sys.stderr)
    write_reports(wd, src, cache, batch=batch)


def fmt_dur(ms: int | None) -> str:
    ms = ms or 0
    return f"{ms // 60000}:{(ms // 1000) % 60:02d}"


def write_reports(wd: Path, src: dict, cache: dict, batch: str | None = None) -> dict:
    """Print the breakdown; write flagged.csv (all open flags) and, if batch is
    given, flagged_<batch>.csv with only that batch's open flags."""
    counts: dict[str, int] = {}
    rows: list[dict] = []
    for it in src["items"]:
        if not it["is_local"]:
            counts["already_spotify"] = counts.get("already_spotify", 0) + 1
            continue
        m = cache.get(str(it["pos"]), {"verdict": "not_searched"})
        v = m["verdict"]
        if m.get("keep_local"):
            v = "keep_local"
        elif m.get("approved"):
            v = "approved_" + v
        counts[v] = counts.get(v, 0) + 1
        if v in ("match", "not_searched", "keep_local") or v.startswith("approved_"):
            continue
        rows.append(
            {
                "batch": m.get("batch", ""),
                "reason": v + (" (recheck)" if m.get("recheck") else ""),
                "pos": it["pos"],
                "title": it["title"],
                "artist": it["artist"],
                "album": it["album"],
                "duration": fmt_dur(it["duration_ms"]),
                "closest_spotify_track": (
                    f"{m.get('title')} — {m.get('artist')} [{m.get('album')}] {fmt_dur(m.get('duration_ms'))}"
                    if m.get("uri") else ""
                ),
                "duration_diff_s": (
                    f"{m['dur_diff_ms'] / 1000:.1f}" if m.get("dur_diff_ms") is not None else ""
                ),
                "closest_uri": m.get("uri") or "",
            }
        )
    rows.sort(key=lambda r: (r["reason"], r["pos"]))
    fields = list(rows[0].keys()) if rows else ["reason"]
    with (wd / "flagged.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    if batch:
        with (wd / f"flagged_{batch}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows([r for r in rows if r["batch"] == batch])
    searched = sum(v for k, v in counts.items() if k not in ("not_searched", "already_spotify"))
    will_add = counts.get("match", 0) + sum(v for k, v in counts.items() if k.startswith("approved_"))
    summary = {
        "total": len(src["items"]),
        "searched": searched,
        "not_searched": counts.get("not_searched", 0),
        "in_playlist_after_build": will_add + counts.get("already_spotify", 0),
        "open_flags": len(rows),
        "counts": counts,
    }
    (wd / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    print(f"Open flags: {wd / 'flagged.csv'}" + (f"; this batch: {wd / f'flagged_{batch}.csv'}" if batch else ""))
    return summary


# ----------------------------------------------------------------------------
# Approve / report (the human-in-the-loop part)
# ----------------------------------------------------------------------------
GLOBAL_APPROVALS = WORK_ROOT / "approvals.json"


def wanted(m: dict | None) -> bool:
    """Should this local track's candidate go into the new playlist?"""
    return bool(m) and bool(m.get("uri")) and (m["verdict"] == "match" or m.get("approved") is True)


def song_key(it: dict) -> str:
    return f"{norm_title(it['title'])}|{norm(it['artist'])}|{(it['duration_ms'] or 0) // 1000}"


def load_global() -> dict:
    return json.loads(GLOBAL_APPROVALS.read_text()) if GLOBAL_APPROVALS.exists() else {}


def record_global(it: dict, m: dict, decision: str, playlist: str) -> None:
    """Standing rule: a decision made in one playlist applies to the same song everywhere."""
    g = load_global()
    g[song_key(it)] = {
        "decision": decision,  # approve | keep_local
        "uri": m.get("uri") if decision == "approve" else None,
        "title": it["title"],
        "artist": it["artist"],
        "from_playlist": playlist,
    }
    GLOBAL_APPROVALS.write_text(json.dumps(g, indent=1))


def apply_global(src: dict, cache: dict) -> int:
    g = load_global()
    n = 0
    for it in src["items"]:
        m = cache.get(str(it["pos"]))
        d = g.get(song_key(it))
        if not m or not d or m["verdict"] == "match" or m.get("approved") is not None or m.get("keep_local"):
            continue
        if d["decision"] == "keep_local":
            m["keep_local"] = True
            m["approved"] = False
        else:
            m["approved"] = True
            if d.get("uri") and d["uri"] != m.get("uri"):
                m["uri"] = d["uri"]
                m["title"] = m.get("title", "") + "  (uri carried from " + d["from_playlist"] + ")"
        m["carried_from"] = d["from_playlist"]
        n += 1
    return n


def load_state() -> tuple[Path, dict, dict]:
    wd = current_work()
    src = json.loads((wd / "source.json").read_text())
    cache = json.loads((wd / "matches.json").read_text()) if (wd / "matches.json").exists() else {}
    if apply_global(src, cache):
        (wd / "matches.json").write_text(json.dumps(cache, indent=1))
    return wd, src, cache


def cmd_approve(args) -> None:
    """Mark flagged tracks as approved (added on next build) / keep-local / recheck."""
    wd, src, cache = load_state()
    reasons = set(filter(None, (args.reasons or "").split(",")))
    positions = {int(x) for x in filter(None, (args.pos or "").split(","))}
    keep = {int(x) for x in filter(None, (args.keep_local or "").split(","))}
    recheck = {int(x) for x in filter(None, (getattr(args, "recheck", None) or "").split(","))}
    changed = 0
    for it in src["items"]:
        m = cache.get(str(it["pos"]))
        if not m:
            continue
        if it["pos"] in keep:
            m["approved"] = False
            m["keep_local"] = True
            m.pop("recheck", None)
            record_global(it, m, "keep_local", src["name"])
            changed += 1
        elif it["pos"] in recheck:
            m["recheck"] = True
            changed += 1
        elif m["verdict"] != "match" and (m["verdict"] in reasons or it["pos"] in positions) and m.get("uri"):
            if not m.get("keep_local"):
                m["approved"] = True
                m.pop("recheck", None)
                record_global(it, m, "approve", src["name"])
                changed += 1
                print(f"  approve #{it['pos']}: {it['title']} — {it['artist']}  ->  {m['title']} — {m['artist']}")
    (wd / "matches.json").write_text(json.dumps(cache, indent=1))
    print(f"{changed} entries updated. Run 'build' to apply.")
    cmd_report(args)


def cmd_research(args) -> None:
    """Re-search one flagged song with a hand-written query (e.g. 'I See Fire Kygo remix')."""
    wd, src, cache = load_state()
    it = src["items"][args.pos]
    m = cache.get(str(it["pos"]), {})
    print(f"#{it['pos']}: {it['title']} — {it['artist']} {fmt_dur(it['duration_ms'])}")
    if args.pick:
        t = api_get(f"/tracks/{args.pick.split(':')[-1]}", market="US")
        r = evaluate(it, t)
        r["approved"] = True
        r["batch"] = m.get("batch")
        cache[str(it["pos"])] = r
        record_global(it, r, "approve", src["name"])
        print(f"  picked: {r['title']} — {r['artist']} {fmt_dur(r['duration_ms'])} ({r['verdict']})")
    else:
        res = api_get("/search", q=args.query, type="track", limit=10, market="US")
        cands = sorted((evaluate(it, t) for t in res.get("tracks", {}).get("items", [])), key=lambda r: -r["score"])
        for r in cands[:8]:
            print(f"  {r['verdict']:17} {r['score']:3}  {r['title']} — {r['artist']} [{r['album']}] {fmt_dur(r['duration_ms'])}  {r['uri']}")
        if cands and (args.take or cands[0]["verdict"] == "match"):
            best = cands[0]
            best["batch"] = m.get("batch")
            if best["verdict"] != "match":
                best["approved"] = True
                record_global(it, best, "approve", src["name"])
            cache[str(it["pos"])] = best
            print(f"  -> stored {best['title']} ({best['verdict']}{', approved' if best.get('approved') else ''})")
        elif cands:
            print("  -> nothing stored (no exact match; use --take to accept the top result, or --pick <uri>)")
    cache[str(it["pos"])].pop("recheck", None) if cache.get(str(it["pos"]), {}).get("verdict") == "match" or cache.get(str(it["pos"]), {}).get("approved") else None
    (wd / "matches.json").write_text(json.dumps(cache, indent=1))


def cmd_verify(args) -> None:
    """Completeness check: source count == Spotify tracks in new playlist + local files to drag in."""
    wd, src, cache = load_state()
    rp = wd / "result.json"
    if not rp.exists():
        sys.exit("No playlist built yet for this source.")
    res = json.loads(rp.read_text())
    live = paged(f"{API}/playlists/{res['playlist_id']}/items", {"limit": 50, "fields": "next,items(is_local,item(uri))"})
    live_spotify = sum(1 for x in live if not x.get("is_local"))
    live_local = sum(1 for x in live if x.get("is_local"))
    expected_spotify, _ = desired_uris(src, cache, False)
    keep_local = [it for it in src["items"] if cache.get(str(it["pos"]), {}).get("keep_local")]
    unresolved = [
        it for it in src["items"]
        if it["is_local"] and not wanted(cache.get(str(it["pos"]))) and not cache.get(str(it["pos"]), {}).get("keep_local")
    ]
    searched = sum(1 for it in src["items"] if str(it["pos"]) in cache)
    print(f"Source '{src['name']}': {len(src['items'])} songs ({searched} searched so far)")
    print(f"New playlist '{res['url']}': {live_spotify} Spotify tracks + {live_local} local files = {len(live)}")
    print(f"  Spotify tracks expected from decisions: {len(expected_spotify)}  {'OK' if live_spotify == len(expected_spotify) else 'MISMATCH'}")
    print(f"  Local files to drag in by hand: {len(keep_local)}  (present now: {live_local})")
    for it in keep_local:
        print(f"    - {it['title']} — {it['artist']}")
    print(f"  Unresolved (not yet decided or searched): {len(unresolved)}")
    for it in unresolved[:40]:
        v = cache.get(str(it["pos"]), {}).get("verdict", "not_searched")
        print(f"    - #{it['pos']} {it['title']} — {it['artist']} [{v}{', recheck' if cache.get(str(it['pos']), {}).get('recheck') else ''}]")
    if len(unresolved) > 40:
        print(f"    ... and {len(unresolved) - 40} more")
    total_accounted = live_spotify + live_local
    if not unresolved and total_accounted == len(src["items"]):
        print("COMPLETE: every source song is accounted for in the new playlist.")
    elif not unresolved:
        print(f"All decided; drag the {len(keep_local) - live_local} remaining local file(s) in to finish.")


def cmd_report(args) -> None:
    wd, src, cache = load_state()
    write_reports(wd, src, cache, batch=getattr(args, "batch", None))


# ----------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------
def desired_uris(src: dict, cache: dict, keep_duplicates: bool) -> tuple[list[str], int]:
    uris: list[str] = []
    for it in src["items"]:
        if not it["is_local"]:
            if it["uri"] and it["uri"].startswith("spotify:track:"):
                uris.append(it["uri"])
            continue
        m = cache.get(str(it["pos"]))
        if wanted(m):
            uris.append(m["uri"])
    before = len(uris)
    if not keep_duplicates:
        seen: set[str] = set()
        uris = [u for u in uris if not (u in seen or seen.add(u))]
    return uris, before - len(uris)


def cmd_build(args) -> None:
    wd, src, cache = load_state()
    uris, dupes = desired_uris(src, cache, args.keep_duplicates)
    result_path = wd / "result.json"
    update_ref = args.update
    if update_ref is None and result_path.exists() and not args.new:
        update_ref = json.loads(result_path.read_text())["playlist_id"]

    if update_ref:
        pl = resolve_playlist(update_ref)
        print(f"Rewriting '{pl['name']}' with {len(uris)} tracks ({dupes} duplicate(s) collapsed) ...")
        if args.dry_run:
            return
        _call("PUT", f"/playlists/{pl['id']}/items", body={"uris": uris[:100]})
        for i in range(100, len(uris), 100):
            api_post(f"/playlists/{pl['id']}/items", {"uris": uris[i : i + 100]})
            print(f"  added {min(i + 100, len(uris))}/{len(uris)}", end="\r", flush=True)
    else:
        name = args.name or f"{src['name']} (spotify version)"
        print(f"Creating playlist '{name}' with {len(uris)} tracks ({dupes} duplicate(s) collapsed) ...")
        if args.dry_run:
            return
        pl = api_post(
            "/me/playlists",
            {
                "name": name,
                "public": False,
                "description": f"Rebuilt from the local-files playlist '{src['name']}'.",
            },
        )
        try:  # Spotify has been ignoring public:false on create; ask again explicitly.
            _call("PUT", f"/playlists/{pl['id']}", body={"public": False})
        except RuntimeError:
            pass
        for i in range(0, len(uris), 100):
            api_post(f"/playlists/{pl['id']}/items", {"uris": uris[i : i + 100]})
            print(f"  added {min(i + 100, len(uris))}/{len(uris)}", end="\r", flush=True)
    print()
    check = api_get(f"/playlists/{pl['id']}", fields="items.total,tracks.total")
    result = {
        "playlist_id": pl["id"],
        "url": pl["external_urls"]["spotify"],
        "count": len(uris),
        "on_spotify": _total(check),
        "duplicates_collapsed": dupes,
    }
    result_path.write_text(json.dumps(result, indent=1))
    print(f"Done: {pl['external_urls']['spotify']}  ({result['on_spotify']} tracks on Spotify)")


def cmd_all(args) -> None:
    cmd_export(args)
    cmd_match(args)
    cmd_build(args)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-w", "--work", help="which exported playlist to operate on (its name); default: the last one exported")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth").set_defaults(fn=cmd_auth)
    f = sub.add_parser("find")
    f.add_argument("name")
    f.set_defaults(fn=cmd_find)
    e = sub.add_parser("export")
    e.add_argument("playlist", help='playlist URL/URI/id, or "name:<playlist name>"')
    e.set_defaults(fn=cmd_export)
    mt = sub.add_parser("match")
    mt.add_argument("--limit", type=int, help="search at most this many new songs (daily quota is roughly 400)")
    mt.set_defaults(fn=cmd_match)

    ap = sub.add_parser("approve", help="approve flagged tracks for adding, or mark them keep-local")
    ap.add_argument("--reasons", help="comma list: duration_mismatch,title_variant,artist_mismatch,no_match")
    ap.add_argument("--pos", help="comma list of source positions (from flagged.csv)")
    ap.add_argument("--keep-local", help="comma list of positions to never add")
    ap.add_argument("--recheck", help="comma list of positions to re-search by hand later (tracked, stays open)")
    ap.set_defaults(fn=cmd_approve)
    rs = sub.add_parser("research", help="re-search one song with a custom query, or pick a specific track uri")
    rs.add_argument("--pos", type=int, required=True)
    rs.add_argument("--query")
    rs.add_argument("--pick", help="spotify:track:... to accept for this song")
    rs.add_argument("--take", action="store_true", help="accept the top result even if not an exact match")
    rs.set_defaults(fn=cmd_research)
    sub.add_parser("verify", help="check the new playlist accounts for every source song").set_defaults(fn=cmd_verify)
    rp = sub.add_parser("report", help="re-print the breakdown and rewrite flagged.csv")
    rp.add_argument("--batch", help="also write flagged_<batch>.csv for this batch date (YYYY-MM-DD)")
    rp.set_defaults(fn=cmd_report)

    def build_opts(sp):
        sp.add_argument("--name", help="name for the new playlist")
        sp.add_argument("--update", help="rewrite this existing playlist (URL/id/name:...) instead of creating one")
        sp.add_argument("--new", action="store_true", help="force creating a new playlist even if result.json exists")
        sp.add_argument("--keep-duplicates", action="store_true")
        sp.add_argument("--dry-run", action="store_true")

    b = sub.add_parser("build")
    build_opts(b)
    b.set_defaults(fn=cmd_build)
    a = sub.add_parser("all")
    a.add_argument("playlist")
    build_opts(a)
    a.set_defaults(fn=cmd_all)
    args = p.parse_args()
    global WORK_OVERRIDE
    WORK_OVERRIDE = args.work
    args.fn(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build data.json for the Tip-Off Ledger dashboard from NBA play-by-play.

Two sources, same output:

  python build_data.py --source github --season 2025 --out data.json
      Downloads the season archive of NBA live play-by-play mirrored at
      github.com/shufinskiy/nba_data (regular season + playoffs).

  python build_data.py --source nba --season 2025 --out data.json
      Calls the NBA's own live-data API (cdn.nba.com) game by game.
      Run this on your own computer; it keeps a local cache in ./pbp_cache
      so later runs only fetch games it has not seen.

Add --html tipoff_ledger.html to write the fresh data straight into the
standalone dashboard file, so you can just reopen it in your browser.

  python3 build_data.py --install
      One-time setup. After this the dashboard lives at http://localhost:8765,
      starts by itself whenever you log in, and pulls new games and the day's
      schedule from the NBA API on its own every two hours. Bookmark the
      address and you never need this script again. Undo with --uninstall.

  python build_data.py --serve
      Opens the dashboard at http://localhost:8765 with a working Refresh
      button. Keep tipoff_ledger.html in the same folder as this script and
      leave the window running while you use the page. Each press pulls any
      new finished games and the coming week's schedule from the NBA API and
      reloads the numbers. Last season comes from the GitHub archive, so the
      Picks tab has tip history on opening night.

--season is the year the season starts (2025 = 2025-26). Left out, it is the
season in progress, falling back to the previous one until games are played.
"""
import argparse, io, json, os, re, shutil, socket, subprocess, sys, tarfile, threading, time, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    import pandas as pd
except ImportError:  # --install adds it
    pd = None

GH = "https://raw.githubusercontent.com/shufinskiy/nba_data/main/datasets/{name}.tar.xz"
# The NBA serves the same files from two hosts. The first refuses some cloud servers (HTTP 403),
# so each request falls back to the second.
NBA_HOSTS = ("https://cdn.nba.com/static/json", "https://nba-prod-us-east-1-mediaops-stats.s3.amazonaws.com/NBA")
NBA_SCHEDULE = "/staticData/scheduleLeagueV2.json"
NBA_PBP = "/liveData/playbyplay/playbyplay_{gid}.json"
NBA_BOX = "/liveData/boxscore/boxscore_{gid}.json"
ESPN_INJ = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
ESPN_ABBR = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nba.com/", "Accept": "application/json"}
COLS = {"gameId", "orderNumber", "period", "clock", "timeActual", "actionType", "subType", "descriptor",
        "personId", "playerNameI", "teamTricode", "shotResult", "shotDistance", "scoreHome",
        "jumpBallWonPersonId", "jumpBallLostPersonId", "jumpBallRecoverdPersonId"}
N_SHOTS = 5            # attempts are kept per team until it has N field-goal attempts
TIP_WINDOW = 690       # opening tip must be logged with >= 11:30 on the Q1 clock
ET = ZoneInfo("America/New_York")


def ssl_context():
    """Python from python.org on a Mac ships without root certificates; use certifi's or the system's."""
    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(path):
                try:
                    ctx.load_verify_locations(cafile=path)
                except Exception:
                    pass
    return ctx


CTX = None


def get(url, timeout=60):
    global CTX
    CTX = CTX or ssl_context()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


# ---------- sources ----------
def get_nba(path):
    """Fetch an NBA data file, remembering whichever host answered."""
    global NBA_HOSTS
    err = None
    for host in NBA_HOSTS:
        try:
            raw = get(host + path)
            if host != NBA_HOSTS[0]:
                NBA_HOSTS = (host,) + tuple(h for h in NBA_HOSTS if h != host)
            return raw
        except Exception as e:
            err = err or e
    raise err


def load_injuries():
    """Current injury designations by team from ESPN, or None if the feed cannot be read."""
    try:
        data = json.loads(get(ESPN_INJ))
        out = {}
        for team in data.get("injuries", []):
            for it in team.get("injuries", []):
                ath = it.get("athlete") or {}
                ab = (ath.get("team") or {}).get("abbreviation")
                short = ath.get("shortName") or (f"{ath.get('firstName', '')[:1]}. {ath.get('lastName', '')}").strip()
                if not ab or not short:
                    continue
                out.setdefault(ESPN_ABBR.get(ab, ab), []).append({
                    "n": short, "f": ath.get("displayName") or short, "st": it.get("status") or "",
                    "ty": (it.get("details") or {}).get("type") or "", "c": (it.get("shortComment") or "")[:200],
                    "d": (it.get("date") or "")[:10]})
        return out
    except Exception as e:
        print(f"injury feed unavailable: {e}", file=sys.stderr)
        return None


def lineup(gid):
    """Starters and inactive players from the NBA box score, once it is posted shortly before tip."""
    try:
        game = json.loads(get_nba(NBA_BOX.format(gid=gid)))["game"]
    except Exception:
        return None
    out = {}
    for side in ("homeTeam", "awayTeam"):
        t = game.get(side) or {}
        ps = t.get("players") or []
        st = [[int(p["personId"]), p.get("nameI") or p.get("name", "")] for p in ps if str(p.get("starter")) == "1"]
        off = [[int(p["personId"]), p.get("nameI") or p.get("name", ""), p.get("notPlayingDescription") or ""]
               for p in ps if p.get("status") == "INACTIVE"]
        if (st or off) and t.get("teamTricode"):
            out[t["teamTricode"]] = {"st": st, "out": off}
    return out or None


def load_github(season):
    frames = []
    for name, po in ((f"cdnnba_{season}", 0), (f"cdnnba_po_{season}", 1)):
        local = f"{name}.csv"
        if not os.path.exists(local):
            try:
                raw = get(GH.format(name=name), timeout=600)
            except Exception as e:  # playoffs file does not exist until the playoffs start
                print(f"skip {name}: {e}", file=sys.stderr)
                continue
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:xz") as t:
                t.extractall(".")
        df = pd.read_csv(local, low_memory=False, usecols=lambda c: c in COLS)
        df["po"] = po
        df["ssn"] = season
        frames.append(df)
    if not frames:
        raise RuntimeError(f"No play-by-play archive found for the {season}-{str(season + 1)[2:]} season")
    return pd.concat(frames, ignore_index=True)


def load_nba(season, cache="pbp_cache"):
    """Returns (play-by-play of finished games or None, games scheduled over the next week)."""
    os.makedirs(cache, exist_ok=True)
    try:
        sched = json.loads(get_nba(NBA_SCHEDULE))["leagueSchedule"]
    except Exception as e:
        raise RuntimeError(f"Could not reach the NBA schedule ({e})")
    yy = str(season)[2:]
    today = datetime.now(ET).date()
    rows, upcoming = [], []
    for day in sched["gameDates"]:
        for g in day["games"]:
            gid = str(g.get("gameId", ""))
            status = g.get("gameStatus")
            if status != 3:
                try:  # not final yet: keep it for the Picks tab if it tips within a week
                    when = pd.to_datetime(g.get("gameDateTimeUTC"), utc=True).tz_convert(ET)
                    ahead = (when.date() - today).days
                    if 0 <= ahead <= 7:
                        upcoming.append({"d": when.strftime("%Y-%m-%d"), "t": when.strftime("%-I:%M %p ET") if os.name != "nt" else when.strftime("%I:%M %p ET").lstrip("0"),
                                         "h": g["homeTeam"]["teamTricode"], "a": g["awayTeam"]["teamTricode"],
                                         "pre": 1 if gid[:3] == "001" else 0, "ts": when.isoformat(), "gid": gid})
                        if status == 2:
                            upcoming[-1]["lv"] = 1
                except Exception:
                    pass
                if status != 2:
                    continue
            # 002 = regular season, 004 = playoffs, 005 = play-in
            if gid[:3] not in ("002", "004", "005") or gid[3:5] != yy:
                continue
            path = os.path.join(cache, f"{gid}.json")
            if status == 2:  # in progress: read what has happened so far, never cache it
                try:
                    actions = json.loads(get_nba(NBA_PBP.format(gid=gid)))["game"]["actions"]
                except Exception:
                    continue
            else:
                if not os.path.exists(path):
                    try:
                        raw = get_nba(NBA_PBP.format(gid=gid))
                    except Exception as e:
                        print(f"skip {gid}: {e}", file=sys.stderr)
                        continue
                    with open(path, "wb") as f:
                        f.write(raw)
                    time.sleep(0.2)
                with open(path) as f:
                    actions = json.load(f)["game"]["actions"]
            for act in actions:
                row = {k: v for k, v in act.items() if k in COLS}
                row["gameId"] = int(gid)
                row["po"] = 0 if gid[:3] == "002" else 1
                row["ssn"] = season
                row["live"] = 1 if status == 2 else 0
                rows.append(row)
    upcoming.sort(key=lambda x: x["ts"])
    now = datetime.now(ET)
    for u in upcoming:
        # lineups are posted roughly half an hour before tip; start looking 90 minutes out
        if (datetime.fromisoformat(u["ts"]) - now).total_seconds() <= 90 * 60:
            lu = lineup(u["gid"])
            if lu:
                u["lu"] = lu
        del u["gid"]
    return (pd.DataFrame(rows) if rows else None), upcoming


# ---------- transform ----------
def clock_s(c):
    # "PT11M56.00S" -> seconds left in the period
    m, s = c[2:-1].split("M")
    return int(m) * 60 + float(s)


def pid(v):
    return int(v) if pd.notna(v) and int(v) > 0 else None


def build(df):
    for col in ("jumpBallWonPersonId", "jumpBallLostPersonId", "jumpBallRecoverdPersonId",
                "shotDistance", "descriptor", "subType", "shotResult", "teamTricode", "playerNameI"):
        if col not in df.columns:
            df[col] = None
    df = df.sort_values(["gameId", "orderNumber"], kind="stable")
    names = (df[df.personId > 0].dropna(subset=["playerNameI"])
             .drop_duplicates("personId", keep="last").set_index("personId").playerNameI.to_dict())
    games = []
    for gid, g in df.groupby("gameId", sort=True):
        teams = [t for t in g.teamTricode.dropna().unique()]
        if len(teams) != 2:
            continue
        q1 = g[g.period == 1]
        if q1.empty:
            continue
        # home team: the side whose scoreHome moves on a made basket
        home = None
        prev_h = 0
        for r in g[g.shotResult == "Made"].itertuples():
            h = int(r.scoreHome)
            home = r.teamTricode if h > prev_h else [t for t in teams if t != r.teamTricode][0]
            break
        if home is None:
            continue
        away = [t for t in teams if t != home][0]
        ts = pd.to_datetime(q1.timeActual.iloc[0], utc=True).tz_convert(ET)
        team_of = (g[g.personId > 0].dropna(subset=["teamTricode"])
                   .drop_duplicates("personId").set_index("personId").teamTricode.to_dict())

        # --- opening tip
        tip = None
        q1 = q1.assign(sec=q1.clock.map(clock_s))
        jb = q1[(q1.actionType == "jumpball") & (q1.sec >= TIP_WINDOW)]
        if len(jb):
            r = jb.iloc[0]
            w, l = pid(r.jumpBallWonPersonId), pid(r.jumpBallLostPersonId)
            pos = r.teamTricode if pd.notna(r.teamTricode) else team_of.get(w)
            wt = team_of.get(w) or pos
            lt = team_of.get(l) or [t for t in teams if t != wt][0]
            if wt == lt:  # a jumper with no other logged action; fall back to possession
                wt, lt = pos, [t for t in teams if t != pos][0]
            tip = {"w": w, "l": l, "wt": wt, "lt": lt, "pos": pos, "rec": pid(r.jumpBallRecoverdPersonId)}
        else:
            v = q1[(q1.actionType == "violation") & (q1.subType == "jumpball") & (q1.sec >= TIP_WINDOW)]
            if len(v):  # jump-ball violation: the other team is awarded the ball
                bad = v.iloc[0].teamTricode
                tip = {"w": None, "l": pid(v.iloc[0].personId), "wt": None, "lt": bad,
                       "pos": [t for t in teams if t != bad][0], "rec": None, "viol": 1}

        # --- starters: first Q1 appearance is not a "SUB in"
        starters = {t: [] for t in teams}
        seen = set()
        for r in q1[q1.personId > 0].itertuples():
            if r.personId in seen or pd.isna(r.teamTricode) or r.personId not in names:  # coaches carry an id but no name
                continue
            seen.add(r.personId)
            if not (r.actionType == "substitution" and r.subType == "in"):
                starters[r.teamTricode].append(int(r.personId))

        # --- first attempts per team, in game order: field goals and free-throw trips.
        # entry: [player, pts (1 = free throws), made, distance, shot type, seconds elapsed,
        #         game order, descriptor, tries in the trip, and-one result]
        att = g[g.actionType.isin(["2pt", "3pt", "freethrow"]) & (g.personId > 0)]
        shots = {t: [] for t in teams}
        nfg = {t: 0 for t in teams}
        order = 0
        first_fg = first_pts = None
        for r in att.itertuples():
            t = r.teamTricode
            if t not in shots:
                continue
            el = int((r.period - 1) * 720 + 720 - clock_s(r.clock))
            made = 1 if r.shotResult == "Made" else 0
            lst = shots[t]
            if r.actionType == "freethrow":
                last = lst[-1] if lst else None
                same = last is not None and last[0] == int(r.personId) and last[5] == el
                if same and last[1] == 1:            # another free throw in the same trip
                    last[2] += made; last[8] += 1
                elif same and last[2] and str(r.subType).strip() == "1 of 1":   # and-one
                    last[9] = made
                elif nfg[t] < N_SHOTS or first_fg is None:
                    order += 1
                    kind = r.descriptor if pd.notna(r.descriptor) else None
                    lst.append([int(r.personId), 1, made, None, "Free Throw", el, order, kind, 1, None])
                if made and first_pts is None:
                    first_pts = [int(r.personId), t]
            else:
                order += 1
                if made and first_fg is None:
                    first_fg = [int(r.personId), t]
                if made and first_pts is None:
                    first_pts = [int(r.personId), t]
                if nfg[t] < N_SHOTS or first_fg is None or (made and first_fg == [int(r.personId), t] and nfg[t] >= N_SHOTS):
                    nfg[t] += 1
                    dist = int(round(r.shotDistance)) if pd.notna(r.shotDistance) else None
                    lst.append([int(r.personId), 3 if r.actionType == "3pt" else 2, made, dist, r.subType,
                                el, order, r.descriptor if pd.notna(r.descriptor) else None, 1, None])
            if first_fg and all(n >= N_SHOTS for n in nfg.values()) and el > max(x[-1][5] for x in shots.values()):
                break
        games.append({"id": int(gid), "d": ts.strftime("%Y-%m-%d"), "po": int(g.po.iloc[0]), "s": int(g.ssn.iloc[0]),
                      "h": home, "a": away, "tip": tip, "fs": shots, "st": starters, "fb": first_fg, "fp": first_pts})
        if "live" in g.columns and g.live.iloc[0] == 1:
            games[-1]["lv"] = 1
    used = set()
    for gm in games:
        t = gm["tip"] or {}
        used.update(p for p in (t.get("w"), t.get("l"), t.get("rec")) if p)
        used.update(p for lst in gm["st"].values() for p in lst)
        used.update(s[0] for lst in gm["fs"].values() for s in lst)
        for k in ("fb", "fp"):
            if gm[k]:
                used.add(gm[k][0])
    return games, {str(p): names.get(p, f"#{p}") for p in sorted(used)}


def current_season():
    now = datetime.now(ET)
    return now.year if now.month >= 10 else now.year - 1


def label(yr):
    return f"{yr}-{str(yr + 1)[2:]}"


FLAGS = "flags.json"
ODDS = "odds.json"
ODDS_API = "https://api.the-odds-api.com/v4/sports/basketball_nba"
TEAM_NAMES = {"Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI",
              "Cleveland Cavaliers": "CLE", "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
              "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND", "Los Angeles Clippers": "LAC",
              "LA Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM", "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
              "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP", "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
              "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
              "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS"}


def parse_first_basket(doc):
    """Best first-basket price per player across books: [[player, american price, book], ...]."""
    best = {}
    for bk in doc.get("bookmakers") or []:
        for mk in bk.get("markets") or []:
            if mk.get("key") != "player_first_basket":
                continue
            for o in mk.get("outcomes") or []:
                who = o.get("description") or o.get("name")
                price = o.get("price")
                if not who or who in ("Yes", "No") or str(o.get("name")) == "No" or not isinstance(price, (int, float)):
                    continue
                if who not in best or price > best[who][1]:
                    best[who] = [who, int(price), bk.get("title") or bk.get("key") or ""]
    return sorted(best.values(), key=lambda x: x[1])


def remember_odds(sched, old):
    """First-basket prices for today's games from The Odds API, kept in odds.json so each game is asked for at most twice.

    Needs the key in the ODDS_API_KEY environment variable; without it this only returns what is already stored.
    The free plan allows 500 requests a month and each game costs one, so a game is fetched once inside four hours
    of tip and refreshed once inside the last hour. Fetching stops when fewer than 15 requests remain.
    """
    store = dict((old or {}).get("odds") or {})
    try:
        with open(ODDS, encoding="utf-8") as f:
            store.update(json.load(f))
    except Exception:
        pass
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        return store
    before = json.dumps(store, sort_keys=True)
    now = datetime.now(ET)
    want = []
    for u in sched:
        if u.get("lv") or not u.get("ts"):
            continue
        mins = (datetime.fromisoformat(u["ts"]) - now).total_seconds() / 60
        k = f"{u['d']}|{u['a']}|{u['h']}"
        rec = store.get(k) or {}
        # empty-handed tries are spaced half an hour apart, so a five-minute refresh does not use them all at once
        waited = not rec.get("tt") or (now - datetime.fromisoformat(rec["tt"]).replace(tzinfo=ET)).total_seconds() >= 30 * 60
        if (not rec.get("p") and 0 < mins <= 240 and rec.get("tries", 0) < 6 and waited) or (rec.get("p") and rec.get("n", 1) < 2 and 0 < mins <= 60):
            want.append((k, u))
    if want:
        try:
            events = json.loads(get(f"{ODDS_API}/events?apiKey={key}"))   # listing events is free
            ids = {(TEAM_NAMES.get(e.get("away_team")), TEAM_NAMES.get(e.get("home_team"))): e["id"] for e in events}
            for k, u in want:
                eid = ids.get((u["a"], u["h"]))
                if not eid:
                    continue
                req = urllib.request.Request(f"{ODDS_API}/events/{eid}/odds?apiKey={key}&regions=us&markets=player_first_basket&oddsFormat=american", headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=60, context=CTX or ssl_context()) as r:
                    left = r.headers.get("x-requests-remaining")
                    prices = parse_first_basket(json.loads(r.read()))
                rec = store.get(k) or {}
                if prices:
                    rec.update({"p": prices, "n": rec.get("n", 0) + 1, "at": now.strftime("%Y-%m-%dT%H:%M")})
                else:
                    rec["tries"] = rec.get("tries", 0) + 1
                    rec["tt"] = now.strftime("%Y-%m-%dT%H:%M")
                store[k] = rec
                if left is not None and float(left) < 15:
                    print("odds: monthly request allowance nearly used; stopping", file=sys.stderr)
                    break
        except Exception as e:
            print(f"odds unavailable: {str(e).replace(key, '***')}", file=sys.stderr)
    if json.dumps(store, sort_keys=True) != before:
        with open(ODDS, "w", encoding="utf-8") as f:
            json.dump(store, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return store


def remember_flags(sched, inj, old):
    """Keep what was flagged before each of today's games, so it can still be shown after they are played.

    One entry per game, keyed "date|away|home": the injury designations for both teams and the posted
    lineups, as last seen before tip. Stored in flags.json next to the script and inside the page data.
    """
    flags = dict((old or {}).get("flags") or {})
    try:
        with open(FLAGS, encoding="utf-8") as f:
            flags.update(json.load(f))
    except Exception:
        pass
    before = json.dumps(flags, sort_keys=True)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    for u in sched:
        if u["d"] != today:
            continue
        key = f"{u['d']}|{u['a']}|{u['h']}"
        rec = flags.get(key, {})
        if inj is not None and not (u.get("lv") and rec.get("inj") is not None):  # freeze designations at tip
            rec["inj"] = {t: [[x["n"], x["st"], x["ty"]] for x in inj.get(t, [])] for t in (u["a"], u["h"]) if inj.get(t)}
        if u.get("lu"):
            rec["lu"] = u["lu"]
        if rec:
            flags[key] = rec
    if json.dumps(flags, sort_keys=True) != before:
        with open(FLAGS, "w", encoding="utf-8") as f:
            json.dump(flags, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return flags


def embedded(path):
    """Data already inside the dashboard file, or None."""
    try:
        page = open(path, encoding="utf-8").read()
        m = re.search(r'<script id="tipoff-data" type="application/json">(.*?)</script>', page, re.S)
        return json.loads(m.group(1))
    except Exception:
        return None


def make(source, season, prev=False, html=None):
    """Build the dashboard data.

    github: one season from the archive (plus the one before it with prev=True).
    nba:    the season in progress from the NBA API, the upcoming week's schedule, and the
            previous season from the archive so early-season numbers have history behind them.
    """
    yr = season or current_season()
    frames, sched, notes = [], [], []
    old_games, old_players, old = [], {}, None
    if source == "nba":
        cur, sched = load_nba(yr)
        if cur is not None:
            frames.append(cur)
        else:
            notes.append(f"No finished {label(yr)} games yet")
        # earlier seasons: reuse what the dashboard file already holds, else fetch the archive
        old = embedded(html) if html else None
        if old:
            old_games = [g for g in old.get("games", []) if g.get("s", yr - 1) < yr]
            old_players = old.get("players", {})
        if not old_games:
            for back in (1, 2):
                try:
                    frames.insert(0, load_github(yr - back))
                except RuntimeError as e:
                    notes.append(str(e))
    else:
        tries = [yr] if season else [yr, yr - 1]
        for y in tries:
            try:
                frames.append(load_github(y))
                yr = y
                break
            except RuntimeError as e:
                notes.append(str(e))
        if prev and frames:
            try:
                frames.insert(0, load_github(yr - 1))
            except RuntimeError as e:
                notes.append(str(e))
    if not frames and not old_games:
        raise RuntimeError("; ".join(notes) or "No play-by-play found")
    games, players = build(pd.concat(frames, ignore_index=True)) if frames else ([], {})
    games = old_games + games
    players = {**old_players, **players}
    games.sort(key=lambda x: (x["d"], x["id"]))
    seasons = sorted({g["s"] for g in games})
    out = {"season": " + ".join(label(y) for y in seasons), "seasons": seasons, "source": source,
           "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "through": games[-1]["d"], "nShots": N_SHOTS, "sched": sched, "players": players, "games": games}
    if source == "nba":
        inj = load_injuries()
        if inj is not None:
            out["inj"] = inj
        out["flags"] = remember_flags(sched, inj, old)
        out["odds"] = remember_odds(sched, old)
    if notes:
        out["note"] = "; ".join(notes)
    return out


def write_html(path, out):
    page = open(path, encoding="utf-8").read()
    blob = json.dumps(out, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    pat = re.compile(r'(<script id="tipoff-data" type="application/json">).*?(</script>)', re.S)
    if not pat.search(page):
        raise RuntimeError(f"{path} has no tipoff-data block to refresh")
    with open(path, "w", encoding="utf-8") as f:
        f.write(pat.sub(lambda m: m.group(1) + blob + m.group(2), page, count=1))


def serve(a):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    html = a.html or "tipoff_ledger.html"
    if not os.path.exists(html):
        sys.exit(f"Put {html} in the same folder as this script, then run it again.")
    lock = threading.Lock()

    def refresh():
        out = make(a.source, a.season, a.prev, html)
        write_html(html, out)
        print(f"{datetime.now(ET):%b %d %H:%M} refreshed: {len(out['games'])} games through {out['through']}, "
              f"{len(out['sched'])} scheduled", flush=True)
        return out

    def keep_fresh():  # on start, then every few hours, with nobody pressing anything
        while True:
            with lock:
                try:
                    refresh()
                except Exception as e:
                    print(f"{datetime.now(ET):%b %d %H:%M} refresh failed: {e}", file=sys.stderr, flush=True)
            time.sleep(max(a.every, 0.25) * 3600)

    class H(BaseHTTPRequestHandler):
        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0].split("#")[0] in ("/", "/index.html"):
                self.send(200, open(html, "rb").read(), "text/html; charset=utf-8")
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/refresh":
                return self.send(404, b"not found", "text/plain")
            if not lock.acquire(blocking=False):
                return self.send(200, json.dumps({"error": "A refresh is already running"}).encode(), "application/json")
            try:
                out = refresh()
                body = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode()
            except Exception as e:  # keep the page usable with the data it already has
                print(f"refresh failed: {e}", file=sys.stderr)
                body = json.dumps({"error": str(e)}).encode()
            finally:
                lock.release()
            self.send(200, body, "application/json")

        def log_message(self, *args):
            pass

    url = f"http://localhost:{a.port}/"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"Tip-Off Ledger is at {url}  (Ctrl+C to stop)", flush=True)
    if a.every:
        threading.Thread(target=keep_fresh, daemon=True).start()
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


HOME_DIR = os.path.join(os.path.expanduser("~"), "TipOffLedger")
PLIST = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.tipoffledger.plist")
WIN_START = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "TipOffLedger.bat")


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def install(a):
    """Copy the dashboard to ~/TipOffLedger and have it start at login and refresh itself."""
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(HOME_DIR, exist_ok=True)
    for name in (os.path.basename(__file__), "tipoff_ledger.html"):
        src, dst = os.path.join(here, name), os.path.join(HOME_DIR, name)
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
    script = os.path.join(HOME_DIR, os.path.basename(__file__))
    if not os.path.exists(os.path.join(HOME_DIR, "tipoff_ledger.html")):
        sys.exit("tipoff_ledger.html needs to be in the same folder as this script. Put it there and run this again.")
    try:
        import certifi  # noqa: F401
        need = [] if pd is not None else ["pandas"]
    except ImportError:
        need = ["certifi"] + ([] if pd is not None else ["pandas"])
    if need:
        print(f"Installing {' and '.join(need)} (one time)...")
        base = [sys.executable, "-m", "pip", "install", "--user", "--quiet"] + need
        if subprocess.call(base) != 0 and subprocess.call(base + ["--break-system-packages"]) != 0:
            sys.exit(f"Could not install {' '.join(need)}. Run:  python3 -m pip install {' '.join(need)}   then run this again.")
    url = f"http://localhost:{a.port}/"
    cmd = [sys.executable, script, "--serve", "--no-open", "--port", str(a.port)]
    if sys.platform == "darwin":
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        args = "".join(f"<string>{c}</string>" for c in cmd)
        log = os.path.join(HOME_DIR, "ledger.log")
        with open(PLIST, "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                    f'<key>Label</key><string>com.tipoffledger</string><key>ProgramArguments</key><array>{args}</array>'
                    '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>'
                    f'<key>WorkingDirectory</key><string>{HOME_DIR}</string>'
                    f'<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>'
                    '</dict></plist>\n')
        subprocess.call(["launchctl", "unload", PLIST], stderr=subprocess.DEVNULL)
        subprocess.call(["launchctl", "load", "-w", PLIST])
    elif os.name == "nt":
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        pyw = pyw if os.path.exists(pyw) else sys.executable
        with open(WIN_START, "w") as f:
            f.write(f'@echo off\nstart "" "{pyw}" "{script}" --serve --no-open --port {a.port}\n')
        subprocess.Popen([pyw] + cmd[1:], cwd=HOME_DIR, creationflags=0x00000008)
    else:
        subprocess.Popen(cmd, cwd=HOME_DIR, start_new_session=True,
                         stdout=open(os.path.join(HOME_DIR, "ledger.log"), "a"), stderr=subprocess.STDOUT)
        print("Started for this session. Add this to your login items to keep it:\n  " + " ".join(cmd))
    for _ in range(40):
        if port_open(a.port):
            break
        time.sleep(0.5)
    else:
        sys.exit(f"Set up, but the dashboard did not start. See {os.path.join(HOME_DIR, 'ledger.log')}")
    print(f"\nDone. Tip-Off Ledger is at {url}\nBookmark that address. It starts when you log in and refreshes itself every two hours.\n"
          f"The first refresh is running now; reload the page in a minute or two.")
    if not a.no_open:
        webbrowser.open(url)


def uninstall(a):
    if sys.platform == "darwin" and os.path.exists(PLIST):
        subprocess.call(["launchctl", "unload", PLIST], stderr=subprocess.DEVNULL)
        os.remove(PLIST)
    if os.name == "nt" and os.path.exists(WIN_START):
        os.remove(WIN_START)
    print(f"Removed the login item. Delete {HOME_DIR} to remove the files. If the page still opens, restart your computer.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["github", "nba"])
    ap.add_argument("--season", type=int)
    ap.add_argument("--out", default="data.json")
    ap.add_argument("--html", help="standalone dashboard file to refresh in place")
    ap.add_argument("--prev", action="store_true", help="github source: also include the season before")
    ap.add_argument("--serve", action="store_true", help="open the dashboard with a working Refresh button")
    ap.add_argument("--install", action="store_true", help="one-time setup: start at login and refresh by itself")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--every", type=float, default=2, help="hours between automatic refreshes while serving (0 = off)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    if a.install:
        return install(a)
    if a.uninstall:
        return uninstall(a)
    if pd is None:
        sys.exit("pandas is missing. Run:  python3 build_data.py --install")
    if a.serve:
        a.source = a.source or "nba"
        return serve(a)
    a.source = a.source or "github"
    try:
        out = make(a.source, a.season, a.prev, a.html)
        with open(a.out, "w") as f:
            json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
        if a.html:
            write_html(a.html, out)
            print(f"refreshed {a.html}")
    except RuntimeError as e:
        sys.exit(str(e))
    tips = sum(1 for g in out["games"] if g["tip"])
    print(f"{len(out['games'])} games, {tips} with an opening tip, {len(out['players'])} players -> {a.out} "
          f"({os.path.getsize(a.out) / 1024:.0f} KB), through {out['through']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env node
/* Locks each night's threes picks, ladders and game numbers.

   Run after build_data.py:   node lock_picks.js [tipoff_ledger.html] [picks.json]

   It runs the page's own model (the script inside the page) on the data embedded in it, works out tonight's Best plays
   and Ladder watch, for threes, rebounds and assists, exactly as the page would, and keeps them in picks.json. Everything about a game is frozen at the
   last run before it tips off; until then it can still change on each run. A pick that drops off the list before its
   game starts is remembered with its first and last price. The store is written back into the page's data as "locks",
   which is what the page and its Tracker read.

   It also keeps the Movers log (movers.json): every run compares each player in a game still to start with the run
   before, in every market, and records what moved and why. The last snapshot and three days of log are kept; the log is
   written into the page's data as "movers". */
const fs = require("fs");
const [html = "tipoff_ledger.html", storePath = "picks.json", moversPath = "movers.json"] = process.argv.slice(2);

const page = fs.readFileSync(html, "utf8");
const TAG = '<script id="tipoff-data" type="application/json">';
const i0 = page.indexOf(TAG), i1 = page.indexOf("</script>", i0);
if (i0 < 0 || i1 < 0) { console.error("lock_picks: no data block in " + html); process.exit(0); }
const raw = page.slice(i0 + TAG.length, i1);
const scripts = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const app = scripts.sort((a, b) => b.length - a.length)[0];

/* a stand-in for the browser: enough for the page's script to start and expose its model */
const els = {};
const el = () => new Proxy({ hidden: false, textContent: "", innerHTML: "", value: "", dataset: {}, style: {}, files: [],
  classList: { add() {}, remove() {}, contains: () => false } }, { get: (t, k) => (k in t ? t[k] : () => null), set: (t, k, v) => { t[k] = v; return true; } });
let api = null;
global.window = { __TL_HOOK: a => { api = a; }, scrollTo() {}, scrollY: 0, innerWidth: 1400, innerHeight: 900, addEventListener() {} };
global.document = { getElementById: id => els[id] || (els[id] = el()), addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
  createElement: () => el(), body: el(), activeElement: null, hidden: true };
els["tipoff-data"] = { textContent: raw };
global.location = { protocol: "node:", hostname: "", hash: "", pathname: "" };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.history = { replaceState() {} };
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0;
global.setTimeout = () => 0;
try { (0, eval)(app); } catch (e) { console.error("lock_picks: the page script failed to start: " + e.message); process.exit(0); }
if (!api || !api.data()) { console.error("lock_picks: the page did not expose its model; nothing locked"); process.exit(0); }

const D = api.data(), S = api.S;
S.book = ""; S.lockOff = 1;                       // work from best-of-all-books prices, ignoring earlier locks
let store = {};
try { store = JSON.parse(fs.readFileSync(storePath, "utf8")); } catch (_) {}

const now = process.env.LOCK_NOW ? Date.parse(process.env.LOCK_NOW) : Date.now();
if (process.env.LOCK_NOW) { const RD = Date; global.Date = class extends RD { constructor(...a) { super(...(a.length ? a : [now])); } static now() { return now; } }; }   // testing: pretend it is another time
const day = process.env.LOCK_DAY || api.todayET();
let ms = [];
const started = m => api.t3Started(m);
const stamp = new Date(now).toISOString(), prev = store[day] || {}, rec = Object.assign({}, prev);

/* Threes and rebounds lock the same way, each under its own keys in the night's record. */
function lockStat(sk, P, R, OP, main = true) {
  const C = api.STAT[sk], T = api.t3Day(day);
  ms = T.ms;
  if (!ms.length) return;
  const gameOf = k => ms.find(m => m.a === k.a && m.h === k.h);
  const gone = k => { const m = gameOf(k); return !m || started(m); };

  /* Best plays. A pick whose game has started is kept exactly as saved and holds its place for good; the remaining
     places go to the best plays from games still to start; a player whose game has started can no longer become one. */
  const kept = (P[C.lk] || []).filter(gone).map(k => Object.assign(k, { lk: 1 }));
  const taken = new Set(kept.map(k => k.p));
  const open = (OP[sk] || []).filter(x => !taken.has(x.row.p)).map(api.t3PickOut);   // this stat's open places (worked out below)
  const picks = kept.concat(open).sort((a, b) => b.vs - a.vs);

  /* Every pick as it was first listed, and the last price on that same bet before its game started. */
  const seen = P[C.seen] || {};
  picks.forEach(k => {
    const id = k.p + "|" + k.lab;
    if (!seen[id]) seen[id] = { p: k.p, n: k.n, t: k.t, o: k.o, a: k.a, h: k.h, ts: k.ts, lab: k.lab, k: k.k, over: k.over, price0: k.price, book0: k.book, at0: stamp };
    if (!k.lk) seen[id].last = stamp;
  });
  Object.values(seen).forEach(e => {
    const m = gameOf(e);
    if (!m || started(m)) return;
    const row = T.rows.find(r => r.p === e.p && r.m === m), c = row && (row.cand || []).find(x => x.lab === e.lab);
    e.close = c ? c.price : null; e.closeBook = c ? c.book : null;
  });

  /* Ladder watch: each ladder's rungs, prices and stake split as of the last run before his game starts. */
  const lw = (P[C.lw] || []).filter(gone);
  const lwGames = lw.map(e => e.a + "@" + e.h), lwOpen = api.t3LadderList(T.rows.filter(r => !started(r.m))).filter(r => r.lad.priced);
  (api.ladFill ? api.ladFill(lwOpen, Math.max(0, T.cap - lw.length), lwGames) : lwOpen.slice(0, Math.max(0, 8 - lw.length)))   // one for every two games, at most five, two a game
    .forEach(r => lw.push(Object.assign(api.t3LadOut(r), { at: stamp })));
  /* every ladder as first listed, so the page can show the ones that fell off before tip */
  const lwSeen = P[C.lw + "seen"] || {};
  lw.forEach(e => { const id = e.p + "|" + e.a + "|" + e.h; if (!lwSeen[id]) lwSeen[id] = Object.assign({}, e, { at0: stamp }); });

  R[C.lk] = picks; R[C.seen] = seen; R[C.lw] = lw; R[C.lw + "seen"] = lwSeen;
  if (!main) return;   // the per-book records share the main record's game numbers
  /* Game numbers: every player's projection as of the last run before his game starts. Games still to start are
     rewritten each run; a started game keeps what it had, so the slate does not move while a game is on. */
  const fz = Object.assign({}, P[C.fz] || {});
  ms.filter(m => !started(m)).forEach(m => {
    fz[day + "|" + m.a + "|" + m.h] = { at: stamp, r: T.rows.filter(r => r.m === m && (r.pr.att >= 1 || r.o)).map(api.t3Freeze) };
  });
  R[C.fz] = fz;
}
/* Best plays: one for every two games and at most five a night in each stat. The picks already locked hold their places;
   the rest go to the best value from games still to start. */
function openOf(P) {
  const kept = [];
  ["t3", "rb", "as"].forEach(sk => api.inSK(sk, () => {
    const C = api.STAT[sk], T = api.t3Day(day, 1);
    (P[C.lk] || []).filter(k => { const m = T.ms.find(x => x.a === k.a && x.h === k.h); return !m || started(m); }).forEach(k => kept.push({ sk, p: k.p, a: k.a, h: k.h }));
  }));
  return api.propOpen ? api.propOpen(day, kept, true) : {};
}
/* picks already listed before this run: they stay listed while they hold 2 points above break-even (new ones need 3) */
const stickyFrom = P => { const o = {}; ["t3", "rb", "as"].forEach(sk => { const C = api.STAT[sk], m = o[sk] = {}; (P[C.lk] || []).forEach(k => { m[k.p + "|" + k.lab] = 1; }); }); return o; };
S._sticky = stickyFrom(prev);
const OPEN = openOf(prev);
["t3", "rb", "as"].forEach(sk => api.inSK(sk, () => lockStat(sk, prev, rec, OPEN)));
/* The same Best plays and Ladder watch worked out at each single book and locked the same way, so choosing a book on the
   page shows picks you can place there. The Tracker and the main picks stay on the best price across all books. */
if (api.booksOn) {
  const kb = S.book, ko = S.bookOff, out = {};
  try {
    api.booksOn(day).forEach(bk => {
      S.book = bk; S.bookOff = 0;
      const P = (prev.bk || {})[bk] || {}, R = {};
      S._sticky = stickyFrom(P);
      const OP = openOf(P);
      ["t3", "rb", "as"].forEach(sk => api.inSK(sk, () => lockStat(sk, P, R, OP, false)));
      if (["t3", "rb", "as", "lw", "rlw", "alw", "seen", "rseen", "aseen"].some(k => Object.keys(R[k] || {}).length)) out[bk] = R;
    });
    rec.bk = out;
  } catch (e) { console.error("per-book picks: " + e.message); }
  finally { S.book = kb; S.bookOff = ko; S._sticky = stickyFrom(prev); }
}
/* First team basket picks lock the same way: kept once their game starts, the rest refilled from games still to start. */
(function lockFtb() {
  const fms = api.ftMs(day);
  if (!fms.length) return;
  const rows = api.ftDay(day), gameOf = k => fms.find(m => m.a === k.a && m.h === k.h), gone = k => { const m = gameOf(k); return !m || started(m); };
  const kept = (prev.ft || []).filter(gone).map(k => Object.assign(k, { lk: 1 }));
  const held = kept.map(k => k.a + "@" + k.h + "|" + k.t), taken = new Set(kept.map(k => k.p));
  const open = api.ftFill(rows.filter(r => !started(r.m) && !taken.has(r.p)), 4 - kept.length, held).map(api.ftOut);
  const picks = kept.concat(open).sort((a, b) => b.vs - a.vs), seen = prev.ftseen || {};
  picks.forEach(k => { if (!seen[k.p]) seen[k.p] = { p: k.p, n: k.n, t: k.t, a: k.a, h: k.h, ts: k.ts, price0: k.price, book0: k.book, at0: stamp }; if (!k.lk) seen[k.p].last = stamp; });
  Object.values(seen).forEach(e => { const m = gameOf(e); if (!m || started(m)) return; const r = rows.find(q => q.p === e.p && q.m.a === e.a && q.m.h === e.h); e.close = r && r.price != null ? r.price : null; });
  rec.ft = picks; rec.ftseen = seen;
  if (!ms.length) ms = fms;
})();
/* Method picks: three player-and-method prices a night, kept once their game starts, the rest refilled from games still to start. */
(function lockMf() {
  if (!api.mfRows) return;
  try {
    const fms = api.ftMs(day);
    if (!fms.length) return;
    const rows = api.mfRows(day), gameOf = k => fms.find(m => m.a === k.a && m.h === k.h), gone = k => { const m = gameOf(k); return !m || started(m); };
    const kept = (prev.mf || []).filter(gone).map(k => Object.assign(k, { lk: 1 }));
    const held = kept.map(k => k.a + "@" + k.h), taken = new Set(kept.map(k => k.p));
    const open = api.mfFill(rows.filter(r => !started(r.m) && !taken.has(r.p)), 3 - kept.length, held).map(api.mfOut);
    const picks = kept.concat(open).sort((a, b) => b.vs - a.vs), seen = prev.mfseen || {};
    picks.forEach(k => { const id = k.p + "|" + k.mt; if (!seen[id]) seen[id] = { p: k.p, n: k.n, t: k.t, a: k.a, h: k.h, ts: k.ts, mt: k.mt, price0: k.price, book0: k.book, at0: stamp }; if (!k.lk) seen[id].last = stamp; });
    Object.values(seen).forEach(e => { const m = gameOf(e); if (!m || started(m)) return; const r = rows.find(q => q.p === e.p && q.mt === e.mt && q.m.a === e.a && q.m.h === e.h); e.close = r ? r.price : null; });
    rec.mf = picks; rec.mfseen = seen;
  } catch (e) { console.error("method picks: " + e.message); }
})();
/* Parlay ideas: an idea is kept as saved once its first game starts; the rest are worked out again from games still to start. */
(function lockPz() {
  if (!api.pzPick) return;
  try {
    const kept = (prev.pz || []).filter(e => e.ts && Date.parse(e.ts) <= now).map(e => Object.assign(e, { lk: 1 }));
    rec.pz = kept.concat(api.pzPick(day, kept, true));
    if (api.pzMore) {   // More combinations: kept once their first game starts, the rest refilled from games still to start, a dozen in all
      const key = z => z.legs.map(l => l.p + ":" + l.m + ":" + l.k).sort().join(";");
      const keptM = (prev.pzm || []).filter(e => e.ts && Date.parse(e.ts) <= now).map(e => Object.assign(e, { lk: 1 })), have = new Set(keptM.map(key));
      const fresh = api.pzMore(day, rec.pz, true).filter(z => !have.has(key(z)));
      rec.pzm = keptM.concat(fresh.slice(0, Math.max(0, 12 - keptM.length)));
    }
    /* The same ideas worked out at each single book, so choosing a book on the page shows parlays you can place there,
       locked the same way. The Tracker's record stays on the best-price ideas above. */
    if (api.booksOn && api.S) {
      const S = api.S, kb = S.book, ko = S.bookOff, lockedAt = e => e.ts && Date.parse(e.ts) <= now;
      const key = z => z.legs.map(l => l.p + ":" + l.m + ":" + l.k).sort().join(";");
      rec.pzb = {}; rec.pzmb = {};
      try {
        api.booksOn(day).forEach(bk => {
          S.book = bk; S.bookOff = 0;
          const kept = ((prev.pzb || {})[bk] || []).filter(lockedAt).map(e => Object.assign(e, { lk: 1 }));
          const ideas = kept.concat(api.pzPick(day, kept, true));
          if (ideas.length) rec.pzb[bk] = ideas;
          if (api.pzMore) {
            const keptM = ((prev.pzmb || {})[bk] || []).filter(lockedAt).map(e => Object.assign(e, { lk: 1 })), have = new Set(keptM.map(key));
            const more = keptM.concat(api.pzMore(day, ideas, true).filter(z => !have.has(key(z))).slice(0, Math.max(0, 12 - keptM.length)));
            if (more.length) rec.pzmb[bk] = more;
          }
        });
      } finally { S.book = kb; S.bookOff = ko; }
    }
  } catch (e) { console.error("parlay ideas: " + e.message); }
})();
if (ms.length) {
  const any = ms.some(started), all = ms.every(started);
  rec.at = (prev.at && all) ? prev.at : stamp; rec.locked = any ? 1 : 0; rec.done = all ? 1 : 0;
  store[day] = rec;
}
/* frozen game numbers are only needed for a little while; the picks and ladders themselves are kept for the season */
Object.keys(store).forEach(k => { if (store[k] && Math.round((Date.parse(day) - Date.parse(k)) / 864e5) > 10) { delete store[k].fz; delete store[k].rfz; delete store[k].afz; } });
store = Object.fromEntries(Object.keys(store).sort().slice(-300).map(k => [k, store[k]]));
fs.writeFileSync(storePath, JSON.stringify(store));

/* Movers: what changed since the last run, for games still to start. */
let movers = {};
try { movers = JSON.parse(fs.readFileSync(moversPath, "utf8")); } catch (_) {}
try {
  movers = api.mvRecord(movers, stamp, process.env.LOCK_DAY || undefined);
  fs.writeFileSync(moversPath, JSON.stringify(movers));
} catch (e) { console.error("movers: " + e.message); }

const doc = JSON.parse(raw);
doc.locks = store;
doc.movers = movers.log || {};
fs.writeFileSync(html, page.slice(0, i0 + TAG.length) + JSON.stringify(doc).replace(/<\//g, "<\\/") + page.slice(i1));

const t = store[day], show = l => (l || []).map(x => x.n + " " + x.lab + " " + (x.price > 0 ? "+" : "") + x.price + (x.lk ? " [locked]" : "")).join(", ");
const nMv = ((movers.log || {})[day] || []).length;
console.log(`lock_picks: ${day} · movers logged today ${nMv} · ${t ? (t.done ? "all locked" : t.locked ? "partly locked" : "open") + "; threes picks: " + show(t.t3) + "; rebound picks: " + show(t.rb) + "; assist picks: " + show(t.as) + "; first team basket: " + (t.ft || []).map(x => x.n + " " + (x.price > 0 ? "+" : "") + x.price + (x.lk ? " [locked]" : "")).join(", ") +
  "; method picks: " + (t.mf || []).map(x => x.n + " " + x.mt + " " + (x.price > 0 ? "+" : "") + x.price + (x.lk ? " [locked]" : "")).join(", ") + "; parlay ideas: " + (t.pz || []).map(z => z.legs.length + " legs " + (z.price > 0 ? "+" : "") + z.price + " " + z.book + (z.lk ? " [locked]" : "")).join(", ") + "; ladders " + (t.lw || []).length + " threes, " + (t.rlw || []).length + " rebounds, " + (t.alw || []).length + " assists" : "no games"}`);

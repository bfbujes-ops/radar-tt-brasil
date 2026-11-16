#!/usr/bin/env python3
"""
Radar TT Brasil — coleta, histórico e busca de trending topics do X (Brasil).

Subcomandos:
  collect   coleta a lista de tendências da hora atual e grava no banco
  backfill  importa o histórico por hora de um período passado (getdaytrends)
  enrich    busca o "porquê" dos episódios novos (posts do X + resumo por IA)
  build-db  reconstrói o SQLite a partir dos CSVs diários (data/snapshots)
  search    pesquisa um termo no histórico (janela padrão de 180 dias)
  export    gera data/index.json para a página de busca (web/index.html)

Fontes de tendências (variável RADAR_SOURCE ou --source):
  x_api          API oficial do X (precisa de X_BEARER_TOKEN; ~US$0,01 por chamada)
  twitterapi_io  API de terceiros (precisa de TWITTERAPI_IO_KEY; bem mais barata)
  getdaytrends   página pública getdaytrends.com (sem chave; também serve para o backfill)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RADAR_DB", ROOT / "data" / "trends.db"))
# Fonte da verdade do histórico: um CSV por dia (só cresce, diffs pequenos no git)
# + um JSONL de contextos. O SQLite é reconstruído a partir deles (build-db).
SNAP_DIR = Path(os.environ.get("RADAR_SNAP_DIR", ROOT / "data" / "snapshots"))
CTX_FILE = Path(os.environ.get("RADAR_CTX_FILE", ROOT / "data" / "contexts.jsonl"))
CSV_FIELDS = ["ts", "rank", "term", "tweet_count", "source"]
WOEID_BRASIL = 23424768
EPISODE_GAP_HOURS = 3          # mais de 3h fora da lista = novo episódio
MAX_RANK = 50
USER_AGENT = "RadarTT-BR/0.1 (+pesquisa pessoal de tendencias)"

# ---------------------------------------------------------------- utilidades

def normalize(term: str) -> str:
    """Minúsculas, sem acento, sem # e sem espaços extras (para busca)."""
    t = unicodedata.normalize("NFKD", term)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("#", " ").replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


def hour_bucket(ts: dt.datetime | None = None) -> str:
    ts = ts or dt.datetime.now(dt.timezone.utc)
    ts = ts.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return ts.strftime("%Y-%m-%dT%H:00Z")


def parse_hour(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:00Z").replace(tzinfo=dt.timezone.utc)


def points(rank: int) -> int:
    """Pontos de atenção de uma aparição: 1º lugar = 50, 50º lugar = 1."""
    return max(1, MAX_RANK + 1 - int(rank))


# ---------------------------------------------------------------- banco

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts          TEXT NOT NULL,      -- hora UTC, 'YYYY-MM-DDTHH:00Z'
    norm        TEXT NOT NULL,      -- termo normalizado (chave de busca)
    term        TEXT NOT NULL,      -- termo como apareceu
    rank        INTEGER NOT NULL,   -- melhor posição naquela hora (1-50)
    tweet_count INTEGER,            -- volume de posts, quando a fonte informa
    source      TEXT NOT NULL,
    PRIMARY KEY (ts, norm)
);
CREATE INDEX IF NOT EXISTS idx_snap_norm ON snapshots(norm);
CREATE INDEX IF NOT EXISTS idx_snap_ts   ON snapshots(ts);

CREATE TABLE IF NOT EXISTS contexts (
    norm          TEXT NOT NULL,
    episode_start TEXT NOT NULL,    -- hora em que o episódio começou
    summary       TEXT,             -- frase "Porque ..." gerada/escrita
    posts         TEXT,             -- JSON com os posts de origem
    created_at    TEXT NOT NULL,
    PRIMARY KEY (norm, episode_start)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def save_snapshot(con, ts: str, trends: list[dict], source: str) -> int:
    """trends: [{'term','rank','tweet_count'}]. Se a hora já tem o termo, fica a melhor posição."""
    n = 0
    for t in trends:
        term = (t.get("term") or "").strip()
        if not term:
            continue
        con.execute(
            """INSERT INTO snapshots(ts, norm, term, rank, tweet_count, source)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(ts, norm) DO UPDATE SET
                 rank = MIN(rank, excluded.rank),
                 tweet_count = MAX(COALESCE(tweet_count,0), COALESCE(excluded.tweet_count,0)),
                 term = CASE WHEN excluded.rank < rank THEN excluded.term ELSE term END""",
            (ts, normalize(term), term, int(t["rank"]), t.get("tweet_count"), source),
        )
        n += 1
    con.commit()
    return n


def csv_hours() -> set[str]:
    """Horas que já existem nos CSVs (para o backfill não repetir)."""
    import csv
    have = set()
    for f in sorted(SNAP_DIR.glob("*.csv")):
        with f.open(encoding="utf-8") as fh:
            have.update(row["ts"] for row in csv.DictReader(fh))
    return have


def append_csv(ts: str, trends: list[dict], source: str) -> None:
    import csv
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    f = SNAP_DIR / f"{ts[:10]}.csv"
    new = not f.exists()
    with f.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        for t in trends:
            if t.get("term"):
                w.writerow({"ts": ts, "rank": t["rank"], "term": t["term"].strip(),
                            "tweet_count": t.get("tweet_count") or "", "source": source})


def append_context(norm: str, episode_start: str, summary, posts) -> None:
    CTX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CTX_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"norm": norm, "episode_start": episode_start, "summary": summary,
                             "posts": posts, "created_at": hour_bucket()}, ensure_ascii=False) + "\n")


def build_db(path: Path = DB_PATH) -> sqlite3.Connection:
    """Reconstrói o SQLite a partir dos CSVs e do JSONL (leva poucos segundos)."""
    import csv
    if path.exists():
        path.unlink()
    con = connect(path)
    for f in sorted(SNAP_DIR.glob("*.csv")):
        with f.open(encoding="utf-8") as fh:
            by_hour = defaultdict(list)
            for row in csv.DictReader(fh):
                by_hour[(row["ts"], row["source"])].append({
                    "term": row["term"], "rank": int(row["rank"]),
                    "tweet_count": int(row["tweet_count"]) if row["tweet_count"] else None})
            for (ts, src), trends in by_hour.items():
                save_snapshot(con, ts, trends, src)
    if CTX_FILE.exists():
        for line in CTX_FILE.read_text(encoding="utf-8").splitlines():
            c = json.loads(line)
            con.execute("INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)",
                        (c["norm"], c["episode_start"], c["summary"],
                         json.dumps(c["posts"], ensure_ascii=False), c["created_at"]))
        con.commit()
    return con


# ---------------------------------------------------------------- fontes

def _http_get(url, headers=None, params=None, timeout=30):
    import requests
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    r = requests.get(url, headers=h, params=params, timeout=timeout)
    r.raise_for_status()
    return r


def fetch_x_api(woeid=WOEID_BRASIL) -> list[dict]:
    token = os.environ["X_BEARER_TOKEN"]
    r = _http_get(
        f"https://api.x.com/2/trends/by/woeid/{woeid}",
        headers={"Authorization": f"Bearer {token}"},
        params={"max_trends": MAX_RANK},
    )
    data = r.json().get("data", [])
    return [
        {"term": d.get("trend_name"), "rank": i + 1, "tweet_count": d.get("tweet_count")}
        for i, d in enumerate(data[:MAX_RANK])
    ]


def fetch_twitterapi_io(woeid=WOEID_BRASIL) -> list[dict]:
    key = os.environ["TWITTERAPI_IO_KEY"]
    r = _http_get(
        "https://api.twitterapi.io/twitter/trends",
        headers={"X-API-Key": key},
        params={"woeid": woeid, "count": MAX_RANK},
    )
    out = []
    for i, item in enumerate(r.json().get("trends", [])[:MAX_RANK]):
        t = item.get("trend", item)
        out.append({
            "term": t.get("name"),
            "rank": t.get("rank") or i + 1,
            "tweet_count": t.get("tweet_count") or t.get("tweet_volume"),
        })
    return out


_TREND_HREF = re.compile(r"^/(?:[a-z]{2}/)?brazil/trend/([^/]+)/?$")
_COUNT = re.compile(r"([\d.,]+)\s*([KkMm])?\s*(?:tweets|posts)", re.I)


def parse_count(raw: str, suffix: str | None) -> int:
    """'136.9', 'K' -> 136900 · '1,234', None -> 1234."""
    if suffix:
        return int(float(raw.replace(",", ".")) * {"k": 1e3, "m": 1e6}[suffix.lower()])
    return int(raw.replace(",", "").replace(".", ""))


def parse_getdaytrends(html: str) -> list[dict]:
    """Extrai a lista principal (top 50 da hora) de uma página do getdaytrends.

    Estrutura verificada no site real (nov/2026): o top 50 vive no container
    `#trends`, dividido em DUAS tabelas (1–15 visível e 16–50 num "ver mais");
    fora dele há outras tabelas de ranking (destaques do dia) que NÃO entram.
    Por isso: prioridade ao container #trends; sem ele, todos os links de
    tendência do documento em ordem, com dedupe e corte em 50.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    def trend_links(node):
        links = []
        for a in node.find_all("a", href=True):
            m = _TREND_HREF.match(a["href"])
            if m:
                links.append(a)
        return links

    region = soup.find(id="trends")
    links = trend_links(region) if region is not None else []
    if len(links) < 10:
        links = trend_links(soup)

    out, seen = [], set()
    for a in links:
        name = a.get_text(" ", strip=True) or unquote(_TREND_HREF.match(a["href"]).group(1))
        key = normalize(name)
        if not key or key in seen:
            continue
        seen.add(key)
        row = a.find_parent("tr") or a.parent
        count = None
        m = _COUNT.search(row.get_text(" ", strip=True)) if row else None
        if m:
            count = parse_count(m.group(1), m.group(2))
        out.append({"term": name, "rank": len(out) + 1, "tweet_count": count})
        if len(out) >= MAX_RANK:
            break
    return out


def _get_retry(url: str, tries=3, backoff=5.0):
    """O arquivo do getdaytrends oscila (respostas 520); tenta de novo com pausa."""
    import requests
    for i in range(tries):
        try:
            return _http_get(url)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (429, 500, 502, 503, 504, 520, 521, 522) and i < tries - 1:
                time.sleep(backoff * (i + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def fetch_getdaytrends(when: dt.datetime | None = None, hours_ago: int | None = None) -> list[dict]:
    """Agora (padrão), `hours_ago` horas atrás (1–23) ou uma data/hora passada."""
    if hours_ago:
        url = f"https://getdaytrends.com/brazil/{int(hours_ago)}/"
    elif when is None:
        url = "https://getdaytrends.com/brazil/"
    else:
        when = when.astimezone(dt.timezone.utc)
        url = f"https://getdaytrends.com/brazil/{when:%Y-%m-%d}/{when.hour}/"
    return parse_getdaytrends(_get_retry(url).text)


SOURCES = {
    "x_api": fetch_x_api,
    "twitterapi_io": fetch_twitterapi_io,
    "getdaytrends": fetch_getdaytrends,
}

# ---------------------------------------------------------------- análise

def episodes_for(rows: list[tuple[str, int]], gap=EPISODE_GAP_HOURS) -> list[dict]:
    """rows: [(ts, rank)] ordenado. Agrupa aparições em episódios."""
    eps = []
    for ts, rank in rows:
        t = parse_hour(ts)
        if eps and (t - parse_hour(eps[-1]["end"])).total_seconds() <= gap * 3600:
            e = eps[-1]
            e["end"] = ts
            e["hours"] += 1
            e["best_rank"] = min(e["best_rank"], rank)
            e["points"] += points(rank)
        else:
            eps.append({"start": ts, "end": ts, "hours": 1, "best_rank": rank, "points": points(rank)})
    return eps


def co_trends(con, hours: list[str], exclude: set[str], top=6, margin_h=12) -> list[str]:
    """Termos que subiram e caíram junto com o termo — pista barata do contexto.

    Assuntos ligados costumam "explodir" juntos. Por isso o peso é a semelhança
    entre os conjuntos de horas (Jaccard) numa janela de ±12h, e não só estar na
    mesma hora: um termo que passou o dia todo na lista não ganha destaque.
    """
    if not hours:
        return []
    own = set(hours)
    lo = hour_bucket(parse_hour(min(hours)) - dt.timedelta(hours=margin_h))
    hi = hour_bucket(parse_hour(max(hours)) + dt.timedelta(hours=margin_h))
    sets, pts, names = defaultdict(set), defaultdict(int), {}
    for ts, norm, term, rank in con.execute(
        "SELECT ts, norm, term, rank FROM snapshots WHERE ts BETWEEN ? AND ?", (lo, hi)
    ):
        if norm in exclude:
            continue
        sets[norm].add(ts)
        names[norm] = term
        if ts in own:
            pts[norm] += points(rank)
    scored = []
    for norm, s in sets.items():
        inter = len(s & own)
        if inter:
            jac = inter / len(s | own)
            scored.append((jac * math.log(1 + pts[norm]), norm))
    scored.sort(reverse=True)
    return [names[n] for _, n in scored[:top]]


def search(con, query: str, days=180, exact=False, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    since = hour_bucket(now - dt.timedelta(days=days))
    qn = normalize(query)
    cond, arg = ("norm = ?", qn) if exact else ("norm LIKE ?", f"%{qn}%")

    variants = con.execute(
        f"""SELECT norm, MAX(term), COUNT(*), MIN(rank) FROM snapshots
            WHERE {cond} AND ts >= ? GROUP BY norm ORDER BY COUNT(*) DESC""",
        (arg, since),
    ).fetchall()

    # força de todos os termos no período (para o percentil)
    all_points = sorted(r[0] for r in con.execute(
        "SELECT SUM(? + 1 - rank) FROM snapshots WHERE ts >= ? GROUP BY norm", (MAX_RANK, since)
    ))
    n_terms = len(all_points)

    result = {"query": query, "window_days": days, "since": since, "terms_in_window": n_terms, "variants": []}
    for norm, term, n_hours, best in variants:
        rows = con.execute(
            "SELECT ts, rank FROM snapshots WHERE norm = ? AND ts >= ? ORDER BY ts", (norm, since)
        ).fetchall()
        eps = episodes_for(rows)
        total = sum(e["points"] for e in eps)
        below = sum(1 for p in all_points if p < total)
        for e in eps:
            ep_hours = [ts for ts, _ in rows if e["start"] <= ts <= e["end"]]
            ctx = con.execute(
                "SELECT summary FROM contexts WHERE norm = ? AND episode_start = ?", (norm, e["start"])
            ).fetchone()
            e["summary"] = ctx[0] if ctx else None
            e["co_trends"] = co_trends(con, ep_hours, {norm})
            e["x_search"] = x_search_url(term, e["start"], e["end"])
        result["variants"].append({
            "term": term, "norm": norm, "hours": n_hours, "best_rank": best,
            "days": len({ts[:10] for ts, _ in rows}), "points": total,
            "percentile": round(100 * below / n_terms) if n_terms else None,
            "first": rows[0][0], "last": rows[-1][0], "episodes": eps,
        })
    return result


def x_search_url(term: str, start: str, end: str) -> str:
    s = parse_hour(start).date()
    e = parse_hour(end).date() + dt.timedelta(days=1)
    q = f'"{term}" since:{s} until:{e}'
    return "https://x.com/search?f=top&q=" + quote(q)

# ---------------------------------------------------------------- contexto (porquê)

def _x_top_posts(term: str, n=10) -> list[dict]:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        return []
    r = _http_get(
        "https://api.x.com/2/tweets/search/recent",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "query": f'"{term}" lang:pt -is:retweet',
            "max_results": n,
            "sort_order": "relevancy",
            "tweet.fields": "public_metrics,created_at",
            "expansions": "author_id",
            "user.fields": "username",
        },
    )
    js = r.json()
    users = {u["id"]: u["username"] for u in js.get("includes", {}).get("users", [])}
    posts = []
    for t in js.get("data", []):
        m = t.get("public_metrics", {})
        user = users.get(t.get("author_id"), "i")
        posts.append({
            "text": t["text"], "likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0),
            "url": f"https://x.com/{user}/status/{t['id']}",
        })
    return sorted(posts, key=lambda p: p["likes"] + 2 * p["reposts"], reverse=True)[:5]


def _summarize(term: str, posts: list[dict], cotrends: list[str]) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not posts:
        return None
    import requests
    corpo = "\n".join(f"- ({p['likes']} curtidas) {p['text'][:280]}" for p in posts)
    prompt = (
        f'O termo "{term}" entrou nos Trending Topics do Brasil no X. '
        f"Outros termos em alta na mesma hora: {', '.join(cotrends) or 'nenhum'}.\n"
        f"Posts mais engajados sobre ele:\n{corpo}\n\n"
        "Escreva UMA frase em português do Brasil, no formato "
        f'"{term}" Porque ..., explicando por que virou tendência. '
        "Use só o que os posts sustentam; se não der para saber, responda exatamente: INCERTO."
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": os.environ.get("RADAR_MODEL", "claude-haiku-5-5"), "max_tokens": 200,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    return None if text.upper().startswith("INCERTO") else text


def enrich(con, max_episodes=15, max_rank=15, lookback_hours=6) -> int:
    """Gera contexto para episódios recentes que chegaram ao top `max_rank` e ainda não têm.

    Custo por episódio com a API oficial: 10 posts lidos × US$0,005 = US$0,05
    (+ ~US$0,001 do resumo). O corte por posição é o que mantém a conta baixa.
    """
    if not os.environ.get("X_BEARER_TOKEN"):
        print("[enrich] sem X_BEARER_TOKEN: contexto fica só com os termos que subiram junto", file=sys.stderr)
        return 0
    since = hour_bucket(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours))
    candidates = con.execute(
        """SELECT norm, MAX(term), MIN(rank) FROM snapshots WHERE ts >= ?
           GROUP BY norm HAVING MIN(rank) <= ? ORDER BY MIN(rank)""",
        (since, max_rank),
    ).fetchall()
    done = 0
    for norm, term, _ in candidates:
        if done >= max_episodes:
            break
        rows = con.execute("SELECT ts, rank FROM snapshots WHERE norm = ? ORDER BY ts", (norm,)).fetchall()
        ep = episodes_for(rows)[-1]
        if con.execute("SELECT 1 FROM contexts WHERE norm=? AND episode_start=?", (norm, ep["start"])).fetchone():
            continue
        hours = [ts for ts, _ in rows if ts >= ep["start"]]
        ct = co_trends(con, hours, {norm})
        try:
            posts = _x_top_posts(term)
            summary = _summarize(term, posts, ct)
        except Exception as exc:  # noqa: BLE001 — uma falha não derruba a coleta
            print(f"[enrich] {term}: {exc}", file=sys.stderr)
            posts, summary = [], None
        con.execute(
            "INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)",
            (norm, ep["start"], summary, json.dumps(posts, ensure_ascii=False), hour_bucket()),
        )
        con.commit()
        append_context(norm, ep["start"], summary, posts)
        done += 1
    return done

# ---------------------------------------------------------------- export para a página

def export(con, out: Path, days=400, demo=False) -> dict:
    """Gera um JSON compacto: dicionário de termos + lista de IDs por hora (em ordem de posição)."""
    now = dt.datetime.now(dt.timezone.utc)
    last = con.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
    if not last:
        raise SystemExit("Banco vazio — rode collect ou backfill primeiro.")
    end = parse_hour(last)
    start = max(parse_hour(con.execute("SELECT MIN(ts) FROM snapshots").fetchone()[0]),
                end - dt.timedelta(days=days))
    n_hours = int((end - start).total_seconds() // 3600) + 1

    term_id, terms = {}, []
    hours = [[] for _ in range(n_hours)]
    counts = []
    for ts, norm, term, rank, tc in con.execute(
        "SELECT ts, norm, term, rank, tweet_count FROM snapshots WHERE ts >= ? ORDER BY ts, rank",
        (hour_bucket(start),),
    ):
        if norm not in term_id:
            term_id[norm] = len(terms)
            terms.append(term)
        h = int((parse_hour(ts) - start).total_seconds() // 3600)
        hours[h].append(term_id[norm])
        if tc:
            counts.append([h, term_id[norm], tc])

    ctx = defaultdict(list)
    for norm, ep_start, summary, posts in con.execute("SELECT norm, episode_start, summary, posts FROM contexts"):
        if norm in term_id and ep_start >= hour_bucket(start):
            h = int((parse_hour(ep_start) - start).total_seconds() // 3600)
            urls = [p["url"] for p in json.loads(posts or "[]")][:3]
            ctx[term_id[norm]].append([h, summary, urls])

    payload = {
        "generated": hour_bucket(now), "start": hour_bucket(start), "demo": demo,
        "terms": terms, "h": hours, "tc": counts, "ctx": ctx,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"terms": len(terms), "hours": n_hours, "bytes": out.stat().st_size}

# ---------------------------------------------------------------- CLI

def cmd_collect(a):
    src = a.source or os.environ.get("RADAR_SOURCE", "getdaytrends")
    trends = SOURCES[src]()
    if a.dry_run:
        for t in trends:
            print(f"{t['rank']:>2}  {t['term']}  {t.get('tweet_count') or ''}")
        return
    if len(trends) < 10:
        sys.exit(f"[collect] só {len(trends)} termos — o layout da fonte pode ter mudado")
    append_csv(hour_bucket(), trends, src)
    print(f"[collect] {len(trends)} tendências gravadas ({src}, {hour_bucket()})")
    if not a.no_catchup:
        catchup()


def catchup(max_hours=23) -> int:
    """Preenche horas das últimas 23h que faltam nos CSVs, pelas páginas
    relativas do getdaytrends (/brazil/N/). Essas páginas são estáveis mesmo
    quando o arquivo por data do site está fora do ar, então uma execução
    perdida é recuperada na seguinte e o histórico fica sem buracos."""
    now = dt.datetime.now(dt.timezone.utc)
    have = csv_hours()
    done = 0
    for n in range(1, max_hours + 1):
        ts = hour_bucket(now - dt.timedelta(hours=n))
        if ts in have:
            continue
        try:
            trends = fetch_getdaytrends(hours_ago=n)
            if len(trends) >= 10:
                append_csv(ts, trends, "getdaytrends-catchup")
                done += 1
                print(f"[catchup] {ts} recuperada ({len(trends)} termos)")
            time.sleep(1.5)
        except Exception as exc:  # noqa: BLE001 — recuperação é melhor-esforço
            print(f"[catchup] {ts}: {exc}", file=sys.stderr)
    return done


def cmd_build_db(a):
    con = build_db()
    n = con.execute("SELECT COUNT(*), COUNT(DISTINCT ts), COUNT(DISTINCT norm) FROM snapshots").fetchone()
    print(f"[build-db] {n[0]} linhas · {n[1]} horas · {n[2]} termos → {DB_PATH}")


def cmd_backfill(a):
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    t = end - dt.timedelta(days=a.days)
    have = csv_hours()
    done = fails = 0
    while t <= end and done < a.max_hours:
        ts = hour_bucket(t)
        if ts not in have:
            try:
                trends = fetch_getdaytrends(t)
                if len(trends) >= 10:
                    append_csv(ts, trends, "getdaytrends-archive")
                    done += 1
                else:
                    fails += 1
            except Exception as exc:  # noqa: BLE001
                fails += 1
                print(f"[backfill] {ts}: {exc}", file=sys.stderr)
            time.sleep(a.sleep)
        t += dt.timedelta(hours=1)
    print(f"[backfill] {done} horas importadas, {fails} falhas")


def cmd_enrich(a):
    print(f"[enrich] {enrich(connect(), max_episodes=a.max, max_rank=a.max_rank)} episódios com contexto")


def cmd_search(a):
    res = search(connect(), a.term, days=a.days, exact=a.exact)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    if not res["variants"]:
        print(f'"{a.term}" não apareceu nos TTs do Brasil nos últimos {a.days} dias.')
        return
    print(f'"{a.term}" — últimos {a.days} dias ({res["terms_in_window"]} termos distintos no período)\n')
    for v in res["variants"]:
        print(f'■ {v["term"]}: {v["hours"]}h em trend em {v["days"]} dia(s), melhor posição #{v["best_rank"]}, '
              f'mais forte que {v["percentile"]}% dos termos do período')
        for e in v["episodes"]:
            print(f'   {e["start"][:16]} → {e["end"][11:16]} UTC · {e["hours"]}h · #{e["best_rank"]}')
            print(f'      contexto: {e["summary"] or "junto com " + ", ".join(e["co_trends"])}')
        print()


def cmd_export(a):
    info = export(connect(), Path(a.out), days=a.days, demo=a.demo)
    print(f"[export] {info}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("collect"); s.add_argument("--source", choices=SOURCES); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-catchup", action="store_true", help="não recuperar horas perdidas das últimas 23h")
    s.set_defaults(fn=cmd_collect)
    s = sp.add_parser("build-db"); s.set_defaults(fn=cmd_build_db)
    s = sp.add_parser("backfill"); s.add_argument("--days", type=int, default=180)
    s.add_argument("--sleep", type=float, default=2.0); s.add_argument("--max-hours", type=int, default=100000)
    s.set_defaults(fn=cmd_backfill)
    s = sp.add_parser("enrich"); s.add_argument("--max", type=int, default=15)
    s.add_argument("--max-rank", type=int, default=15); s.set_defaults(fn=cmd_enrich)
    s = sp.add_parser("search"); s.add_argument("term"); s.add_argument("--days", type=int, default=180)
    s.add_argument("--exact", action="store_true"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)
    s = sp.add_parser("export"); s.add_argument("--out", default=str(ROOT / "web" / "data" / "index.json"))
    s.add_argument("--days", type=int, default=400); s.add_argument("--demo", action="store_true")
    s.set_defaults(fn=cmd_export)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

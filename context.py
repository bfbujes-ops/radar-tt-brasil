#!/usr/bin/env python3
"""
Contexto do Radar TT: manchetes dos jornais e explicação de cada trend.

Subcomandos:
  news           coleta as manchetes dos principais jornais (RSS) e grava em data/news/
  analyze-news   lê as manchetes recentes e escreve a "pauta da imprensa" (temas do momento)
  explain        explica o top 10 do X: notícias + manchetes + tweets + assuntos que sobem junto
                 → frase "Porque…", categoria e contexto (web/context/<slug>.json)
  all            news + explain + analyze-news

A IA usada é a do GitHub Models (grátis dentro do GitHub Actions, via GITHUB_TOKEN).
Se ANTHROPIC_API_KEY existir, ela é usada no lugar. Tweets só entram com TWITTERAPI_IO_KEY.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent
SNAP = ROOT / "data" / "snapshots"
NEWS_DIR = ROOT / "data" / "news"
WEB_NEWS = ROOT / "web" / "news"
WEB_CTX = ROOT / "web" / "context"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"}
NOW = lambda: dt.datetime.now(dt.timezone.utc)

FEEDS = {
    "g1": ("g1", "https://g1.globo.com/rss/g1/"),
    "folha": ("Folha", "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml"),
    "estadao": ("Estadão", "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/ultimas/?outputType=xml"),
    "oglobo": ("O Globo", "https://oglobo.globo.com/rss/oglobo"),
    "cnn": ("CNN Brasil", "https://www.cnnbrasil.com.br/feed/"),
    "bbc": ("BBC Brasil", "https://feeds.bbci.co.uk/portuguese/rss.xml"),
    "metropoles": ("Metrópoles", "https://www.metropoles.com/feed"),
    "poder360": ("Poder360", "https://www.poder360.com.br/feed/"),
    "veja": ("Veja", "https://veja.abril.com.br/feed/"),
    "exame": ("Exame", "https://exame.com/feed/"),
    "ge": ("ge", "https://ge.globo.com/rss/ge/"),
}
CATEGORIAS = ["Política", "Esporte", "TV e reality", "Música", "Cinema e séries", "Famosos",
              "Internet e memes", "Games", "Economia", "Tecnologia", "Polícia e Justiça",
              "Mundo", "Religião", "Outros"]


# ---------------------------------------------------------------- utilidades

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("#", " ").replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-") or "termo"


def search_text(term: str) -> str:
    """'#ShampooChallenge' → 'Shampoo Challenge' (para buscar em notícias)."""
    t = term.lstrip("#")
    if term.startswith("#") and " " not in t:
        t = re.sub(r"(?<=[a-zà-ú])(?=[A-ZÀ-Ú])|(?<=[A-Za-z])(?=\d)", " ", t)
    return t.strip()


def term_regex(term: str):
    words = norm(search_text(term)).split()
    if not words or (len(words) == 1 and len(words[0]) < 3):
        return None
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(map(re.escape, words)) + r"(?![a-z0-9])")


def clean(s: str | None) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def rss_items(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        f = {}
        for c in it:
            ct = c.tag.rsplit("}", 1)[-1]
            if ct == "link" and not (c.text or "").strip():
                f.setdefault("link", c.get("href"))
            elif ct in ("title", "link", "pubDate", "published", "updated", "date"):
                f.setdefault(ct, c.text)
            elif ct == "source":
                f["source"] = c.text
        title = clean(f.get("title"))
        if title and f.get("link"):
            out.append({"title": title, "link": f["link"].strip(),
                        "pub": parse_date(f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date")),
                        "source": clean(f.get("source"))})
    return out


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=25, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- IA

def llm_json(prompt: str, max_tokens=700) -> dict | None:
    """Pede JSON à IA. GitHub Models (grátis no Actions) ou Claude se houver chave."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=90, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json={
            "model": os.environ.get("RADAR_MODEL", "claude-haiku-5-5"), "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt + "\n\nResponda apenas com o JSON."}]})
        if r.ok:
            txt = r.json()["content"][0]["text"]
            return _parse_json(txt)
        print(f"[ia] Claude {r.status_code}: {r.text[:200]}", file=sys.stderr)
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        return None
    for model in ("openai/gpt-4.1-mini", "openai/gpt-4o-mini"):
        strict = True
        for attempt in range(3):
            body = {"model": model, "max_tokens": max_tokens, "temperature": 0.2,
                    "messages": [{"role": "system", "content": "Você é editor de uma página brasileira que explica "
                                  "os trending topics do X. Escreve em português do Brasil, frases curtas, tom neutro e "
                                  "factual. Nunca inventa: usa só o material fornecido. Responde só em JSON."},
                                 {"role": "user", "content": prompt}]}
            if strict:
                body["response_format"] = {"type": "json_object"}
            try:
                r = requests.post("https://models.github.ai/inference/chat/completions", timeout=90, headers={
                    "Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                    "Accept": "application/json"}, json=body)
            except requests.RequestException as exc:
                print(f"[ia] {model}: {exc}", file=sys.stderr)
                time.sleep(5)
                continue
            if r.ok:
                time.sleep(4.5)                       # limite de ~15 pedidos por minuto
                try:
                    return _parse_json(r.json()["choices"][0]["message"]["content"])
                except (ValueError, KeyError, IndexError):
                    print(f"[ia] {model}: resposta inesperada ({r.headers.get('content-type')}) "
                          f"{r.text[:200]!r}", file=sys.stderr)
                    strict = False                    # tenta de novo sem exigir JSON
                    continue
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", "20"))
                if wait > 120:
                    print(f"[ia] {model}: limite diário atingido", file=sys.stderr)
                    break
                time.sleep(wait + 1)
                continue
            print(f"[ia] {model} {r.status_code}: {r.text[:200]}", file=sys.stderr)
            break
    return None


def _parse_json(txt: str) -> dict | None:
    m = re.search(r"\{.*\}", txt or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- manchetes

def news_load(hours=48) -> list[dict]:
    since = NOW() - dt.timedelta(hours=hours)
    out = []
    for f in sorted(NEWS_DIR.glob("*.jsonl"))[-4:]:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                n = json.loads(line)
            except json.JSONDecodeError:
                continue
            if dt.datetime.fromisoformat(n["pub"]) >= since:
                out.append(n)
    out.sort(key=lambda n: n["pub"], reverse=True)
    return out


def cmd_news(_a=None) -> int:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    known = {n["link"] for n in news_load(96)}
    now = NOW()
    fresh = []
    for key, (name, url) in FEEDS.items():
        try:
            items = rss_items(get(url).content)
        except Exception as exc:  # noqa: BLE001
            print(f"[news] {name}: {exc}", file=sys.stderr)
            continue
        n = 0
        for it in items:
            if it["link"] in known:
                continue
            pub = it["pub"] or now
            if pub > now + dt.timedelta(minutes=10):
                pub = now
            if pub < now - dt.timedelta(days=2):
                continue
            known.add(it["link"])
            fresh.append({"pub": pub.astimezone(dt.timezone.utc).isoformat(timespec="minutes"),
                          "src": key, "title": it["title"][:300], "link": it["link"]})
            n += 1
        print(f"[news] {name}: {n} novas")
    by_day = defaultdict(list)
    for n in fresh:
        by_day[n["pub"][:10]].append(n)
    for day, rows in by_day.items():
        with (NEWS_DIR / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
            for n in rows:
                fh.write(json.dumps(n, ensure_ascii=False) + "\n")
    print(f"[news] {len(fresh)} manchetes novas")
    return len(fresh)


# ---------------------------------------------------------------- trends (lê os CSVs)

def snapshots(days=3) -> dict[str, list[tuple[int, str]]]:
    """{ts: [(rank, term)]} das últimas `days` dias."""
    out = defaultdict(list)
    for f in sorted(SNAP.glob("*.csv"))[-days - 1:]:
        with f.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out[r["ts"]].append((int(r["rank"]), r["term"]))
    for ts in out:
        seen, lst = set(), []
        for rank, term in sorted(out[ts]):
            if norm(term) not in seen:
                seen.add(norm(term)); lst.append((rank, term))
        out[ts] = lst
    return out


def current_top(snap, n=10) -> tuple[str, list[tuple[int, str]]]:
    for ts in sorted(snap, reverse=True):
        if len(snap[ts]) >= 10:
            return ts, snap[ts][:n]
    return "", []


def hours_of(snap, term) -> list[str]:
    k = norm(term)
    return sorted(ts for ts, lst in snap.items() if any(norm(t) == k for _, t in lst))


def episode_start(hrs: list[str]) -> str | None:
    if not hrs:
        return None
    p = lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:00Z")
    start = hrs[-1]
    for a, b in zip(reversed(hrs[:-1]), reversed(hrs[1:])):
        if (p(b) - p(a)).total_seconds() > 3 * 3600:
            break
        start = a
    return start


def co_trends(snap, term, ep_hours: list[str], top=6) -> list[str]:
    if not ep_hours:
        return []
    own, k = set(ep_hours), norm(term)
    sets, pts, names = defaultdict(set), defaultdict(int), {}
    for ts, lst in snap.items():
        for rank, t in lst:
            nt = norm(t)
            if nt == k:
                continue
            sets[nt].add(ts); names[nt] = t
            if ts in own:
                pts[nt] += 51 - rank
    import math
    sc = []
    for nt, s in sets.items():
        inter = len(s & own)
        if inter:
            sc.append((inter / len(s | own) * math.log(1 + pts[nt]), names[nt]))
    return [t for _, t in sorted(sc, reverse=True)[:top]]


# ---------------------------------------------------------------- fontes por termo

def google_news(term: str, n=8) -> list[dict]:
    q = f'"{search_text(term)}" when:2d'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"})
    try:
        items = rss_items(get(url).content)
    except Exception as exc:  # noqa: BLE001
        print(f"[gnews] {term}: {exc}", file=sys.stderr)
        return []
    rx = term_regex(term)
    out = []
    for it in items:
        title = it["title"]
        src = it["source"] or ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        if rx and not rx.search(norm(title)):
            continue                                  # o Google às vezes devolve notícias sem o termo
        out.append({"t": title, "src": src or "Google Notícias", "link": it["link"],
                    "pub": (it["pub"] or NOW()).isoformat(timespec="minutes")})
        if len(out) >= n:
            break
    return out


def tweets_for(term: str, n=6) -> list[dict]:
    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key:
        return []
    q = (f'"{search_text(term)}"' if not term.startswith("#") else term) + " lang:pt -is:retweet"
    try:
        r = requests.get("https://api.twitterapi.io/twitter/tweet/advanced_search", timeout=30,
                         headers={"X-API-Key": key}, params={"query": q, "queryType": "Top"})
        r.raise_for_status()
        data = r.json().get("tweets", [])
    except Exception as exc:  # noqa: BLE001
        print(f"[tweets] {term}: {exc}", file=sys.stderr)
        return []
    out = []
    for t in sorted(data, key=lambda t: t.get("likeCount", 0) + 2 * t.get("retweetCount", 0), reverse=True)[:n]:
        a = t.get("author") or {}
        media = ((t.get("extendedEntities") or {}).get("media") or [{}])[0].get("media_url_https")
        out.append({"id": t.get("id"), "url": t.get("url") or f"https://x.com/{a.get('userName')}/status/{t.get('id')}",
                    "text": (t.get("text") or "")[:500], "name": a.get("name"), "user": a.get("userName"),
                    "avatar": a.get("profilePicture"), "verified": bool(a.get("isBlueVerified")),
                    "likes": t.get("likeCount", 0), "rts": t.get("retweetCount", 0),
                    "replies": t.get("replyCount", 0), "views": t.get("viewCount", 0),
                    "created": t.get("createdAt"), "media": media})
    return out


# ---------------------------------------------------------------- explicação

def explain_term(term: str, snap, headlines, rank=None) -> dict:
    hrs = hours_of(snap, term)
    start = episode_start(hrs) or NOW().strftime("%Y-%m-%dT%H:00Z")
    ep_hours = [h for h in hrs if h >= start]
    co = co_trends(snap, term, ep_hours)
    gn = google_news(term)
    rx = term_regex(term)
    hl = [{"t": h["title"], "src": FEEDS.get(h["src"], (h["src"],))[0], "link": h["link"], "pub": h["pub"]}
          for h in headlines if rx and rx.search(norm(h["title"]))][:6]
    seen = {n["t"].lower() for n in gn}
    news = gn + [h for h in hl if h["t"].lower() not in seen]
    tw = tweets_for(term)

    ep = {"start": start, "updated": NOW().isoformat(timespec="minutes"), "rank": rank,
          "hours": len(ep_hours), "co": co, "news": news[:10], "tweets": tw,
          "porque": None, "categoria": "Outros", "contexto": None, "confianca": "baixa"}

    if not news and not tw:
        ep["contexto"] = None
        return ep

    lines = [f"[N{i}] {n['src']}: {n['t']}" for i, n in enumerate(news[:10])]
    lines += [f"[T{i}] @{t['user']} ({t['likes']} curtidas): {t['text'][:280]}" for i, t in enumerate(tw)]
    prompt = (
        f'O termo "{term}" está nos trending topics do X no Brasil'
        + (f" ({rank}º lugar agora)" if rank else "") + ".\n"
        f"Outros termos em alta junto com ele: {', '.join(co) or 'nenhum'}.\n\n"
        "Material disponível (notícias e posts):\n" + "\n".join(lines) + "\n\n"
        "Tarefa: explique por que o termo está em alta.\n"
        "Responda em JSON com as chaves:\n"
        f'- "porque": uma frase no formato \'"{term}" Porque ...\' (até 30 palavras), explicando o motivo mais '
        "provável, só com base no material. Se o material não explica o termo, use null.\n"
        "- \"contexto\": 2 ou 3 frases curtas com o que se sabe (quem, o quê, quando), sem opinião. null se não houver.\n"
        f"- \"categoria\": uma destas: {', '.join(CATEGORIAS)}.\n"
        '- "confianca": "alta" se várias fontes confirmam, "media" se uma fonte, "baixa" se é dedução.\n'
        '- "usados": lista dos códigos do material que sustentam a explicação (ex.: ["N0","T2"]).\n'
        "Atenção: nomes comuns (ex.: um primeiro nome) podem se referir a pessoas diferentes; só "
        "atribua o termo a uma notícia se ela claramente for o assunto."
    )
    res = llm_json(prompt)
    if res:
        ep["porque"] = (res.get("porque") or None)
        ep["contexto"] = res.get("contexto") or None
        ep["categoria"] = res.get("categoria") if res.get("categoria") in CATEGORIAS else "Outros"
        ep["confianca"] = res.get("confianca") if res.get("confianca") in ("alta", "media", "baixa") else "baixa"
        used = set(res.get("usados") or [])
        ep["news_used"] = [i for i in range(len(news)) if f"N{i}" in used]
        ep["tweets_used"] = [i for i in range(len(tw)) if f"T{i}" in used]
    return ep


def save_episode(term: str, ep: dict):
    WEB_CTX.mkdir(parents=True, exist_ok=True)
    f = WEB_CTX / f"{slug(term)}.json"
    rec = {"term": term, "episodes": []}
    if f.exists():
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    rec["term"] = term
    rec["episodes"] = [e for e in rec.get("episodes", []) if e.get("start") != ep["start"]]
    rec["episodes"].insert(0, ep)
    rec["episodes"] = sorted(rec["episodes"], key=lambda e: e["start"], reverse=True)[:12]
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def rebuild_index():
    idx = {}
    for f in WEB_CTX.glob("*.json"):
        if f.name == "index.json":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            e = rec["episodes"][0]
        except Exception:  # noqa: BLE001
            continue
        idx[f.stem] = {"term": rec["term"], "start": e["start"], "updated": e["updated"],
                       "porque": e.get("porque"), "categoria": e.get("categoria"),
                       "confianca": e.get("confianca"), "contexto": e.get("contexto"),
                       "n_news": len(e.get("news", [])), "n_tweets": len(e.get("tweets", [])),
                       "co": e.get("co", [])[:4]}
    (WEB_CTX / "index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")


def cmd_explain(a) -> int:
    snap = snapshots()
    headlines = news_load(48)
    if a.terms:
        todo = [(None, t.strip()) for t in a.terms if t.strip()]
    else:
        _, top = current_top(snap, a.top)
        todo = top
    done = 0
    for rank, term in todo:
        f = WEB_CTX / f"{slug(term)}.json"
        start = episode_start(hours_of(snap, term))
        if f.exists() and not a.force and not a.terms:
            try:
                last = json.loads(f.read_text(encoding="utf-8"))["episodes"][0]
                age = NOW() - dt.datetime.fromisoformat(last["updated"])
                if last["start"] == start and (last.get("porque") or age < dt.timedelta(hours=2)) \
                        and age < dt.timedelta(hours=a.refresh_hours):
                    print(f"[explain] {term}: já explicado")
                    continue
            except Exception:  # noqa: BLE001
                pass
        try:
            ep = explain_term(term, snap, headlines, rank)
        except Exception as exc:  # noqa: BLE001 — um termo com problema não derruba os outros
            print(f"[explain] {term}: erro {exc}", file=sys.stderr)
            continue
        save_episode(term, ep)
        done += 1
        print(f"[explain] {term}: {ep.get('porque') or '(sem explicação)'} · {len(ep['news'])} notícias, "
              f"{len(ep['tweets'])} tweets")
    rebuild_index()
    return done


def cmd_analyze_news(_a=None):
    WEB_NEWS.mkdir(parents=True, exist_ok=True)
    items = news_load(36)
    recent = [n for n in items if dt.datetime.fromisoformat(n["pub"]) >= NOW() - dt.timedelta(hours=10)][:180]
    snap = snapshots(1)
    _, top = current_top(snap, 30)
    tt_terms = [t for _, t in top]

    # cruzamento TT × imprensa (determinístico)
    overlap = {}
    for t in tt_terms:
        rx = term_regex(t)
        if rx:
            c = sum(1 for n in items[:400] if rx.search(norm(n["title"])))
            if c:
                overlap[t] = c

    out_items = [[n["pub"], n["src"], n["title"], n["link"]] for n in items[:500]]
    pos = {n["link"]: i for i, n in enumerate(items[:500])}
    prev = {}
    f = WEB_NEWS / "latest.json"
    if f.exists():
        try:
            prev = json.loads(f.read_text(encoding="utf-8")).get("analysis") or {}
        except json.JSONDecodeError:
            prev = {}

    analysis = prev
    if recent:
        lines = [f"[{i}] {FEEDS.get(n['src'], (n['src'],))[0]}: {n['title']}" for i, n in enumerate(recent)]
        prompt = (
            "Manchetes publicadas nas últimas horas pelos principais sites de notícias do Brasil:\n"
            + "\n".join(lines)
            + f"\n\nTermos em alta no X agora: {', '.join(tt_terms) or 'nenhum'}.\n\n"
            "Monte a pauta da imprensa. Responda em JSON com:\n"
            '- "resumo": uma frase que sintetize o noticiário do momento.\n'
            '- "temas": de 5 a 7 temas, do mais para o menos destacado (mais veículos e mais manchetes '
            'contam mais). Cada tema: {"titulo": até 8 palavras, "resumo": 1 ou 2 frases factuais, '
            f'"categoria": uma destas {CATEGORIAS}, "itens": lista dos números das manchetes do tema, '
            '"tt": o termo da lista do X que trata do mesmo assunto, ou null}.\n'
            "Ignore horóscopo, receitas, promoções e conteúdo patrocinado. Não invente nada."
        )
        res = llm_json(prompt, max_tokens=1600)
        if res and res.get("temas"):
            temas = []
            for t in res["temas"][:7]:
                idxs = [i for i in t.get("itens", []) if isinstance(i, int) and 0 <= i < len(recent)]
                if not idxs:
                    continue
                links = [recent[i]["link"] for i in idxs]
                temas.append({"titulo": t.get("titulo"), "resumo": t.get("resumo"),
                              "categoria": t.get("categoria") if t.get("categoria") in CATEGORIAS else "Outros",
                              "itens": [pos[l] for l in links if l in pos][:8],
                              "fontes": sorted({recent[i]["src"] for i in idxs}),
                              "tt": t.get("tt") if t.get("tt") in tt_terms else None})
            analysis = {"generated": NOW().isoformat(timespec="minutes"), "resumo": res.get("resumo"),
                        "temas": temas}
        else:
            # mantém a análise anterior, mas os índices podem ter mudado: refaz pelo link
            analysis = prev

    payload = {"generated": NOW().isoformat(timespec="minutes"),
               "sources": {k: v[0] for k, v in FEEDS.items()},
               "items": out_items, "overlap": overlap, "analysis": analysis}
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"[pauta] {len(out_items)} manchetes · {len((analysis or {}).get('temas', []))} temas · "
          f"{len(overlap)} TTs na imprensa")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("news").set_defaults(fn=cmd_news)
    sp.add_parser("analyze-news").set_defaults(fn=cmd_analyze_news)
    for name in ("explain", "all"):
        s = sp.add_parser(name)
        s.add_argument("--terms", nargs="*")
        s.add_argument("--top", type=int, default=10)
        s.add_argument("--force", action="store_true")
        s.add_argument("--refresh-hours", type=float, default=6)
        s.set_defaults(fn=cmd_explain if name == "explain" else None)
    a = p.parse_args()
    if a.cmd == "all":
        cmd_news(); cmd_explain(a); cmd_analyze_news()
    else:
        a.fn(a)


if __name__ == "__main__":
    main()

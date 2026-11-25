#!/usr/bin/env python3
"""
Filtro de clientes do Radar TT: liga trends e manchetes ao universo de cada marca.

Para cada cliente em data/clients.json, procura nos trending topics das últimas 24h
(e nas explicações deles) e nas manchetes recentes palavras que puxam um ativo da marca
(produto, ícone, embaixador) ou um território (amor, bebês, festa junina…).
Exemplo: "Everton Cebolinha" → Outback (Bloomin' Onion, a cebola) e Huggies (Cebolinha,
da Turma da Mônica).

Com ANTHROPIC_API_KEY, a IA revisa as conexões, acrescenta associações que não dependem
da palavra exata e escreve uma ideia de conteúdo para cada uma. Sem a chave, cada conexão
vem com uma frase automática.

Gera web/clients/latest.json (agora + últimos 7 dias) e guarda o histórico em data/clients/.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

import context as C

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "data" / "clients.json"
HIST = ROOT / "data" / "clients"
OUT = ROOT / "web" / "clients"

SENSIVEL = ["morte", "morre", "morreu", "mort", "falece", "luto", "velorio", "enterro", "acidente", "tragedia",
            "assassin", "homicid", "estupr", "abuso", "cancer", "tiroteio", "chacina", "atentado", "guerra",
            "enchente", "desabamento", "sequestr", "violencia", "racis", "preso", "prisao", "pedofil",
            "suicid", "overdose", "massacre", "vitima", "ferido", "bomba", "cetamina", "droga", "dependencia quimica",
            "vicio", "crack", "cocaina"]
RX_SENS = re.compile(r"(?<![a-z0-9])(" + "|".join(SENSIVEL) + ")")


def load_cfg() -> dict:
    return {k: v for k, v in json.loads(CFG.read_text(encoding="utf-8")).items() if not k.startswith("_")}


def compile_rx(radicais):
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(C.norm(r)) for r in radicais) + ")")


def find_hits(text: str, client: dict) -> list[dict]:
    """Ativos e territórios citados no texto (já normalizado)."""
    hits = []
    for kind in ("ativos", "territorios"):
        for a in client.get(kind, []):
            m = a.setdefault("_rx", compile_rx(a["radicais"])).search(text)
            if m and a.get("excluir") and a.setdefault("_rx_ex", compile_rx(a["excluir"])).search(text):
                continue
            if m:
                hits.append({"tipo": "ativo" if kind == "ativos" else "territorio", "nome": a["nome"],
                             "desc": a.get("desc", ""), "palavra": m.group(1).strip()})
    return hits


def original_word(text: str, norm_word: str) -> str:
    """Recupera o trecho como apareceu no texto original ('cebol' → 'Cebolinha',
    'sonho de valsa' → 'Sonho de Valsa')."""
    words = re.findall(r"[\wÀ-ÿ'’&-]+", text)
    parts = norm_word.split()
    for i, w in enumerate(words):
        if C.norm(w).startswith(parts[0]):
            return " ".join(words[i:i + len(parts)])
    return norm_word


def trends_24h() -> list[dict]:
    snap = C.snapshots(2)
    since = (C.NOW() - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:00Z")
    agg = {}
    for ts in sorted(snap):
        if ts < since:
            continue
        for rank, term in snap[ts]:
            k = C.norm(term)
            a = agg.setdefault(k, {"term": term, "best": rank, "hours": 0, "last": ts})
            a["best"] = min(a["best"], rank); a["hours"] += 1; a["last"] = max(a["last"], ts)
    return list(agg.values())


def context_index() -> dict:
    f = ROOT / "web" / "context" / "index.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


EN_WORDS = {"the", "of", "and", "or", "for", "to", "with", "is", "are", "my", "i'd", "how", "which", "stock",
            "shares", "vs", "better", "next", "years", "only", "one", "gets", "hold", "pay", "month"}
FIN_NOISE = re.compile(r"(?<![a-z0-9])(dividend|dividendo|acoes da|acao da|acoes de|previsao semanal|bdr|cotacao|"
                       r"portfolio|stock|wall street|bolsa de nova york|nyse|nasdaq|preco-alvo|preco alvo)")
NOISE_SOURCES = {"tikr.com", "traders union", "marketbeat", "seeking alpha", "simply wall st"}


def is_noise(title: str, src: str) -> bool:
    """Notícia de cotação de ação, em inglês ou de agregador financeiro não interessa à agência."""
    words = re.findall(r"[a-z']+", title.lower())
    if sum(w in EN_WORDS for w in words) >= 2:
        return True
    return (src or "").strip().lower() in NOISE_SOURCES or bool(FIN_NOISE.search(C.norm(title)))


def gnews_client(client: dict) -> list[dict]:
    """Notícias dos últimos 3 dias sobre a marca/empresa e sobre os concorrentes (Google Notícias)."""
    nt = client.get("noticiario") or {}
    out = []
    for grp in ("marca", "concorrentes"):
        terms = [t for t in nt.get(grp, []) if len(t) >= 4]
        if not terms:
            continue
        q = "(" + " OR ".join(f'"{t}"' for t in terms[:8]) + ") when:3d"
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q, "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"})
        try:
            items = C.rss_items(C.get(url).content)
        except Exception as exc:  # noqa: BLE001
            print(f"[clientes] Google Notícias ({client['nome']}): {exc}", file=sys.stderr)
            continue
        for it in items[:40]:
            title, src = it["title"], it["source"] or "Google Notícias"
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            out.append({"title": title, "src": src, "link": it["link"],
                        "pub": (it["pub"] or C.NOW()).astimezone(dt.timezone.utc).isoformat(timespec="minutes")})
    return out


def auto_idea(item: dict, client: dict, hit: dict) -> str:
    word = hit.get("palavra_original") or hit["palavra"]
    if item["kind"] == "trend":
        onde = f"está em {item['best']}º nos trending topics" if item["best"] <= 10 else "está nos trending topics"
        alvo = f"“{item['term']}” {onde}"
    else:
        alvo = f"A manchete do {item['src']} fala em “{word}”"
    if hit["tipo"] == "ativo":
        return f"{alvo} e a palavra “{word}” puxa {hit['nome']}, {hit['desc']}."
    return f"{alvo} e entra no território de {hit['nome'].lower()} de {client['nome']}."


def collect_matches(cfg: dict) -> dict[str, list[dict]]:
    ctx = context_index()
    out = {k: [] for k in cfg}
    # trends (termo + explicação)
    for t in trends_24h():
        c = ctx.get(C.slug(t["term"]), {})
        why = c.get("porque") or (c.get("destaque") or {}).get("t") or ""
        for key, client in cfg.items():
            in_term = find_hits(C.norm(t["term"]), client)
            in_ctx = [] if in_term else find_hits(C.norm(why), client)
            for hit in (in_term or in_ctx)[:1]:
                hit["palavra_original"] = original_word(t["term"] if in_term else why, hit["palavra"])
                text_all = C.norm(t["term"] + " " + why)
                risco = bool(RX_SENS.search(text_all)) or any(C.norm(e) in text_all for e in client.get("evitar", []))
                score = (51 - t["best"]) + 2 * min(t["hours"], 12) + (25 if hit["tipo"] == "ativo" else 8) \
                    + (10 if in_term else 0) - (30 if risco else 0)
                item = {"kind": "trend", "term": t["term"], "best": t["best"], "hours": t["hours"],
                        "last": t["last"], "why": why or None, "hit": hit, "onde": "termo" if in_term else "contexto",
                        "risco": risco, "score": score, "fonte": "regra"}
                item["ideia"] = auto_idea(item, client, hit)
                out[key].append(item)
    # manchetes (últimas 24h): só marca/empresa, concorrentes e categoria — nada de coincidência de palavra
    GRUPOS = (("marca", "Marca"), ("concorrentes", "Concorrente"), ("categoria", "Categoria"))
    seen = defaultdict(set)
    pool = [(n, None) for n in C.news_load(72)]
    for key, client in cfg.items():                   # busca dirigida: marca e concorrentes no Google Notícias
        for n in gnews_client(client):
            pool.append((n, key))
    for n, only in pool:
        if is_noise(n["title"], n.get("src", "")):
            continue
        text = C.norm(n["title"])
        for key, client in cfg.items():
            if only and only != key:
                continue
            nt = client.get("noticiario") or {}
            if nt.get("excluir") and nt.setdefault("_rx_ex", compile_rx(nt["excluir"])).search(text):
                continue
            hit = None
            for grp, label in GRUPOS:
                rx = nt.setdefault("_rx_" + grp, compile_rx(nt.get(grp) or ["\x00"]))
                m = rx.search(text)
                if m:
                    hit = {"tipo": grp, "nome": label, "desc": "", "palavra": m.group(1).strip()}
                    break
            if not hit:
                continue
            sig = " ".join(re.sub(r"[^a-z0-9 ]", "", text).split()[:7])
            if sig in seen[key]:
                continue
            seen[key].add(sig)
            hit["palavra_original"] = original_word(n["title"], hit["palavra"])
            risco = bool(RX_SENS.search(text)) or any(C.norm(e) in text for e in client.get("evitar", []))
            age_h = (C.NOW() - dt.datetime.fromisoformat(n["pub"])).total_seconds() / 3600
            score = {"marca": 40, "concorrentes": 30, "categoria": 18}[hit["tipo"]] - age_h / 6
            out[key].append({"kind": "news", "title": n["title"], "src": C.FEEDS.get(n["src"], (n["src"],))[0] if n["src"] in C.FEEDS else n["src"],
                             "link": n["link"], "pub": n["pub"], "hit": hit, "risco": risco,
                             "score": round(score, 1), "fonte": "regra"})

    for key in out:
        out[key].sort(key=lambda i: i["score"], reverse=True)
        kept = out[key]
        out[key] = kept[:25]
    return out


def ai_review(cfg: dict, matches: dict) -> None:
    """Com chave da Anthropic: revisa conexões, sugere associações criativas e escreve ideias."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return
    ctx = context_index()
    trends = sorted(trends_24h(), key=lambda t: t["best"])[:30]
    lines = []
    for t in trends:
        c = ctx.get(C.slug(t["term"]), {})
        why = c.get("porque") or (c.get("destaque") or {}).get("t") or "sem explicação"
        lines.append(f"- {t['term']} (melhor posição {t['best']}º): {why}")
    for key, client in cfg.items():
        cand = [m for m in matches[key] if m["kind"] == "trend"][:10]
        cand_txt = "\n".join(f"[{i}] {(m.get('term') or m.get('title'))} → {m['hit']['nome']}" for i, m in enumerate(cand))
        prompt = (
            f"Cliente: {client['nome']}. Universo da marca: {client['universo']}\n\n"
            f"Trending topics do X no Brasil nas últimas 24h e o motivo de cada um:\n" + "\n".join(lines) + "\n\n"
            f"Conexões encontradas por palavra-chave:\n{cand_txt or 'nenhuma'}\n\n"
            "Você é estrategista de conteúdo de uma agência brasileira e procura oportunidades de real time "
            "marketing para o cliente. Responda em JSON com:\n"
            '- "revisao": lista de {"i": número da conexão, "manter": true/false, "ideia": uma ideia de post em '
            'uma frase, curta e espirituosa, "risco": true se o assunto é sensível (morte, violência, tragédia, '
            'polêmica política) e a marca deve evitar}.\n'
            '- "novas": até 3 conexões que a palavra-chave não pegou (trocadilho, território, comportamento), cada '
            'uma {"term": termo exatamente como na lista de trends, "ponte": o elemento da marca, "ideia": uma frase, '
            '"risco": true/false}.\n'
            "Só use trends da lista. Não force conexões fracas: prefira poucas e boas."
        )
        res = C.llm_json(prompt, max_tokens=900)
        if not res:
            continue
        keep = []
        for r in res.get("revisao", []):
            i = r.get("i")
            if isinstance(i, int) and 0 <= i < len(cand):
                m = cand[i]
                if r.get("manter") is False:
                    m["descartado"] = True
                if r.get("ideia"):
                    m["ideia"] = r["ideia"]; m["fonte"] = "ia"
                m["risco"] = bool(r.get("risco")) or m["risco"]
        by_term = {C.norm(t["term"]): t for t in trends}
        for nv in res.get("novas", [])[:3]:
            t = by_term.get(C.norm(nv.get("term", "")))
            if not t:
                continue
            c = ctx.get(C.slug(t["term"]), {})
            keep.append({"kind": "trend", "term": t["term"], "best": t["best"], "hours": t["hours"],
                         "last": t["last"], "why": c.get("porque"), "onde": "ia",
                         "hit": {"tipo": "ia", "nome": nv.get("ponte") or "associação", "desc": "", "palavra": ""},
                         "ideia": nv.get("ideia"), "risco": bool(nv.get("risco")), "fonte": "ia",
                         "score": (51 - t["best"]) + 20})
        matches[key] = sorted([m for m in matches[key] if not m.get("descartado")] + keep,
                              key=lambda i: i["score"], reverse=True)


def item_key(m):
    return m["kind"] + "|" + C.norm(m.get("term") or m.get("title") or "") + "|" + m["hit"]["nome"]


def save(cfg: dict, matches: dict) -> None:
    HIST.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    day = C.NOW().strftime("%Y-%m-%d")
    f = HIST / f"{day}.jsonl"
    known = set()
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line); known.add(r["client"] + "|" + r["key"])
            except (json.JSONDecodeError, KeyError):
                pass
    with f.open("a", encoding="utf-8") as fh:
        for key, items in matches.items():
            for m in items:
                k = item_key(m)
                if key + "|" + k not in known:
                    fh.write(json.dumps({"client": key, "key": k, "seen": C.NOW().isoformat(timespec="minutes"),
                                         **{x: m.get(x) for x in ("kind", "term", "best", "title", "src", "link",
                                                                  "pub", "hit", "ideia", "risco", "score", "fonte")}},
                                        ensure_ascii=False) + "\n")
    # últimos 7 dias
    week = defaultdict(dict)
    for hf in sorted(HIST.glob("*.jsonl"))[-7:]:
        for line in hf.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "news" and (r.get("hit") or {}).get("tipo") not in ("marca", "concorrentes", "categoria"):
                continue
            prev = week[r["client"]].get(r["key"])
            if not prev or (r.get("score") or 0) > (prev.get("score") or 0):
                week[r["client"]][r["key"]] = r
    payload = {"generated": C.NOW().isoformat(timespec="minutes"), "clients": {}}
    for key, client in cfg.items():
        trends_ = [m for m in matches[key] if m["kind"] == "trend"][:12]
        news_ = [m for m in matches[key] if m["kind"] == "news"][:12]
        agora = [{k: v for k, v in m.items() if not k.startswith("_")} for m in trends_ + news_]
        wk = sorted(week[key].values(), key=lambda r: r.get("score") or 0, reverse=True)
        semana = [r for r in wk if r.get("kind") == "trend"][:15] + [r for r in wk if r.get("kind") == "news"][:15]
        payload["clients"][key] = {"nome": client["nome"], "cor": client["cor"], "universo": client["universo"],
                                   "agora": agora, "semana": semana}
    (OUT / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print("[clientes] " + " · ".join(f"{c['nome']}: {len(matches[k])}" for k, c in cfg.items()))


def main():
    cfg = load_cfg()
    matches = collect_matches(cfg)
    try:
        ai_review(cfg, matches)
    except Exception as exc:  # noqa: BLE001 — a IA é opcional
        print(f"[clientes] IA: {exc}", file=sys.stderr)
    for client in cfg.values():
        for kind in ("ativos", "territorios"):
            for a in client.get(kind, []):
                a.pop("_rx", None); a.pop("_rx_ex", None)
        for g in ("marca", "concorrentes", "categoria", "ex"):
            (client.get("noticiario") or {}).pop("_rx_" + g, None)
    save(cfg, matches)


if __name__ == "__main__":
    main()

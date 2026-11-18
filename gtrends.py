#!/usr/bin/env python3
"""
Coletor do Google Trends (Brasil) para o Radar TT.

Baixa, para cada termo, os mesmos dados que a página do Google Trends mostra:
  - interesse ao longo do tempo (0 a 100)
  - pesquisas relacionadas (principais e em ascensão)
  - interesse por estado
e grava em web/google/<slug>.json, que a página desenha com gráficos próprios.

Uso:
  python gtrends.py "james bond" "shampoo"
  python gtrends.py --top10            # os 10 termos mais fortes da última hora coletada no X
  python gtrends.py --from-file pedidos.txt
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "web" / "google"
BASE = "https://trends.google.com/trends"
HL, TZ, GEO = "pt-BR", 180, "BR"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
def _six_months() -> str:
    e = dt.date.today(); b = e - dt.timedelta(days=180)
    return f"{b:%Y-%m-%d} {e:%Y-%m-%d}"   # até ~9 meses o Google devolve um ponto por dia


WINDOWS = {"3m": lambda: "today 3-m", "6m": _six_months, "12m": lambda: "today 12-m"}


def slug(term: str) -> str:
    t = unicodedata.normalize("NFKD", term)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().replace("#", "")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-") or "termo"


class Trends:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        self._warm()

    def _warm(self):
        # O Google só responde a API depois de dar o cookie de sessão (NID).
        try:
            self.s.get(f"{BASE}/explore", params={"geo": GEO, "hl": HL}, timeout=20)
        except requests.RequestException:
            pass

    def _get(self, path, params, tries=5):
        for i in range(tries):
            r = self.s.get(f"{BASE}/api/{path}", params=params, timeout=30)
            if r.status_code == 200:
                txt = r.text
                return json.loads(txt[txt.index("{"):] if path == "explore" else txt[5:])
            if r.status_code in (429, 500, 502, 503):
                wait = 8 * (i + 1) + random.uniform(0, 4)
                print(f"  {path}: HTTP {r.status_code}, nova tentativa em {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                if r.status_code == 429:
                    self._warm()
                continue
            r.raise_for_status()
        raise RuntimeError(f"{path}: Google recusou depois de {tries} tentativas")

    def fetch(self, term: str, time_range: str) -> dict:
        req = {"comparisonItem": [{"keyword": term, "geo": GEO, "time": time_range}],
               "category": 0, "property": ""}
        ex = self._get("explore", {"hl": HL, "tz": TZ, "req": json.dumps(req)})
        widgets = {w["id"]: w for w in ex.get("widgets", [])}
        out = {}

        w = widgets.get("TIMESERIES")
        if w:
            time.sleep(1.2)
            d = self._get("widgetdata/multiline",
                          {"hl": HL, "tz": TZ, "req": json.dumps(w["request"]), "token": w["token"]})
            # terceiro campo = 1 quando o Google marca o ponto como parcial (período ainda em curso)
            out["timeline"] = [[int(p["time"]), p["value"][0], 1 if p.get("isPartial") else 0]
                               for p in d["default"]["timelineData"]]

        w = widgets.get("RELATED_QUERIES")
        if w:
            time.sleep(1.2)
            d = self._get("widgetdata/relatedsearches",
                          {"hl": HL, "tz": TZ, "req": json.dumps(w["request"]), "token": w["token"]})
            lists = d["default"]["rankedList"]
            pick = lambda L: [[k["query"], k.get("formattedValue", k.get("value"))] for k in L["rankedKeyword"]]
            out["related_top"] = pick(lists[0])[:25] if lists else []
            out["related_rising"] = pick(lists[1])[:25] if len(lists) > 1 else []

        w = widgets.get("GEO_MAP")
        if w:
            time.sleep(1.2)
            d = self._get("widgetdata/comparedgeo",
                          {"hl": HL, "tz": TZ, "req": json.dumps(w["request"]), "token": w["token"]})
            out["regions"] = sorted(
                [[g["geoName"], g["value"][0]] for g in d["default"]["geoMapData"] if g.get("hasData", [True])[0]],
                key=lambda x: -x[1])
        return out


def top10_terms() -> list[str]:
    import csv
    files = sorted((ROOT / "data" / "snapshots").glob("*.csv"))
    rows = []
    for f in files[-2:]:
        with f.open(encoding="utf-8") as fh:
            rows += list(csv.DictReader(fh))
    if not rows:
        return []
    by_ts = {}
    for r in rows:
        by_ts.setdefault(r["ts"], []).append(r)
    for ts in sorted(by_ts, reverse=True):
        lst = sorted(by_ts[ts], key=lambda r: int(r["rank"]))
        if len(lst) >= 10:
            seen, out = set(), []
            for r in lst:
                if r["term"].lower() not in seen:
                    seen.add(r["term"].lower()); out.append(r["term"])
            return out[:10]
    return []


def run(terms: list[str], windows=("3m", "6m", "12m")) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tr = Trends()
    ok = 0
    for term in terms:
        term = term.strip()
        if not term:
            continue
        rec = {"term": term, "geo": GEO, "fetched": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
               "windows": {}}
        print(f"[google] {term}")
        for key in windows:
            try:
                rec["windows"][key] = tr.fetch(term, WINDOWS[key]())
            except Exception as exc:  # noqa: BLE001
                print(f"  {key}: {exc}", file=sys.stderr)
            time.sleep(2 + random.uniform(0, 2))
        if rec["windows"]:
            (OUT / f"{slug(term)}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            ok += 1
    # índice dos termos já analisados (a página consulta antes de pedir o arquivo)
    idx = {}
    for f in OUT.glob("*.json"):
        if f.name == "index.json":
            continue
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            idx[f.stem] = {"term": j["term"], "fetched": j["fetched"]}
        except Exception:  # noqa: BLE001
            pass
    (OUT / "index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    print(f"[google] {ok}/{len(terms)} termos gravados")
    return ok


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("terms", nargs="*")
    p.add_argument("--top10", action="store_true")
    p.add_argument("--from-file")
    a = p.parse_args()
    terms = list(a.terms)
    if a.top10:
        terms += top10_terms()
    if a.from_file and Path(a.from_file).exists():
        terms += [t for t in Path(a.from_file).read_text(encoding="utf-8").splitlines() if t.strip()]
    seen, uniq = set(), []
    for t in terms:
        if slug(t) not in seen:
            seen.add(slug(t)); uniq.append(t)
    if not uniq:
        sys.exit("nenhum termo")
    if run(uniq) == 0:
        sys.exit("[google] nada gravado — o Google pode estar bloqueando o acesso")


if __name__ == "__main__":
    main()

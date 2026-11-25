"""Teste de fontes (temporário)."""
import os
import urllib.parse
from xml.etree import ElementTree as ET

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"}
FEEDS = {
    "g1": "https://g1.globo.com/rss/g1/",
    "folha": "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml",
    "uol": "https://rss.uol.com.br/feed/noticias.xml",
    "cnn": "https://www.cnnbrasil.com.br/feed/",
    "bbc": "https://feeds.bbci.co.uk/portuguese/rss.xml",
    "estadao": "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/ultimas/?outputType=xml",
    "metropoles": "https://www.metropoles.com/feed",
    "poder360": "https://www.poder360.com.br/feed/",
    "oglobo": "https://oglobo.globo.com/rss/oglobo",
    "exame": "https://exame.com/feed/",
    "veja": "https://veja.abril.com.br/feed/",
    "ge": "https://ge.globo.com/rss/ge/",
}


def items(xml):
    root = ET.fromstring(xml)
    out = []
    for it in root.iter():
        if it.tag.endswith("item") or it.tag.endswith("entry"):
            out.append(next((c.text for c in it if c.tag.endswith("title")), None))
    return out


for k, u in FEEDS.items():
    try:
        r = requests.get(u, headers=UA, timeout=20)
        its = items(r.content) if r.ok else []
        print(f"FEED {k}: {r.status_code} {len(its)} itens | {(its[0] or '')[:70] if its else ''}")
    except Exception as e:  # noqa: BLE001
        print(f"FEED {k}: ERRO {e}")

u = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
    {"q": "Boechat when:2d", "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"})
r = requests.get(u, headers=UA, timeout=20)
its = items(r.content) if r.ok else []
print(f"GNEWS {r.status_code} {len(its)} | " + " || ".join((t or "")[:80] for t in its[:3]))

tok = os.environ.get("GITHUB_TOKEN")
for model in ["openai/gpt-4.1-mini", "openai/gpt-4o-mini", "meta/llama-3.3-70b-instruct"]:
    r = requests.post(
        "https://models.github.ai/inference/chat/completions",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                 "Accept": "application/vnd.github+json"},
        json={"model": model, "messages": [{"role": "user", "content": "Responda só: ok, em português."}],
              "max_tokens": 10},
        timeout=60)
    print(f"MODEL {model}: {r.status_code} {r.text[:160]}")
    print("  limits:", {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()})

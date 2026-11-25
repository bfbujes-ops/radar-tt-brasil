"""Teste temporário da IA do GitHub Models."""
import os

import requests

tok = os.environ.get("GITHUB_TOKEN", "")
print("token presente:", bool(tok), len(tok))
body = {"model": "openai/gpt-4.1-mini", "max_tokens": 20,
        "messages": [{"role": "user", "content": "Diga a palavra 'pronto' em JSON: {\"r\": ...}"}]}
for url in ["https://models.github.ai/inference/chat/completions",
            "https://models.inference.ai.azure.com/chat/completions"]:
    for hdr in [{"Accept": "application/json"},
                {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}]:
        b = dict(body)
        if "azure" in url:
            b["model"] = "gpt-4o-mini"
        r = requests.post(url, json=b, allow_redirects=False, timeout=60,
                          headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json", **hdr})
        print(f"URL {url} {hdr.get('Accept')}: {r.status_code} ct={r.headers.get('content-type')} "
              f"loc={r.headers.get('location')} body={r.text[:220]!r}")
r = requests.get("https://models.github.ai/catalog/models", timeout=30,
                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
print("CATALOG", r.status_code, r.text[:300])

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import context as C  # noqa: E402

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>x</title>
<item><title><![CDATA[Boechat &eacute; homenageado em S&atilde;o Paulo]]></title>
<link>https://exemplo.com/a</link><pubDate>Tue, 25 Nov 2026 14:00:00 -0300</pubDate>
<source url="https://exemplo.com">Folha</source></item>
<item><title>Sem link</title></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom ok</title>
<link href="https://exemplo.com/b"/><updated>2026-11-25T10:00:00Z</updated></entry></feed>"""


def test_rss_and_atom():
    a = C.rss_items(RSS)
    assert len(a) == 1 and a[0]["title"] == "Boechat é homenageado em São Paulo"
    assert a[0]["source"] == "Folha" and a[0]["pub"].astimezone(__import__("datetime").timezone.utc).hour == 17
    b = C.rss_items(ATOM)
    assert b[0]["link"] == "https://exemplo.com/b"


def test_search_text_and_regex():
    assert C.search_text("#ShampooChallenge") == "Shampoo Challenge"
    assert C.search_text("#GTA6") == "GTA 6"
    rx = C.term_regex("Alan")
    assert rx.search(C.norm("Alan Turing faz aniversário")) and not rx.search(C.norm("Alana vence"))
    assert C.term_regex("#ShampooChallenge").search(C.norm("o shampoo challenge viralizou"))
    assert C.term_regex("Ok") is None


def test_episode_start_and_cotrends():
    hrs = ["2026-11-25T01:00Z", "2026-11-25T10:00Z", "2026-11-25T11:00Z", "2026-11-25T13:00Z"]
    assert C.episode_start(hrs) == "2026-11-25T10:00Z"
    snap = {"2026-11-25T10:00Z": [(1, "Boechat"), (2, "Band"), (3, "Pix")],
            "2026-11-25T11:00Z": [(1, "Boechat"), (2, "Band")],
            "2026-11-25T12:00Z": [(1, "Pix")]}
    co = C.co_trends(snap, "Boechat", ["2026-11-25T10:00Z", "2026-11-25T11:00Z"])
    assert co[0] == "Band"


def test_parse_json_tolerant():
    assert C._parse_json('aqui vai: {"porque": null, "categoria": "Outros"} fim')["categoria"] == "Outros"
    assert C._parse_json("nada") is None

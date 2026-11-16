import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import radar  # noqa: E402

# Estrutura fiel ao site real (verificada em nov/2026): calendário antes,
# top 50 em DUAS tabelas dentro de #trends, e rankings do dia FORA do container.
HTML = """
<html><body>
<table><tbody><tr><td><a class="url-hours" href="/brazil/2026-11-01/21/">1</a></td></tr></tbody></table>
<div id="trends" class="inset">
<table class="table ranking trends"><tbody>
  <tr><th class="pos">1</th><td class="main"><a href="/brazil/trend/%23RoçaAFazenda/">#RoçaAFazenda</a><div>136.9K tweets</div></td></tr>
  <tr><th class="pos">2</th><td class="main"><a class="string" href="/brazil/trend/Vasco%20Da%20Gama/">Vasco Da Gama</a><div>12,345 tweets</div></td></tr>
  """ + "".join(
    f'<tr><th>{i}</th><td><a href="/brazil/trend/Termo{i}/">Termo{i}</a></td></tr>' for i in range(3, 16)
) + """
</tbody></table>
<table class="table ranking trends collapse" id="moreTrends"><tbody>
  """ + "".join(
    f'<tr><th>{i}</th><td><a href="/brazil/trend/Termo{i}/">Termo{i}</a></td></tr>' for i in range(16, 51)
) + """
</tbody></table>
</div>
<h3>Longest trending</h3>
<table class="table ranking trends"><tbody><tr><td><a href="/brazil/trend/%23Outra/">#Outra</a> 18 hours</td></tr>
""" + "".join(f'<tr><td><a href="/brazil/trend/Dia{i}/">Dia{i}</a></td></tr>' for i in range(11)) + """
</tbody></table>
</body></html>
"""


def test_normalize():
    assert radar.normalize("#ShampooChallenge") == "shampoochallenge"
    assert radar.normalize("  São   Paulo ") == "sao paulo"
    assert radar.normalize("ÁLBUM_NOVO") == "album novo"


def test_parse_count():
    assert radar.parse_count("136.9", "K") == 136900
    assert radar.parse_count("1,2", "M") == 1200000
    assert radar.parse_count("12,345", None) == 12345


def test_parse_getdaytrends_joins_both_tables_in_trends_region():
    out = radar.parse_getdaytrends(HTML)
    assert len(out) == 50
    assert out[0] == {"term": "#RoçaAFazenda", "rank": 1, "tweet_count": 136900}
    assert out[1]["tweet_count"] == 12345
    assert out[15]["term"] == "Termo16" and out[15]["rank"] == 16
    assert out[49]["term"] == "Termo50"
    names = {t["term"] for t in out}
    assert "#Outra" not in names and "Dia1" not in names


def test_parse_getdaytrends_fallback_without_region():
    html = HTML.replace('id="trends"', 'id="outro"')
    out = radar.parse_getdaytrends(html)
    assert out[0]["term"] == "#RoçaAFazenda" and len(out) == 50


def _db():
    path = Path(tempfile.mkdtemp()) / "t.db"
    return radar.connect(path)


def test_snapshot_keeps_best_rank():
    con = _db()
    radar.save_snapshot(con, "2026-11-01T10:00Z", [{"term": "Shampoo", "rank": 20}], "a")
    radar.save_snapshot(con, "2026-11-01T10:00Z", [{"term": "shampoo", "rank": 5}], "a")
    assert con.execute("SELECT rank, term FROM snapshots").fetchall() == [(5, "shampoo")]


def test_episodes_split_on_gap():
    rows = [("2026-11-01T10:00Z", 5), ("2026-11-01T11:00Z", 3), ("2026-11-01T14:00Z", 9),
            ("2026-11-01T20:00Z", 40)]
    eps = radar.episodes_for(rows)
    assert [(e["start"], e["hours"], e["best_rank"]) for e in eps] == [
        ("2026-11-01T10:00Z", 3, 3), ("2026-11-01T20:00Z", 1, 40)]


def test_search_contains_vs_exact_and_window():
    con = _db()
    now = dt.datetime(2026, 11, 13, 12, tzinfo=dt.timezone.utc)
    radar.save_snapshot(con, "2026-11-10T10:00Z", [{"term": "Shampoo", "rank": 3}, {"term": "Cabelo", "rank": 4}], "a")
    radar.save_snapshot(con, "2026-11-10T11:00Z", [{"term": "#ShampooChallenge", "rank": 1}], "a")
    radar.save_snapshot(con, "2026-01-01T10:00Z", [{"term": "Shampoo", "rank": 1}], "a")  # fora de 180 dias
    r = radar.search(con, "shampoo", now=now)
    assert {v["term"] for v in r["variants"]} == {"Shampoo", "#ShampooChallenge"}
    sh = next(v for v in r["variants"] if v["term"] == "Shampoo")
    assert sh["hours"] == 1 and sh["best_rank"] == 3
    assert sh["episodes"][0]["co_trends"] == ["Cabelo"]
    assert "since%3A2026-11-10" in sh["episodes"][0]["x_search"]
    r2 = radar.search(con, "shampoo", exact=True, now=now)
    assert [v["term"] for v in r2["variants"]] == ["Shampoo"]


def test_export_roundtrip(tmp_path=None):
    con = _db()
    radar.save_snapshot(con, "2026-11-10T10:00Z", [{"term": "A", "rank": 1}, {"term": "B", "rank": 2}], "a")
    radar.save_snapshot(con, "2026-11-10T12:00Z", [{"term": "B", "rank": 1}], "a")
    out = Path(tempfile.mkdtemp()) / "i.json"
    info = radar.export(con, out)
    import json
    js = json.loads(out.read_text())
    assert info["hours"] == 3 and js["terms"] == ["A", "B"]
    assert js["h"] == [[0, 1], [], [1]]


def test_csv_store_roundtrip(monkeypatch=None):
    d = Path(tempfile.mkdtemp())
    radar.SNAP_DIR, radar.CTX_FILE = d / "snap", d / "ctx.jsonl"
    radar.append_csv("2026-11-10T10:00Z", [{"term": "Shampoo", "rank": 9}], "x_api")
    radar.append_csv("2026-11-10T10:00Z", [{"term": "Shampoo", "rank": 2, "tweet_count": 5000}], "x_api")
    radar.append_csv("2026-11-11T03:00Z", [{"term": "Pix", "rank": 1}], "x_api")
    radar.append_context("shampoo", "2026-11-10T10:00Z", '"Shampoo" Porque teste', [])
    assert radar.csv_hours() == {"2026-11-10T10:00Z", "2026-11-11T03:00Z"}
    con = radar.build_db(d / "t.db")
    assert con.execute("SELECT rank, tweet_count FROM snapshots WHERE norm='shampoo'").fetchall() == [(2, 5000)]
    assert con.execute("SELECT summary FROM contexts").fetchone()[0] == '"Shampoo" Porque teste'


def test_parse_real_page_fixture():
    html = (Path(__file__).parent / "fixtures" / "brazil_3h_real.html").read_text(encoding="utf-8")
    out = radar.parse_getdaytrends(html)
    assert len(out) == 50
    assert out[0]["term"] == "James Bond" and out[15]["term"] == "Showgirl" and out[49]["term"] == "Beyonce"

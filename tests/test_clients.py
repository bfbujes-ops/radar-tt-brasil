import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clients as CL  # noqa: E402
import context as C  # noqa: E402

CFG = CL.load_cfg()


def names(text, client):
    return [h["nome"] for h in CL.find_hits(C.norm(text), CFG[client])]


def test_cebolinha_liga_outback_e_huggies():
    assert "Bloomin' Onion" in names("Everton Cebolinha", "outback")
    assert "Turma da Mônica" in names("Everton Cebolinha", "huggies")


def test_falsos_positivos_corrigidos():
    assert names("Da rotina em casa à decisão na Série B", "yoki") == []
    assert names("A iraniana acusada de declarar guerra a Deus", "sonho-de-valsa") == []
    assert names("Torcedores do São João del-Rei celebram acesso", "yoki") == []


def test_acertos_por_marca():
    assert "Pipoca de micro-ondas" in names("Cinema lança pipoca sabor churros", "yoki")
    assert "Ketchup" in names("Brigaram por causa do ketchup", "heinz")
    assert "Declare-se" in names("Ator fez pedido de casamento ao vivo", "sonho-de-valsa")


def test_risco():
    assert CL.RX_SENS.search(C.norm("Acidente deixa mortos"))
    assert not CL.RX_SENS.search(C.norm("Cebolinha brilha no Santos"))


def test_ruido_de_noticias():
    assert CL.is_noise("How Many Shares of Coca-Cola or PepsiCo Pay $1,000 a Month in Dividends?", "TIKR.com")
    assert CL.is_noise("Previsão semanal das ações da PepsiCo: risco de recuperação", "Traders Union")
    assert not CL.is_noise("Heinz e Tabasco lançam ketchup picante no Brasil", "EP Grupo")
    nt = CFG["heinz"]["noticiario"]
    assert CL.compile_rx(nt["excluir"]).search(C.norm("Uli Hoeneß e Karl-Heinz Rummenigge se incomodavam"))
    assert not CL.compile_rx(CFG["sonho-de-valsa"]["noticiario"]["marca"]).search(C.norm("Sine de Ouro Branco abre vagas"))

from types import SimpleNamespace

from app.main import _instrument_label


def test_btc():
    assert _instrument_label(SimpleNamespace(question="Will Bitcoin reach $100k?", category="crypto")) == "BTC"


def test_eth():
    assert _instrument_label(SimpleNamespace(question="Will Ethereum dip to $2000?", category="crypto")) == "ETH"


def test_eurusd():
    assert _instrument_label(SimpleNamespace(question="Will EUR/USD hit 1.20?", category="forex")) == "EURUSD"


def test_xau():
    assert _instrument_label(SimpleNamespace(question="Will XAUUSD print 2700?", category="forex")) == "XAU"

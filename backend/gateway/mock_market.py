"""Synthetic US plates / constituents / fundamentals for mock mode.

Real large caps are mixed with synthetic small and mid caps (codes US.MKxxyy) so the
size filter for sector leaders has something to filter out.
"""
from __future__ import annotations

import math
import random
import zlib

INDUSTRY = [
    ("半导体", ["NVDA", "AMD", "AVGO", "INTC", "MU", "QCOM", "TXN", "ADI", "MRVL", "ON"]),
    ("软件", ["MSFT", "ORCL", "CRM", "ADBE", "NOW", "INTU", "PLTR", "SNOW"]),
    ("互联网服务", ["GOOGL", "META", "NFLX", "PINS", "SNAP", "BABA", "BIDU"]),   # incl. China-concept ADRs
    ("消费电子", ["AAPL", "SONO", "GPRO"]),
    ("电动汽车", ["TSLA", "RIVN", "LCID", "NIO"]),
    ("银行", ["JPM", "BAC", "WFC", "C", "GS", "MS"]),
    ("保险", ["BRK.B", "PGR", "TRV", "ALL"]),
    ("生物科技", ["AMGN", "GILD", "VRTX", "REGN", "MRNA"]),
    ("制药", ["LLY", "JNJ", "PFE", "MRK", "ABBV", "RHHBY"]),   # RHHBY trades OTC: OpenD will not quote it
    ("医疗设备", ["ISRG", "SYK", "BSX", "MDT"]),
    ("石油天然气", ["XOM", "CVX", "COP", "OXY", "EOG"]),
    ("零售", ["WMT", "COST", "TGT", "HD", "LOW"]),
    ("航空", ["DAL", "UAL", "AAL", "LUV"]),
    ("电力公用事业", ["NEE", "DUK", "SO", "AEP"]),
    ("数据中心 REITs", ["EQIX", "DLR", "AMT"]),
    ("通信设备", ["CSCO", "ANET", "JNPR"]),
    ("燃料电池", ["BE", "PLUG", "FCEL"]),
    ("航天军工", ["LMT", "RTX", "NOC", "GD"]),
]
CONCEPT = [
    ("人工智能", ["NVDA", "MSFT", "GOOGL", "META", "PLTR", "AMD", "SMCI"]),
    ("半导体精选", ["NVDA", "AVGO", "AMD", "MU", "TSM", "ASML"]),
    ("新能源", ["TSLA", "BE", "PLUG", "ENPH", "FSLR"]),
    ("减肥药", ["LLY", "NVO", "VKTX"]),
    ("网络安全", ["CRWD", "PANW", "ZS", "FTNT"]),
    ("加密货币", ["COIN", "MSTR", "MARA", "RIOT"]),
    ("核电", ["CEG", "VST", "OKLO", "SMR"]),
    ("热门中概股", ["BABA", "PDD", "NIO", "BIDU", "JD", "LI", "XPEV", "TCEHY", "BILI", "NTES", "HUYA"]),  # China-themed
    ("腾讯概念", ["TCEHY", "BILI", "NTES", "HUYA", "SPOT"]),        # mostly China-concept -> hidden too
    ("云计算", ["AMZN", "MSFT", "GOOGL", "ORCL", "NET"]),
]
BIG_CAPS = {
    "NVDA": 4.1e12, "MSFT": 3.8e12, "AAPL": 3.9e12, "GOOGL": 3.2e12, "AMZN": 2.5e12, "META": 1.8e12,
    "AVGO": 1.6e12, "TSLA": 1.4e12, "BRK.B": 1.0e12, "LLY": 0.85e12, "JPM": 0.8e12, "WMT": 0.8e12,
    "ORCL": 0.7e12, "XOM": 0.48e12, "COST": 0.42e12, "TSM": 1.2e12, "ASML": 0.35e12,
}
ETF_PROXIES = {"US.XLK": "科技", "US.XLF": "金融", "US.XLE": "能源", "US.XLV": "医疗保健", "US.XLY": "可选消费",
               "US.XLP": "必需消费", "US.XLI": "工业", "US.XLU": "公用事业", "US.XLB": "原材料", "US.XLRE": "房地产",
               "US.XLC": "通信服务", "US.SMH": "半导体"}


class MockMarket:
    def __init__(self, seed: int = 20261111):
        rng = random.Random(seed)
        self.plates: dict[str, list[dict]] = {"INDUSTRY": [], "CONCEPT": []}
        self.members: dict[str, list[str]] = {}
        self.bias: dict[str, float] = {}       # code -> today's drift (%)
        self.names: dict[str, str] = {}
        pid = 2000
        for kind, table in (("INDUSTRY", INDUSTRY), ("CONCEPT", CONCEPT)):
            for idx, (name, tickers) in enumerate(table):
                pid += 1
                code = f"US.LIST{pid}"
                self.plates[kind].append({"code": code, "plate_name": name, "plate_id": f"LIST{pid}"})
                sector_bias = rng.uniform(-3.0, 3.0)
                members = [f"US.{t}" for t in tickers]
                for j in range(rng.randint(5, 9)):
                    members.append(f"US.MK{(pid % 100):02d}{j:02d}")
                for c in members:
                    self.bias.setdefault(c, sector_bias + rng.gauss(0, 1.6))
                    self.names.setdefault(c, c.split(".", 1)[1])
                self.members[code] = members
        self.etf_bias = {c: rng.uniform(-2, 2) for c in ETF_PROXIES}

    def is_plate(self, code: str) -> bool:
        return code in self.members

    @staticmethod
    def market_cap(code: str) -> float:
        sym = code.split(".", 1)[1]
        if sym in BIG_CAPS:
            return BIG_CAPS[sym]
        h = zlib.crc32(code.encode())
        if sym.startswith("MK"):  # synthetic: 0.2B .. 12B, so some fail the 2B filter
            return math.exp(math.log(2e8) + (h % 1000) / 1000 * math.log(60))
        return math.exp(math.log(5e9) + (h % 1000) / 1000 * math.log(60))   # 5B .. 300B

    @staticmethod
    def turnover_rate(code: str) -> float:
        h = zlib.crc32(code.encode()) >> 3
        return 0.002 + (h % 1000) / 1000 * 0.025   # 0.2% .. 2.7% of market cap per day

    def drift(self, code: str) -> float:
        return self.bias.get(code, self.etf_bias.get(code, (zlib.crc32(code.encode()) % 400 - 200) / 100))

"""설정 로딩. YAML + 환경변수. API 키는 절대 YAML 에 두지 않는다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

import yaml

from .adapt import PromotionRules, WalkForwardConfig
from .fees import CostModel
from .risk import RiskParams
from .session import SessionParams
from .strategy import StrategyParams
from .universe import UniverseParams


@dataclass
class EngineParams:
    markets: list[str] = field(default_factory=lambda: ["KRW-BTC", "KRW-ETH", "KRW-SOL"])
    candle_unit: int = 5              # 분봉 단위
    candle_count: int = 200
    poll_seconds: int = 30            # 루프 주기
    initial_krw: float = 1_000_000.0  # paper 모드 초기 자본
    db_path: str = "data/ubbit.db"
    log_level: str = "INFO"
    kill_switch_file: str = ".KILL"
    dynamic_slippage: bool = True     # 주문 직전 호가창으로 슬리피지 실측
    liquidity_min_value_krw: float = 100_000_000.0  # 최근 봉 거래대금 하한
    universe_auto: bool = False       # True 면 markets 를 무시하고 자동 선별
    universe_refresh_minutes: int = 60
    adaptive_params: bool = False     # data/params.json 의 승격 파라미터 사용
    param_file: str = "data/params.json"


@dataclass
class Config:
    mode: str = "paper"               # paper | live
    engine: EngineParams = field(default_factory=EngineParams)
    cost: CostModel = field(default_factory=CostModel)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    session: SessionParams = field(default_factory=SessionParams)
    universe: UniverseParams = field(default_factory=UniverseParams)
    walk_forward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    promotion: PromotionRules = field(default_factory=PromotionRules)
    access_key: str = ""
    secret_key: str = ""

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def _build(cls: type, data: dict[str, Any] | None):
    if not data:
        return cls()
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{cls.__name__} 에 알 수 없는 설정 키: {sorted(unknown)}")
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | None = None) -> Config:
    raw: dict[str, Any] = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    cfg = Config(
        mode=raw.get("mode", "paper"),
        engine=_build(EngineParams, raw.get("engine")),
        cost=_build(CostModel, raw.get("cost")),
        strategy=_build(StrategyParams, raw.get("strategy")),
        risk=_build(RiskParams, raw.get("risk")),
        session=_build(SessionParams, raw.get("session")),
        universe=_build(UniverseParams, raw.get("universe")),
        walk_forward=_build(WalkForwardConfig, raw.get("walk_forward")),
        promotion=_build(PromotionRules, raw.get("promotion")),
        access_key=os.getenv("UPBIT_ACCESS_KEY", ""),
        secret_key=os.getenv("UPBIT_SECRET_KEY", ""),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.mode not in ("paper", "live"):
        raise ValueError(f"mode 는 paper 또는 live: {cfg.mode}")
    if cfg.is_live and not (cfg.access_key and cfg.secret_key):
        raise ValueError("live 모드에는 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 환경변수가 필요합니다.")
    if cfg.cost.breakeven_edge <= 0:
        raise ValueError("비용 모델이 잘못되었습니다. 손익분기가 0 이하입니다.")
    if cfg.strategy.min_net_target <= cfg.cost.breakeven_edge:
        raise ValueError(
            f"min_net_target({cfg.strategy.min_net_target:.4%})가 손익분기"
            f"({cfg.cost.breakeven_edge:.4%}) 이하입니다. 구조적으로 이길 수 없는 설정입니다."
        )
    if not cfg.engine.markets and not cfg.engine.universe_auto:
        raise ValueError("engine.markets 가 비어 있습니다. (universe_auto: true 를 쓰거나 종목을 지정하세요)")
    for market in cfg.engine.markets:
        if not market.startswith("KRW-"):
            raise ValueError(f"KRW 마켓만 지원합니다(수수료 0.05% 전제): {market}")
    if cfg.session.enabled:
        for label, value in (("start", cfg.session.start), ("end", cfg.session.end)):
            try:
                hour, minute = value.split(":")
                if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                    raise ValueError
            except ValueError:
                raise ValueError(f"session.{label} 형식 오류(HH:MM): {value}") from None
        if cfg.session.start >= cfg.session.end:
            raise ValueError(f"session.start({cfg.session.start}) 가 end({cfg.session.end}) 이상입니다.")
        cfg.session.days = tuple(cfg.session.days)
    # 일일 회전수 상한이 비용 드래그 한도를 넘는지 사전 경고
    drag = cfg.cost.drag_per_day(cfg.risk.daily_trade_limit)
    if drag > cfg.risk.daily_loss_limit:
        raise ValueError(
            f"daily_trade_limit({cfg.risk.daily_trade_limit}회)의 확정 비용 드래그 {drag*100:.2f}%가 "
            f"일일 손실한도 {cfg.risk.daily_loss_limit*100:.1f}%를 초과합니다. "
            f"알파가 0이면 손실한도에 먼저 닿습니다. 회전수를 줄이거나 한도를 올리세요."
        )
    if cfg.risk.risk_per_trade > 0.02:
        raise ValueError("risk_per_trade 2% 초과는 허용하지 않습니다.")
    if cfg.risk.max_total_exposure > 1.0:
        raise ValueError("max_total_exposure 는 1.0 이하여야 합니다.")

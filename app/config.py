from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
CONFIG_FILE = ROOT_DIR / "ts2db_config.yaml"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
THEMES_FILE = DATA_DIR / "themes.yaml"
RULES_FILE = DATA_DIR / "trading_rules.yaml"
POSITION_FILE = DATA_DIR / "positions.json"
INTRADAY_WATCHTOWER_DB_FILE = DATA_DIR / "runtime" / "intraday_watchtower.sqlite"
AUCTION_HISTORY_FILE = DATA_DIR / "runtime" / "auction_snapshots.jsonl"
OPENING_DECISION_FILE = DATA_DIR / "runtime" / "opening_decisions.jsonl"
WEBHOOK_SUBSCRIPTIONS_FILE = DATA_DIR / "webhook_subscriptions.json"
USER_STATE_FILE = DATA_DIR / "runtime" / "principal_state.json"


def load_yaml(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return default or {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return default or {}
    return data


class AppSettings:
    def __init__(self) -> None:
        self.root_dir = ROOT_DIR
        self.data_dir = DATA_DIR
        self.config_file = Path(os.getenv("WATCH_CONFIG_FILE", str(CONFIG_FILE)))
        self.watchlist_file = Path(os.getenv("WATCHLIST_FILE", str(WATCHLIST_FILE)))
        self.themes_file = Path(os.getenv("THEMES_FILE", str(THEMES_FILE)))
        self.rules_file = Path(os.getenv("TRADING_RULES_FILE", str(RULES_FILE)))
        self.position_file = Path(os.getenv("WATCH_POSITION_FILE", str(POSITION_FILE)))
        # 飞书 webhook 信号推送：订阅持久化文件 + 同票推送去重窗口 + 投递超时
        self.webhook_subscriptions_file = Path(
            os.getenv("WATCH_WEBHOOK_SUBSCRIPTIONS_FILE", str(WEBHOOK_SUBSCRIPTIONS_FILE))
        )
        self.push_signal_dedup_seconds = max(30, int(os.getenv("WATCH_PUSH_SIGNAL_DEDUP_SECONDS", "300")))
        self.push_http_timeout_seconds = max(1.0, float(os.getenv("WATCH_PUSH_HTTP_TIMEOUT_SECONDS", "6.0")))
        self.persistence_backend = os.getenv("WATCH_PERSISTENCE_BACKEND", "local").strip().lower() or "local"
        self.cloudbase_env_id = os.getenv("WATCH_CLOUDBASE_ENV_ID", "").strip()
        self.cloudbase_api_token = os.getenv("WATCH_CLOUDBASE_API_TOKEN", "").strip()
        self.message_store_backend = (
            os.getenv("WATCH_MESSAGE_STORE_BACKEND", "cloudbase_mysql").strip().lower() or "cloudbase_mysql"
        )
        # Principal-scoped personal state.  Local development uses the atomic
        # JSON backend; production sets WATCH_USER_STORE_BACKEND=mysql and
        # points at the dedicated watchtower_user RDS database.
        self.user_store_backend = os.getenv("WATCH_USER_STORE_BACKEND", "json").strip().lower() or "json"
        self.user_store_file = Path(os.getenv("WATCH_USER_STORE_FILE", str(USER_STATE_FILE)))
        self.user_mysql_host = os.getenv("WATCH_USER_MYSQL_HOST", "").strip()
        self.user_mysql_port = int(os.getenv("WATCH_USER_MYSQL_PORT", "3306"))
        self.user_mysql_user = os.getenv("WATCH_USER_MYSQL_USER", "").strip()
        self.user_mysql_pwd = os.getenv("WATCH_USER_MYSQL_PWD", "")
        self.user_mysql_db = os.getenv("WATCH_USER_MYSQL_DB", "watchtower_user").strip() or "watchtower_user"
        self.user_mysql_connect_timeout = max(
            0.1, float(os.getenv("WATCH_USER_MYSQL_CONNECT_TIMEOUT", "5"))
        )
        self.user_mysql_pool_size = max(1, int(os.getenv("WATCH_USER_MYSQL_POOL_SIZE", "4")))
        self.cloudbase_mysql_instance = os.getenv("WATCH_CLOUDBASE_MYSQL_INSTANCE", "default").strip() or "default"
        self.cloudbase_mysql_schema = (
            os.getenv("WATCH_CLOUDBASE_MYSQL_SCHEMA", self.cloudbase_env_id).strip() or self.cloudbase_env_id
        )
        self.cloudbase_mysql_openid = os.getenv("WATCH_CLOUDBASE_MYSQL_OPENID", "watchtower").strip() or "watchtower"
        # 消息存储直连 MySQL（WATCH_MESSAGE_STORE_BACKEND=mysql，目标阿里云 RDS）：
        # 连接参数只走环境变量，不落 ts2db_config.yaml。
        self.msg_mysql_host = os.getenv("WATCH_MSG_MYSQL_HOST", "").strip()
        self.msg_mysql_port = int(os.getenv("WATCH_MSG_MYSQL_PORT", "3306"))
        self.msg_mysql_user = os.getenv("WATCH_MSG_MYSQL_USER", "").strip()
        self.msg_mysql_pwd = os.getenv("WATCH_MSG_MYSQL_PWD", "")
        self.msg_mysql_db = os.getenv("WATCH_MSG_MYSQL_DB", "").strip()
        self.cloudbase_state_collection = (
            os.getenv("WATCH_CLOUDBASE_STATE_COLLECTION", "watchtower_state").strip() or "watchtower_state"
        )
        self.cloudbase_database_instance = os.getenv("WATCH_CLOUDBASE_DATABASE_INSTANCE", "(default)").strip() or "(default)"
        self.cloudbase_database_name = os.getenv("WATCH_CLOUDBASE_DATABASE_NAME", "(default)").strip() or "(default)"
        self.cloudbase_api_base_url = os.getenv("WATCH_CLOUDBASE_API_BASE_URL", "").strip()
        # CloudBase REST 网关偶发慢请求（消息表全表 count 经常 >3s），
        # 3s 超时会把状态接口拖成全 0、把批量写入拖成 500，默认放宽到 15s。
        self.cloudbase_api_timeout_seconds = max(0.5, float(os.getenv("WATCH_CLOUDBASE_API_TIMEOUT_SECONDS", "15.0")))
        # 星球消息证据/状态读缓存：消息按批次推送入库，读侧不需要秒级新鲜度，
        # 默认 60s，避免每次打开详情都对 CloudBase MySQL REST 做全量查询风暴。
        self.message_store_cache_seconds = max(0.0, float(os.getenv("WATCH_MESSAGE_STORE_CACHE_SECONDS", "60")))
        self.intraday_watchtower_db_file = Path(
            os.getenv("WATCH_INTRADAY_DB_FILE", str(INTRADAY_WATCHTOWER_DB_FILE))
        )
        self.trajectory_enabled = os.getenv("WATCH_TRAJECTORY_ENABLED", "1").lower() in {"1", "true", "yes"}
        self.trajectory_cleanup_on_start = os.getenv("WATCH_TRAJECTORY_CLEANUP_ON_START", "1").lower() in {
            "1",
            "true",
            "yes",
        }
        self.trajectory_retention_trade_days = max(
            1,
            int(os.getenv("WATCH_TRAJECTORY_RETENTION_TRADE_DAYS", "1")),
        )
        # The backend owns collection so losing every browser connection does
        # not create a hole in the intraday trajectory.
        self.background_collector_enabled = os.getenv("WATCH_BACKGROUND_COLLECTOR", "1").lower() in {
            "1",
            "true",
            "yes",
        }
        self.background_collector_seconds = max(5, int(os.getenv("WATCH_BACKGROUND_COLLECTOR_SECONDS", "10")))
        self.data_mode = os.getenv("WATCH_DATA_MODE", "auto").lower()
        self.scan_scope = os.getenv("WATCH_SCAN_SCOPE", "full_market").lower()
        self.include_watchlist_in_scan = os.getenv("WATCH_INCLUDE_WATCHLIST", "0").lower() in {"1", "true", "yes"}
        # Synthetic replay remains available for explicit testing, but an
        # automatic production fallback must never look like real market data.
        self.allow_replay_fallback = os.getenv("WATCH_ALLOW_REPLAY_FALLBACK", "0").lower() in {"1", "true", "yes"}
        # Keep the terminal responsive during the session while allowing operators
        # to relax polling for a rate-limited data source.
        self.full_market_refresh_seconds = max(1, int(os.getenv("WATCH_FULL_MARKET_REFRESH_SECONDS", "2")))
        self.minute_series_live_cache_seconds = max(1, int(os.getenv("WATCH_MINUTE_SERIES_LIVE_CACHE_SECONDS", "2")))
        self.minute_series_static_cache_seconds = int(os.getenv("WATCH_MINUTE_SERIES_STATIC_CACHE_SECONDS", "3600"))
        self.terminal_context_live_cache_seconds = max(1, int(os.getenv("WATCH_TERMINAL_CONTEXT_LIVE_CACHE_SECONDS", "2")))
        # 盘中 terminal/dashboard 载荷共享缓存 TTL：所有 WS 连接/HTTP 请求共用
        # 同一次构建结果，避免每个连接每秒各自重算整份载荷。冻结/静态期不加
        # TTL，缓存随 context 签名或写操作失效。
        self.terminal_payload_live_cache_seconds = max(
            0.2,
            float(os.getenv("WATCH_TERMINAL_PAYLOAD_LIVE_CACHE_SECONDS", "1.0")),
        )
        self.terminal_context_frozen_cache_seconds = int(os.getenv("WATCH_TERMINAL_CONTEXT_FROZEN_CACHE_SECONDS", "300"))
        self.stream_live_interval_seconds = max(1, int(os.getenv("WATCH_STREAM_LIVE_INTERVAL_SECONDS", "1")))
        self.stream_static_interval_seconds = max(5, int(os.getenv("WATCH_STREAM_STATIC_INTERVAL_SECONDS", "30")))
        # WS 广播模式：同一参数组合（频道）只跑一个发布者循环，构建/序列化/delta
        # 计算全局一次，每连接只剩队列取字符串发送。0 时回退到每连接轮询旧实现。
        self.stream_broadcaster_enabled = os.getenv("WATCH_STREAM_BROADCASTER", "1").lower() in {
            "1",
            "true",
            "yes",
        }
        # 每订阅者队列长度：慢客户端队列满则投 Resync 哨兵，退化为重发全量快照，
        # 不拖垮频道内其他连接。
        self.stream_queue_size = max(2, int(os.getenv("WATCH_STREAM_QUEUE_SIZE", "8")))
        # 频道无订阅者后的回收宽限期：参数抖动（翻页/切板块）不至于频繁重建频道。
        self.stream_channel_idle_seconds = max(1, int(os.getenv("WATCH_STREAM_CHANNEL_IDLE_SECONDS", "30")))
        # 频道总数上限：超出时新连接回退到每连接轮询，防止异常参数组合打爆内存。
        self.stream_max_channels = max(8, int(os.getenv("WATCH_STREAM_MAX_CHANNELS", "256")))
        self.visible_quote_refresh_budget_ms = max(0, int(os.getenv("WATCH_VISIBLE_QUOTE_REFRESH_BUDGET_MS", "120")))
        self.visible_quote_cache_seconds = max(0.5, float(os.getenv("WATCH_VISIBLE_QUOTE_CACHE_SECONDS", "2.0")))
        self.visible_quote_min_interval_seconds = max(
            0.2,
            float(os.getenv("WATCH_VISIBLE_QUOTE_MIN_INTERVAL_SECONDS", "0.8")),
        )
        self.visible_quote_max_codes = max(20, int(os.getenv("WATCH_VISIBLE_QUOTE_MAX_CODES", "48")))
        self.sector_flow_refresh_seconds = max(5, int(os.getenv("WATCH_SECTOR_FLOW_REFRESH_SECONDS", "10")))
        # 盘中板块资金动能：分钟线后台重建的最低间隔（秒），快照代理曲线不受此限制。
        self.sector_flow_minute_refresh_seconds = max(
            15,
            int(os.getenv("WATCH_SECTOR_FLOW_MINUTE_REFRESH_SECONDS", "60")),
        )
        # 分钟线重建时的并发拉取线程数（每线程独立 TdxClient 连接）。
        self.sector_flow_workers = max(1, min(8, int(os.getenv("WATCH_SECTOR_FLOW_WORKERS", "4"))))
        self.board_static_cache_seconds = int(os.getenv("WATCH_BOARD_STATIC_CACHE_SECONDS", "86400"))
        self.board_member_cache_seconds = int(os.getenv("WATCH_BOARD_MEMBER_CACHE_SECONDS", "86400"))
        self.board_member_warmup_enabled = os.getenv("WATCH_BOARD_MEMBER_WARMUP", "1").lower() in {
            "1",
            "true",
            "yes",
        }
        self.easy_tdx_timeout_seconds = float(os.getenv("WATCH_EASY_TDX_TIMEOUT_SECONDS", "1.2"))
        self.easy_tdx_quote_workers = max(1, int(os.getenv("WATCH_EASY_TDX_QUOTE_WORKERS", "12")))
        # 批次快照缺返回个股（停牌/退市）的补拉负缓存冷却期
        self.quote_missing_cache_seconds = int(os.getenv("WATCH_QUOTE_MISSING_CACHE_SECONDS", "1800"))
        self.easy_tdx_f10_timeout_seconds = float(os.getenv("WATCH_EASY_TDX_F10_TIMEOUT_SECONDS", "4.0"))
        self.fundamentals_cache_seconds = int(os.getenv("WATCH_FUNDAMENTALS_CACHE_SECONDS", "3600"))
        # F10 聚合数据（tushare 财务/股东/分红等）属低频变化数据：持久缓存 18h 内直接复用，
        # 盘前 08:40 起对候选池（自选+持仓+近期查看）做一轮增量预热，只刷超过 12h 的缓存。
        self.f10_cache_seconds = int(os.getenv("WATCH_F10_CACHE_SECONDS", "64800"))
        self.f10_preopen_refresh = os.getenv("WATCH_F10_PREOPEN_REFRESH", "1").lower() in {
            "1",
            "true",
            "yes",
        }
        self.f10_preopen_time = os.getenv("WATCH_F10_PREOPEN_TIME", "08:40").strip() or "08:40"
        self.f10_preopen_window_minutes = max(5, int(os.getenv("WATCH_F10_PREOPEN_WINDOW_MINUTES", "25")))
        self.f10_preopen_max_codes = max(1, int(os.getenv("WATCH_F10_PREOPEN_MAX_CODES", "80")))
        self.f10_preopen_stale_seconds = int(os.getenv("WATCH_F10_PREOPEN_STALE_SECONDS", "43200"))
        self.transaction_cache_seconds = int(os.getenv("WATCH_TRANSACTION_CACHE_SECONDS", "5"))
        # 全日逐笔磁带缓存：历史/收盘后磁带不可变，长 TTL 直接复用；
        # 盘中超过 transaction_cache_seconds 后走增量对齐补新，不再整段重拉。
        self.transaction_tape_static_cache_seconds = int(os.getenv("WATCH_TRANSACTION_TAPE_STATIC_CACHE_SECONDS", "1800"))
        self.transaction_tape_max_ticks = max(1800, int(os.getenv("WATCH_TRANSACTION_TAPE_MAX_TICKS", str(8 * 1800))))
        self.transaction_rows = int(os.getenv("WATCH_TRANSACTION_ROWS", "240"))
        self.auction_history_max_points = int(os.getenv("WATCH_AUCTION_HISTORY_MAX_POINTS", "180"))
        self.auction_history_file = Path(os.getenv("WATCH_AUCTION_HISTORY_FILE", str(AUCTION_HISTORY_FILE)))
        # 暗盘资金模块：官方口径走 EOD 访问层（app/eod_store.py，整包 300s TTL），
        # 本地默认 pymysql 直连 MySQL（watchtower_eod 库，db_config 可覆盖），
        # 生产云托管 WATCH_EOD_STORE_BACKEND=cloudbase_snapshot 读 NoSQL 预计算快照；
        # 盘中资金地图走东财快照缓存（WATCH_EM_MONEYFLOW=0 关闭，TTL 见 em_moneyflow.py）；
        # 均与 5 秒大盘刷新循环完全隔离；WATCH_DARK_POOL=0 可整体关闭。
        self.dark_pool_enabled = os.getenv("WATCH_DARK_POOL", "1").lower() in {"1", "true", "yes"}
        self.eod_store_backend = os.getenv("WATCH_EOD_STORE_BACKEND", "mysql").strip().lower() or "mysql"
        self.opening_decision_file = Path(
            os.getenv("WATCH_OPENING_DECISION_FILE", str(OPENING_DECISION_FILE))
        )
        self.max_signals_per_group = int(os.getenv("WATCH_MAX_SIGNALS_PER_GROUP", "0"))
        self.host = os.getenv("WATCH_HOST", "127.0.0.1")
        self.port = int(os.getenv("WATCH_PORT", "8788"))

    @property
    def secret_config(self) -> dict[str, Any]:
        return load_yaml(self.config_file, {})

    @property
    def eod_db_config(self) -> dict[str, Any]:
        """本地 MySQL 连接配置：ts2db_config.yaml 的 db_config，环境变量优先。"""
        raw = self.secret_config.get("db_config")
        cfg = dict(raw) if isinstance(raw, dict) else {}
        return {
            "host": os.getenv("WATCH_EOD_MYSQL_HOST", str(cfg.get("host") or "127.0.0.1")).strip(),
            "port": int(os.getenv("WATCH_EOD_MYSQL_PORT", str(cfg.get("port") or 3306))),
            "user": os.getenv("WATCH_EOD_MYSQL_USER", str(cfg.get("user") or "root")).strip(),
            "pwd": os.getenv("WATCH_EOD_MYSQL_PWD", str(cfg.get("pwd") or cfg.get("password") or "")),
            "db": os.getenv("WATCH_EOD_MYSQL_DB", str(cfg.get("db") or "watchtower_eod")).strip(),
        }

    @property
    def msg_mysql_config(self) -> dict[str, Any]:
        """消息存储直连 MySQL 配置（阿里云 RDS），对应实例属性在 __init__ 读取环境变量。"""
        return {
            "host": self.msg_mysql_host,
            "port": self.msg_mysql_port,
            "user": self.msg_mysql_user,
            "pwd": self.msg_mysql_pwd,
            "db": self.msg_mysql_db,
        }

    @property
    def user_mysql_config(self) -> dict[str, Any]:
        """Principal-state RDS connection settings (password stays private)."""

        return {
            "host": self.user_mysql_host,
            "port": self.user_mysql_port,
            "user": self.user_mysql_user,
            "pwd": self.user_mysql_pwd,
            "db": self.user_mysql_db,
        }

    @property
    def public_source_status(self) -> dict[str, Any]:
        secrets = self.secret_config
        analysis_provider = self._analysis_provider(secrets)
        return {
            "config_file": str(self.config_file),
            "has_news_key": bool(secrets.get("news_api_key")),
            "has_ai_interface": bool(analysis_provider),
            "analysis_provider": analysis_provider or "none",
            "data_mode": self.data_mode,
            "scan_scope": self.scan_scope,
            "include_watchlist_in_scan": self.include_watchlist_in_scan,
            "allow_replay_fallback": self.allow_replay_fallback,
            "full_market_refresh_seconds": self.full_market_refresh_seconds,
            "minute_series_live_cache_seconds": self.minute_series_live_cache_seconds,
            "terminal_context_live_cache_seconds": self.terminal_context_live_cache_seconds,
            "terminal_payload_live_cache_seconds": self.terminal_payload_live_cache_seconds,
            "terminal_context_frozen_cache_seconds": self.terminal_context_frozen_cache_seconds,
            "stream_live_interval_seconds": self.stream_live_interval_seconds,
            "stream_static_interval_seconds": self.stream_static_interval_seconds,
            "stream_broadcaster_enabled": self.stream_broadcaster_enabled,
            "stream_queue_size": self.stream_queue_size,
            "stream_channel_idle_seconds": self.stream_channel_idle_seconds,
            "stream_max_channels": self.stream_max_channels,
            "visible_quote_refresh_budget_ms": self.visible_quote_refresh_budget_ms,
            "visible_quote_cache_seconds": self.visible_quote_cache_seconds,
            "visible_quote_max_codes": self.visible_quote_max_codes,
            "sector_flow_refresh_seconds": self.sector_flow_refresh_seconds,
            "sector_flow_minute_refresh_seconds": self.sector_flow_minute_refresh_seconds,
            "sector_flow_workers": self.sector_flow_workers,
            "board_static_cache_seconds": self.board_static_cache_seconds,
            "board_member_cache_seconds": self.board_member_cache_seconds,
            "board_member_warmup_enabled": self.board_member_warmup_enabled,
            "easy_tdx_timeout_seconds": self.easy_tdx_timeout_seconds,
            "easy_tdx_quote_workers": self.easy_tdx_quote_workers,
            "easy_tdx_f10_timeout_seconds": self.easy_tdx_f10_timeout_seconds,
            "fundamentals_cache_seconds": self.fundamentals_cache_seconds,
            "f10_cache_seconds": self.f10_cache_seconds,
            "f10_preopen_refresh": self.f10_preopen_refresh,
            "f10_preopen_time": self.f10_preopen_time,
            "f10_preopen_max_codes": self.f10_preopen_max_codes,
            "transaction_cache_seconds": self.transaction_cache_seconds,
            "transaction_tape_static_cache_seconds": self.transaction_tape_static_cache_seconds,
            "transaction_tape_max_ticks": self.transaction_tape_max_ticks,
            "transaction_rows": self.transaction_rows,
            "auction_history_max_points": self.auction_history_max_points,
            "opening_decision_file": str(self.opening_decision_file),
            "max_signals_per_group": self.max_signals_per_group,
            "message_ingest_enabled": bool(self.message_ingest_token),
            "trajectory_enabled": self.trajectory_enabled,
            "trajectory_db_file": str(self.intraday_watchtower_db_file),
            "trajectory_cleanup_on_start": self.trajectory_cleanup_on_start,
            "trajectory_retention_trade_days": self.trajectory_retention_trade_days,
            "background_collector_enabled": self.background_collector_enabled,
            "persistence_backend": self.persistence_backend,
            "message_store_backend": self.message_store_backend,
            "user_store_backend": self.user_store_backend,
            "user_store_db": self.user_mysql_db,
            "user_store_configured": (
                self.user_store_backend == "json"
                or bool(self.user_mysql_host and self.user_mysql_user and self.user_mysql_db)
            ),
            "message_store_configured": (
                bool(self.msg_mysql_host and self.msg_mysql_user and self.msg_mysql_db)
                if self.message_store_backend == "mysql"
                else bool(
                    self.cloudbase_env_id
                    and self.cloudbase_api_token
                    and self.cloudbase_mysql_instance
                    and self.cloudbase_mysql_schema
                )
            ),
            "cloudbase_mysql_instance": self.cloudbase_mysql_instance,
            "cloudbase_mysql_schema": self.cloudbase_mysql_schema,
            "cloud_persistence_configured": bool(
                self.persistence_backend == "cloudbase_nosql"
                and self.cloudbase_env_id
                and self.cloudbase_api_token
            ),
            "cloudbase_env_id": self.cloudbase_env_id,
            "cloudbase_state_collection": self.cloudbase_state_collection,
        }

    @property
    def message_ingest_token(self) -> str:
        token = os.getenv("WATCH_INGEST_TOKEN")
        if token:
            return token
        value = self.secret_config.get("message_ingest_token") or self.secret_config.get("watch_ingest_token")
        return str(value or "")

    def _analysis_provider(self, secrets: dict[str, Any]) -> str | None:
        if secrets.get("cf_base_url") and secrets.get("cf_key"):
            return "cf_proxy"
        if secrets.get("deepseek-key"):
            return "deepseek"
        if secrets.get("zhipu_key"):
            return "zhipu"
        if secrets.get("bailian_key"):
            return "bailian"
        if secrets.get("huoshan_key"):
            return "huoshan"
        return None


settings = AppSettings()

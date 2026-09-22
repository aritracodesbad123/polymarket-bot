-- POLYGROK schema. Reconstructable trades. One SQLite file.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paper_trading_started_at TEXT,
    halted INTEGER NOT NULL DEFAULT 0,
    halt_reason TEXT,
    trading_mode TEXT NOT NULL DEFAULT 'paper',
    live_activated_at TEXT,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    -- Day-scoped AI burn (UTC date + call count). Cost = count * ESTIMATED_USD_PER_AI_CALL.
    ai_calls_utc_day TEXT,
    ai_call_count INTEGER NOT NULL DEFAULT 0,
    -- Weekly equity stop baseline. week_started_on is the Monday (UTC) of that week.
    week_started_on TEXT,
    week_baseline_equity REAL,
    -- Day-scoped realized P&L for the absolute daily-loss cap.
    daily_pnl_utc_day TEXT,
    daily_realized_pnl REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT,
    yes_token_id TEXT,
    no_token_id TEXT,
    question TEXT,
    description TEXT,
    resolution_criteria TEXT,
    close_time TEXT,
    resolution_time TEXT,
    category TEXT,
    event_id TEXT,
    correlation_group TEXT,
    neg_risk INTEGER,
    tick_size TEXT,
    min_order_size TEXT,
    status TEXT,
    raw_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    yes_price REAL,
    no_price REAL,
    midpoint REAL,
    spread REAL,
    volume REAL,
    liquidity REAL,
    best_bid REAL,
    best_ask REAL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    bids_json TEXT NOT NULL,
    asks_json TEXT NOT NULL,
    tick_size TEXT,
    min_order_size TEXT,
    neg_risk INTEGER,
    hash TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS research_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    version TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    estimated_probability REAL,
    confidence TEXT,
    confidence_score REAL,
    should_abstain INTEGER,
    estimate_json TEXT NOT NULL,
    evidence_id INTEGER,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT,
    side TEXT,
    approved INTEGER NOT NULL,
    reject_reason TEXT,
    gates_json TEXT NOT NULL,
    grok_p REAL,
    market_price REAL,
    raw_edge REAL,
    execution_adjusted_edge REAL,
    kelly REAL,
    size_usd REAL,
    size_shares REAL,
    strategy_version TEXT,
    prompt_version TEXT,
    snapshot_id INTEGER,
    prediction_id INTEGER,
    idempotency_key TEXT,
    UNIQUE (idempotency_key)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE,
    idempotency_key TEXT,
    broker TEXT NOT NULL,
    market_id TEXT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size_shares REAL,
    status TEXT NOT NULL,
    decision_id INTEGER,
    remote_order_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    extra_json TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    ts TEXT NOT NULL,
    token_id TEXT,
    side TEXT,
    shares REAL,
    price REAL,
    fee REAL,
    slippage REAL,
    is_partial INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS positions (
    token_id TEXT PRIMARY KEY,
    market_id TEXT,
    shares REAL NOT NULL,
    avg_price REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    category TEXT,
    correlation_group TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cash REAL NOT NULL,
    reserved_cash REAL NOT NULL,
    equity REAL NOT NULL,
    exposure REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    extra_json TEXT
);

CREATE TABLE IF NOT EXISTS resolved_markets (
    market_id TEXT PRIMARY KEY,
    outcome TEXT,
    resolved_at TEXT NOT NULL,
    prediction_id INTEGER,
    brier REAL
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS activation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    operator_confirmation TEXT NOT NULL,
    paper_duration_seconds REAL,
    git_commit TEXT,
    config_hash TEXT,
    strategy_version TEXT,
    prompt_version TEXT,
    risk_config_json TEXT,
    report_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_idem ON orders(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_decisions_market ON trade_decisions(market_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON system_events(kind);
CREATE INDEX IF NOT EXISTS idx_predictions_market ON ai_predictions(market_id);

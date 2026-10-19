"""
NexIntel Model Library — every major financial model family as feature generators.
Each model outputs normalized features [-3, +3]-ish. The row-echelon optimizer
in nexintel.py weights them by realized predictive power. Models that don't
predict get zeroed automatically — no complexity theater.

Families:
  TREND:      MACD proxy, MA alignment, Donchian position
  MOMENTUM:   RSI, rate-of-change, stochastic proxy
  VOLATILITY: Bollinger %B proxy, ATR proxy, vol regime
  MEAN-REV:   z-score, extreme-move fade
  RISK:       VaR proxy, Kelly sizing, drawdown guard
"""

def _clip(x, lo=-3.0, hi=3.0):
    return max(lo, min(hi, x))

# ---------------------------------------------------------------- TREND
def macd_proxy(chg24, chg7d):
    """MACD histogram proxy: fast (24h) minus slow (7d avg). + = bullish cross."""
    return _clip((chg24 - chg7d / 7.0) / 3.0)

def ma_alignment(chg24, chg7d):
    """Are short and long trends aligned? +1 / -1."""
    return 1.0 if (chg24 > 0) == (chg7d > 0) else -1.0

def donchian_pos(chg24):
    """Position within recent range proxy. Extreme + = breakout, - = breakdown."""
    return _clip(chg24 / 8.0)

# ---------------------------------------------------------------- MOMENTUM
def rsi_feature(chg24):
    rs = max(chg24, 0.01) / max(-chg24, 0.01)
    rsi = 100 - (100 / (1 + rs))
    return _clip((rsi - 50) / 25.0)  # oversold < -1, overbought > +1

def roc_feature(chg24, chg7d):
    """Rate of change acceleration: is momentum speeding up?"""
    return _clip((chg24 - chg7d / 7.0) / 4.0)

def stochastic_proxy(chg24):
    """Where in the recent range are we? Simplified %K."""
    return _clip(chg24 / 6.0)

# ---------------------------------------------------------------- VOLATILITY
def bollinger_pctb(chg24):
    """%B proxy: position relative to 2-std band. >1 = above band, <0 = below."""
    return _clip(0.5 + chg24 / 10.0, lo=-0.5, hi=1.5)

def atr_proxy(chg24, chg7d):
    """Average true range proxy: typical daily swing magnitude."""
    return _clip((abs(chg24) + abs(chg7d) / 7.0) / 2.0 / 4.0, lo=0, hi=3.0)

def vol_regime(chg24, hist_vol=5.0):
    """Current vol vs typical. >1 = expanding (danger/opportunity)."""
    cur = abs(chg24)
    return _clip(cur / max(hist_vol, 0.5) - 1.0, lo=-1.0, hi=2.0)

# ---------------------------------------------------------------- MEAN REVERSION
def zscore(chg24, mean=0.0, std=5.0):
    return _clip((chg24 - mean) / max(std, 0.5))

def extreme_fade(chg24, threshold=8.0):
    """Violent moves fade: returns contrarian signal strength."""
    if abs(chg24) < threshold: return 0.0
    return _clip(-chg24 / threshold * 0.5)

# ---------------------------------------------------------------- RISK
def var_proxy(chg24, confidence=0.95):
    """Value-at-risk proxy: expected worst daily loss (negative = risk)."""
    return -_clip(abs(chg24) * 1.65 / 5.0, lo=0, hi=3.0)

def kelly_fraction(win_rate, avg_win, avg_loss):
    """
    Kelly criterion: optimal fraction = (p*b - q) / b.
    Returns 0 if inputs invalid; capped at 0.25 (quarter-Kelly for safety).
    """
    try:
        if not avg_win or avg_win <= 0 or not avg_loss or avg_loss <= 0: return 0.0
        p = win_rate; q = 1 - p; b = avg_win / avg_loss
        f = (p * b - q) / b
        return max(0.0, min(0.25, f))
    except: return 0.0

def drawdown_guard(recent_returns, max_dd=15.0):
    """If recent picks are in drawdown, reduce new position confidence."""
    if not recent_returns: return 1.0
    cum = 0.0; peak = 0.0; dd = 0.0
    for r in recent_returns:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    if dd > max_dd: return 0.5   # halved confidence in deep drawdown
    if dd > max_dd / 2: return 0.8
    return 1.0

# ---------------------------------------------------------------- FEATURE PACK
# Maps to extended feature indices 10-23 in the engine.
MODEL_FEATURES = [
    ('macd', macd_proxy), ('ma_align', ma_alignment), ('donchian', donchian_pos),
    ('rsi', rsi_feature), ('roc', roc_feature), ('stoch', stochastic_proxy),
    ('boll_pctb', bollinger_pctb), ('atr', atr_proxy), ('vol_regime', vol_regime),
    ('zscore', zscore), ('extreme_fade', extreme_fade), ('var', var_proxy),
]

def build_model_features(chg24, chg7d):
    """Returns 12 normalized model features for [chg24, chg7d] input."""
    return [
        macd_proxy(chg24, chg7d),
        ma_alignment(chg24, chg7d),
        donchian_pos(chg24),
        rsi_feature(chg24),
        roc_feature(chg24, chg7d),
        stochastic_proxy(chg24),
        bollinger_pctb(chg24),
        atr_proxy(chg24, chg7d),
        vol_regime(chg24),
        zscore(chg24),
        extreme_fade(chg24),
        var_proxy(chg24),
    ]

MODEL_NAMES = [n for n, _ in MODEL_FEATURES]

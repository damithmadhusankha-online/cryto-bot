"""Chart rendering for Telegram broadcasts (close + EMAs + signal markers)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from strategy import compute_signals


def save_chart(df, symbol, tf, variant="momentum", path="signal_chart.png",
               bars=120):
    df = compute_signals(df, variant=variant)
    df = df.tail(bars)
    if len(df) == 0:
        return None

    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=110)
    ax.plot(df.index, df["Close"], color="#1f77b4", lw=1.4, label="Close")
    ax.plot(df.index, df["ema_fast"], color="#ff7f0e", lw=1.0, label="EMA9")
    ax.plot(df.index, df["ema_mid"], color="#2ca02c", lw=1.0, label="EMA21")
    ax.plot(df.index, df["ema_slow"], color="#9467bd", lw=1.0, label="EMA50")

    sig = df["signal"]
    longs = df[sig > 0]
    shorts = df[sig < 0]
    ax.scatter(longs.index, longs["Close"], marker="^", s=45, color="#2ca02c",
               zorder=5, label="CALL signals")
    ax.scatter(shorts.index, shorts["Close"], marker="v", s=45, color="#d62728",
               zorder=5, label="PUT signals")

    ax.set_title(f"{symbol} {tf} [{variant}] - last {len(df)} bars")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path
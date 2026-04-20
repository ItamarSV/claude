import re
from datetime import datetime
from pathlib import Path

COST_LOGS_DIR = Path(__file__).parent / "cost_logs"
COST_LOGS_DIR.mkdir(exist_ok=True)

# Gemini 1.5 Pro pricing (USD per 1M tokens)
PRICING = {
    1: {"input": 1.25, "output": 5.00},   # <= 128K total tokens
    2: {"input": 2.50, "output": 10.00},  # >  128K total tokens
}
TIER_THRESHOLD = 128_000


def _log_file(year: int, month: int) -> Path:
    return COST_LOGS_DIR / f"{year:04d}-{month:02d}.txt"


def record_call(group_id: str, input_tokens: int, output_tokens: int) -> float:
    total_tokens = input_tokens + output_tokens
    tier = 1 if total_tokens <= TIER_THRESHOLD else 2
    price = PRICING[tier]

    cost = (input_tokens / 1_000_000 * price["input"]) + \
           (output_tokens / 1_000_000 * price["output"])

    now = datetime.utcnow()
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    line = (
        f"[{timestamp}] group={group_id} tier={tier} "
        f"in={input_tokens} out={output_tokens} cost=${cost:.5f}\n"
    )

    with open(_log_file(now.year, now.month), "a", encoding="utf-8") as f:
        f.write(line)

    return cost


def get_monthly_summary(year: int, month: int) -> dict:
    path = _log_file(year, month)
    if not path.exists():
        return {"total_cost": 0.0, "total_calls": 0, "total_tokens": 0,
                "tier1_calls": 0, "tier2_calls": 0, "by_group": {}}

    pattern = re.compile(
        r"\[.+?\] group=(\S+) tier=(\d) in=(\d+) out=(\d+) cost=\$([0-9.]+)"
    )

    summary = {
        "total_cost": 0.0,
        "total_calls": 0,
        "total_tokens": 0,
        "tier1_calls": 0,
        "tier2_calls": 0,
        "by_group": {},
    }

    for line in path.read_text(encoding="utf-8").splitlines():
        m = pattern.search(line)
        if not m:
            continue
        group_id, tier, in_tok, out_tok, cost = (
            m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), float(m.group(5))
        )
        summary["total_cost"] += cost
        summary["total_calls"] += 1
        summary["total_tokens"] += in_tok + out_tok
        if tier == 1:
            summary["tier1_calls"] += 1
        else:
            summary["tier2_calls"] += 1

        g = summary["by_group"].setdefault(group_id, {"cost": 0.0, "calls": 0})
        g["cost"] += cost
        g["calls"] += 1

    summary["total_cost"] = round(summary["total_cost"], 5)
    return summary

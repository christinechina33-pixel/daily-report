# -*- coding: utf-8 -*-
"""
close_report.py — 收盘总结 Renderer（17:00 专用）

职责（P0-4 拆分层）：
  · 只读 today_snapshot.json（19:00 数据快照，含 close_of_day 收盘价），不跑数据管线
  · 回答："今天到底发生了什么？我的判断有没有被验证？明天怎么办？"
  · 输出：reports/close_YYYY-MM-DD.html
  · 不可操作，纯阅读型产物

数据源：
  · Ashare/data/tencent/today_snapshot.json（含 close_of_day 更新后的收盘价）
  · Asset_OS/learning/learning_store/outcome_memory.jsonl（若有，供 prediction→result 闭环）
  · Ashare/data/tencent/human_decisions.jsonl（今日新增人工决策）

输出路径：Ashare/reports/close_YYYY-MM-DD.html
"""
from __future__ import annotations
import json
import os
import datetime
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "data" / "tencent"
REPORTS_DIR = HERE / "reports"
SNAPSHOT_PATH = DATA_DIR / "today_snapshot.json"
HUMAN_DECS_PATH = DATA_DIR / "human_decisions.jsonl"
os.makedirs(REPORTS_DIR, exist_ok=True)

CST = datetime.timezone(datetime.timedelta(hours=8))


def _load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        raise SystemExit(f"[FATAL] {SNAPSHOT_PATH} 不存在")
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _load_today_human_decisions() -> list:
    if not HUMAN_DECS_PATH.exists():
        return []
    today = datetime.date.today().strftime("%Y-%m-%d")
    rows = []
    with open(HUMAN_DECS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
                ts = d.get("timestamp", "")
                if today in ts:
                    rows.append(d)
            except json.JSONDecodeError:
                continue
    return rows


def _verdict_badge(v: str) -> str:
    m = {
        "BUY":       '<span style="color:#2e7d32;font-weight:600">买入</span>',
        "ADD":       '<span style="color:#1565c0;font-weight:600">加仓</span>',
        "HOLD":      '<span style="color:#757575">持有</span>',
        "REDUCE":    '<span style="color:#e65100;font-weight:600">减仓</span>',
        "SELL":      '<span style="color:#c62828;font-weight:600">卖出</span>',
        "BUY_WATCH": '<span style="color:#f57f17;font-weight:600">观察买入</span>',
    }
    return m.get(v, f'<span style="color:#757575">{v}</span>')


def _compute_pnl_pct(p: dict) -> float:
    """snapshot 的 positions[] 仅有浮动盈亏绝对值，无百分比字段，需补算 fallback。

    优先级：显式百分比字段 → 浮动盈亏 / (成本价 × 持股数) × 100
    """
    raw = p.get("浮动盈亏%", p.get("pnl_pct", None))
    if raw is not None:
        return float(raw)
    pnl = p.get("浮动盈亏", p.get("pnl", 0))
    cost = p.get("成本价", p.get("avg_cost", 0))
    shares = p.get("持股数", p.get("shares", 0))
    if cost and shares:
        return pnl / (cost * shares) * 100
    return 0.0


def _render_market_overview(snapshot: dict) -> str:
    """区块1：今日市场全貌（大盘指数 / 涨跌家数 / 成交量 / 市场情绪）"""
    reg = snapshot.get("regime", {})
    label = reg.get("regime_label", reg.get("regime", "?"))
    bias = reg.get("action_bias", "")
    indicators = snapshot.get("indicators", {})
    market_radar = snapshot.get("market_radar", {})
    # 尝试从 decisions 统计涨跌
    decisions = snapshot.get("decisions", [])
    up = sum(1 for d in decisions if d.get("current_price") and d.get("verdict", {}).get("rating", 0) > 2)
    down = sum(1 for d in decisions if d.get("verdict", {}).get("rating", 0) <= 2)
    total = len(decisions)
    degrade = snapshot.get("degraded_mode", {})
    degrade_text = ""
    if degrade.get("degraded"):
        degrade_text = f'<div class="warn">⚠️ {degrade.get("reason", "")}</div>'
    return f"""
    <div class="card">
      <h3>① 今日市场全貌</h3>
      <div class="kv">
        <span>市场状态</span><span>{label}</span>
        <span>操作倾向</span><span>{bias}</span>
        <span>跟踪标的</span><span>{total} 只</span>
        <span>偏强</span><span>{up}</span>
        <span>偏弱</span><span>{down}</span>
      </div>
      <div class="kv" style="margin-top:6px">
        <span>盈利分</span><span>{reg.get("earnings_score", "?")}</span>
        <span>估值分</span><span>{reg.get("valuation_score", "?")}</span>
      </div>
      {degrade_text}
    </div>"""


def _render_portfolio_today(snapshot: dict) -> str:
    """区块2：我的持仓今日发生了什么"""
    positions = snapshot.get("portfolio", {}).get("positions", [])
    if not positions:
        return '<div class="card"><h3>② 持仓今日</h3><p class="muted">无持仓数据</p></div>'
    total_assets = snapshot.get("portfolio", {}).get("total_assets", 0)
    cash = snapshot.get("portfolio", {}).get("cash", 0)

    rows = []
    for p in positions:
        code = p.get("代码", p.get("code", ""))
        name = p.get("名称", p.get("name", code))
        shares = p.get("持股数", p.get("shares", 0))
        cost = p.get("成本价", p.get("avg_cost", 0))
        price = p.get("现价", p.get("current_price", 0))
        pnl = p.get("浮动盈亏", 0)
        pnl_pct = _compute_pnl_pct(p)
        bucket = p.get("池", p.get("bucket", ""))
        note = p.get("备注", "")
        color = "#2e7d32" if pnl > 0 else ("#c62828" if pnl < 0 else "#757575")
        rows.append(f"""
          <tr>
            <td><code>{code}</code></td>
            <td>{name}</td>
            <td>{bucket}</td>
            <td>{shares:.0f}</td>
            <td>¥{cost:.2f}</td>
            <td>¥{price:.2f}</td>
            <td style="color:{color};font-weight:600">{'+' if pnl>0 else ''}¥{pnl:,.0f}</td>
            <td style="color:{color};font-weight:600">{'+' if pnl_pct>0 else ''}{pnl_pct:.2f}%</td>
            <td class="note">{note}</td>
          </tr>""")

    return f"""
    <div class="card">
      <h3>② 持仓今日</h3>
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>池</th><th>股数</th><th>成本</th><th>收盘价</th><th>盈亏</th><th>盈亏%</th><th>备注</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <div class="meta">总资产 ¥{total_assets:,.0f} · 现金 ¥{cash:,.0f} · 仓位 {total_assets/700000*100:.1f}% · {len(positions)} 只持仓</div>
    </div>"""


def _render_ai_narrative(snapshot: dict) -> str:
    """AI 研判区块：渲染 WorkBuddy 平台 LLM 生成的影响解读 + 市场叙述。

    数据来自 snapshot 的 llm_narrative 字段（由 wb-news-impact Skill 注入）。
    若字段缺失，显示诚实占位，不编造。
    """
    nar = snapshot.get("llm_narrative", {})
    if not nar:
        return '<div class="card"><h3>🤖 AI 研判</h3><p class="muted">今日尚未生成 LLM 研判（wb-news-impact 未运行）。</p></div>'
    gen = nar.get("generated_at", "?")
    engine = nar.get("engine", "WorkBuddy platform LLM")
    comment = nar.get("market_comment", "")
    impacts = nar.get("news_impact", [])
    rows = []
    for it in impacts:
        title = it.get("title", "")
        targets = it.get("targets", "")
        impact = it.get("impact", "中性")
        reason = it.get("reason", "")
        color = {"利好": "#2e7d32", "利空": "#c62828"}.get(impact, "#757575")
        badge = {"利好": "利好", "利空": "利空"}.get(impact, "中性")
        rows.append(f"""
          <li><span class="chip" style="background:{color}20;color:{color}">{badge}</span>
          <strong>{targets}</strong> · {title}
          <br><span class="muted" style="font-size:12px">{reason}</span></li>""")
    impact_html = "".join(rows) if rows else '<li class="muted">今日新闻未命中持仓清单</li>'
    comment_html = f'<div style="margin-top:10px;padding:10px 12px;background:#15171d;border-radius:8px;font-size:13px;line-height:1.7">{comment}</div>' if comment else ""
    return f"""
    <div class="card">
      <h3>🤖 AI 研判 <span style="font-size:11px;color:#666;font-weight:400">· {engine}</span></h3>
      <ul>{impact_html}</ul>
      {comment_html}
      <div class="meta" style="margin-top:8px">研判生成于 {gen} · 由 WorkBuddy 平台 LLM 生成，仅供参考，不构成投资建议</div>
    </div>"""


def _render_execution_gate() -> str:
    """可执行交易监查区块：读 Asset_OS/execution_gate.json，渲染四问 + 分组候选。

    投资判断(opportunity_gate) 与 交易可执行性 分离；只展示，不替人决策。
    """
    import json as _json
    eg_path = Path(__file__).resolve().parent.parent / "Asset_OS" / "execution_gate.json"
    try:
        eg = _json.loads(eg_path.read_text(encoding="utf-8"))
    except Exception:
        return '<div class="card"><h3>🎯 可执行交易监查</h3><p class="muted">今日尚未生成 Execution Gate（execution_gate.py 未运行）。</p></div>'
    ctx = eg.get("context", {})
    u = eg.get("universe_state", {})
    cands = eg.get("candidates", [])
    gen = eg.get("generated_at", "?")
    summary = (f"值不值得(观察级)={u.get('worth_buy')} · 确定买点={u.get('clear_buy')} · "
               f"价格到了={u.get('price_ready')} · 实际能买={u.get('tradeable_now')} · 已拒={u.get('rejected_count')}")
    dl = u.get("draft_locked_count", 0)
    banner = ""
    if dl:
        banner += f'<p style="color:#8d4e00;font-weight:600;margin:6px 0">🔒 DRAFT 锁定(不得执行·待补全)={dl} —— 仅观察/等待补全，不进 EXECUTE 候选</p>'
    if u.get("nothing_to_do_today"):
        banner += '<p style="color:#2e7d32;font-weight:600;margin:6px 0">✅ 今天什么都不做（无标的可执行第一手）</p>'
    if ctx.get("portfolio_over_cap"):
        banner += (f'<p class="warn">🔒 Q5 组合 Regime 资本预算不足：当前市值{ctx.get("current_equity")} '
                   f'已超 regime 授权 {ctx.get("equity_cap_amt")} —— 新增暴露(BUY/ADD/INCREASE)一律 BLOCK；'
                   f'降低暴露(SELL/REDUCE/EXIT/DE-RISK)不受限</p>')
    else:
        banner += '<p style="color:#2e7d32;font-weight:600;margin:6px 0">✅ Q5 组合 Regime 资本预算充足：新增暴露授权有效</p>'

    exec_c = [c for c in cands if (not c["is_held"]) and c["q3_tradeable"].get("tradeable")]
    watch_c = [c for c in cands if (not c["is_held"]) and not c["q3_tradeable"].get("tradeable")]
    held_c = [c for c in cands if c["is_held"]]

    def _row(c):
        q1 = c["q1_worth_buy"]; q2 = c["q2_price_ready"]; q3 = c["q3_tradeable"]
        q5 = c.get("q5_regime_budget") or {}
        can = "能" if q3.get("tradeable") else "不能"
        blockers = "; ".join(c.get("q4_blockers", [])) or "无"
        q5txt = "不足(不得新增暴露)" if q5.get("regime_budget_blocked") else "充足"
        return (f"<li><strong>{c['name']}({c['code']})</strong> · {c['state']} / {c['action']}<br>"
                f"<span class='muted' style='font-size:12px'>Q1 {q1.get('verdict')}({q1.get('strength')}) · "
                f"Q2 {q2.get('note','')} · Q3 {can}(一手{q3.get('lot_cost')}) · Q4 {blockers} · "
                f"Q5 {q5txt}</span></li>")

    rows = []
    if exec_c:
        rows.append(f"<p style='margin:8px 0 4px;color:#2e7d32;font-size:13px'>【可执行/待拍板】{len(exec_c)}</p><ul>" + "".join(_row(c) for c in exec_c) + "</ul>")
    if watch_c:
        rows.append(f"<p style='margin:8px 0 4px;color:#f57f17;font-size:13px'>【观察候选】{len(watch_c)}</p><ul>" + "".join(_row(c) for c in watch_c) + "</ul>")
    if held_c:
        rows.append(f"<p style='margin:8px 0 4px;color:#888;font-size:13px'>【持仓管理】{len(held_c)}</p><ul>" + "".join(_row(c) for c in held_c) + "</ul>")
    rows_html = "".join(rows) if rows else '<li class="muted">无候选</li>'
    dl = u.get("draft_locked_count", 0)
    rb = u.get("regime_budget_blocked_count", 0)
    strip = (f"EXECUTE {len(exec_c)} · WATCH {len(watch_c)} · HOLD {len(held_c)} · DRAFT锁定 {dl} · Q5超限 {rb}")
    return f"""
    <div class="card">
      <h3>🎯 可执行交易监查 <span style="font-size:11px;color:#666;font-weight:400">· Execution Gate</span></h3>
      <div class="meta">{summary}</div>
      <div class="meta" style="margin-top:4px;font-weight:600">{strip}</div>
      {banner}
      {rows_html}
      <div class="meta" style="margin-top:8px">监查生成于 {gen} · 投资判断与交易可执行性分离，仅供参考，不替你决策</div>
    </div>"""


def _render_triggered_signals(snapshot: dict) -> str:
    """区块3：今天哪些信号真的触发了"""
    decisions = snapshot.get("decisions", [])
    triggered = []
    for d in decisions:
        v = d.get("verdict", {})
        verdict = v.get("verdict", "")
        price = d.get("current_price", 0)
        pp = v.get("position_plan", {})
        stop_price = pp.get("stop_price", 0)
        if stop_price and price and price <= stop_price:
            triggered.append(f"{d.get('code','')} {d.get('name','')}: 现价¥{price:.2f} ≤ 止损¥{stop_price:.2f}")
        for rule in pp.get("add_rules", []):
            trigger_str = str(rule.get("trigger", "0"))
            try:
                trigger_pct = float(trigger_str.replace("%", ""))
            except ValueError:
                trigger_pct = 0
            add_pct = rule.get("add", 0)
            if trigger_pct < 0 and add_pct > 0 and price:
                entry = pp.get("entry_price", price)
                threshold = entry * (1 + trigger_pct / 100)
                if price <= threshold:
                    triggered.append(f"{d.get('code','')} {d.get('name','')}: 回落{abs(trigger_pct)}% 至¥{price:.2f} ≤ 阈值¥{threshold:.2f}，可加仓×{add_pct}")
    if not triggered:
        return '<div class="card"><h3>③ 触发信号</h3><p class="muted">今日无触发信号</p></div>'
    return f"""
    <div class="card">
      <h3>③ 触发信号 {len(triggered)} 条</h3>
      <ul>{"".join(f"<li>{t}</li>" for t in triggered[:10])}</ul>
    </div>"""


def _render_judgment_check(snapshot: dict) -> str:
    """区块4：今天哪些判断被市场验证/证伪"""
    decisions = snapshot.get("decisions", [])
    # 从 decisions 中挑有 fair_gap + position 的，对比当前价 vs 合理价
    verified = []
    for d in decisions:
        code = d.get("code", "")
        name = d.get("name", code)
        price = d.get("current_price", 0)
        fair_gap = d.get("fair_gap", None)
        v = d.get("verdict", {})
        verdict = v.get("verdict", "")
        pp = v.get("position_plan", {})
        entry_price = pp.get("entry_price", 0)
        target_price = pp.get("target_price", 0)
        stop_price = pp.get("stop_price", 0)
        # 系统已建议加仓/减仓的，检查是否合理
        if verdict in ("BUY", "ADD"):
            if fair_gap is not None and fair_gap < -0.1 and price:
                verified.append(f"✅ {code} {name}: 系统建议{verdict}，合理价偏低估{fair_gap*100:.1f}%，现价¥{price:.2f}，方向正确")
            elif fair_gap is not None and fair_gap > 0.1:
                verified.append(f"⚠️ {code} {name}: 系统建议{verdict}，但合理价偏高估{fair_gap*100:.1f}%，现价¥{price:.2f}，建议谨慎")
        if verdict == "REDUCE" and target_price and price and price >= target_price:
            verified.append(f"✅ {code} {name}: 系统建议{verdict}，现价¥{price:.2f} ≥ 目标价¥{target_price:.2f}，方向正确")
        if verdict == "HOLD" and abs(fair_gap or 0) < 0.05:
            verified.append(f"→ {code} {name}: 合理价±5%，{verdict}，中性，符合系统判断")

    # 人工决策（今日）
    human_decisions = _load_today_human_decisions()
    if human_decisions:
        for hd in human_decisions:
            code = hd.get("code", hd.get("asset", ""))
            action = hd.get("action", hd.get("human_action", ""))
            reason = hd.get("reason", hd.get("human_rationale", ""))[:60]
            verified.append(f"📝 人工: {code} {action} — {reason}")

    if not verified:
        return '<div class="card"><h3>④ 判断验证</h3><p class="muted">无足够数据做验证分析</p></div>'
    return f"""
    <div class="card">
      <h3>④ 判断验证 {len(verified)} 条</h3>
      <ul>{"".join(f"<li>{v}</li>" for v in verified[:12])}</ul>
    </div>"""


def _render_tomorrow(snapshot: dict) -> str:
    """区块5：明日需要关注的事件与行动条件"""
    decisions = snapshot.get("decisions", [])
    positions = snapshot.get("portfolio", {}).get("positions", [])
    action_items = []

    # 持仓中标的系统建议非 HOLD 的
    for p in positions:
        code = p.get("代码", p.get("code", ""))
        name = p.get("名称", p.get("name", code))
        for d in decisions:
            if d.get("code") == code:
                v = d.get("verdict", {})
                verdict = v.get("verdict", "")
                pp = v.get("position_plan", {})
                if verdict in ("BUY", "ADD", "REDUCE", "SELL"):
                    action_items.append(f"🔴 {code} {name}: 系统明日关注 {verdict}（conf={v.get('confidence',0):.2f}）")
                elif verdict == "HOLD":
                    stop = pp.get("stop_price", 0)
                    if stop and p.get("现价", 0):
                        action_items.append(f"🟡 {code} {name}: 止损¥{stop:.2f}，跌破需重新评估")
                break

    # 新候选
    for d in decisions:
        v = d.get("verdict", {})
        if v.get("verdict") == "BUY" and v.get("confidence", 0) >= 0.5:
            action_items.append(f"🟢 {d.get('code','')} {d.get('name','')}: 明日可建仓（conf={v.get('confidence',0):.2f}，entry={d.get('verdict',{}).get('position_plan',{}).get('entry_price',0):.2f}）")

    reg = snapshot.get("regime", {})
    bias = reg.get("action_bias", "")
    regime_advice = reg.get("advice", "")
    degrade = snapshot.get("degraded_mode", {})
    degrade_note = ""
    if degrade.get("degraded"):
        degrade_note = f"<li>⚠️ 系统处于降级模式（{degrade.get('reason','')}），明日建议人工复核关键决策</li>"

    if not action_items:
        action_items.append("🟢 明日无特别操作，按计划持有即可")

    footer = ""
    if regime_advice:
        footer = f'<p class="muted" style="margin-top:8px">系统建议：{regime_advice}</p>'

    return f"""
    <div class="card">
      <h3>⑤ 明日关注</h3>
      <ul>{''.join(f"<li>{a}</li>" for a in action_items[:8])}
        {degrade_note}
      </ul>
      {footer}
    </div>"""


def _run_health(snapshot: dict) -> str:
    health = snapshot.get("run_health", {})
    overall = health.get("overall", "UNKNOWN")
    steps = health.get("steps", [])
    step_colors = {"PASS": "#2e7d32", "WARN": "#f57f17", "FAIL": "#c62828"}
    items = []
    for s in steps:
        name = s.get("name", s.get("step", "?"))
        status = s.get("status", "UNKNOWN")
        color = step_colors.get(status, "#757575")
        items.append(f'<span class="chip" style="background:{color}20;color:{color}">{name}: {status}</span>')
    return f"""
    <div class="meta" style="margin-top:20px">
      <span>数据状态: <strong style="color:{step_colors.get(overall, '#757575')}">{overall}</strong></span>
      {''.join(items)}
    </div>"""


def render() -> str:
    snapshot = _load_snapshot()
    today = snapshot.get("date", datetime.date.today().strftime("%Y-%m-%d"))
    ts = snapshot.get("snapshot_time", "unknown")
    run_id = snapshot.get("run_id", "unknown")
    title = f"收盘总结 · {today}"
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#0f1117;color:#e8e8ec;line-height:1.6;padding:24px;max-width:920px;margin:0 auto}}
h1{{font-size:24px;font-weight:700;margin-bottom:4px;color:#fff}}
.subtitle{{font-size:13px;color:#888;margin-bottom:20px}}
.card{{background:#1a1d24;border-radius:10px;padding:18px 20px;margin-bottom:16px;border:1px solid #2a2d34}}
.card h3{{font-size:15px;font-weight:600;margin-bottom:10px;color:#c8a86b}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid #2a2d34}}
th{{color:#888;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:0.5px}}
code{{font-family:'SF Mono','Fira Code',monospace;color:#c8a86b;font-size:12px}}
.muted{{color:#666;font-size:13px}}
.warn{{color:#f57f17;font-size:13px;margin-top:8px}}
.kv{{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;margin-top:8px}}
.kv span:nth-child(odd){{color:#888}}
.kv span:nth-child(even){{color:#e8e8ec;font-weight:500}}
.meta{{font-size:12px;color:#888;padding:8px 0;display:flex;flex-wrap:wrap;gap:12px}}
.chip{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}}
ul{{padding-left:18px;font-size:13px}}
ul li{{margin-bottom:5px}}
.footer{{margin-top:24px;padding-top:16px;border-top:1px solid #2a2d34;font-size:11px;color:#555}}
.note{{font-size:11px;color:#666;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="subtitle">数据快照: {ts} · run_id: {run_id} · 阅读型产物，不可操作</p>
{_render_market_overview(snapshot)}
{_render_portfolio_today(snapshot)}
{_render_ai_narrative(snapshot)}
{_render_execution_gate()}
{_render_triggered_signals(snapshot)}
{_render_judgment_check(snapshot)}
{_render_tomorrow(snapshot)}
{_run_health(snapshot)}
<div class="footer">
生成时间: {datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} · 数据源: today_snapshot.json（含 close_of_day 收盘更新） · 管线: 17:00 close_of_day → 19:00 轻量渲染
</div>
</body>
</html>"""
    out = REPORTS_DIR / f"close_{today}.html"
    out.write_text(html, encoding="utf-8")
    return str(out)


if __name__ == "__main__":
    out_path = render()
    print(f"[OK] Close Report → {out_path}")

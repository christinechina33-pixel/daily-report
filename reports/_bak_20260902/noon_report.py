# -*- coding: utf-8 -*-
"""
noon_report.py — 午间评论 Renderer（12:00 专用）

职责（P0-4 拆分层）：
  · 只读 today_snapshot.json（12:00 数据快照），不跑数据管线
  · 回答："上午已经发生什么？下午有没有必要行动？"
  · 输出：reports/noon_YYYY-MM-DD.html
  · 不可操作（无执行按钮、无实时状态），纯阅读型产物

数据源：
  · Ashare/data/tencent/today_snapshot.json（regime / decisions / portfolio / run_health）
  · 不依赖 decision_feed.json（避免重复读大文件）

输出路径：Ashare/reports/noon_YYYY-MM-DD.html
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
os.makedirs(REPORTS_DIR, exist_ok=True)

CST = datetime.timezone(datetime.timedelta(hours=8))


def _load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        raise SystemExit(f"[FATAL] {SNAPSHOT_PATH} 不存在，请先运行完整数据管线")
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _get_prefix(code: str) -> str:
    if code == "00700":
        return "hk"
    elif code.startswith("688"):
        return "sh"
    elif code.startswith("0") or code.startswith("3"):
        return "sz"
    elif code.startswith("6"):
        return "sh"
    return "sh"


def _refresh_positions_prices(snapshot: dict) -> dict:
    """实时行情回写：遍历持仓，拉 Tencent 实时报价，更新 portfolio.json 和 snapshot 中的现价。

    修复：daily_update 只在内存中计算 qm，从不回写 portfolio.json；
    导致 today_snapshot.json 中的「现价」= 上次写入时的价格（通常=昨收）。
    午间评论必须用真实午间价，所以在此补上回写。

    纪律：
    - 只更新 price 字段，不碰成本/股数/止盈止损
    - 单只 API 失败则跳过该只（用旧价），不打断整批
    - 使用 urllib 避免引入额外依赖
    """
    positions = snapshot.get("portfolio", {}).get("positions", [])
    if not positions:
        return snapshot

    # 同步更新 portfolio.json（snapshot 的持仓来源）
    portfolio_path = HERE / "data" / "portfolio.json"
    updated_count = 0
    skipped = []

    for pos in positions:
        code = pos.get("代码") or pos.get("code", "")
        if not code:
            continue
        prefix = _get_prefix(code)
        for attempt in range(2):
            try:
                import urllib.request
                url = f"https://qt.gtimg.cn/q={prefix}{code}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=8)
                raw = resp.read().decode("gbk")
                parts = raw.split("~")
                if len(parts) > 32:
                    new_price = float(parts[3])
                    if new_price > 0:
                        pos["现价"] = new_price
                        updated_count += 1
                        break
                else:
                    raise ValueError("字段不足")
            except Exception as e:
                if attempt == 1:
                    skipped.append(f"{code}:{e}")
                else:
                    import time
                    time.sleep(0.3)

    if updated_count or skipped:
        print(f"  [REFRESH] 持仓实时行情：更新 {updated_count}/{len(positions)} 只",
              f"{'；跳过 ' + ', '.join(skipped) if skipped else ''}")

        # 写回 portfolio.json（同步更新所有字段以保持一致性）
        if portfolio_path.exists() and updated_count > 0:
            try:
                pdata = json.loads(portfolio_path.read_text(encoding="utf-8"))
                raw_positions = pdata.get("持仓") or pdata.get("positions", [])
                snap_by_code = {p.get("代码") or p.get("code", ""): p for p in positions}
                for rp in raw_positions:
                    rc = rp.get("代码") or rp.get("code", "")
                    if rc in snap_by_code:
                        rp["现价"] = snap_by_code[rc]["现价"]
                pdata["更新时间"] = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
                portfolio_path.write_text(json.dumps(pdata, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                print(f"  [WARN] 写回 portfolio.json 失败: {e}")

        # 更新 snapshot 中的 portfolio.positions（保持引用一致）
        # snapshot 的 positions 已经是 positions 列表的引用，无需额外操作
    else:
        print("  [REFRESH] 无持仓数据可更新")

    return snapshot


def _rebuild_snapshot(snapshot: dict) -> None:
    """将更新后的 snapshot 写回 today_snapshot.json（含新时间戳），供渲染使用。"""
    snapshot["snapshot_time"] = datetime.datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def _pnl_color(pct: float) -> str:
    if pct > 0.5:
        return "#2e7d32"
    if pct < -0.5:
        return "#c62828"
    return "#757575"


def _compute_pnl_pct(p: dict) -> float:
    """从已有字段补算盈亏%，snapshot 无此字段时 fallback。"""
    raw = p.get("浮动盈亏%", p.get("pnl_pct", None))
    if raw is not None:
        return float(raw)
    pnl = p.get("浮动盈亏", p.get("pnl", 0))
    cost = p.get("成本价", p.get("avg_cost", 0))
    shares = p.get("持股数", p.get("shares", 0))
    if cost and shares:
        return pnl / (cost * shares) * 100
    return 0.0


def _verdict_badge(v: str) -> str:
    m = {
        "BUY":       ('<span style="color:#2e7d32;font-weight:600">买入</span>', "BUY"),
        "ADD":       ('<span style="color:#1565c0;font-weight:600">加仓</span>', "ADD"),
        "HOLD":      ('<span style="color:#757575">持有</span>', "HOLD"),
        "REDUCE":    ('<span style="color:#e65100;font-weight:600">减仓</span>', "REDUCE"),
        "SELL":      ('<span style="color:#c62828;font-weight:600">卖出</span>', "SELL"),
        "BUY_WATCH": ('<span style="color:#f57f17;font-weight:600">观察买入</span>', "BUY_WATCH"),
    }
    return m.get(v, (f'<span style="color:#757575">{v}</span>', v))


def _render_positions(snapshot: dict) -> str:
    positions = snapshot.get("portfolio", {}).get("positions", [])
    if not positions:
        return '<div class="card"><h3>持仓</h3><p class="muted">无持仓数据</p></div>'

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
        sector = p.get("板块", "")
        color = _pnl_color(pnl_pct)
        rows.append(f"""
          <tr>
            <td><code>{code}</code></td>
            <td>{name}</td>
            <td>{bucket}</td>
            <td>{sector}</td>
            <td>{shares:.0f}</td>
            <td>¥{cost:.2f}</td>
            <td>¥{price:.2f}</td>
            <td style="color:{color};font-weight:600">{'+' if pnl>0 else ''}{pnl:.0f}</td>
            <td style="color:{color};font-weight:600">{'+' if pnl_pct>0 else ''}{pnl_pct:.2f}%</td>
          </tr>""")

    total_assets = snapshot.get("portfolio", {}).get("total_assets", 0)
    cash = snapshot.get("portfolio", {}).get("cash", 0)
    return f"""
    <div class="card">
      <h3>持仓 {len(positions)} 只</h3>
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>池</th><th>板块</th><th>股数</th><th>成本</th><th>现价</th><th>盈亏</th><th>盈亏%</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <div class="meta">总资产 ¥{total_assets:,.0f} · 现金 ¥{cash:,.0f} · 仓位 {total_assets/700000*100:.1f}%</div>
    </div>"""


def _render_regime(snapshot: dict) -> str:
    reg = snapshot.get("regime", {})
    if not reg:
        return '<div class="card"><h3>市场状态</h3><p class="muted">暂无数据</p></div>'
    label = reg.get("regime_label", reg.get("regime", "?"))
    bias = reg.get("action_bias", "")
    earning_s = reg.get("earnings_score", 0)
    val_s = reg.get("valuation_score", 0)
    degraded = snapshot.get("degraded_mode", {})
    degrade_note = ""
    if degraded.get("degraded"):
        degrade_note = f'<p class="warn">⚠️ 降级模式：{degraded.get("reason", "")}</p>'
    return f"""
    <div class="card">
      <h3>市场状态 · {label}</h3>
      <div class="kv">
        <span>操作倾向</span><span>{bias}</span>
        <span>盈利分</span><span>{earning_s}</span>
        <span>估值分</span><span>{val_s}</span>
      </div>
      {degrade_note}
    </div>"""


def _render_actionable(snapshot: dict) -> str:
    """筛选 actionable 决策：verdict 为 BUY/ADD/REDUCE/SELL + confidence >= 0.5"""
    decisions = snapshot.get("decisions", [])
    actionable = []
    for d in decisions:
        v = d.get("verdict", {})
        verdict = v.get("verdict", "")
        conf = v.get("confidence", 0)
        reasons = v.get("reasons", {})
        if verdict in ("BUY", "ADD", "REDUCE", "SELL") and conf >= 0.5:
            actionable.append(d)
    if not actionable:
        return '<div class="card"><h3>今日可行动项</h3><p class="muted">无 actionable 决策（confidence ≥ 0.5）</p></div>'

    rows = []
    for d in actionable[:6]:
        code = d.get("code", "")
        name = d.get("name", code)
        v = d.get("verdict", {})
        verdict, _ = _verdict_badge(v.get("verdict", ""))
        price = d.get("current_price", 0)
        fair_gap = d.get("fair_gap", None)
        reasons_text = v.get("reasons", {}).get("valuation", "")
        if len(reasons_text) > 80:
            reasons_text = reasons_text[:77] + "…"
        entry_strategy = d.get("verdict", {}).get("position_plan", {}).get("entry_strategy", "")
        stop = d.get("verdict", {}).get("position_plan", {}).get("stop_price", 0)
        target = d.get("verdict", {}).get("position_plan", {}).get("target_price", 0)
        rows.append(f"""
          <tr>
            <td><code>{code}</code></td>
            <td>{name}</td>
            <td>{verdict}</td>
            <td>¥{price:.2f}</td>
            <td>{reasons_text}</td>
            <td>{entry_strategy}</td>
            <td>¥{stop:.2f}</td>
            <td>¥{target:.2f}</td>
          </tr>""")

    return f"""
    <div class="card">
      <h3>今日可行动项 {len(actionable)} 项</h3>
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>判断</th><th>现价</th><th>估值理由</th><th>入场策略</th><th>止损</th><th>目标</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>"""


def _render_triggers(snapshot: dict) -> str:
    """从 snapshot 的 decisions 中提取今日触发的监控信号（verdict 为 REDUCE/SELL + position_plan 含 add_rules 的标的）"""
    decisions = snapshot.get("decisions", [])
    triggered = []
    for d in decisions:
        v = d.get("verdict", {})
        verdict = v.get("verdict", "")
        price = d.get("current_price", 0)
        pp = v.get("position_plan", {})
        stop_price = pp.get("stop_price", 0)
        add_rules = pp.get("add_rules", [])
        if verdict in ("REDUCE", "SELL") and stop_price and price <= stop_price:
            triggered.append({**d, "type": "止损触发", "detail": f"现价¥{price:.2f} ≤ 止损¥{stop_price:.2f}"})
        for rule in add_rules:
            trigger_pct = float(str(rule.get("trigger", "0")).replace("%", ""))
            add_pct = rule.get("add", 0)
            if price and add_pct > 0:
                threshold = pp.get("entry_price", price) * (1 + trigger_pct / 100)
                if price <= threshold:
                    triggered.append({**d, "type": "加仓信号", "detail": f"回落 {abs(trigger_pct)}%，可加仓 ×{add_pct}"})

    if not triggered:
        return '<div class="card"><h3>今日触发信号</h3><p class="muted">无触发信号</p></div>'

    rows = []
    for t in triggered[:8]:
        code = t.get("code", "")
        name = t.get("name", code)
        price = t.get("current_price", 0)
        rows.append(f"<tr><td><code>{code}</code></td><td>{name}</td><td>¥{price:.2f}</td><td>{t['type']}</td><td>{t['detail']}</td></tr>")

    return f"""
    <div class="card">
      <h3>今日触发信号 {len(triggered)} 条</h3>
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>现价</th><th>类型</th><th>说明</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>"""


def _render_afternoon(snapshot: dict) -> str:
    """下午行动建议：基于当前持仓和系统建议，提炼「下午该盯什么」"""
    positions = snapshot.get("portfolio", {}).get("positions", [])
    decisions = snapshot.get("decisions", [])
    action_list = []
    # 持仓中标的系统建议非 HOLD 的
    for p in positions:
        code = p.get("代码", p.get("code", ""))
        name = p.get("名称", p.get("name", code))
        for d in decisions:
            if d.get("code") == code:
                v = d.get("verdict", {})
                verdict = v.get("verdict", "")
                if verdict in ("BUY", "ADD", "REDUCE", "SELL"):
                    action_list.append(f"{code} {name}: 系统建议 {verdict}（conf={v.get('confidence',0):.2f}）")
                break
    # 新候选买入
    for d in decisions:
        v = d.get("verdict", {})
        if v.get("verdict") == "BUY" and v.get("confidence", 0) >= 0.5:
            action_list.append(f"{d.get('code','')} {d.get('name','')}: 新买入候选（conf={v.get('confidence',0):.2f}）")
    if not action_list:
        return '<div class="card"><h3>下午关注</h3><p class="muted">无特别需要操作的标的，按计划持有即可</p></div>'
    return f"""
    <div class="card">
      <h3>下午关注</h3>
      <ul>{"".join(f"<li>{a}</li>" for a in action_list[:5])}</ul>
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
    banner = ""
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


def _run_health(snapshot: dict) -> str:
    health = snapshot.get("run_health", {})
    overall = health.get("overall", "UNKNOWN")
    steps = health.get("steps", [])
    step_colors = {"PASS": "#2e7d32", "WARN": "#f57f17", "FAIL": "#c62828"}
    step_items = []
    for s in steps:
        name = s.get("name", s.get("step", "?"))
        status = s.get("status", "UNKNOWN")
        color = step_colors.get(status, "#757575")
        step_items.append(f'<span class="chip" style="background:{color}20;color:{color}">{name}: {status}</span>')
    return f"""
    <div class="meta" style="margin-top:20px">
      <span>数据状态: <strong style="color:{step_colors.get(overall, '#757575')}">{overall}</strong></span>
      {''.join(step_items)}
    </div>"""


def render() -> str:
    snapshot = _load_snapshot()
    # 关键修复：实时行情回写，解决「午间报告显示昨收价」的问题
    snapshot = _refresh_positions_prices(snapshot)
    _rebuild_snapshot(snapshot)
    snapshot = _load_snapshot()  # 重新读取含新时间戳的版本
    today = snapshot.get("date", datetime.date.today().strftime("%Y-%m-%d"))
    ts = snapshot.get("snapshot_time", "unknown")
    run_id = snapshot.get("run_id", "unknown")
    title = f"午间评论 · {today}"
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#0f1117;color:#e8e8ec;line-height:1.6;padding:24px;max-width:880px;margin:0 auto}}
h1{{font-size:24px;font-weight:700;margin-bottom:4px;color:#fff}}
.subtitle{{font-size:13px;color:#888;margin-bottom:20px}}
.card{{background:#1a1d24;border-radius:10px;padding:18px 20px;margin-bottom:16px;border:1px solid #2a2d34}}
.card h3{{font-size:15px;font-weight:600;margin-bottom:10px;color:#c8a86b;display:flex;align-items:center;gap:6px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid #2a2d34}}
th{{color:#888;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:0.5px}}
code{{font-family:'SF Mono','Fira Code',monospace;color:#c8a86b;font-size:12px}}
.muted{{color:#666;font-size:13px}}
.warn{{color:#f57f17;font-size:13px;margin-top:8px}}
.kv{{display:flex;flex-wrap:wrap;gap:16px;font-size:13px;margin-top:8px}}
.kv span:nth-child(odd){{color:#888}}
.kv span:nth-child(even){{color:#e8e8ec;font-weight:500}}
.meta{{font-size:12px;color:#888;padding:8px 0;display:flex;flex-wrap:wrap;gap:12px}}
.chip{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}}
ul{{padding-left:18px;font-size:13px}}
ul li{{margin-bottom:4px}}
.footer{{margin-top:24px;padding-top:16px;border-top:1px solid #2a2d34;font-size:11px;color:#555}}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="subtitle">数据快照: {ts} · run_id: {run_id} · 阅读型产物，不可操作</p>
{_render_regime(snapshot)}
{_render_positions(snapshot)}
{_render_ai_narrative(snapshot)}
{_render_execution_gate()}
{_render_actionable(snapshot)}
{_render_triggers(snapshot)}
{_render_afternoon(snapshot)}
{_run_health(snapshot)}
<div class="footer">
生成时间: {datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} · 数据源: today_snapshot.json · 管线: 12:00 轻量渲染（零数据重拉）
</div>
</body>
</html>"""
    out = REPORTS_DIR / f"noon_{today}.html"
    out.write_text(html, encoding="utf-8")
    return str(out)


if __name__ == "__main__":
    out_path = render()
    print(f"[OK] Noon Report → {out_path}")

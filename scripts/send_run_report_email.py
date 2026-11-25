#!/usr/bin/env python3
"""
Email Run Performance Report via SMTP
=====================================
Formats and sends a comprehensive trading bot evaluation summary
to the specified recipient via SMTP (direct MX or authenticated relay).
"""

import argparse
import json
import os
from pathlib import Path
import smtplib
import socket
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

ROOT_DIR = Path(__file__).resolve().parent.parent

def build_report_data():
    live_data_path = ROOT_DIR / "live_data.json"
    meta_path = ROOT_DIR / ".parallel_run_meta.json"

    data = {}
    if live_data_path.exists():
        try:
            with open(live_data_path, "r") as f:
                data = json.load(f)
        except Exception:
            pass

    meta = {}
    if meta_path.exists():
        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
        except Exception:
            pass

    return data, meta

def generate_report_content(data: dict, meta: dict, recipient: str):
    summary = data.get("summary", {})
    strategies = data.get("strategies", [])
    positions = data.get("positions", [])

    total_trades = summary.get("total_trades", 0)
    settled_trades = summary.get("settled_trades", 0)
    open_trades = summary.get("open_trades", 0)
    wins = summary.get("wins", 0)
    losses = summary.get("losses", 0)
    win_rate = summary.get("win_rate", 0.0)
    pnl_dollars = summary.get("pnl_dollars", 0.0)

    prod_live = data.get("prod_live", {})
    demo_live = data.get("demo_live", {})

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Plain text version
    text_lines = [
        "=" * 68,
        f" KALSHI BOT PARALLEL RUN RESULTS - {now_utc}",
        "=" * 68,
        f"Recipient: {recipient}",
        f"Evaluation Phase: {data.get('phase_name', 'Parallel Strategy Run')}",
        "",
        "--- OVERALL SUMMARY ---",
        f"Aggregated PnL:   ${pnl_dollars:+.2f}",
        f"Overall Win Rate: {win_rate:.1f}% ({wins}W / {losses}L)",
        f"Trades Placed:    {total_trades} (Settled: {settled_trades}, Open: {open_trades})",
        "",
    ]

    if prod_live:
        p_pnl = prod_live.get("pnl_dollars", 0.0)
        text_lines.extend([
            "--- LIVE PRODUCTION PERFORMANCE (kalshi.com) ---",
            f"Strategy:         {prod_live.get('tag', 'spot_lag_strict')}",
            f"Live PnL:         ${p_pnl:+.2f}",
            f"Win Rate:         {prod_live.get('win_rate', 0.0):.1f}% ({prod_live.get('wins', 0)}W / {prod_live.get('losses', 0)}L)",
            f"Settled Trades:   {prod_live.get('settled', 0)}",
            "",
        ])

    if demo_live:
        d_pnl = demo_live.get("pnl_dollars", 0.0)
        text_lines.extend([
            "--- LIVE DEMO PERFORMANCE (demo.kalshi.co) ---",
            f"Strategy:         {demo_live.get('tag', 'crypto_lead_spillover')}",
            f"Live Demo PnL:    ${d_pnl:+.2f}",
            f"Win Rate:         {demo_live.get('win_rate', 0.0):.1f}% ({demo_live.get('wins', 0)}W / {demo_live.get('losses', 0)}L)",
            f"Settled Trades:   {demo_live.get('settled', 0)}",
            "",
        ])

    text_lines.extend([
        "--- STRATEGY BREAKDOWN ---",
        f"{'Strategy / Tag':<35} | {'Env':<5} | {'Mode':<5} | {'Trades':<6} | {'W/L':<7} | {'WinRate':<8} | {'PnL ($)':<8}",
        "-" * 84,
    ])

    for s in strategies:
        tag = s.get("tag", s.get("name", "Unknown"))
        env = s.get("env", "prod")
        mode = "LIVE" if s.get("live") else "SIM"
        tot = s.get("total", s.get("trades", 0))
        wl = f"{s.get('wins', 0)}/{s.get('losses', 0)}"
        wr = f"{s.get('win_rate', 0.0):.1f}%"
        spnl = f"${s.get('pnl_dollars', 0.0):+.2f}"
        text_lines.append(f"{tag:<35} | {env:<5} | {mode:<5} | {tot:<6} | {wl:<7} | {wr:<8} | {spnl:<8}")

    text_body = "\n".join(text_lines)

    # HTML Version
    strat_rows = ""
    for s in strategies:
        tag = s.get("tag", s.get("name", "Unknown"))
        env = s.get("env", "prod").upper()
        mode = "LIVE" if s.get("live") else "SIM"
        tot = s.get("total", s.get("trades", 0))
        sett = s.get("settled", 0)
        wl = f"{s.get('wins', 0)} / {s.get('losses', 0)}"
        wr = f"{s.get('win_rate', 0.0):.1f}%"
        spnl_val = s.get("pnl_dollars", 0.0)
        pnl_color = "#3fb950" if spnl_val > 0 else ("#f85149" if spnl_val < 0 else "#8b949e")
        mode_color = "#3fb950" if mode == "LIVE" and env == "PROD" else ("#58a6ff" if mode == "LIVE" else "#8b949e")
        strat_rows += f"""
        <tr>
            <td style="padding: 8px 12px; border-bottom: 1px solid #30363d; font-weight: 600;">{tag}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #30363d;">{env}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #30363d; color: {mode_color}; font-weight: 600;">{mode}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #30363d;">{tot} ({sett} settled)</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #30363d;">{wl}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #30363d;">{wr}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #30363d; color: {pnl_color}; font-weight: 700;">${spnl_val:+.2f}</td>
        </tr>
        """

    pnl_overall_color = "#3fb950" if pnl_dollars > 0 else ("#f85149" if pnl_dollars < 0 else "#8b949e")

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #0d1117; color: #c9d1d9; margin: 0; padding: 24px; }}
.container {{ max-width: 800px; margin: 0 auto; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 24px; }}
h2 {{ color: #58a6ff; margin-top: 0; }}
.cards {{ display: flex; gap: 12px; margin-bottom: 24px; }}
.card {{ flex: 1; background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 14px; text-align: center; }}
.card-label {{ font-size: 12px; color: #8b949e; text-transform: uppercase; font-weight: 600; margin-bottom: 6px; }}
.card-val {{ font-size: 22px; font-weight: 700; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 16px; background: #161b22; font-size: 14px; }}
th {{ background: #21262d; padding: 10px 12px; text-align: left; color: #8b949e; font-size: 12px; border-bottom: 2px solid #30363d; }}
.footer {{ margin-top: 24px; font-size: 12px; color: #8b949e; border-top: 1px solid #30363d; padding-top: 12px; }}
</style>
</head>
<body>
<div class="container">
    <h2>📈 Kalshi Trading Bot - Evaluation Results</h2>
    <p style="color: #8b949e; margin-bottom: 20px;">Run completed at <strong>{now_utc}</strong> | Phase: <strong>{data.get('phase_name', 'Live Run')}</strong></p>

    <table style="width: 100%; margin-bottom: 20px;">
        <tr>
            <td style="padding: 10px; background: rgba(210, 153, 34, 0.1); border: 1px solid #d29922; border-radius: 6px; width: 48%;">
                <div style="font-size: 11px; color: #d29922; font-weight: 700;">🟢 PROD LIVE PnL (kalshi.com)</div>
                <div style="font-size: 20px; font-weight: 700; color: {'#3fb950' if prod_live.get('pnl_dollars', 0) >= 0 else '#f85149'}; margin: 4px 0;">${prod_live.get('pnl_dollars', 0.0):+.2f}</div>
                <div style="font-size: 12px; color: #8b949e;">{prod_live.get('wins', 0)}W / {prod_live.get('losses', 0)}L ({prod_live.get('win_rate', 0.0):.1f}% WR)</div>
            </td>
            <td style="width: 4%;"></td>
            <td style="padding: 10px; background: rgba(88, 166, 255, 0.1); border: 1px solid #58a6ff; border-radius: 6px; width: 48%;">
                <div style="font-size: 11px; color: #58a6ff; font-weight: 700;">🔵 DEMO LIVE PnL (demo.kalshi.co)</div>
                <div style="font-size: 20px; font-weight: 700; color: {'#3fb950' if demo_live.get('pnl_dollars', 0) >= 0 else '#f85149'}; margin: 4px 0;">${demo_live.get('pnl_dollars', 0.0):+.2f}</div>
                <div style="font-size: 12px; color: #8b949e;">{demo_live.get('wins', 0)}W / {demo_live.get('losses', 0)}L ({demo_live.get('win_rate', 0.0):.1f}% WR)</div>
            </td>
        </tr>
    </table>

    <table style="width: 100%; margin-bottom: 24px;">
        <tr>
            <td style="padding: 10px; background: #21262d; border-radius: 6px; text-align: center; width: 32%;">
                <div style="font-size: 11px; color: #8b949e;">AGGREGATED PNL</div>
                <div style="font-size: 18px; font-weight: 700; color: {pnl_overall_color};">${pnl_dollars:+.2f}</div>
            </td>
            <td style="width: 2%;"></td>
            <td style="padding: 10px; background: #21262d; border-radius: 6px; text-align: center; width: 32%;">
                <div style="font-size: 11px; color: #8b949e;">OVERALL WIN RATE</div>
                <div style="font-size: 18px; font-weight: 700; color: #c9d1d9;">{win_rate:.1f}%</div>
                <div style="font-size: 11px; color: #8b949e;">{wins}W / {losses}L</div>
            </td>
            <td style="width: 2%;"></td>
            <td style="padding: 10px; background: #21262d; border-radius: 6px; text-align: center; width: 32%;">
                <div style="font-size: 11px; color: #8b949e;">TRADES</div>
                <div style="font-size: 18px; font-weight: 700; color: #c9d1d9;">{total_trades}</div>
                <div style="font-size: 11px; color: #8b949e;">{settled_trades} settled</div>
            </td>
        </tr>
    </table>

    <h3 style="color: #c9d1d9; font-size: 16px; margin-bottom: 8px;">Strategy Breakdown</h3>
    <table>
        <thead>
            <tr>
                <th>Strategy</th>
                <th>Env</th>
                <th>Mode</th>
                <th>Trades</th>
                <th>W / L</th>
                <th>Win Rate</th>
                <th>PnL ($)</th>
            </tr>
        </thead>
        <tbody>
            {strat_rows}
        </tbody>
    </table>

    <div class="footer">
        Automated notification from Kalshi Trading Bot on {socket.gethostname()} | Local Dashboard: <a href="http://localhost:8000" style="color: #58a6ff;">http://localhost:8000</a>
    </div>
</div>
</body>
</html>"""

    return text_body, html_body


def send_email(recipient: str, subject: str, text_body: str, html_body: str) -> bool:
    os.makedirs(ROOT_DIR / "log", exist_ok=True)
    report_html_path = ROOT_DIR / "log" / "run_completion_report.html"
    report_txt_path = ROOT_DIR / "log" / "run_completion_report.txt"

    with open(report_html_path, "w") as f:
        f.write(html_body)
    with open(report_txt_path, "w") as f:
        f.write(text_body)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Kalshi Trading Bot <trading-bot@{socket.gethostname()}>"
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=f"{socket.gethostname()}.local")

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    # Check for SMTP credentials in environment
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")

    delivery_log = ROOT_DIR / "log" / "email_delivery.log"

    if smtp_host and smtp_user and smtp_password:
        try:
            print(f"Connecting to authenticated SMTP server {smtp_host}:{smtp_port}...", flush=True)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.sendmail(msg["From"], [recipient], msg.as_string())
            print(f"✅ Successfully sent email report to {recipient} via {smtp_host}!", flush=True)
            with open(delivery_log, "a") as f:
                f.write(f"[{datetime.now(timezone.utc).isoformat()}] Sent to {recipient} via authenticated SMTP {smtp_host}\n")
            return True
        except Exception as e:
            print(f"Notice: Authenticated SMTP failed ({e}). Falling back to direct MX...", flush=True)

    # Fallback to direct MX delivery
    mx_host = "gmail-smtp-in.l.google.com"
    try:
        print(f"Attempting direct SMTP delivery via {mx_host}:25 to {recipient}...", flush=True)
        with smtplib.SMTP(mx_host, 25, timeout=15) as server:
            server.ehlo(socket.getfqdn() or "kalshi-bot")
            try:
                server.starttls()
                server.ehlo(socket.getfqdn() or "kalshi-bot")
            except Exception:
                pass
            server.sendmail("trading-bot@kalshi.local", [recipient], msg.as_string())
        print(f"✅ Successfully delivered email report to {recipient} via {mx_host}!", flush=True)
        with open(delivery_log, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] Delivered to {recipient} via direct MX {mx_host}\n")
        return True
    except Exception as e:
        print(f"❌ Direct SMTP delivery failed: {e}", flush=True)
        with open(delivery_log, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] Direct MX delivery error to {recipient}: {e}\n")
        return False


def main():
    parser = argparse.ArgumentParser(description="Send Trading Bot Evaluation Report via SMTP")
    parser.add_argument("--recipient", type=str, default="theotherogeflagg@gmail.com", help="Email recipient")
    parser.add_argument("--subject", type=str, default=None, help="Email subject")
    args = parser.parse_args()

    data, meta = build_report_data()
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    subject = args.subject or f"Kalshi Bot Evaluation Report ({now_str}) - PnL: ${data.get('summary', {}).get('pnl_dollars', 0.0):+.2f}"

    text_body, html_body = generate_report_content(data, meta, args.recipient)
    send_email(args.recipient, subject, text_body, html_body)


if __name__ == "__main__":
    main()

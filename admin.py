from fastapi import Request, Response, HTTPException
from fastapi.responses import HTMLResponse
import os
import hashlib
import json

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
COOKIE_NAME = "viento_admin"

def make_token(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

def is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME, "")
    return cookie == make_token(ADMIN_KEY)

def login_page(error: bool = False) -> str:
    error_html = '<p style="color:#ef4444;margin:0 0 16px">Wrong password. Try again.</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Viento Cafe — Admin</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0f0f0f; color: #f5f5f5; display: flex;
          align-items: center; justify-content: center; min-height: 100vh; }}
  .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px;
           padding: 40px; width: 100%; max-width: 360px; }}
  h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 8px; }}
  p.sub {{ color: #888; font-size: 14px; margin-bottom: 28px; }}
  input {{ width: 100%; padding: 10px 14px; background: #111; border: 1px solid #333;
           border-radius: 8px; color: #f5f5f5; font-size: 15px; outline: none;
           margin-bottom: 12px; }}
  input:focus {{ border-color: #555; }}
  button {{ width: 100%; padding: 11px; background: #f5f5f5; color: #111;
            border: none; border-radius: 8px; font-size: 15px; font-weight: 600;
            cursor: pointer; }}
  button:hover {{ background: #ddd; }}
</style>
</head>
<body>
<div class="card">
  <h1>☕ Viento Cafe</h1>
  <p class="sub">Admin Dashboard</p>
  {error_html}
  <form method="POST" action="/admin/login">
    <input type="password" name="password" placeholder="Enter password" autofocus />
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>"""

def dashboard_page(records: list) -> str:
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    today_orders = [r for r in records if str(r.get("Timestamp", "")).startswith(today)]

    total_revenue = 0.0
    item_counts = {}
    status_counts = {"Preparing": 0, "Cooking": 0, "Ready": 0}

    for r in today_orders:
        # Parse revenue from items string e.g. "2x Flat White, 1x Croissant"
        items_str = str(r.get("Items", ""))
        for part in items_str.split(","):
            part = part.strip()
            if "x " in part:
                try:
                    qty, item = part.split("x ", 1)
                    qty = int(qty.strip())
                    item = item.strip()
                    item_counts[item] = item_counts.get(item, 0) + qty
                except:
                    pass

        status = str(r.get("Status", "Preparing"))
        if status in status_counts:
            status_counts[status] += 1

    # Calculate revenue from Google Sheet prices column not available,
    # so count orders instead
    total_orders = len(today_orders)
    all_time_orders = len(records)

    # Build orders table rows (last 20, newest first)
    recent = list(reversed(records))[:20]

    def status_badge(s):
        colors = {
            "Preparing": "#f59e0b",
            "Cooking": "#3b82f6",
            "Ready": "#22c55e"
        }
        color = colors.get(s, "#888")
        return f'<span style="background:{color}20;color:{color};padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600">{s}</span>'

    rows = ""
    for r in recent:
        if str(r.get("Order ID", "")) == "N/A":
            continue
        rows += f"""<tr>
            <td>#{r.get('Order ID','')}</td>
            <td>Table {r.get('Table','')}</td>
            <td>{r.get('Name','')}</td>
            <td style="color:#888;font-size:13px">{r.get('Items','')}</td>
            <td>{status_badge(str(r.get('Status','Preparing')))}</td>
            <td style="color:#888;font-size:13px">{str(r.get('Timestamp',''))[11:16]}</td>
        </tr>"""

    top_items = sorted(item_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    top_items_html = "".join(
        f'<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #2a2a2a">'
        f'<span>{item}</span><span style="color:#f59e0b;font-weight:600">{qty}x</span></div>'
        for item, qty in top_items
    ) or '<p style="color:#888;font-size:14px">No orders today</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Viento Cafe — Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0f0f0f; color: #f5f5f5; padding: 24px; }}
  h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .sub {{ color: #888; font-size: 14px; margin-bottom: 28px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 16px; margin-bottom: 28px; }}
  .stat {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px; padding: 20px; }}
  .stat-label {{ font-size: 12px; color: #888; text-transform: uppercase;
                 letter-spacing: 0.05em; margin-bottom: 8px; }}
  .stat-value {{ font-size: 32px; font-weight: 700; }}
  .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px;
           padding: 20px; margin-bottom: 20px; }}
  .card h2 {{ font-size: 15px; font-weight: 600; margin-bottom: 16px; color: #ccc; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; color: #666; font-size: 12px; text-transform: uppercase;
        letter-spacing: 0.05em; padding-bottom: 10px; border-bottom: 1px solid #2a2a2a; }}
  td {{ padding: 12px 0; border-bottom: 1px solid #1e1e1e; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  .logout {{ float: right; color: #888; font-size: 13px; text-decoration: none; }}
  .logout:hover {{ color: #f5f5f5; }}
  .refresh {{ display: inline-block; margin-left: 12px; color: #888; font-size: 13px;
              text-decoration: none; cursor: pointer; }}
</style>
</head>
<body>
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
  <h1>☕ Viento Cafe</h1>
  <a href="/admin/logout" class="logout">Sign out</a>
</div>
<p class="sub">Dashboard · Today: {today} 
  <a href="/admin" class="refresh">↻ Refresh</a>
</p>

<div class="grid">
  <div class="stat">
    <div class="stat-label">Today's Orders</div>
    <div class="stat-value">{total_orders}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Preparing</div>
    <div class="stat-value" style="color:#f59e0b">{status_counts['Preparing']}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Ready</div>
    <div class="stat-value" style="color:#22c55e">{status_counts['Ready']}</div>
  </div>
  <div class="stat">
    <div class="stat-label">All Time Orders</div>
    <div class="stat-value" style="font-size:24px">{all_time_orders}</div>
  </div>
</div>

<div style="display:grid;grid-template-columns:2fr 1fr;gap:20px">
  <div class="card">
    <h2>Recent Orders</h2>
    <table>
      <thead>
        <tr>
          <th>Order</th>
          <th>Table</th>
          <th>Name</th>
          <th>Items</th>
          <th>Status</th>
          <th>Time</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <div class="card">
    <h2>Today's Top Items</h2>
    {top_items_html}
  </div>
</div>
</body>
</html>"""

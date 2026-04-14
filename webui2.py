import os
import sys
# 確保 Python 可以找到 `src/` 內的模組
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))
import json
import threading
import traceback
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sock import Sock
from datetime import datetime
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from src.main import run_hedge_fund

# 加載 .env 環境變數
load_dotenv()

# Discord Webhook 設定
# 預設關閉，需在 .env 中設定 DISCORD_WEBHOOK_ENABLED=true 才會啟用
DISCORD_WEBHOOK_ENABLED = os.environ.get("DISCORD_WEBHOOK_ENABLED", "false").lower() == "true"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# 設置 Flask 伺服器
app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app, resources={r"/*": {"origins": "*"}})  # 允許跨域請求
sock = Sock(app)

# WebSocket 客戶端列表
websocket_clients = []

def send_discord_notification(tickers, result, analysis_date):
    """發送分析結果到 Discord"""
    # 檢查是否啟用 Discord 通知
    if not DISCORD_WEBHOOK_ENABLED:
        return
    
    if not DISCORD_WEBHOOK_URL:
        print("[Discord] DISCORD_WEBHOOK_URL 未設定，跳過通知")
        return
    
    try:
        # 構建 Discord Embed 訊息
        embeds = []
        
        # 主要標題 Embed
        main_embed = {
            "title": "🤖 AI Hedge Fund 分析報告",
            "description": f"**分析日期:** {analysis_date}\n**標的:** {', '.join(tickers)}",
            "color": 0x00ff00,  # 綠色
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {
                "text": "AI Hedge Fund API"
            }
        }
        embeds.append(main_embed)
        
        # 決策結果 Embed
        if "decisions" in result:
            for ticker, decision in result["decisions"].items():
                action = decision.get("action", "N/A").upper()
                confidence = decision.get("confidence", 0)
                quantity = decision.get("quantity", 0)
                reasoning = decision.get("reasoning", "N/A")
                
                # 根據動作設定顏色
                if action == "BUY":
                    color = 0x00ff00  # 綠色
                    emoji = "🟢"
                elif action == "SELL" or action == "SHORT":
                    color = 0xff0000  # 紅色
                    emoji = "🔴"
                else:
                    color = 0xffff00  # 黃色
                    emoji = "🟡"
                
                decision_embed = {
                    "title": f"{emoji} {ticker} - {action}",
                    "fields": [
                        {"name": "信心度", "value": f"{confidence}%", "inline": True},
                        {"name": "數量", "value": str(quantity), "inline": True},
                        {"name": "分析理由", "value": reasoning[:1000] if len(reasoning) > 1000 else reasoning, "inline": False}
                    ],
                    "color": color
                }
                embeds.append(decision_embed)
        
        # 分析師信號摘要 Embed
        if "analyst_signals" in result:
            signals_summary = []
            for agent_name, signals in result["analyst_signals"].items():
                if agent_name == "risk_management_agent":
                    continue
                for ticker, signal_data in signals.items():
                    signal = signal_data.get("signal", "N/A")
                    conf = signal_data.get("confidence", 0)
                    
                    if signal == "bullish":
                        emoji = "🟢"
                    elif signal == "bearish":
                        emoji = "🔴"
                    else:
                        emoji = "🟡"
                    
                    agent_display = agent_name.replace("_agent", "").replace("_", " ").title()
                    signals_summary.append(f"{emoji} **{agent_display}**: {signal} ({conf}%)")
            
            if signals_summary:
                signals_embed = {
                    "title": "📊 分析師信號摘要",
                    "description": "\n".join(signals_summary[:15]),  # 限制顯示前15個
                    "color": 0x0099ff
                }
                embeds.append(signals_embed)
        
        # 發送到 Discord
        payload = {
            "username": "AI Hedge Fund",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/2103/2103633.png",
            "embeds": embeds[:10]  # Discord 限制最多10個 embeds
        }
        
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        
        if response.status_code == 204:
            print(f"[Discord] 通知發送成功")
        else:
            print(f"[Discord] 通知發送失敗: {response.status_code} - {response.text}")
            
    except Exception as e:
        print(f"[Discord] 發送通知時發生錯誤: {str(e)}")

def broadcast_log(message, level="info"):
    log_data = {"level": level, "message": message}
    for client in websocket_clients[:]:
        try:
            client.send(json.dumps(log_data))
        except Exception:
            websocket_clients.remove(client)

@app.route('/api/health', methods=['GET'])
def health_check():
    """健康檢查端點"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })

@app.route('/docs')
@app.route('/swagger')
def swagger_ui():
    """Swagger UI 文檔頁面"""
    return '''
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Hedge Fund API - Swagger UI</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.9.0/swagger-ui.css">
    <style>
        body { margin: 0; padding: 0; }
        .swagger-ui .topbar { display: none; }
    </style>
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5.9.0/swagger-ui-bundle.js"></script>
    <script>
        window.onload = function() {
            SwaggerUIBundle({
                url: "/static/swagger.json",
                dom_id: '#swagger-ui',
                presets: [
                    SwaggerUIBundle.presets.apis,
                    SwaggerUIBundle.SwaggerUIStandalonePreset
                ],
                layout: "BaseLayout"
            });
        };
    </script>
</body>
</html>
'''

@app.route('/api/analysis', methods=['POST'])
def run_analysis():
    """執行對股票的分析"""
    try:
        data = request.get_json()
        ticker_list = data.get('tickers', '').split(',')
        selected_analysts = data.get('selectedAnalysts', [])
        model_name = data.get('modelName')

        # 設定開始與結束時間
        end_date = data.get('endDate') or datetime.now().strftime('%Y-%m-%d')
        start_date = data.get('startDate') or (datetime.strptime(end_date, '%Y-%m-%d') - relativedelta(months=3)).strftime('%Y-%m-%d')

        # 初始投資組合
        portfolio = {
            "cash": data.get('initialCash', 100000),
            "positions": {},
            "cost_basis": {},
            "realized_gains": {ticker: {"long": 0.0, "short": 0.0} for ticker in ticker_list}
        }

        # 執行完整分析
        broadcast_log(f"Starting analysis for {ticker_list}", "info")
        from src.llm.models import get_model_info
        model_info = get_model_info(model_name)
        model_provider = model_info.provider.value if model_info else "OpenAI"

        result = run_hedge_fund(
            tickers=ticker_list,
            start_date=start_date,
            end_date=end_date,
            portfolio=portfolio,
            show_reasoning=True,
            selected_analysts=selected_analysts,
            model_name=model_name,
            model_provider=model_provider,
            is_crypto=False
        )

        broadcast_log("Analysis completed successfully", "success")
        
        # 發送 Discord 通知
        send_discord_notification(ticker_list, result, end_date)
        
        return jsonify(result)

    except Exception as e:
        error_message = f"API Error: {str(e)}"
        broadcast_log(error_message, "error")
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500

@sock.route('/ws/logs')
def logs(ws):
    """WebSocket 端點來監控日誌"""
    websocket_clients.append(ws)
    try:
        while True:
            ws.receive()  # 只是保持連線，前端不會傳送訊息
    except Exception:
        websocket_clients.remove(ws)

if __name__ == "__main__":
    api_thread = threading.Thread(target=app.run, kwargs={"host": "0.0.0.0", "port": 9876, "debug": True, "use_reloader": False})
    api_thread.daemon = True
    api_thread.start()
    print("API Server started on http://localhost:9876")
    api_thread.join()

import os
import json
import glob
import gradio as gr
import time
from google import genai
from google.genai import errors,types

# 1. 讀取 API Key (建議從環境變數讀取，見工部署最安全)
# 如果你在本地端測試，可以在 Terminal 執行: export GEMINI_API_KEY="你的Key"
# 或者直接將下面改為: os.environ.get("GEMINI_API_KEY", "你的_API_KEY")
client = genai.Client()

# 2. 自動讀取 knowledge_base 資料夾結構
def load_knowledge_base(kb_path="knowledge_base"):
    full_text = []
    
    # 讀取公司政策等通用文件 (.md)
    policy_files = glob.glob(f"{kb_path}/*.md")
    for filepath in policy_files:
        with open(filepath, "r", encoding="utf-8") as f:
            full_text.append(f"=== {os.path.basename(filepath)} ===\n" + f.read())
            
    # 讀取各大茶園 (Gardens)
    gardens_dir = os.path.join(kb_path, "gardens")
    if os.path.exists(gardens_dir):
        for garden_name in os.listdir(gardens_dir):
            garden_path = os.path.join(gardens_dir, garden_name)
            if os.path.isdir(garden_path):
                full_text.append(f"\n==========================================")
                full_text.append(f"【茶園專區】：{garden_name}")
                full_text.append(f"==========================================")
                
                # 讀取茶園背景故事
                info_file = os.path.join(garden_path, "garden_info.md")
                if os.path.exists(info_file):
                    with open(info_file, "r", encoding="utf-8") as f:
                        full_text.append(f.read())
                
                # 讀取茶園旗下產品列表
                prod_file = os.path.join(garden_path, "products.json")
                if os.path.exists(prod_file):
                    with open(prod_file, "r", encoding="utf-8") as f:
                        products = json.load(f)
                        full_text.append("\n【旗下產品系列】：")
                        for p in products:
                            full_text.append(
                                f"- ID: {p['id']} | 名稱: {p['name']} | 級數: {p['grade']}\n"
                                f"  風味: {p['flavor_profile']} | 口感: {p['taste_notes']}\n"
                                f"  用途: {p['usage']} | 參考標價: HK${p['base_price']}"
                            )
                            
    return "\n\n".join(full_text)

# 載入所有資料夾內容作為靜態知識庫
STORE_KNOWLEDGE_BASE = load_knowledge_base()

# 3. 模擬動態庫存資料庫 (Function Calling 專用)
INVENTORY_DB = {
    "SF-001": {"stock": 5,  "sale_price": 280, "on_sale": False}, # 天雲
    "SF-002": {"stock": 12, "sale_price": 160, "on_sale": True},  # 瑞雲 (特價中)
    "MD-001": {"stock": 3,  "sale_price": 95,  "on_sale": True},  # 山綠 (特價中)
    "KM-001": {"stock": 0,  "sale_price": 85,  "on_sale": False}, # 木漏 (缺貨)
}

def check_inventory_and_price(product_id_or_name: str) -> str:
    """
    根據產品 ID 或名稱，查詢該抹茶產品的實時庫存數量與優惠價。
    """
    for p_id, item in INVENTORY_DB.items():
        if p_id in product_id_or_name or product_id_or_name in p_id:
            return json.dumps({
                "product_id": p_id,
                "in_stock": item["stock"] > 0,
                "stock_quantity": item["stock"],
                "current_price": f"HK${item['sale_price']}",
                "is_on_sale": item["on_sale"],
                "status": "🔥 特價優惠中" if item["on_sale"] else "原價發售",
                "availability": "❌ 暫時缺貨" if item["stock"] == 0 else f"✅ 現貨剩餘 {item['stock']} 件"
            }, ensure_ascii=False)
            
    return json.dumps({"note": "庫存充足，正常發售"}, ensure_ascii=False)

# 4. System Instruction 設定
SYSTEM_INSTRUCTION = f"""
你是一位專業的「綠韻抹茶」線上茶藝顧問。

請遵循以下原則回答顧客：
1. 【茶園與產品知識】：講述茶園歷史、風味風格、推薦產品級數時，請「嚴格根據」【店家茶園知識庫】的資料回答。
2. 【動態查庫存】：當涉及產品價格、是否有現貨，或主動向顧客推介某款產品時，必須調用 `check_inventory_and_price` 工具查詢實時庫存與優惠！
3. 【公司政策】：關於門市地址、運費、付款及退換貨，請根據知識庫內容回答。
4. 【語氣】：親切禮貌、專業，使用香港廣東話/繁體中文回答。

【店家茶園知識庫】：
{STORE_KNOWLEDGE_BASE}
"""

# 5. 對話處理邏輯 (具備 Retry 與 Model Fallback 雙重防禦)import time
def chat_response(user_message, history):
    tools = [check_inventory_and_price]
    contents = []
    
    for msg in history:
        if isinstance(msg, dict):
            role = "user" if msg.get("role") == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=str(msg.get("content", "")))]))
        elif isinstance(msg, (list, tuple)) and len(msg) == 2:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=str(msg[0]))]))
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=str(msg[1]))]))

    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_message)]))

    models_to_try = [
    "gemini-flash-latest",     # 動態指向最新的 Flash 模型 (最保險)
    "gemini-3.7-flash",        # 最新旗艦 Flash (2026年8月上市)
    "gemini-3.6-flash",        # 穩定版 Gemini 3.6 Flash
    "gemini-3.5-flash-lite"    # 超低延遲/省 Quota 備用模型
    ]

    last_error_msg = ""

    for model_name in models_to_try:
        max_retries = 2
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        tools=tools,
                        temperature=0.3
                    )
                )
                return response.text

            except Exception as e:
                last_error_msg = str(e)
                print(f"⚠️ [{model_name}] Retry {attempt + 1}: {e}")
                time.sleep(1)

    # 💡 當所有 Retry 同 Fallback 都爆咗，輸出一個極具專業度的 Error Banner
    return f"""⚠️ **[System Notice: Google API Service Busy]**

Google Gemini API 伺服器目前處於高流量狀態 (High Demand)。
系統已嘗試自動重試 (Auto-Retry) 及模型切換 (Model Failover)，但 API 暫時未有回應。

**原廠 Error Log:**
> `{last_error_msg}`

💡 *請稍等幾秒後再發送一次訊息，感謝您的測試！*"""


    tools = [check_inventory_and_price]
    
    contents = []
    
    # 處理 Gradio history (相容 Dict 與 Tuple 格式)
    for msg in history:
        if isinstance(msg, dict):
            role = "user" if msg.get("role") == "user" else "model"
            content_text = msg.get("content", "")
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=str(content_text))]))
        elif isinstance(msg, (list, tuple)) and len(msg) == 2:
            human, ai = msg
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=str(human))]))
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=str(ai))]))

    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_message)]))


# 6. Gradio UI 介面

# 頁面頂部的說明標籤
description_markdown = """
### 🍵 抹茶小助手 - AI 客服專案 PoC
歡迎測試對話！本專案整合 **Google Gemini API** 與 **Function Calling (庫存/價格實時查詢)**。

> 💡 **測試提示 (System Notice)**：
> * 本專案使用 Google API 免費測試方案 (Free Tier Rate Limit)。若短時間內連續發送訊息觸發流量限制 (429 Rate Limit)，系統會啟動 **Auto-Retry & Model Failover** 容錯機制。
> * 如遇到回應較慢或提示額度上限，請稍候 30-40 秒再試，感謝您的體貼測試！
"""


# 💡 用 gr.Blocks 指定主題包著 ChatInterface
with gr.Blocks(theme=gr.themes.Soft(primary_hue="emerald", neutral_hue="slate")) as demo:
    gr.ChatInterface(
        fn=chat_response,
        title="Matcha Store AI Assistant",
        description=description_markdown,
        examples=[
            "介紹下「翠風茶園」既歷史同風格？",
            "我想搵一款適合整抹茶 Lattee 既產品，有冇特價？",
            "烘焙專用既抹茶粉有貨嗎？幾多錢？",
            "買滿幾多錢免運費？可唔可以去門市自取？"
        ]
    )

if __name__ == "__main__":
    # 讀取 Render 自動指派的 PORT，預設為 7860
    server_port = int(os.environ.get("PORT", 7860))
    
    # 務必指定 server_name="0.0.0.0" 讓 Render 能對外連線
    demo.launch(server_name="0.0.0.0", server_port=server_port)

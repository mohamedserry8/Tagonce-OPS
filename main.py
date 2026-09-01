import os
import re
import time
import json
import pytz
import gspread
import pandas as pd
from datetime import datetime
from slack_sdk import WebClient

SLACK_TOKEN = os.environ["SLACK_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]
SHEET_NAME = os.environ["SHEET_NAME"]

# تحديد توقيت مصر
egypt_tz = pytz.timezone('Africa/Cairo')

start_date = datetime(2026, 8, 1).timestamp()
client = WebClient(token=SLACK_TOKEN)

target_keywords = ["match id:", "category:", "details:"]
data = []
user_cache = {}
usergroup_cache = None

def resolve_slack_text(text):
    global usergroup_cache
    def replace_user(match):
        uid = match.group(1)
        if uid in user_cache: return f"@{user_cache[uid]}"
        try:
            res = client.users_info(user=uid)
            name = res['user'].get('real_name') or res['user'].get('name')
            user_cache[uid] = name
            return f"@{name}"
        except: return f"@{uid}"

    text = re.sub(r'<@([UW][A-Z0-9]+)>', replace_user, text)
    
    def replace_subteam(match):
        global usergroup_cache
        sid = match.group(1)
        if usergroup_cache is None:
            usergroup_cache = {}
            try:
                res = client.usergroups_list()
                for ug in res.get('usergroups', []): usergroup_cache[ug['id']] = ug['handle']
            except: pass
        if sid in usergroup_cache: return f"@{usergroup_cache[sid]}"
        return f"@subteam"

    text = re.sub(r'<!subteam\^([S][A-Z0-9]+)(?:\|[^>]+)?>', replace_subteam, text)
    text = re.sub(r'<[^|>]+\|([^>]+)>', r'\1', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    return text

print("جاري سحب البيانات من Slack...")
cursor = None
has_more = True

while has_more:
    time.sleep(1) 
    result = client.conversations_history(channel=CHANNEL_ID, limit=1000, cursor=cursor, oldest=start_date)
    messages = result.get("messages", [])
    
    for msg in messages:
        raw_text = msg.get("text", "")
        if "blocks" in msg:
            for block in msg["blocks"]:
                if block.get("type") == "section" and "text" in block:
                    raw_text += " \n " + block["text"].get("text", "")
        if "attachments" in msg:
            for att in msg["attachments"]:
                raw_text += " \n " + att.get("fallback", "") + " \n " + att.get("text", "") + " \n " + att.get("pretext", "")

        clean_text = resolve_slack_text(raw_text).replace('*', '')
        lower_text = clean_text.lower()

        if all(k in lower_text for k in target_keywords):
            # تحويل التوقيت لمصر
            msg_ts = msg.get("ts")
            formatted_time = datetime.fromtimestamp(float(msg_ts), egypt_tz).strftime("%Y-%m-%d %H:%M:%S") if msg_ts else "غير متوفر"

            match_id = re.search(r"Match ID:\s*(\d+)", clean_text, re.IGNORECASE)
            arqam_id = re.search(r"ArqamId\s*:\s*([^\n]+)", clean_text, re.IGNORECASE)
            match_name = re.search(r"Match:\s*([^\n]+)", clean_text, re.IGNORECASE)
            date_val = re.search(r"Date:\s*([^\n]+)", clean_text, re.IGNORECASE)
            
            category_match = re.search(r"Category:\s*([^\n]+)", clean_text, re.IGNORECASE)
            cat_main, cat_sub = "", ""
            if category_match:
                cat_full = category_match.group(1).strip()
                if "-" in cat_full:
                    cat_parts = cat_full.split("-", 1)
                    cat_main = cat_parts[0].strip()
                    cat_sub = cat_parts[1].strip()
                else: cat_main = cat_full

            details_match = re.search(r"Details:\s*(.*?)(?=Origin:)", clean_text, re.IGNORECASE | re.DOTALL)
            details_text = details_match.group(1).strip() if details_match else "غير متوفر"

            reporter_match = re.search(r"Reporter:\s*([^\n]+)", clean_text, re.IGNORECASE)
            reporter_text = reporter_match.group(1).strip() if reporter_match else "غير متوفر"

            mentions_match = re.search(r"Reporter:[^\n]*\n(.*?)(?=React:)", clean_text, re.IGNORECASE | re.DOTALL)
            mentions_text = re.sub(r"-+", "", mentions_match.group(1)).strip() if mentions_match else ""

            first_comment, last_comment, last_comment_time = "", "", ""
            has_comments = "نعم" if msg.get("reply_count", 0) > 0 else "لا"
            
            if has_comments == "نعم":
                try:
                    time.sleep(1.5) 
                    replies = client.conversations_replies(channel=CHANNEL_ID, ts=msg["ts"])
                    replies_list = replies.get("messages", [])
                    if len(replies_list) > 1:
                        first_comment = resolve_slack_text(replies_list[1].get("text", ""))
                        last_msg = replies_list[-1]
                        last_comment = resolve_slack_text(last_msg.get("text", ""))
                        if last_msg.get("ts"):
                            # تحويل التوقيت لمصر في التعليقات
                            last_comment_time = datetime.fromtimestamp(float(last_msg.get("ts")), egypt_tz).strftime("%Y-%m-%d %H:%M:%S")
                except: pass
            
            data.append({
                "Date & Time": formatted_time,
                "Match ID": match_id.group(1).strip() if match_id else "غير متوفر",
                "Arqam ID": arqam_id.group(1).strip() if arqam_id else "غير متوفر",
                "Match": match_name.group(1).strip() if match_name else "غير متوفر",
                "Match Date": date_val.group(1).strip() if date_val else "غير متوفر",
                "Main Category": cat_main,
                "Sub Category": cat_sub,
                "Issue Details": details_text,
                "Reporter": reporter_text,
                "Mentions in Issue": mentions_text,
                "First Comment": first_comment,
                "Last Comment": last_comment,
                "Last Comment Time": last_comment_time
            })
    
    has_more = result.get("has_more", False)
    if has_more: cursor = result.get("response_metadata", {}).get("next_cursor")

if data:
    print("جاري تحديث جوجل شيت...")
    df = pd.DataFrame(data)
    
    credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    gc = gspread.service_account_from_dict(credentials_dict)
    sh = gc.open(SHEET_NAME)
    worksheet = sh.sheet1
    
    worksheet.clear()
    worksheet.update([df.columns.values.tolist()] + df.values.tolist())
    print("تم تحديث جوجل شيت بنجاح!")
else:
    print("لم يتم العثور على بيانات جديدة.")

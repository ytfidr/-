#!/usr/bin/env python3
"""
voice_server.py 补丁2：
1. 扩展查询关键词，加入"任务状态"
2. Gemma4 结果写入前清洗，过滤空内容和特殊字符
3. TTS 前加保底文本，防止空串送入
"""

import shutil, sys
from pathlib import Path

TARGET = Path("/root/voice_app/voice_server.py")
BACKUP = Path("/root/voice_app/voice_server.py.bak_local_task2")

shutil.copy2(TARGET, BACKUP)
print(f"✅ 已备份 → {BACKUP}")

src = TARGET.read_text(encoding="utf-8")

# ── 1. 扩展查询关键词（加入"任务状态"已在原代码，但"no return"说明没匹配到）
# 原因：chat/text 里用户说"任务状态"，但 current_llm_mode 可能是 online
# 所以查询指令要在 llm_mode 判断之前处理

OLD_QUERY_BLOCK = '''    # ── local mod 异步任务处理 ──────────────────────────────────────────────
    current_llm_mode = get_llm_mode(uname)
    if current_llm_mode == "local":
        # 查询结果指令
        query_kw = ["查看结果", "结果怎么样", "好了吗", "完成了吗", "任务状态"]
        if any(kw in text for kw in query_kw):'''

NEW_QUERY_BLOCK = '''    # ── 任务查询：无论当前 llm_mode，只要有关键词就响应 ────────────────────
    query_kw = ["查看结果", "结果怎么样", "好了吗", "完成了吗", "任务状态", "看结果", "出来了吗"]
    if any(kw in text for kw in query_kw):
        task = _local_tasks.get(uname)
        if not task:
            reply = "目前没有后台任务记录。"
        elif task["status"] == "running":
            reply = "任务还在运行中，完成后会通知你，请稍候。"
        elif task["status"] == "error":
            reply = f"任务执行出错：{task['result'][:60]}"
        else:
            result = task["result"]
            reply = result if len(result) <= 200 else result[:200] + "……内容较长，已截取前段。"
        audio_data = await tts_synthesize(reply)
        audio_b64 = base64.b64encode(audio_data).decode() if audio_data else None
        return JSONResponse({
            "text": reply, "audio": audio_b64,
            "session_id": req.get("session_id", ""), "switch_user": False,
        })

    # ── local mod 异步任务处理 ──────────────────────────────────────────────
    current_llm_mode = get_llm_mode(uname)
    if current_llm_mode == "local":
        # 查询结果指令（已在上方处理，此处只处理提交）
        if any(kw in text for kw in ["查看结果", "结果怎么样", "好了吗", "完成了吗", "任务状态", "看结果", "出来了吗"]):'''

if OLD_QUERY_BLOCK not in src:
    print("❌ 找不到 local mod 任务处理块，请检查")
    sys.exit(1)

src = src.replace(OLD_QUERY_BLOCK, NEW_QUERY_BLOCK, 1)
print("✅ 已将查询关键词提升到 llm_mode 判断之前")

# ── 2. 修复 _run_local_task 里结果写入前清洗 ─────────────────────────────
OLD_RESULT = '''        result = resp.json().get("response", "（无内容）")
        _local_tasks[user] = {"status": "done", "prompt": prompt, "result": result}
        preview = result[:40].replace("\\n", " ")'''

NEW_RESULT = '''        raw = resp.json().get("response", "")
        # 清洗：去除首尾空白，过滤纯符号/空内容
        import re as _re
        result = raw.strip()
        result = _re.sub(r"[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]", "", result)
        if not result or not _re.search(r"[\\u4e00-\\u9fffA-Za-z0-9]", result):
            result = "（模型返回内容为空或无法识别）"
        _local_tasks[user] = {"status": "done", "prompt": prompt, "result": result}
        preview = result[:40].replace("\\n", " ")'''

if OLD_RESULT not in src:
    print("❌ 找不到 _run_local_task 结果写入行，请检查")
    sys.exit(1)

src = src.replace(OLD_RESULT, NEW_RESULT, 1)
print("✅ 已添加 Gemma4 结果清洗逻辑")

# ── 3. tts_synthesize 前加保底，防止空串 ─────────────────────────────────
OLD_TTS_GUARD = '''async def tts_synthesize(text: str) -> bytes | None:
    try:'''

NEW_TTS_GUARD = '''async def tts_synthesize(text: str) -> bytes | None:
    # 保底：空内容不送 TTS
    if not text or not text.strip():
        log.warning("TTS skipped: empty text")
        return None
    try:'''

if OLD_TTS_GUARD not in src:
    print("❌ 找不到 tts_synthesize 函数，请检查")
    sys.exit(1)

src = src.replace(OLD_TTS_GUARD, NEW_TTS_GUARD, 1)
print("✅ 已添加 TTS 空串保底")

TARGET.write_text(src, encoding="utf-8")
print("✅ 已写回 voice_server.py")
print()
print("下一步：")
print("  systemctl restart voice-server")
print("  journalctl -u voice-server -f")

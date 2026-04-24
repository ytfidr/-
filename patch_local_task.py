#!/usr/bin/env python3
"""
voice_server.py 补丁：异步本地 LLM 任务
用法：python3 patch_local_task.py
"""

import re, shutil, sys
from pathlib import Path

TARGET = Path("/root/voice_app/voice_server.py")
BACKUP = Path("/root/voice_app/voice_server.py.bak_local_task")

# ── 1. 备份 ────────────────────────────────────────────────────────────────
shutil.copy2(TARGET, BACKUP)
print(f"✅ 已备份 → {BACKUP}")

src = TARGET.read_text(encoding="utf-8")

# ── 2. 在 llm_call 函数之前插入：任务存储 + 后台任务函数 ──────────────────
TASK_CODE = '''
# ──────────────────────────────────────────────
# 异步本地 LLM 任务队列
# ──────────────────────────────────────────────
_local_tasks: dict[str, dict] = {}
# 格式: {user: {"status": "running"|"done"|"error", "prompt": str, "result": str}}

async def _run_local_task(user: str, prompt: str):
    """后台跑 Gemma4，完成后 Bark 通知"""
    _local_tasks[user] = {"status": "running", "prompt": prompt, "result": ""}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": "gemma4-uncensored",
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "num_ctx": 512,
                        "num_predict": 256,
                        "num_thread": 4,
                    },
                },
            )
        result = resp.json().get("response", "（无内容）")
        _local_tasks[user] = {"status": "done", "prompt": prompt, "result": result}
        preview = result[:40].replace("\\n", " ")
        asyncio.create_task(bark("本地任务完成", f"{user}｜{preview}…"))
    except Exception as e:
        err = str(e)[:80]
        _local_tasks[user] = {"status": "error", "prompt": prompt, "result": err}
        asyncio.create_task(bark("本地任务失败", f"{user}｜{err}"))

def _detect_local_task(text: str) -> bool:
    """判断是否是提交后台任务的指令（local mod 下非查询指令）"""
    query_kw = ["查看结果", "结果怎么样", "好了吗", "完成了吗", "任务状态"]
    return not any(kw in text for kw in query_kw)

def _get_task_reply(user: str) -> str | None:
    """查询任务结果，返回回复文本；无任务返回 None"""
    query_kw = ["查看结果", "结果怎么样", "好了吗", "完成了吗", "任务状态"]
    return None  # 由调用方判断关键词后调用 _local_tasks.get

'''

# 插入位置：llm_call 函数定义之前
anchor = "\nasync def llm_call("
if anchor not in src:
    print("❌ 找不到 llm_call 函数，请检查文件")
    sys.exit(1)

src = src.replace(anchor, TASK_CODE + anchor, 1)
print("✅ 已插入异步任务代码块")

# ── 3. 替换 llm_call 中 local 模式的处理逻辑 ──────────────────────────────
OLD_LOCAL = '''    mode = get_llm_mode(user)
    if mode == "local":
        try:
            text = await llm_gemma4(messages)
            return text, "gemma4-local"
        except Exception as e:
            asyncio.create_task(bark("LLM全挂", f"{user}｜本地Gemma4失败｜{str(e)[:60]}"))
        return "我现在有点问题，稍后再试。", "fallback"'''

NEW_LOCAL = '''    mode = get_llm_mode(user)
    if mode == "local":
        # local 模式：不走实时推理，由调用方提交后台任务
        return "__LOCAL_ASYNC__", "gemma4-local-async"'''

if OLD_LOCAL not in src:
    print("❌ 找不到 llm_call local 分支，请手动检查")
    sys.exit(1)

src = src.replace(OLD_LOCAL, NEW_LOCAL, 1)
print("✅ 已替换 llm_call local 分支")

# ── 4. 替换 chat/text 中 llm_switch 之后的正常对话流程 ──────────────────────
# 在 llm_switch 处理之后、history/messages 构建之前插入 local 任务逻辑

OLD_CHAT = '''    system = SYSTEM_PROMPT_ELDER if mode == "elder" else SYSTEM_PROMPT_NORMAL
    history = db_get_context(uname)
    messages = [{"role": "system", "content": system}] + history + [
        {"role": "user", "content": text}
    ]
    reply, chain = await llm_call(messages, uname)
    if chain != "fallback":
        db_save(uname, "user", text)
        db_save(uname, "assistant", reply)
    audio_data = await tts_synthesize(reply)
    audio_b64 = None
    if audio_data:
        import base64
        audio_b64 = base64.b64encode(audio_data).decode()
    return JSONResponse({
        "text":        reply,
        "audio":       audio_b64,
        "session_id":  req.get("session_id", ""),
        "switch_user": False,
        "switch_mode": switch_mode,
    })'''

NEW_CHAT = '''    # ── local mod 异步任务处理 ──────────────────────────────────────────────
    current_llm_mode = get_llm_mode(uname)
    if current_llm_mode == "local":
        # 查询结果指令
        query_kw = ["查看结果", "结果怎么样", "好了吗", "完成了吗", "任务状态"]
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
                reply = result if len(result) <= 200 else result[:200] + "……（内容较长，已截取前段）"
            audio_data = await tts_synthesize(reply)
            audio_b64 = base64.b64encode(audio_data).decode() if audio_data else None
            return JSONResponse({
                "text": reply, "audio": audio_b64,
                "session_id": req.get("session_id", ""), "switch_user": False,
            })
        # 提交后台任务
        existing = _local_tasks.get(uname, {})
        if existing.get("status") == "running":
            reply = "上一个任务还在运行中，完成后会通知你。"
        else:
            asyncio.create_task(_run_local_task(uname, text))
            reply = "好的，已提交后台任务，完成后发送通知，你可以切换在线模式继续对话。"
        audio_data = await tts_synthesize(reply)
        audio_b64 = base64.b64encode(audio_data).decode() if audio_data else None
        return JSONResponse({
            "text": reply, "audio": audio_b64,
            "session_id": req.get("session_id", ""), "switch_user": False,
        })

    # ── online 模式正常流程 ───────────────────────────────────────────────
    system = SYSTEM_PROMPT_ELDER if mode == "elder" else SYSTEM_PROMPT_NORMAL
    history = db_get_context(uname)
    messages = [{"role": "system", "content": system}] + history + [
        {"role": "user", "content": text}
    ]
    reply, chain = await llm_call(messages, uname)
    if chain != "fallback":
        db_save(uname, "user", text)
        db_save(uname, "assistant", reply)
    audio_data = await tts_synthesize(reply)
    audio_b64 = None
    if audio_data:
        import base64
        audio_b64 = base64.b64encode(audio_data).decode()
    return JSONResponse({
        "text":        reply,
        "audio":       audio_b64,
        "session_id":  req.get("session_id", ""),
        "switch_user": False,
        "switch_mode": switch_mode,
    })'''

if OLD_CHAT not in src:
    print("❌ 找不到 chat/text 正常对话流程，请手动检查")
    sys.exit(1)

src = src.replace(OLD_CHAT, NEW_CHAT, 1)
print("✅ 已替换 chat/text 本地任务逻辑")

# ── 5. 写回文件 ────────────────────────────────────────────────────────────
TARGET.write_text(src, encoding="utf-8")
print("✅ 已写回 voice_server.py")
print()
print("下一步：")
print("  systemctl restart voice-server")
print("  journalctl -u voice-server -f")

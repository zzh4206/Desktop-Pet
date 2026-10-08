# -*- coding: utf-8 -*-
"""test_v21_swe_env.py —— v0.21 mini-swe 集成 M1 沙箱执行器单测（纯 Python，无 Qt）。

覆盖：工作区锁定 / 黑名单硬拒 / 危险确认（取消·放行·无通道 fail-closed）/
自定义黑名单 / 超时杀树 / 后台子进程孤儿清理 / 输出截断。
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.swe_env import SweEnvironment, scan_command

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_dead(pid, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


ws = tempfile.mkdtemp(prefix="swe_env_test_")
env = SweEnvironment(ws, command_timeout_s=5.0, output_max_chars=8000)

# ---- M1.1 正常执行：工作区锁定 ----
r = env.execute("pwd")
check("M1.1a pwd 落在工作区", r["returncode"] == 0
      and r["output"].strip() == env.workspace)
r = env.execute("echo hello")
check("M1.1b echo 正常返回", r["returncode"] == 0
      and r["output"].strip() == "hello" and not r["blocked"])

# ---- M1.2 非零返回码透传 ----
r = env.execute("false")
check("M1.2 非零返回码透传", r["returncode"] == 1)

# ---- M1.3 黑名单硬拒（fail-closed，不执行）----
cases = [
    ("rm -rf /", "递归强删"),
    ("sudo rm x", "提权"),
    ("echo $(whoami)", "命令替换"),
    ("echo `whoami`", "命令替换"),
    ("cd ..", "路径穿越"),
    ("cat /etc/passwd", "越出工作区"),
    ("ls ~", "家目录逃逸"),
    ("shutdown -h now", "关机"),
]
for cmd, why in cases:
    decision, reason = scan_command(cmd, ws)
    check(f"M1.3a 硬拒 {cmd!r}（{reason}）",
          decision == "block" and why in reason)
    r = env.execute(cmd)
    check(f"M1.3b execute 拦截 {cmd!r}", r["blocked"] and r["returncode"] == -1)

# ---- M1.4 危险命令确认：取消 / 放行 / 无通道 ----
f = os.path.join(ws, "foo.txt")
with open(f, "w") as fh:
    fh.write("x")

cancel_env = SweEnvironment(ws, confirm_fn=lambda *a: False)
r = cancel_env.execute("rm foo.txt")
check("M1.4a 危险命令取消 → 拦截且文件保留", r["blocked"]
      and r["reason"] == "用户取消" and os.path.exists(f))

ok_env = SweEnvironment(ws, confirm_fn=lambda *a: True)
r = ok_env.execute("rm foo.txt")
check("M1.4b 危险命令放行 → 执行且文件删除", (not r["blocked"])
      and r["returncode"] == 0 and not os.path.exists(f))

no_confirm_env = SweEnvironment(ws)
r = no_confirm_env.execute("rm whatever.txt")
check("M1.4c 无确认通道 → fail-closed", r["blocked"]
      and "无确认通道" in r["reason"])

# ---- M1.5 自定义黑名单（config 注入）----
custom_env = SweEnvironment(ws, block_patterns=["secret"])
decision, reason = scan_command("echo secret", ws, custom_env._block_patterns)
check("M1.5 自定义黑名单命中", decision == "block" and reason == "自定义黑名单")

# ---- M1.6 超时杀树 ----
t0 = time.time()
r = env.execute("sleep 60", timeout=0.5)
elapsed = time.time() - t0
check("M1.6 超时立即返回（<3s）", elapsed < 3.0)
check("M1.6b 超时 exception_info 标记", "超时" in r["exception_info"])

# ---- M1.7 后台子进程孤儿清理（正常退出也清 straggler）----
r = env.execute("sleep 60 & echo BG=$!")
pid_line = r["output"].strip()
check("M1.7a 拿到后台 pid", pid_line.startswith("BG="))
if pid_line.startswith("BG="):
    pid = int(pid_line.split("=", 1)[1])
    check("M1.7b 后台子进程已被清理（无孤儿）", _wait_dead(pid))

# ---- M1.8 输出截断 ----
cap_env = SweEnvironment(ws, command_timeout_s=10.0, output_max_chars=100)
r = cap_env.execute("awk 'BEGIN{for(i=0;i<200000;i++)printf \"x\"}'",
                    timeout=10)
check("M1.8 输出截断生效", r["truncated"]
      and "已截断" in r["output"] and len(r["output"]) < 2000)

shutil.rmtree(ws, ignore_errors=True)
print(f"\nswe_env 沙箱执行器: {len(PASS)} 通过, {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)

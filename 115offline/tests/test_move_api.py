"""`/api/move` 的接口级回归测试（真起 FastAPI + 假 p115 客户端）

跑法（在项目根目录，用装了依赖的 venv）：
    python tests/test_move_api.py

⚠️ 需要 fastapi / httpx / p115client（与 test_push_api.py 同一套）。
   本机 venv：~/.workbuddy/binaries/python/envs/default/Scripts/python.exe

验的是**接线**，不是「115 会不会移动」：
  - 单文件 / 45 个对象 ⇒ 真的按 chunk 切成 1 次 / 3 次（20/20/5）
  - 每次请求都是**数组形态** `fid[0..n]` + `pid=目标`（逗号拼接会被 115 回 90008）
  - 目标就是被移动对象本身 ⇒ 400，且**一次请求都不发**
  - 990009「尚未执行完成」⇒ 退避重试后成功（不是直接判失败）
  - dry_run ⇒ 只算计划、不动数据
"""
import json
import os
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))

TMP = Path(tempfile.mkdtemp(prefix="move-api-test-"))
os.environ["DATA_DIR"] = str(TMP)
os.environ["ACCESS_TOKEN"] = ""

(TMP / "accounts.json").write_text(json.dumps([
    {"id": "acc-t", "name": "测试号", "cookie": "UID=1; CID=2; SEID=3",
     "checked": "11-20 09:00", "default_dir": "", "default_dir_name": ""}
], ensure_ascii=False), "utf-8")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

fail = []


def check(name, got, want):
    ok = got == want
    print(("  [OK] " if ok else "  [!!] ") + name)
    if not ok:
        print("       got :", got)
        print("       want:", want)
        fail.append(name)


class FakeClient:
    """假 115：把每次 fs_move 的 payload 原样记下来，供断言数组形态 / 目标 id。"""

    def __init__(self, busy_times=0, error=""):
        self.calls = []
        self.busy_times = busy_times     # 前几次回「尚未执行完成」，用于验退避重试
        self.error = error

    def fs_move(self, payload):
        self.calls.append(dict(payload))
        if self.busy_times:
            self.busy_times -= 1
            return {"state": False, "errno": 990009,
                    "error_msg": "删除[...]操作尚未执行完成，请稍后再试"}
        if self.error:
            return {"state": False, "errno": 90008, "error_msg": self.error}
        return {"state": True, "errno": "", "error": ""}


def use(fake):
    server._client = lambda acc: fake      # 不碰真 115
    return fake


def ids_of(payload):
    return [payload[k] for k in sorted(payload) if k.startswith("fid[")]


c = TestClient(server.app)

print("=== 单个目录 ⇒ 1 次请求，数组形态 + pid ===")
fake = use(FakeClient())
r = c.post("/api/move", json={
    "account_id": "acc-t", "file_ids": ["111"], "target_cid": "222", "delay": 0,
}).json()
check("ok=True", r["ok"], True)
check("moved_count=1", r["moved_count"], 1)
check("只发 1 次", len(fake.calls), 1)
check("fid 用数组下标形态", ids_of(fake.calls[0]), ["111"])
check("没有逗号拼接的 fid", "fid" in fake.calls[0], False)
check("pid = 目标目录", fake.calls[0].get("pid"), "222")

print("\n=== 45 个对象 chunk=20 ⇒ 3 批（20/20/5），每批都带 pid ===")
fake = use(FakeClient())
many = [str(1000 + i) for i in range(45)]
r = c.post("/api/move", json={
    "account_id": "acc-t", "file_ids": many, "target_cid": "0", "chunk": 20, "delay": 0,
}).json()
check("被调 3 次", len(fake.calls), 3)
check("三批条数 20/20/5", [len(ids_of(x)) for x in fake.calls], [20, 20, 5])
check("每批都指向同一个目标", {x.get("pid") for x in fake.calls}, {"0"})
check("全部记为已移", r["moved_count"], 45)
check("目标原样回给前端", r["target_cid"], "0")
check("计划里的目标也是它", r["plan"]["target_cid"], "0")

print("\n=== 重复 id 去重 ===")
fake = use(FakeClient())
r = c.post("/api/move", json={
    "account_id": "acc-t", "file_ids": ["7", "7", " 7 ", "8"], "target_cid": "9", "delay": 0,
}).json()
check("去重后 2 个", r["plan"]["count"], 2)
check("一次请求装 2 条", ids_of(fake.calls[0]), ["7", "8"])

print("\n=== 目标就是对象自己 ⇒ 400，一次请求都不发 ===")
fake = use(FakeClient())
r = c.post("/api/move", json={
    "account_id": "acc-t", "file_ids": ["555"], "target_cid": "555", "delay": 0,
})
check("HTTP 400", r.status_code, 400)
check("没发任何 fs_move", fake.calls, [])
check("错误说得清", "不能把对象移动到它自己里面" in r.text, True)

print("\n=== dry_run ⇒ 只回计划 ===")
fake = use(FakeClient())
r = c.post("/api/move", json={
    "account_id": "acc-t", "file_ids": ["1", "2"], "target_cid": "3",
    "chunk": 1, "delay": 0, "dry_run": True,
}).json()
check("dry_run=True", r["dry_run"], True)
check("没发请求", fake.calls, [])
check("计划 2 批", r["plan"]["groups"], 2)

print("\n=== 990009 尚未执行完成 ⇒ 退避重试后成功（约 3 秒） ===")
fake = use(FakeClient(busy_times=1))
r = c.post("/api/move", json={
    "account_id": "acc-t", "file_ids": ["333"], "target_cid": "444", "delay": 0,
}).json()
check("同一批重试了 2 次", len(fake.calls), 2)
check("最终成功", r["ok"], True)
check("已移 1 个", r["moved_count"], 1)

print("\n=== 115 报错（90008）⇒ 记失败，不吞 ===")
fake = use(FakeClient(error="文件（夹）不存在或已经删除"))
r = c.post("/api/move", json={
    "account_id": "acc-t", "file_ids": ["888"], "target_cid": "999", "delay": 0,
}).json()
check("ok=False", r["ok"], False)
check("失败 1 个", r["failed_count"], 1)
check("带原始错误", "90008" in json.dumps(r["failed"], ensure_ascii=False)
      or "不存在" in json.dumps(r["failed"], ensure_ascii=False), True)
check("已移 0 个", r["moved_count"], 0)

print("\n=== 空选择 ⇒ 400 ===")
r = c.post("/api/move", json={"account_id": "acc-t", "file_ids": [], "target_cid": "1"})
check("HTTP 400", r.status_code, 400)

print()
if fail:
    print("FAILED:", fail)
    sys.exit(1)
print("全部通过")

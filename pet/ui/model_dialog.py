"""模型管理对话框 —— v0.20（托盘“模型管理…”唤出）。

列表 + 表单的 QDialog：新增/删除/编辑（自定义命名 + Base URL + 模型 ID +
API Key）、设为当前使用（运行时切换）、测试连接（1 token 后台 ping）。

职责边界（对齐 chat_bridge 注入风格）：
- 本对话框**只管编辑态与 UI**；落盘经 ``on_commit(providers)``、切换经
  ``on_select(name)`` 回调给 app（app 负责 config 原子写 + 客户端重建 +
  托盘刷新）。
- Key 读写经注入的 ``key_reader(name)``/``key_writer(name, key)``（app 包
  platform adapter 的 Keychain 存取）——对话框不 import keyring。
- 校验/落盘/ping 复用 pet.model_registry 纯函数。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from ..model_registry import entry_label, validate_entry

log = logging.getLogger("pet")

# 运行中 ping worker 的保活引用（对话框先关时防 QThread 包装被 GC 截断
# 运行中的 C++ 线程——finished 后丢弃）
_ORPHAN_PINGS: set = set()


class _PingWorker(QThread):
    """后台连通性测试（最多 ~25s，不卡对话框 UI）。"""

    done = Signal(bool, str)

    def __init__(self, base_url: str, api_key: str, model: str,
                 parent=None) -> None:
        super().__init__(parent)
        self._base = base_url
        self._key = api_key
        self._model = model

    def run(self) -> None:
        from ..model_registry import ping_provider

        try:
            ok, detail = ping_provider(self._base, self._key, self._model)
        except Exception as e:  # ping_provider 已兜底，双保险
            ok, detail = False, f"测试异常：{e}"
        self.done.emit(ok, detail)


class ModelManagerDialog(QDialog):
    """provider 列表 + 条目表单。保存即落盘（经 on_commit），点“设为当前
    使用”触发运行时切换（经 on_select）。"""

    def __init__(self, providers: dict, selected: str,
                 key_reader, key_writer,
                 on_commit, on_select, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("模型管理")
        self.resize(560, 380)
        # 对话框持有 providers 深拷贝编辑态；提交经 on_commit 交 app 落盘
        import copy

        self._providers = copy.deepcopy(providers or {})
        self._selected = selected or ""
        self._key_reader = key_reader
        self._key_writer = key_writer
        self._on_commit = on_commit
        self._on_select = on_select
        self._editing: str | None = None  # 当前表单载入的条目名（None=新增态）
        self._loading = False
        self._ping: _PingWorker | None = None
        # 密钥存在态缓存：mac Keychain 单次读取有几十 ms 开销，列表整刷
        # 每行查一次会卡；写 Key 的时机全部经过 _write_key，缓存随之失效
        self._key_cache: dict[str, bool] = {}

        tip = QLabel("接入 OpenAI 兼容模型：填 Base URL、模型 ID 与 API Key。"
                     "多数端点 Base URL 需以 /v1 结尾。")
        tip.setWordWrap(True)

        # 左列：列表 + 新增/删除
        self._list = QListWidget()
        btn_add = QPushButton("新增")
        btn_del = QPushButton("删除")
        left = QVBoxLayout()
        left.addWidget(self._list)
        row_l = QHBoxLayout()
        row_l.addWidget(btn_add)
        row_l.addWidget(btn_del)
        left.addLayout(row_l)

        # 右列：表单 + 测试/保存
        form_box = QGroupBox("模型配置")
        form = QFormLayout(form_box)
        self._ed_name = QLineEdit()
        self._ed_name.setPlaceholderText("自定义名称，如 我的GPT")
        self._ed_base = QLineEdit()
        self._ed_base.setPlaceholderText("https://api.example.com/v1")
        self._ed_model = QLineEdit()
        self._ed_model.setPlaceholderText("如 deepseek-chat / gpt-4o-mini")
        self._ed_key = QLineEdit()
        self._ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._ed_base.editingFinished.connect(self._refresh_key_hint)
        self._ed_name.editingFinished.connect(self._refresh_key_hint)
        form.addRow("名称", self._ed_name)
        form.addRow("Base URL", self._ed_base)
        form.addRow("模型 ID", self._ed_model)
        form.addRow("API Key", self._ed_key)
        self._key_hint = QLabel("")
        self._key_hint.setWordWrap(True)
        form.addRow("", self._key_hint)
        self._btn_ping = QPushButton("测试连接")
        self._ping_label = QLabel("")
        self._ping_label.setWordWrap(True)
        ping_row = QHBoxLayout()
        ping_row.addWidget(self._btn_ping)
        ping_row.addWidget(self._ping_label, 1)
        form.addRow("", ping_row)
        btn_save = QPushButton("保存修改")
        right = QVBoxLayout()
        right.addWidget(form_box)
        right.addWidget(btn_save)
        right.addStretch(1)

        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(right, 1)

        # 底部：设为当前使用 / 关闭
        self._btn_use = QPushButton("设为当前使用")
        btn_close = QPushButton("关闭")
        bottom = QHBoxLayout()
        bottom.addWidget(self._btn_use)
        bottom.addStretch(1)
        bottom.addWidget(btn_close)

        root = QVBoxLayout(self)
        root.addWidget(tip)
        root.addLayout(body)
        root.addLayout(bottom)

        self._list.currentRowChanged.connect(self._on_pick)
        btn_add.clicked.connect(self._on_add)
        btn_del.clicked.connect(self._on_delete)
        btn_save.clicked.connect(self._on_save)
        self._btn_use.clicked.connect(self._on_use)
        self._btn_ping.clicked.connect(self._on_ping)
        btn_close.clicked.connect(self.reject)

        self._reload_list()
        first = self._providers and next(iter(self._providers)) or None
        if first is not None:
            self._load_entry(first)
        else:
            self._on_add()

    # ---- 列表 ----

    def _reload_list(self) -> None:
        """重建列表行（保存/删除/切换后调）。"""
        if self._loading:
            return
        self._loading = True
        try:
            cur = self._editing
            self._list.blockSignals(True)
            self._list.clear()
            for name, pcfg in self._providers.items():
                has_key = self._has_key(name)
                item = QListWidgetItem(entry_label(name, pcfg, self._selected,
                                                   has_key))
                item.setData(0x0100, name)  # Qt.UserRole
                self._list.addItem(item)
                if name == cur:
                    self._list.setCurrentItem(item)
        finally:
            self._list.blockSignals(False)
            self._loading = False
        self._btn_use.setEnabled(self._selected in self._providers)

    def _has_key(self, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        if name not in self._key_cache:
            try:
                self._key_cache[name] = bool(self._key_reader(name))
            except Exception:
                self._key_cache[name] = False
        return self._key_cache[name]

    def _write_key(self, name: str, key: str) -> None:
        self._key_writer(name, key)
        self._key_cache[name.strip()] = bool(key)

    # ---- 表单载入 ----

    def _load_entry(self, name: str) -> None:
        pcfg = self._providers.get(name) or {}
        self._editing = name
        self._ed_name.setText(name)
        self._ed_base.setText(pcfg.get("base_url", ""))
        self._ed_model.setText(pcfg.get("model", ""))
        self._ed_key.clear()
        self._ping_label.clear()
        self._refresh_key_hint()

    def _on_pick(self, row: int) -> None:
        if self._loading or row < 0:
            return
        name = self._list.item(row).data(0x0100)
        if name and name != self._editing:
            self._load_entry(name)

    def _refresh_key_hint(self) -> None:
        """Key 框占位提示：该名称已存 Key → "留空不修改"，否则提示输入。"""
        name = (self._editing or self._ed_name.text() or "").strip()
        if name and self._has_key(name):
            self._ed_key.setPlaceholderText("已保存，留空则不修改")
            self._key_hint.setText("密钥已存入系统钥匙串，不回显。")
        else:
            self._ed_key.setPlaceholderText("sk-…（存入系统钥匙串，不写明文文件）")
            self._key_hint.setText("")

    # ---- 动作 ----

    def _on_add(self) -> None:
        self._editing = None
        self._list.setCurrentRow(-1)
        for ed in (self._ed_name, self._ed_base, self._ed_model, self._ed_key):
            ed.clear()
        self._ping_label.clear()
        self._refresh_key_hint()
        self._ed_name.setFocus()

    def _on_delete(self) -> None:
        name = self._editing
        if name is None:
            name = (self._ed_name.text() or "").strip()
        if not name or name not in self._providers:
            QMessageBox.information(self, "模型管理", "请先在列表中选择要删除的模型。")
            return
        if QMessageBox.question(
                self, "模型管理", f"删除模型“{name}”？（配置移除；密钥保留在钥匙串）"
        ) != QMessageBox.StandardButton.Yes:
            return
        del self._providers[name]
        if self._selected == name:
            self._selected = ""
        self._editing = None
        self._on_commit(self._providers, self._selected)
        self._reload_list()
        if self._providers:
            self._load_entry(next(iter(self._providers)))
        else:
            self._on_add()

    def _on_save(self) -> None:
        name = self._ed_name.text().strip()
        base = self._ed_base.text().strip()
        model = self._ed_model.text().strip()
        err = validate_entry(name, base, model,
                             list(self._providers), self_name=self._editing or "")
        if err:
            QMessageBox.warning(self, "模型管理", err)
            return
        # 保留条目既有附加键（api_key_env/max_tokens 等），只覆写编辑的字段
        old_name = self._editing
        entry = dict(self._providers.get(old_name or name) or {})
        entry["base_url"] = base
        entry["model"] = model
        if old_name and old_name != name:
            del self._providers[old_name]
            # 改名迁移密钥（Keychain 按名称存）
            if self._has_key(old_name):
                self._write_key(name, self._key_reader(old_name) or "")
            if self._selected == old_name:
                self._selected = name
        typed_key = self._ed_key.text().strip()
        if typed_key:
            self._write_key(name, typed_key)
        self._providers[name] = entry
        self._editing = name
        self._on_commit(self._providers, self._selected)
        self._ed_key.clear()
        self._reload_list()
        self._load_entry(name)
        self._ping_label.setText("已保存。")

    def _on_use(self) -> None:
        # 表单有未保存改动时先落盘再切换（用户点"使用"意图明确是当前表单）
        name = self._ed_name.text().strip()
        if (self._editing != name
                or (name in self._providers
                    and (self._providers[name].get("base_url")
                         != self._ed_base.text().strip()
                         or self._providers[name].get("model")
                         != self._ed_model.text().strip()
                         or self._ed_key.text().strip()))):
            self._on_save()
            name = self._ed_name.text().strip()
        if name not in self._providers:
            QMessageBox.warning(self, "模型管理", "请先保存有效的模型配置。")
            return
        if not self._has_key(name):
            QMessageBox.warning(
                self, "模型管理",
                f"模型“{name}”还没有 API Key，请填写并保存后再切换。")
            return
        self._selected = name
        self._reload_list()
        self._on_select(name)

    def _on_ping(self) -> None:
        if self._ping is not None and self._ping.isRunning():
            return
        base = self._ed_base.text().strip()
        model = self._ed_model.text().strip()
        # Key 优先取表单输入（未保存的 Key 也能测），其次该条目已存的
        key = self._ed_key.text().strip() or (
            self._key_reader(self._editing or self._ed_name.text().strip())
            if self._has_key(self._editing or self._ed_name.text().strip())
            else "")
        if not base or not model:
            self._ping_label.setText("请先填写 Base URL 和模型 ID。")
            return
        self._btn_ping.setEnabled(False)
        self._ping_label.setText("测试中…")
        w = _PingWorker(base, key, model)
        _ORPHAN_PINGS.add(w)
        w.finished.connect(lambda: _ORPHAN_PINGS.discard(w))
        w.finished.connect(w.deleteLater)
        w.done.connect(self._on_ping_done)
        self._ping = w
        w.start()

    def _on_ping_done(self, ok: bool, detail: str) -> None:
        self._btn_ping.setEnabled(True)
        self._ping = None
        self._ping_label.setText(("✅ " if ok else "❌ ") + detail)

    def reject(self) -> None:  # noqa: D102 - 关闭时摘除在飞 ping 的 UI 回调
        if self._ping is not None and self._ping.isRunning():
            try:
                self._ping.done.disconnect(self._on_ping_done)
            except (RuntimeError, TypeError):
                pass
            self._ping = None
        super().reject()

"""Qt views for the durable queue; submitting never starts a model request."""
import datetime as dt
import json
import os

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QComboBox, QPushButton, QFileDialog, QFormLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox)


STATUS_NAMES = {'pending': '待启动', 'starting': '启动中', 'running': '运行中',
    'waiting_quota': '等待额度', 'waiting_resume': '等待接续',
    'waiting_authorization': '等待授权', 'completed': '已完成',
    'failed': '执行失败', 'cancelled': '已取消'}
SANDBOX_NAMES = {'read-only': '只读', 'workspace-write': '工作目录读写',
    'danger-full-access': '完整访问（无沙箱）'}
APPROVAL_NAMES = {'on-request': '需要时询问', 'untrusted': '非可信操作询问',
    'on-failure': '失败后询问', 'never': '不询问（受沙箱限制）'}
EDITABLE = {'pending', 'waiting_quota', 'failed'}


def normalized_max_concurrent_tasks(value):
    return value if type(value) is int and 3 <= value <= 10 else 3


class ConcurrentSpinBox(QSpinBox):
    """Keep incomplete or out-of-range text from changing the scheduler."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(3, 10)
        self.setKeyboardTracking(False)
        self.setAccessibleName('最大并发任务数')

    def validate(self, text, position):
        if text == '' or text == '1':
            return QValidator.State.Intermediate, text, position
        if text.isascii() and text.isdigit() and str(int(text)) == text and 3 <= int(text) <= 10:
            return QValidator.State.Acceptable, text, position
        return QValidator.State.Invalid, text, position


def _text(value, wrap=False):
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    return widget


def _combo_value(combo):
    return str(combo.currentData() or '')


def _select(combo, value, unknown_label=None):
    value = str(value or '')
    index = combo.findData(value)
    if index < 0:
        combo.addItem(unknown_label or value + '（启动时验证）', value)
        index = combo.count() - 1
    combo.setCurrentIndex(index)


def _stamp(value):
    try:
        return dt.datetime.fromtimestamp(float(value)).strftime('%m/%d %H:%M:%S') if value else '—'
    except (ValueError, TypeError, OSError, OverflowError):
        return '—'


class TaskComposer(QWidget):
    submitted = Signal(str)
    dismissed = Signal()

    def __init__(self, service, preferences, save_preferences, parent=None):
        super().__init__(parent)
        self.service, self.preferences = service, preferences
        self.save_preferences = save_preferences
        self.edit_id = None
        self.capabilities = {}
        self._capabilities_signature = None
        self.setObjectName('taskComposer')
        self.setStyleSheet('QWidget#taskComposer{background:rgba(255,255,255,150);border:1px solid rgba(39,67,76,30);border-radius:18px;}'
            'QLabel{color:#354750;background:transparent;}'
            'QTextEdit,QComboBox{background:rgba(255,255,255,220);color:#1D1D1F;border:1px solid #D7DFE6;border-radius:9px;padding:5px;}'
            'QTextEdit:focus,QComboBox:focus{border-color:#007AFF;}'
            'QComboBox QAbstractItemView{background:white;color:#1D1D1F;selection-background-color:#E5F0FF;selection-color:#1D1D1F;}'
            'QPushButton{background:#EDF2F7;color:#354750;border:0;border-radius:9px;padding:7px 10px;}'
            'QPushButton:hover{background:#DFEAF6;}'
            'QPushButton#submitTask{background:#1D1D1F;color:white;}')
        box = QVBoxLayout(self)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(8)
        self.heading = _text('准备一个新任务')
        box.addWidget(self.heading)
        self.description = QTextEdit()
        self.description.setAcceptRichText(False)
        self.description.setPlaceholderText('描述你想完成的任务…\n额度恢复后自动开始')
        self.description.setAccessibleName('任务描述')
        self.description.setMinimumHeight(88)
        self.description.setMaximumHeight(104)
        box.addWidget(self.description)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(7)
        path_row = QHBoxLayout()
        path_row.setSpacing(5)
        self.cwd = QComboBox()
        self.cwd.setEditable(True)
        self.cwd.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.cwd.setMinimumWidth(0)
        self.cwd.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cwd.setAccessibleName('工作目录')
        self.cwd.lineEdit().setPlaceholderText('选择已有文件夹')
        for directory in preferences.get('task_recent_directories', []):
            if isinstance(directory, str):
                self.cwd.addItem(directory)
        self.cwd.setEditText(str(preferences.get('task_cwd') or ''))
        self.browse_button = QPushButton('选择')
        self.browse_button.clicked.connect(self.choose_directory)
        path_row.addWidget(self.cwd, 1)
        path_row.addWidget(self.browse_button)
        form.addRow('工作目录', path_row)
        self.model, self.effort = QComboBox(), QComboBox()
        self.model.setAccessibleName('模型选择')
        self.effort.setAccessibleName('推理强度')
        self.model.addItem('当前 Codex 配置', '')
        _select(self.model, preferences.get('task_model'))
        self.effort.addItem('模型默认', '')
        _select(self.effort, preferences.get('task_reasoning_effort'))
        self.model.currentIndexChanged.connect(self._model_changed)
        form.addRow('模型', self.model)
        form.addRow('推理强度', self.effort)
        self.sandbox, self.approval = QComboBox(), QComboBox()
        self.sandbox.setAccessibleName('沙箱权限')
        self.approval.setAccessibleName('审批模式')
        # These are the safe submission defaults, not a claimed capability catalog.
        self.sandbox.addItem('工作目录读写', 'workspace-write')
        self.approval.addItem('需要时询问', 'on-request')
        form.addRow('访问权限', self.sandbox)
        form.addRow('审批', self.approval)
        box.addLayout(form)
        self.capability_status = _text('可先入队；启动前核对模型与权限。', True)
        self.capability_status.setStyleSheet('color:#596A76;font-size:11px;background:transparent;')
        box.addWidget(self.capability_status)
        self.status = _text('', True)
        self.status.setAccessibleName('任务提交结果')
        box.addWidget(self.status)
        actions = QHBoxLayout()
        self.reload_button = QPushButton('刷新模型')
        self.reload_button.setToolTip('只读取 Codex 支持的选项，不开始任务')
        self.reload_button.clicked.connect(self.refresh_capabilities)
        actions.addWidget(self.reload_button)
        actions.addStretch()
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.cancel)
        self.submit_button = QPushButton('加入队列 ↑')
        self.submit_button.setObjectName('submitTask')
        self.submit_button.clicked.connect(self.submit)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.submit_button)
        box.addLayout(actions)

    def choose_directory(self):
        directory = QFileDialog.getExistingDirectory(self, '选择任务工作目录', self.cwd.currentText())
        if directory:
            self.cwd.setEditText(directory)

    def refresh_capabilities(self):
        try:
            self.service.request_task_capabilities()
            self.capability_status.setText('正在读取 Codex 支持的选项…可继续编辑和入队。')
        except Exception as exc:
            self.capability_status.setText('选项暂不可用：%s。仍可入队，启动时验证。' % exc)

    def apply_snapshot(self, snapshot):
        capabilities = snapshot.get('task_capabilities') or {}
        signature = repr(capabilities)
        if signature != self._capabilities_signature:
            self._capabilities_signature = signature
            self.capabilities = capabilities
            selected_model, selected_effort = _combo_value(self.model), _combo_value(self.effort)
            with QSignalBlocker(self.model):
                self.model.clear()
                default = capabilities.get('default_model')
                self.model.addItem('当前配置' + (' · ' + str(default) if default else ''), '')
                for model in capabilities.get('models') or []:
                    if not isinstance(model, dict):
                        continue
                    identifier = model.get('model') or model.get('id')
                    if identifier and self.model.findData(str(identifier)) < 0:
                        self.model.addItem(str(model.get('displayName') or identifier), str(identifier))
                _select(self.model, selected_model)
            self._model_changed(preferred=selected_effort)
            for combo, key, names, fallback in [(self.sandbox, 'sandbox_modes', SANDBOX_NAMES, 'workspace-write'),
                                              (self.approval, 'approval_modes', APPROVAL_NAMES, 'on-request')]:
                current = _combo_value(combo) or fallback
                available = capabilities.get(key)
                if isinstance(available, list) and available:
                    combo.clear()
                    for value in available:
                        if isinstance(value, str):
                            combo.addItem(names.get(value, value), value)
                    _select(combo, current)
        if snapshot.get('task_capabilities_loading'):
            self.capability_status.setText('正在读取 Codex 支持的选项…可继续入队。')
        elif snapshot.get('task_capabilities_error'):
            self.capability_status.setText('选项暂不可用：%s。可先入队，启动时验证。' % snapshot['task_capabilities_error'])
        elif capabilities.get('models'):
            self.capability_status.setText('选项来自当前 Codex；任务只在额度可用时启动。')
        else:
            self.capability_status.setText('可先入队；沿用当前配置，启动前核对模型与权限。')

    def _model_changed(self, index=None, preferred=None):
        selected = _combo_value(self.effort) if preferred is None else str(preferred or '')
        identifier = _combo_value(self.model) or self.capabilities.get('default_model')
        model = next((m for m in self.capabilities.get('models') or [] if isinstance(m, dict)
                      and (m.get('model') or m.get('id')) == identifier), None)
        self.effort.clear()
        self.effort.addItem('模型默认', '')
        if model is None:
            _select(self.effort, selected)
            return
        for effort in model.get('supportedReasoningEfforts') or []:
            value = effort.get('reasoningEffort') if isinstance(effort, dict) else effort
            if isinstance(value, str):
                self.effort.addItem(value, value)
        index = self.effort.findData(selected)
        self.effort.setCurrentIndex(max(0, index))
        if selected and index < 0:
            self.status.setText('此模型不支持原推理强度，已选择模型默认。')

    def load_task(self, task):
        self.edit_id = task['id']
        self.heading.setText('修改待启动任务')
        self.description.setPlainText(task.get('description', ''))
        self.cwd.setEditText(task.get('cwd', ''))
        _select(self.model, task.get('model'))
        self._model_changed(preferred=task.get('reasoning_effort'))
        _select(self.sandbox, task.get('sandbox') or 'workspace-write')
        _select(self.approval, task.get('approval_policy') or 'on-request')
        self.submit_button.setText('保存修改')
        self.status.setText('尚未启动，可以修改任务。')

    def cancel(self):
        self.description.clear()
        self.edit_id = None
        self.heading.setText('准备一个新任务')
        self.submit_button.setText('加入队列 ↑')
        self.status.clear()
        self.dismissed.emit()

    def submit(self):
        description = self.description.toPlainText().strip()
        cwd = os.path.abspath(os.path.expanduser(os.path.expandvars(self.cwd.currentText().strip().strip('"'))))
        if not description:
            self.status.setText('请输入任务描述。')
            self.description.setFocus()
            return
        if not self.cwd.currentText().strip() or not os.path.isdir(cwd):
            self.status.setText('工作目录不存在，请选择已有文件夹。')
            self.cwd.setFocus()
            return
        spec = dict(description=description, cwd=cwd, model=_combo_value(self.model),
                    reasoning_effort=_combo_value(self.effort), sandbox=_combo_value(self.sandbox),
                    approval_policy=_combo_value(self.approval))
        try:
            if self.edit_id:
                self.service.queue_update(self.edit_id, spec)
                message = '已保存待启动任务'
            else:
                self.service.queue_submit(spec)
                message = '已加入待启动队列'
        except Exception as exc:
            self.status.setText('保存失败：' + str(exc))
            return
        history = [cwd] + [p for p in self.preferences.get('task_recent_directories', [])
                           if isinstance(p, str) and os.path.normcase(p) != os.path.normcase(cwd)]
        self.preferences.update(task_cwd=cwd, task_recent_directories=history[:10],
                                task_model=spec['model'], task_reasoning_effort=spec['reasoning_effort'])
        self.save_preferences()
        self.cwd.clear()
        self.cwd.addItems(history[:10])
        self.cwd.setEditText(cwd)
        self.description.clear()
        self.edit_id = None
        self.heading.setText('准备一个新任务')
        self.submit_button.setText('加入队列 ↑')
        self.status.setText(message)
        self.submitted.emit(message)


class TaskQueueDialog(QDialog):
    edit_requested = Signal(dict)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service, self.rows = service, []
        self._signature = None
        self.setWindowTitle('新任务队列')
        self.resize(760, 590)
        box = QVBoxLayout(self)
        box.setContentsMargins(20, 18, 20, 18)
        self.summary = _text('新任务队列', True)
        box.addWidget(self.summary)
        box.addWidget(_text('旧任务优先恢复；优先级数字越大越先启动，同优先级按提交顺序。', True))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['任务', '状态', '优先级', '创建时间'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 4):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        box.addWidget(self.table, 1)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(140)
        self.detail.setMaximumHeight(200)
        box.addWidget(self.detail)
        row = QHBoxLayout()
        self.edit_button = QPushButton('修改')
        self.edit_button.clicked.connect(self._edit)
        self.delete_button = QPushButton('移出队列')
        self.delete_button.clicked.connect(lambda: self._action('queue_delete'))
        self.retry_button = QPushButton('重试 / 继续')
        self.retry_button.clicked.connect(lambda: self._action('queue_retry'))
        for widget in [self.edit_button, self.delete_button, self.retry_button]:
            row.addWidget(widget)
        row.addStretch()
        self.priority = QSpinBox()
        self.priority.setAccessibleName('任务优先级')
        self.priority.setRange(-999, 999)
        self.priority.setToolTip('数字越大越先启动；相同时按提交顺序')
        row.addWidget(self.priority)
        self.priority_button = QPushButton('设优先级')
        self.priority_button.clicked.connect(lambda: self._action('queue_reprioritize', self.priority.value()))
        row.addWidget(self.priority_button)
        box.addLayout(row)
        approvals = QHBoxLayout()
        self.allow_button = QPushButton('仅允许这一次')
        self.allow_button.clicked.connect(lambda: self._action('queue_respond_approval', 'accept'))
        self.deny_button = QPushButton('拒绝这一次')
        self.deny_button.clicked.connect(lambda: self._action('queue_respond_approval', 'decline'))
        self.cancel_approval_button = QPushButton('取消当前执行')
        self.cancel_approval_button.clicked.connect(lambda: self._action('queue_respond_approval', 'cancel'))
        approvals.addWidget(self.allow_button)
        approvals.addWidget(self.deny_button)
        approvals.addWidget(self.cancel_approval_button)
        approvals.addStretch()
        box.addLayout(approvals)
        self.feedback = _text('', True)
        box.addWidget(self.feedback)
        self._selection_changed()

    def selected(self):
        row = self.table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def refresh(self, snapshot):
        rows = [task for task in snapshot.get('new_tasks') or []
                if task.get('status') not in ('completed', 'cancelled')]
        error = snapshot.get('capacity_error')
        self.summary.setText('运行 %s  ·  已保留名额 %s  ·  上限 %s%s' % (
            snapshot.get('running_task_count', 0), snapshot.get('reserved_task_count', 0),
            snapshot.get('maxConcurrentTasks', 3), '  ·  ' + str(error) if error else ''))
        signature = repr(rows)
        if signature == self._signature:
            return
        self._signature = signature
        selected_id = (self.selected() or {}).get('id')
        self.rows = rows
        with QSignalBlocker(self.table):
            self.table.setRowCount(len(rows))
            selection = 0
            for index, task in enumerate(rows):
                if task.get('id') == selected_id:
                    selection = index
                values = [str(task.get('description', '')).replace('\n', ' ')[:100],
                    STATUS_NAMES.get(task.get('status'), task.get('status', '未知')),
                    str(task.get('priority', 0)), _stamp(task.get('created_at'))]
                for col, value in enumerate(values):
                    self.table.setItem(index, col, QTableWidgetItem(value))
            if rows:
                self.table.selectRow(selection)
        self._selection_changed()

    def _selection_changed(self):
        task = self.selected() or {}
        editable = bool(task and task.get('started_at') is None and not task.get('thread_id') and task.get('status') in EDITABLE)
        self.edit_button.setEnabled(editable)
        self.edit_button.setToolTip('已启动的任务沿用原会话，不能修改' if task and not editable else '')
        self.delete_button.setEnabled(editable or (task.get('status') == 'failed'
            and task.get('phase') == 'settled' and not task.get('worker_pid')
            and task.get('error_kind') != 'uncertain'))
        can_prioritize = task.get('status') in ('pending', 'waiting_quota', 'waiting_resume', 'failed')
        self.priority_button.setEnabled(can_prioritize)
        self.priority.setEnabled(can_prioritize)
        self.priority.setValue(int(task.get('priority') or 0))
        self.retry_button.setEnabled(task.get('status') in ('failed', 'waiting_authorization') and not task.get('worker_pid'))
        approval = task.get('approval')
        waiting = bool(task.get('status') == 'waiting_authorization' and approval and not task.get('approval_response'))
        self.allow_button.setVisible(waiting)
        self.deny_button.setVisible(waiting)
        self.cancel_approval_button.setVisible(waiting)
        supported = isinstance(approval, dict) and approval.get('method') in (
            'item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/permissions/requestApproval')
        self.allow_button.setEnabled(bool(waiting and supported))
        self.allow_button.setToolTip('允许显示的这一次操作' if supported else '此请求需要具体输入；请取消后在客户端继续处理。')
        if not task:
            self.detail.setPlainText('还没有任务。点击 HUD 的“新任务”即可提交，额度为零时也可加入队列。')
            return
        text = '%s\n\n工作目录：%s\n模型：%s · 推理：%s\n权限：%s · 审批：%s\n创建：%s · 启动：%s · 完成：%s\n会话：%s\n任务 ID：%s\n错误：%s\n日志：%s' % (
            task.get('description', ''), task.get('cwd', ''), task.get('model') or '当前配置',
            task.get('reasoning_effort') or '模型默认', task.get('sandbox', ''), task.get('approval_policy', ''),
            _stamp(task.get('created_at')), _stamp(task.get('started_at')), _stamp(task.get('completed_at')),
            task.get('thread_id') or '尚未创建', task.get('id', ''), task.get('error') or '无', task.get('log_path') or '尚未生成')
        if approval:
            text += '\n\n待授权请求（请核对完整内容后选择）：\n' + json.dumps(approval, ensure_ascii=False, indent=2)
        self.detail.setPlainText(text)

    def _edit(self):
        task = self.selected()
        if task:
            self.edit_requested.emit(dict(task))
            self.hide()

    def _action(self, method, *args):
        task = self.selected()
        if not task:
            return
        try:
            if method == 'queue_respond_approval':
                args = (task.get('approval'), *args)
            getattr(self.service, method)(task['id'], *args)
            self.feedback.setText({
                'queue_retry': '已重新排队，等待调度。',
                'queue_delete': '已移出队列，可在任务历史查看。',
            }.get(method, '已保存。'))
            self.refresh(self.service.snapshot())
        except Exception as exc:
            self.feedback.setText('操作未完成：' + str(exc))

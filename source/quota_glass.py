"""A frosted Windows quota companion. Automation is always opt-in."""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import queue
import sys
import threading
import time
import traceback

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QVariantAnimation, QEasingCurve, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPainterPath, QLinearGradient, QPen, QFont, QIcon, QPixmap, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QToolButton, QAbstractButton, QSizePolicy, QDialog, QFormLayout,
    QLineEdit, QPushButton, QFileDialog, QMessageBox, QCheckBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QTextEdit, QSlider)
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from quota_hud import remaining, percent, countdown, freshness, read_json, discover_runtime, DemoService, log_error, clamp_position
from hud_controls import ClickthroughControl, window_title, health_label
from windows_hud_shell import WindowsHUDShell
from windows_hud_effects import WindowEffects
import follow_gpt

GREEN, RED, BLUE = '#30B66C', '#E45D69', '#007AFF'
INK, MUTED = '#1D1D1F', '#354750'
STATE_NAMES = {'pending': '等待额度 / 待重试', 'sending': '正在投递', 'queued': '已投递，等待确认',
    'uncertain': '投递不明，请核查', 'confirmed': '已确认本次接续', 'superseded': '会话已由其他操作继续',
    'observed': '已发现后续轮次（旧记录）', 'ignored': '已忽略'}


def normalized_transparency(value):
    try:
        if isinstance(value, bool):
            return 55
        number = float(value)
        return max(0, min(80, round(number))) if math.isfinite(number) else 55
    except (TypeError, ValueError, OverflowError):
        return 55


def surface_alphas(transparency):
    opacity = 1 - normalized_transparency(transparency) / 100
    return dict(base=round(255*opacity), card=round(66*opacity),
                controls=round(56*opacity))


def label(text='', size=10, color=INK, bold=False):
    widget = QLabel(text)
    widget.setStyleSheet('color:%s;background:transparent;' % color)
    font = QFont('Microsoft YaHei UI', size)
    font.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    widget.setFont(font)
    return widget


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(BLUE))
    painter.drawRoundedRect(QRectF(1, 1, 62, 62), 18, 18)
    pen = QPen(QColor('white'), 4.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(18, 36), QPointF(18, 44))
    painter.drawLine(QPointF(31, 27), QPointF(31, 44))
    painter.drawLine(QPointF(44, 18), QPointF(44, 44))
    painter.end()
    return QIcon(pixmap)


class ToggleSwitch(QAbstractButton):
    """Antialiased switch with stable red/off and green/on semantics."""
    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.setAccessibleName(name)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(52, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.position = 0.
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(170)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._animate)
        self.toggled.connect(self._toggled)

    def _animate(self, value):
        self.position = float(value)
        self.update()

    def _toggled(self, checked):
        self.animation.stop()
        self.animation.setStartValue(self.position)
        self.animation.setEndValue(1. if checked else 0.)
        self.animation.start()

    def reflect(self, checked):
        if self.isChecked() != bool(checked):
            with QSignalBlocker(self):
                self.setChecked(bool(checked))
            self._toggled(bool(checked))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        off, on = QColor(RED), QColor(GREEN)
        t = self.position
        p.setBrush(QColor(*[round(off.getRgb()[i] * (1-t) + on.getRgb()[i] * t) for i in range(3)]))
        p.drawRoundedRect(QRectF(1, 1, 50, 28), 14, 14)
        p.setBrush(QColor(0, 0, 0, 23))
        p.drawEllipse(QRectF(4 + t*22, 4.5, 24, 24))
        p.setBrush(QColor('white'))
        p.drawEllipse(QRectF(4 + t*22, 3, 24, 24))
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(BLUE), 1.5))
            p.drawRoundedRect(QRectF(.8, .8, 50.4, 28.4), 14, 14)


class IconButton(QAbstractButton):
    """One consistent vector grid, independent of symbol-font metrics."""
    def __init__(self, kind, tooltip, parent=None, size=30):
        super().__init__(parent)
        self.kind = kind
        self._keyboard_focus = False
        self.setAccessibleName(tooltip)
        self.setToolTip(tooltip)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCheckable(kind == 'pin')

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        self._keyboard_focus = False
        super().focusOutEvent(event)
        self.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        if self.isChecked() or self.underMouse() or self.isDown():
            p.setBrush(QColor(52, 120, 246, 22) if self.isChecked() else QColor(107, 125, 151, 20))
            if self.kind == 'close' and self.underMouse():
                p.setBrush(QColor(228, 93, 105, 24))
            p.drawRoundedRect(QRectF(1, 1, self.width()-2, self.height()-2), 9, 9)
        color = RED if self.kind == 'close' and self.underMouse() else BLUE if self.isChecked() else MUTED
        pen = QPen(QColor(color), 1.55)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.translate(self.width()/2, self.height()/2)
        if self.kind == 'refresh':
            p.drawArc(QRectF(-6, -6, 12, 12), 50*16, 285*16)
            p.drawLine(QPointF(6, -6), QPointF(6, -1))
            p.drawLine(QPointF(6, -1), QPointF(1, -1))
        elif self.kind == 'pin':
            if not self.isChecked():
                p.rotate(35)
            path = QPainterPath(QPointF(-3.5, -6))
            for point in [(3.5,-6),(2,-6),(2,-1),(5,3),(-5,3),(-2,-1),(-2,-6)]:
                path.lineTo(*point)
            p.drawPath(path)
            p.drawLine(QPointF(0,3), QPointF(0,8))
        elif self.kind == 'minimize':
            p.drawLine(QPointF(-6, 0), QPointF(6, 0))
        elif self.kind == 'close':
            p.drawLine(QPointF(-4.5,-4.5), QPointF(4.5,4.5))
            p.drawLine(QPointF(4.5,-4.5), QPointF(-4.5,4.5))
        elif self.kind == 'records':
            p.drawEllipse(QRectF(-7,-7,14,14))
            p.drawLine(QPointF(0,-3.5), QPointF(0,0))
            p.drawLine(QPointF(0,0), QPointF(3,2))
        elif self.kind == 'settings':
            p.drawEllipse(QRectF(-5.5,-5.5,11,11))
            p.drawEllipse(QRectF(-2,-2,4,4))
            for angle in range(0,360,45):
                p.save()
                p.rotate(angle)
                p.drawLine(QPointF(0,-5.5), QPointF(0,-8))
                p.restore()
        if self._keyboard_focus:
            p.resetTransform()
            p.setPen(QPen(QColor(BLUE), 1))
            p.drawRoundedRect(QRectF(1,1,self.width()-2,self.height()-2),9,9)


class QuotaBar(QWidget):
    def __init__(self, color=BLUE):
        super().__init__()
        self.setFixedHeight(8)
        self.value = None
        self.color = color
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(220)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._value)

    def _value(self, value):
        self.value = float(value)
        self.update()

    def set_value(self, value):
        if value is None:
            self.animation.stop()
            self.value = None
            self.update()
        elif self.value is None or abs(self.value-value) > .05:
            self.animation.stop()
            self.animation.setStartValue(self.value if self.value is not None else value)
            self.animation.setEndValue(value)
            self.animation.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        rect = QRectF(0, 0, self.width(), self.height())
        p.setBrush(QColor(60, 60, 67, 22))
        p.drawRoundedRect(rect, 4, 4)
        if self.value is not None and self.value > 0:
            rect.setWidth(max(3., rect.width() * self.value/100.))
            gradient = QLinearGradient(rect.topLeft(), rect.topRight())
            base = QColor('#C77916' if self.value < 15 else self.color)
            gradient.setColorAt(0, base.lighter(108))
            gradient.setColorAt(1, base)
            p.setBrush(gradient)
            p.drawRoundedRect(rect, 4, 4)


class GlassHUD(QWidget):
    def __init__(self, service, core=None, demo=False, configured=True, shell=None, effects_factory=WindowEffects):
        super().__init__()
        self.service, self.core = service, core
        self.demo, self.configured = demo, configured
        self.closed, self.hidden = False, False
        self._effects_factory = effects_factory
        self.effects = self.control = None
        self.last = {}
        self.drag_offset = None
        self.records_dialog = self.settings_dialog = None
        self._requested_passthrough = False
        self._pass_timer = QTimer(self)
        self._pass_timer.setSingleShot(True)
        self._pass_timer.setInterval(80)
        self._pass_timer.timeout.connect(lambda: self._set_clickthrough(self._requested_passthrough))
        directory = os.path.dirname(core.CONFIG_FILE) if core else os.path.join(
            os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'CodexQuotaWatchdog')
        self.settings_path = os.path.join(directory, 'hud-settings.json')
        self.preferences = {} if demo else read_json(self.settings_path)
        self.pinned = self.preferences.get('pinned', True) is not False
        self.glass = self.preferences.get('glass', True) is not False
        self.transparency = normalized_transparency(self.preferences.get('transparency', 55))
        self.glass_mode = 'fallback'
        self.pass_switch = self.pass_state = self.pass_hint = None
        self.setWindowTitle('Codex 额度管家' + (' — 离线演示' if demo else ''))
        icon = app_icon()
        self.setWindowIcon(icon)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                            (Qt.WindowType.WindowStaysOnTopHint if self.pinned else Qt.WindowType.Widget))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(428)
        self.setStyleSheet('QToolTip{color:#17243A;background:#F5F7FB;border:1px solid #DCE3EE;padding:6px;border-radius:6px;}')
        self._build()
        self.adjustSize()
        if shell is not None:
            self.shell = shell
        else:
            native_icon = None
            if os.name == 'nt':
                try:
                    native_icon = icon.pixmap(32, 32).toImage().toHICON()
                except (AttributeError, OSError, TypeError, RuntimeError):
                    pass  # The system icon remains a usable fallback.
            self.shell = WindowsHUDShell(native_icon)
        self.pin_button.setChecked(self.pinned)
        if not self.shell.hotkey:
            self.minimize_button.setToolTip('隐藏到托盘（Ctrl+Alt+H 被其他程序占用）')
        self._style_surfaces()
        self._position()
        QShortcut(QKeySequence('Ctrl+R'), self, activated=self.refresh)
        QShortcut(QKeySequence('Ctrl+,'), self, activated=self.settings)
        QShortcut(QKeySequence('Ctrl+L'), self, activated=self.records)
        QShortcut(QKeySequence('Escape'), self, activated=self.minimize)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        if configured:
            self.service.start()
        else:
            QTimer.singleShot(300, self.settings)
        self._poll()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 22, 22, 16)
        outer.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(0)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.addWidget(label('额度管家', 16, bold=True))
        titles.addWidget(label('CODEX · ' + ('离线演示' if self.demo else '随时掌握用量'), 8, MUTED))
        top.addLayout(titles)
        top.addStretch()
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.refresh_button = self._tool('refresh', '刷新额度  Ctrl+R', self.refresh)
        self.pin_button = self._tool('pin', '切换置顶', self.pin)
        self.minimize_button = self._tool('minimize', '隐藏到托盘  Ctrl+Alt+H', self.minimize)
        self.close_button = self._tool('close', '退出并停止后续自动接续', self.close)
        self.header_buttons = [self.refresh_button, self.pin_button, self.minimize_button, self.close_button]
        for button in self.header_buttons:
            buttons.addWidget(button, alignment=Qt.AlignmentFlag.AlignVCenter)
        top.addLayout(buttons)
        outer.addLayout(top)

        self.quota_labels, self.quota_values, self.quota_bars, self.reset_labels = [], [], [], []
        card = self.quota_card = QFrame()
        card.setObjectName('quotaCard')
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        self.window_cards = []
        for i, (title, color) in enumerate([('主额度窗口', BLUE), ('周额度窗口', '#5856D6')]):
            tile = QFrame()
            tile.setObjectName('quotaTile')
            tile_box = QVBoxLayout(tile)
            tile_box.setContentsMargins(18, 14, 18, 15)
            tile_box.setSpacing(8)
            row = QHBoxLayout()
            caption = label(title, 10, MUTED)
            row.addWidget(caption)
            row.addStretch()
            row.addWidget(label('剩余额度', 8, MUTED))
            tile_box.addLayout(row)
            value = label('未知', 38 if i == 0 else 28, INK, True)
            value.setFont(QFont('Segoe UI Variable Display', 38 if i == 0 else 28, QFont.Weight.DemiBold))
            tile_box.addWidget(value)
            bar = QuotaBar(color)
            reset = label('等待服务端重置时间', 8, MUTED)
            tile_box.addWidget(bar)
            tile_box.addWidget(reset)
            box.addWidget(tile)
            self.window_cards.append(tile)
            self.quota_labels.append(caption)
            self.quota_values.append(value)
            self.quota_bars.append(bar)
            self.reset_labels.append(reset)
        outer.addWidget(card)

        health_group = QVBoxLayout()
        health_group.setSpacing(3)
        health = QHBoxLayout()
        self.health_dot = label('●', 9, MUTED)
        self.status_label = label('自动接续已关闭', 9, bold=True)
        health.addWidget(self.health_dot)
        health.addWidget(self.status_label, 1)
        self.records_button = self._tool('records', '查看恢复记录  Ctrl+L', self.records, 28)
        health.addWidget(self.records_button)
        health_group.addLayout(health)
        self.count_label = label('待续 0   投递 0   确认 0', 8, MUTED)
        health_group.addWidget(self.count_label)
        outer.addLayout(health_group)

        controls = self.controls_card = QFrame()
        controls.setObjectName('controls')
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(16, 11, 16, 11)
        controls_layout.setSpacing(10)
        self.auto_switch, self.auto_state = self._switch_row(controls_layout, '自动接续', '额度恢复后继续原任务', self.toggle_automation)
        outer.addWidget(controls)

        footer = QHBoxLayout()
        self.fresh_label = label('等待额度数据', 8, MUTED)
        footer.addWidget(self.fresh_label, 1)
        self.material_label = label('磨砂', 8, MUTED)
        footer.addWidget(self.material_label)
        footer.addWidget(self._tool('settings', '设置', self.settings, 28))
        outer.addLayout(footer)

    def _tool(self, kind, tooltip, callback, size=30):
        button = IconButton(kind, tooltip, size=size)
        button.clicked.connect(callback)
        return button

    def _switch_row(self, layout, title, subtitle, callback):
        row = QHBoxLayout()
        words = QVBoxLayout()
        words.setSpacing(1)
        words.addWidget(label(title, 10, bold=True))
        hint_label = label(subtitle, 8, MUTED)
        words.addWidget(hint_label)
        if title == '鼠标穿透':
            self.pass_hint = hint_label
        row.addLayout(words, 1)
        state = label('关', 9, RED, True)
        row.addWidget(state)
        switch = ToggleSwitch(title)
        switch.clicked.connect(callback)
        row.addWidget(switch)
        layout.addLayout(row)
        return switch, state

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width()-2, self.height()-2)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        alpha = surface_alphas(self.transparency)['base']
        # Preserve contrast when Composition is unavailable or glass is disabled.
        if not self.glass or self.glass_mode != 'acrylic':
            alpha = max(240, alpha)
        gradient.setColorAt(0, QColor(255, 255, 255, min(255, alpha+32)))
        gradient.setColorAt(.45, QColor(248, 248, 250, alpha))
        gradient.setColorAt(1, QColor(242, 242, 247, min(255, alpha+18)))
        painter.setBrush(gradient)
        # One quiet outline gives the glass an edge without a second bright rim.
        painter.setPen(QPen(QColor(39, 67, 76, 68), 1.0))
        painter.drawRoundedRect(rect, 26, 26)

    def _style_surfaces(self):
        alphas = surface_alphas(self.transparency)
        self.quota_card.setStyleSheet(
            'QFrame#quotaCard{background:transparent;border:none;}'
            'QFrame#quotaTile{background:rgba(255,255,255,%s);'
            'border:1px solid rgba(39,67,76,28);border-radius:20px;}' %
            min(255, alphas['card'] + 32))
        self.controls_card.setStyleSheet('QFrame#controls{background:rgba(255,255,255,%s);border:1px solid rgba(39,67,76,24);border-radius:20px;}' % (alphas['controls'] + 24))
        self.update()

    def set_transparency(self, value):
        self.transparency = normalized_transparency(value)
        self._style_surfaces()
        if hasattr(self, 'transparency_value'):
            self.transparency_value.setText('%s%%' % self.transparency)
        self._refresh_material_label()
        self._save_preferences()

    def _position(self):
        screens = QApplication.screens()
        areas = [(s.availableGeometry().left(), s.availableGeometry().top(),
                  s.availableGeometry().right()+1, s.availableGeometry().bottom()+1) for s in screens]
        current = QApplication.primaryScreen().availableGeometry()
        try:
            x = int(self.preferences.get('x', current.right()-self.width()-30))
            y = int(self.preferences.get('y', current.top()+70))
        except (TypeError, ValueError):
            x, y = current.left()+30, current.top()+70
        self.move(*clamp_position(x, y, self.width(), self.height(), areas))

    def _save_preferences(self):
        if self.demo:
            return
        self.preferences.update(x=self.x(), y=self.y(), pinned=self.pinned, glass=self.glass,
                                transparency=self.transparency)
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            temporary = self.settings_path + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(self.preferences, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.settings_path)
        except OSError as exc:
            log_error('HUD preferences: ' + str(exc))

    def showEvent(self, event):
        super().showEvent(event)
        self.hidden = False
        if self.effects is None:
            self.effects = self._effects_factory(int(self.winId()))
            self.control = ClickthroughControl(self.effects, self.shell)
            self._apply_glass()
        self._restore_interaction()
        QTimer.singleShot(0,self._sync_backdrop)

    def _apply_glass(self):
        if self.effects:
            if self.glass:
                self.glass_mode = self.effects.apply_glass()
            else:
                self.effects.disable_glass()
                self.glass_mode = 'fallback'
            self.effects.round_corners(26)
        self._refresh_material_label()
        self.update()

    def _refresh_material_label(self):
        material = '磨砂玻璃' if self.glass and self.glass_mode == 'acrylic' else '清晰底色'
        self.material_label.setText('%s · %s%%' % (material, self.transparency) if self.transparency else '纯色')
        self.material_label.setToolTip(
            '背景透明度 %s%%；文字和图标保持不透明。' % self.transparency
            if self.glass and self.glass_mode == 'acrylic'
            else '磨砂未启用或不可用，已使用清晰浅色背景。透明度偏好保留。')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, 'effects', None):
            self._sync_backdrop()
            # Qt can emit resize before Windows reports the new client size.
            QTimer.singleShot(0, self._sync_backdrop)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._sync_backdrop()

    def hideEvent(self, event):
        if getattr(self,'effects',None):
            sync = getattr(self.effects,'sync_backdrop',None)
            if sync:
                sync(False)
        super().hideEvent(event)

    def _sync_backdrop(self):
        if self.closed or not getattr(self,'effects',None):
            return
        # Keep the foreground region aligned even if native resize completed
        # after the Qt event; unchanged geometry does not reset the region.
        self.effects.round_corners(26)
        sync = getattr(self.effects,'sync_backdrop',None)
        if sync and not sync() and self.glass_mode == 'acrylic':
            self.glass_mode = 'fallback'
            self._refresh_material_label()

    def _restore_interaction(self):
        self._cancel_pending_pass()
        if self.control and not self.control.restore_interaction():
            self._notify('鼠标穿透未能解除', self.control.error)
        self._reflect_controls()

    def _reflect_controls(self):
        enabled = self.service.snapshot().get('enabled', False)
        passing = bool(self.control and self.control.enabled)
        for switch, state, value in [(self.auto_switch, self.auto_state, enabled), (self.pass_switch, self.pass_state, passing)]:
            if switch is None:
                continue
            switch.reflect(value)
            state.setText('开' if value else '关')
            state.setStyleSheet('color:%s;background:transparent;' % (GREEN if value else RED))

    def _notify(self, title, text):
        self._restore_for_dialog()
        QMessageBox.warning(self, title, text)

    def _restore_for_dialog(self):
        self._cancel_pending_pass()
        if self.control and self.control.enabled:
            self.control.restore_interaction()
            self._reflect_controls()

    def toggle_automation(self, checked=None):
        if not self.configured:
            self._reflect_controls()
            self.settings()
            return
        self.service.set_enabled(not self.service.snapshot().get('enabled', False))
        self._reflect_controls()

    def toggle_clickthrough(self, checked=None):
        if not self.control:
            return
        requested = not (self._requested_passthrough if self._pass_timer.isActive() else self.control.enabled)
        # Defer until the native mouse release finishes; the switch then stays visible.
        self._requested_passthrough = requested
        self._pass_timer.start()

    def _cancel_pending_pass(self):
        self._pass_timer.stop()
        self._requested_passthrough = False

    def _set_clickthrough(self, requested):
        if self.closed:
            return
        requested = bool(requested) and not self.hidden
        if self.control and not self.control.set_enabled(requested):
            self._notify('鼠标穿透不可用', self.control.error)
        self._reflect_controls()

    def pin(self):
        self._restore_interaction()
        requested = not self.pinned
        if self.effects and self.effects.set_topmost(requested):
            self.pinned = requested
            self.pin_button.setChecked(self.pinned)
            self.pin_button.setToolTip('已置顶' if self.pinned else '未置顶')
            self._save_preferences()
        self.pin_button.setChecked(self.pinned)

    def refresh(self):
        if self.configured:
            self.service.request_refresh()
        else:
            self.settings()

    def minimize(self):
        if not self.shell.ready:
            self._notify('托盘不可用', '窗口将保持可见。请使用关闭按钮退出。')
            return
        self._restore_interaction()
        self._save_preferences()
        self.hide()
        self.hidden = True

    def restore(self):
        self._restore_interaction()
        self._position()
        self.show()
        self.raise_()
        self.activateWindow()
        self.hidden = False

    def toggle_visibility(self):
        if self.hidden or (self.control and self.control.enabled):
            self.restore()
        else:
            self.minimize()

    def _shell_events(self):
        while True:
            try:
                event = self.shell.events.get_nowait()
            except queue.Empty:
                break
            if event == 'visibility': self.toggle_visibility()
            elif event == 'show': self.restore()
            elif event == 'toggle': self.toggle_automation()
            elif event == 'clickthrough': self.toggle_clickthrough()
            elif event == 'records': self.records()
            elif event == 'close': self.close(); return
            elif event in ('tray_failed', 'shell_error'):
                if self.control:
                    self.control.ensure_recoverable()
                self.restore()
                self.fresh_label.setText('托盘连接异常 · 窗口保持可操作')

    def _poll(self):
        if self.closed:
            return
        self._sync_backdrop()
        self._shell_events()
        if self.closed:
            return
        snap = self.last = self.service.snapshot()
        quota = snap.get('quota') or {}
        for i, (key, reset_key, slot) in enumerate([('used', 'reset_at', 'primary'), ('secondary_used', 'secondary_reset_at', 'secondary')]):
            value = remaining(quota.get(key))
            self.quota_labels[i].setText(window_title(quota, slot))
            self.quota_values[i].setText(percent(value))
            self.quota_bars[i].set_value(value)
            reset = quota.get(reset_key)
            try:
                stamp = dt.datetime.fromtimestamp(float(reset)).strftime('%m/%d %H:%M')
                self.reset_labels[i].setText('重置 ' + stamp + '   ·   ' + countdown(reset))
            except (TypeError, ValueError, OSError, OverflowError):
                self.reset_labels[i].setText('服务端尚未提供重置时间')
        self.fresh_label.setText(('演示 · ' if self.demo else '') + freshness(snap))
        self.fresh_label.setToolTip(snap.get('quota_error') or '只读刷新额度；倒计时不额外请求账户')
        self.status_label.setText(health_label(snap) if self.configured else '请在设置中配置 Codex 连接')
        color = RED if snap.get('quota_error') or snap.get('monitor_state') in ('blocked', 'error') or snap.get('service_alive') is False else GREEN if snap.get('enabled') else MUTED
        self.health_dot.setStyleSheet('color:%s;background:transparent;' % color)
        count = '待续 %s   投递 %s   确认 %s' % (snap.get('pending_count', 0), snap.get('queued_count', 0), snap.get('confirmed_count', 0))
        if snap.get('uncertain_count'):
            count += '   待核 %s' % snap['uncertain_count']
        self.count_label.setText(count)
        if self.control:
            self.control.ensure_recoverable()
        self._reflect_controls()
        self.shell.update(snap.get('enabled', False), self.hidden, bool(self.control and self.control.enabled), summary=health_label(snap))
        if self.records_dialog and self.records_dialog.isVisible():
            self._populate_records()

    def records(self):
        self._restore_for_dialog()
        if self.hidden:
            self.restore()
        if self.records_dialog:
            self.records_dialog.show()
            self.records_dialog.raise_()
            self._populate_records()
            return
        dialog = self.records_dialog = QDialog(self)
        dialog.setWindowTitle('任务恢复记录')
        dialog.resize(720, 390)
        self._dialog_style(dialog)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.addWidget(label('任务恢复记录', 15, bold=True))
        layout.addWidget(label('“已投递”表示命令已接受；“已确认”表示找到了本次续接消息及后续轮次。', 9, MUTED))
        self.records_table = QTableWidget(0, 4)
        self.records_table.setHorizontalHeaderLabels(['任务', '状态', '尝试', '更新时间'])
        self.records_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.records_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.records_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.records_table.verticalHeader().hide()
        self.records_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.records_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.records_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.records_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.records_table.itemSelectionChanged.connect(self._record_detail)
        layout.addWidget(self.records_table, 1)
        self.record_info = QTextEdit()
        self.record_info.setReadOnly(True)
        self.record_info.setMaximumHeight(90)
        layout.addWidget(self.record_info)
        self._records_signature = None
        self._populate_records()
        dialog.show()

    def _populate_records(self):
        events = self.last.get('recent_events') or []
        signature = repr(events)
        if signature == self._records_signature:
            return
        self._records_signature = signature
        self._record_rows = events
        self.records_table.setRowCount(len(events))
        for i, event in enumerate(events):
            try:
                stamp = dt.datetime.fromtimestamp(float(event.get('updated_at', 0))).strftime('%m/%d %H:%M')
            except (TypeError, ValueError, OSError):
                stamp = '未知'
            for j, text in enumerate([event.get('title') or event.get('thread_id', '')[:12], STATE_NAMES.get(event.get('state'), event.get('state', '未知')), str(event.get('attempts', 0)), stamp]):
                self.records_table.setItem(i, j, QTableWidgetItem(text))
        if not events:
            self.record_info.setPlainText('还没有恢复记录。开启自动接续后，仅处理明确因额度中断的原任务。')
        else:
            self.records_table.selectRow(0)

    def _record_detail(self):
        row = self.records_table.currentRow()
        if row < 0 or row >= len(getattr(self, '_record_rows', [])):
            return
        item = self._record_rows[row]
        self.record_info.setPlainText('会话：%s\n原因：%s' % (item.get('thread_id', ''), item.get('error') or STATE_NAMES.get(item.get('state'), '暂无额外说明')))

    @staticmethod
    def _dialog_style(dialog):
        dialog.setStyleSheet('QDialog{background:#F3F6FB;color:#17243A;} QLabel{background:transparent;} QLineEdit,QTextEdit,QTableWidget{background:white;color:#17243A;border:1px solid #DCE3EE;border-radius:8px;padding:6px;} QPushButton{background:#3478F6;color:white;border:none;border-radius:9px;padding:8px 14px;} QPushButton:hover{background:#2867DF;} QHeaderView::section{background:#EAF0F9;color:#66768B;border:0;padding:7px;} QSlider::groove:horizontal{height:4px;background:#DCE4F0;border-radius:2px;} QSlider::sub-page:horizontal{background:#3478F6;border-radius:2px;} QSlider::handle:horizontal{background:white;border:1px solid #3478F6;width:16px;margin:-6px 0;border-radius:8px;}')

    def settings(self):
        self._restore_for_dialog()
        if self.settings_dialog:
            self.settings_dialog.show()
            self.settings_dialog.raise_()
            return
        dialog = self.settings_dialog = QDialog(self)
        dialog.setWindowTitle('额度管家设置')
        dialog.setMinimumWidth(520)
        self._dialog_style(dialog)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(13)
        layout.addWidget(label('设置', 16, bold=True))
        layout.addWidget(label('外观', 10, bold=True))
        self.glass_checkbox = QCheckBox('磨砂玻璃背景')
        self.glass_checkbox.setChecked(self.glass)
        layout.addWidget(self.glass_checkbox)
        opacity_heading = QHBoxLayout()
        opacity_heading.addWidget(label('背景透明度', 10))
        opacity_heading.addStretch()
        self.transparency_value = label('%s%%' % self.transparency, 10, BLUE, True)
        opacity_heading.addWidget(self.transparency_value)
        layout.addLayout(opacity_heading)
        self.transparency_slider = QSlider(Qt.Orientation.Horizontal)
        self.transparency_slider.setRange(0, 80)
        self.transparency_slider.setSingleStep(5)
        self.transparency_slider.setPageStep(10)
        self.transparency_slider.setAccessibleName('背景透明度')
        self.transparency_slider.setValue(self.transparency)
        self.transparency_slider.valueChanged.connect(self.set_transparency)
        layout.addWidget(self.transparency_slider)
        ends = QHBoxLayout()
        ends.addWidget(label('更实 · 0%', 8, MUTED))
        ends.addStretch()
        ends.addWidget(label('更透 · 80%', 8, MUTED))
        layout.addLayout(ends)
        layout.addWidget(label('即时预览并记住设置，仅调整背景透明度。', 8, MUTED))
        def change_glass(checked):
            self.glass = checked
            self._apply_glass()
            self._save_preferences()
        self.glass_checkbox.toggled.connect(change_glass)

        layout.addWidget(label('操作', 10, bold=True))
        recovery = 'Ctrl+Alt+P 切换 · 托盘可解除' if self.shell.pass_hotkey else '从托盘解除 · 全局快捷键被占用'
        self.pass_switch, self.pass_state = self._switch_row(layout, '鼠标穿透', recovery, self.toggle_clickthrough)
        self._reflect_controls()
        layout.addWidget(label('重新显示 HUD 时自动解除穿透。', 8, MUTED))

        self.follow_checkbox = QCheckBox('随 GPT 启动')
        self.follow_checkbox.setChecked(False if self.demo else follow_gpt.is_enabled())
        self.follow_checkbox.setEnabled(not self.demo)
        layout.addWidget(self.follow_checkbox)
        self.follow_status = label('打开 GPT 后自动打开 HUD；手动退出后不会反复弹回。', 8, MUTED)
        self.follow_status.setWordWrap(True)
        layout.addWidget(self.follow_status)
        def change_follow(checked):
            try:
                follow_gpt.set_enabled(checked)
                self.follow_status.setText('已开启，下一次打开 GPT 会自动打开 HUD。' if checked else '已关闭随 GPT 启动。')
            except OSError as exc:
                with QSignalBlocker(self.follow_checkbox):
                    self.follow_checkbox.setChecked(follow_gpt.is_enabled())
                self.follow_status.setText('设置失败：' + str(exc))
        self.follow_checkbox.toggled.connect(change_follow)

        layout.addWidget(label('连接', 10, bold=True))
        form = QFormLayout()
        entries = []
        runtime = (self.core.CODEX_RUNTIME or {}) if self.core else {}
        saved = read_json(self.core.CONFIG_FILE) if self.core else {}
        for title, key, fallback in [('Codex CLI', 'codex_cli_path', runtime.get('codex_js', '')), ('Node.js', 'node_path', runtime.get('node', ''))]:
            row = QHBoxLayout()
            entry = QLineEdit(saved.get(key) or fallback)
            entry.setEnabled(not self.demo)
            browse = QPushButton('选择')
            browse.setEnabled(not self.demo)
            def choose(checked=False, target=entry):
                chosen, _ = QFileDialog.getOpenFileName(dialog, '选择程序或 CLI 入口', '', '程序 (*.exe *.cmd *.js);;所有文件 (*)')
                if chosen:
                    target.setText(chosen)
            browse.clicked.connect(choose)
            row.addWidget(entry, 1)
            row.addWidget(browse)
            form.addRow(title, row)
            entries.append(entry)
        original_paths = tuple(entry.text() for entry in entries)
        layout.addLayout(form)
        status = label('仅保存程序路径，沿用已有登录。' if not self.demo else '离线演示不会读写账户与个人设置。', 9, MUTED)
        status.setWordWrap(True)
        layout.addWidget(status)
        row = QHBoxLayout()
        folder = QPushButton('打开日志文件夹')
        def open_logs():
            directory = os.path.dirname(self.settings_path)
            os.makedirs(directory, exist_ok=True)
            os.startfile(directory)
        folder.clicked.connect(open_logs)
        row.addWidget(folder)
        row.addStretch()
        save = self.settings_done = QPushButton('完成')
        row.addWidget(save)
        layout.addLayout(row)
        def apply():
            if not self.demo and tuple(entry.text() for entry in entries) != original_paths:
                cli = self.core.resolve_cli_js(entries[0].text())
                node = os.path.expandvars(os.path.expanduser(entries[1].text().strip().strip('"')))
                if not cli or not os.path.isfile(node) or os.path.basename(node).lower() not in ('node.exe', 'node'):
                    status.setText('路径无效，请选择 Codex CLI 和 node.exe。')
                    return
                try:
                    values = read_json(self.core.CONFIG_FILE)
                    values.update(codex_cli_path=cli, node_path=os.path.abspath(node))
                    os.makedirs(os.path.dirname(self.core.CONFIG_FILE), exist_ok=True)
                    temporary = self.core.CONFIG_FILE + '.hud.tmp'
                    with open(temporary, 'w', encoding='utf-8') as stream:
                        json.dump(values, stream, ensure_ascii=False, indent=2)
                    os.replace(temporary, self.core.CONFIG_FILE)
                    self.core.CODEX_RUNTIME = {'node': os.path.abspath(node), 'codex_js': cli}
                except OSError as exc:
                    status.setText('保存失败：' + str(exc))
                    return
                self.configured = True
                self.service.start()
                self.service.request_refresh()
            self._save_preferences()
            dialog.accept()
        save.clicked.connect(apply)
        dialog.show()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.drag_offset is not None:
            self.drag_offset = None
            self._save_preferences()

    def closeEvent(self, event):
        if not self.closed:
            self.closed = True
            self._cancel_pending_pass()
            self.timer.stop()
            self._save_preferences()
            self.service.set_enabled(False)
            if self.control:
                self.control.restore_interaction()
            if self.effects:
                self.effects.close()
            self.shell.close()
            threading.Thread(target=self.service.stop, kwargs={'timeout': 2}, daemon=True).start()
            QApplication.instance().quit()
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--auto-close', type=float, default=0)
    parser.add_argument('--demo-report', help='Write offline graphics diagnostics (requires --demo)')
    args = parser.parse_args(argv)
    if args.demo_report and not args.demo:
        parser.error('--demo-report requires --demo')
    try:
        app = QApplication(sys.argv[:1])
        app.setQuitOnLastWindowClosed(False)
        app.setFont(QFont('Microsoft YaHei UI', 9))
        server = None
        if not args.demo:
            identity = os.path.normcase(os.path.abspath(os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))))
            name = 'codex-quota-glass-' + hashlib.sha256(identity.encode()).hexdigest()[:20]
            peer = QLocalSocket()
            peer.connectToServer(name)
            if peer.waitForConnected(350):
                peer.write(b'show')
                peer.waitForBytesWritten(350)
                return 0
            server = QLocalServer()
            server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            if not server.listen(name):
                raise RuntimeError('另一个额度管家实例正在启动，请稍后再次打开。')
        if args.demo:
            service, core, configured = DemoService(), None, True
        else:
            import codex_watchdog as core
            from watchdog_runtime import WatchdogService
            configured = bool(discover_runtime(core))
            service = WatchdogService(core=core)
        hud = GlassHUD(service, core=core, demo=args.demo, configured=configured)
        hud.show()
        if args.demo_report:
            def report_graphics():
                effects = hud.effects
                backdrop = getattr(effects, '_backdrop', None)
                report = dict(glass_mode=hud.glass_mode,
                              backdrop_handle=int(backdrop.Handle.ToInt64()) if backdrop else 0,
                              error=effects.error if effects else 'No native effects',
                              tray_ready=hud.shell.ready,
                              tray_icon_custom=bool(getattr(hud.shell, '_owned_icon', None)),
                              desktop_pixels_verified=False)
                with open(args.demo_report, 'w', encoding='utf-8') as stream:
                    json.dump(report, stream, indent=2)
            QTimer.singleShot(800, report_graphics)
        if server:
            def accept_peer():
                connection = server.nextPendingConnection()
                def ready():
                    connection.readAll()
                    hud.restore()
                    connection.disconnectFromServer()
                connection.readyRead.connect(ready)
                connection.disconnected.connect(connection.deleteLater)
                if connection.bytesAvailable():
                    ready()
            server.newConnection.connect(accept_peer)
        if args.auto_close > 0:
            QTimer.singleShot(int(args.auto_close*1000), hud.close)
        def report_error(kind, value, tb):
            log_error(''.join(traceback.format_exception(kind, value, tb)))
        sys.excepthook = report_error
        return app.exec()
    except Exception:
        log_error(traceback.format_exc())
        raise


if __name__ == '__main__':
    raise SystemExit(main())

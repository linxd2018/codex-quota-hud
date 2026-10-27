"""Small, UI-independent presentation and desktop interaction policies."""


class ClickthroughControl:
    def __init__(self, effects, shell):
        self.effects, self.shell = effects, shell
        self.enabled = False
        self.error = ''

    def recoverable(self):
        return bool(self.shell and (self.shell.ready or self.shell.pass_hotkey))

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled and not self.recoverable():
            self.error = '托盘和恢复快捷键均不可用，无法开启鼠标穿透。'
            return False
        if not self.effects.set_clickthrough(enabled):
            # A native write can partially succeed before rollback fails.
            # Keep the recovery guard and switch aligned with the observed HWND.
            self.enabled = bool(getattr(self.effects, 'clickthrough', self.enabled))
            self.error = getattr(self.effects, 'error', '') or '未能更改鼠标穿透，请使用托盘退出后重启。'
            return False
        self.enabled, self.error = enabled, ''
        return True

    def restore_interaction(self):
        return self.set_enabled(False)

    def ensure_recoverable(self):
        if self.enabled and not self.recoverable():
            return self.restore_interaction()
        return True


def window_title(quota, slot):
    key = 'window_minutes' if slot == 'primary' else 'secondary_window_minutes'
    value = quota.get(key)
    try:
        minutes = int(value)
    except (TypeError, ValueError, OverflowError):
        return '主额度窗口' if slot == 'primary' else '周额度窗口'
    if minutes <= 0:
        return '主额度窗口' if slot == 'primary' else '周额度窗口'
    count, unit = (minutes // 1440, '天') if minutes % 1440 == 0 else (
        (minutes // 60, '小时') if minutes % 60 == 0 else (minutes, '分钟'))
    return '%s %s窗口' % (count, unit)


def health_label(snapshot):
    if snapshot.get('service_alive') is False:
        return '后台已停止 · 请重启'
    state = snapshot.get('monitor_state')
    if state == 'blocked':
        return '另一看门狗正在运行'
    if not snapshot.get('enabled'):
        return '自动接续已关闭'
    if state == 'waiting':
        return '等待额度恢复'
    if state == 'error':
        return '监控遇到错误 · 将重试'
    return snapshot.get('status') or '正在监控额度中断'

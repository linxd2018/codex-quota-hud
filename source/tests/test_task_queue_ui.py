import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt, QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import quota_glass
from quota_hud import DemoService
from tests.test_glass_ui import FakeShell, FakeEffects


class TaskQueueUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = DemoService()
        self.hud = quota_glass.GlassHUD(self.service, demo=True,
            shell=FakeShell(), effects_factory=FakeEffects)
        self.hud.show()
        self.app.processEvents()

    def tearDown(self):
        self.hud.close()
        self.app.processEvents()

    def composer(self):
        self.assertTrue(hasattr(self.hud, 'new_task_button'), 'HUD must expose new task entry')
        self.hud.new_task_button.click()
        self.app.processEvents()
        return self.hud.task_composer

    def fill(self, composer, text='检查工程并给出摘要'):
        composer.description.setPlainText(text)
        composer.cwd.setEditText(self.temp.name)

    def test_zero_quota_submission_only_enqueues(self):
        composer = self.composer()
        self.fill(composer)
        self.service.snapshot = lambda original=self.service.snapshot: dict(original(), quota={'used': 100, 'available': False})
        composer.submit_button.click()
        tasks = self.service.snapshot()['new_tasks']
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['status'], 'pending')
        self.assertEqual(tasks[0]['sandbox'], 'workspace-write')
        self.assertEqual(tasks[0]['approval_policy'], 'on-request')
        self.assertFalse(self.service.enabled)
        self.assertIn('已加入待启动队列', composer.status.text())

    def test_empty_prompt_and_missing_directory_keep_draft(self):
        composer = self.composer()
        composer.cwd.setEditText(self.temp.name)
        composer.submit_button.click()
        self.assertIn('任务描述', composer.status.text())
        self.fill(composer)
        composer.cwd.setEditText(str(Path(self.temp.name) / 'missing'))
        composer.submit_button.click()
        self.assertIn('工作目录', composer.status.text())
        self.assertEqual(self.service.snapshot()['new_tasks'], [])
        self.assertTrue(composer.description.toPlainText())

    def test_collapse_keeps_draft_and_cancel_clears(self):
        composer = self.composer()
        self.fill(composer, '草稿\n第二行')
        self.hud.new_task_button.click()
        self.assertFalse(self.hud.task_scroll.isVisible())
        self.hud.new_task_button.click()
        self.assertEqual(composer.description.toPlainText(), '草稿\n第二行')
        composer.cancel_button.click()
        self.assertFalse(self.hud.task_scroll.isVisible())
        self.assertEqual(composer.description.toPlainText(), '')

    def test_dynamic_models_limit_reasoning_and_preserve_unknown_offline_choice(self):
        composer = self.composer()
        composer.apply_snapshot({'task_capabilities': {'models': [
            {'id': 'model-a', 'model': 'model-a', 'displayName': 'Model A',
             'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}]},
            {'id': 'model-b', 'model': 'model-b', 'displayName': 'Model B',
             'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}],
            'sandbox_modes': ['read-only', 'workspace-write'], 'approval_modes': ['on-request', 'never']}})
        composer.model.setCurrentIndex(composer.model.findData('model-a'))
        composer.effort.setCurrentIndex(composer.effort.findData('high'))
        composer.model.setCurrentIndex(composer.model.findData('model-b'))
        self.assertEqual(composer.effort.currentData(), '')
        self.assertEqual(composer.effort.findData('high'), -1)
        self.assertGreaterEqual(composer.effort.findData('medium'), 0)

    def test_submit_remembers_directory_model_and_effort(self):
        composer = self.composer()
        self.fill(composer)
        composer.apply_snapshot({'task_capabilities': {'models': [
            {'model': 'real-model', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}]}})
        composer.model.setCurrentIndex(composer.model.findData('real-model'))
        composer.effort.setCurrentIndex(composer.effort.findData('high'))
        composer.submit_button.click()
        self.assertEqual(self.hud.preferences['task_model'], 'real-model')
        self.assertEqual(self.hud.preferences['task_reasoning_effort'], 'high')
        self.assertEqual(self.hud.preferences['task_recent_directories'][0], self.temp.name)

    def test_settings_concurrency_updates_and_persists_without_enabling(self):
        self.hud.demo = False
        self.hud.settings_path = str(Path(self.temp.name) / 'hud-settings.json')
        self.hud.core = SimpleNamespace(CONFIG_FILE=str(Path(self.temp.name) / 'config.json'), CODEX_RUNTIME={})
        with patch('quota_glass.follow_gpt.is_enabled', return_value=False):
            self.hud.settings()
        spin = self.hud.max_concurrent_spin
        self.assertEqual((spin.minimum(), spin.maximum(), spin.value()), (3, 10, 3))
        spin.setValue(7)
        self.assertEqual(self.service.snapshot()['maxConcurrentTasks'], 7)
        self.assertEqual(json.loads(Path(self.hud.settings_path).read_text())['maxConcurrentTasks'], 7)
        self.assertFalse(self.service.enabled)
        self.hud.demo = True

    def test_spin_rejects_invalid_typing_and_restores_valid_persisted_limit(self):
        from task_queue_ui import ConcurrentSpinBox
        from PySide6.QtGui import QValidator
        spin = ConcurrentSpinBox()
        for text in ('2', '11', '3.5', '-3', 'abc', '03'):
            self.assertEqual(spin.validate(text, len(text))[0], QValidator.State.Invalid)
        preferences = Path(self.temp.name) / 'hud-settings.json'
        preferences.write_text(json.dumps({'maxConcurrentTasks': 8}), encoding='utf-8')
        core = SimpleNamespace(CONFIG_FILE=str(Path(self.temp.name) / 'config.json'))
        other = quota_glass.GlassHUD(DemoService(), core=core, demo=False,
            shell=FakeShell(), effects_factory=FakeEffects)
        try:
            self.assertEqual(other.max_concurrent_tasks, 8)
            self.assertEqual(other.service.snapshot()['maxConcurrentTasks'], 8)
        finally:
            other.close()

    def test_approval_requires_explicit_choice_and_can_cancel(self):
        composer = self.composer()
        self.fill(composer)
        composer.submit_button.click()
        task = self.service.tasks[0]
        task.update(status='waiting_authorization', started_at=1,
            approval={'id': 9, 'method': 'item/commandExecution/requestApproval',
                      'params': {'command': 'write local file', 'cwd': self.temp.name}})
        self.hud.open_task_queue()
        dialog = self.hud.task_queue_dialog
        self.assertNotIn('approval_response', task)
        self.assertIn('write local file', dialog.detail.toPlainText())
        self.assertFalse(dialog.edit_button.isEnabled())
        self.assertFalse(dialog.delete_button.isEnabled())
        self.assertTrue(hasattr(dialog, 'cancel_approval_button'))
        dialog.cancel_approval_button.click()
        self.assertEqual(task['approval_response'], {'decision': 'cancel'})
        self.assertFalse(dialog.allow_button.isVisible())

    def test_stopped_authorization_can_be_retried_from_queue(self):
        composer = self.composer()
        self.fill(composer)
        composer.submit_button.click()
        task = self.service.tasks[0]
        task.update(status='waiting_authorization', started_at=1,
                    phase='approval_timeout', worker_pid=None, thread_id='existing-thread')
        self.hud.open_task_queue()
        dialog = self.hud.task_queue_dialog
        self.assertTrue(dialog.retry_button.isEnabled())
        dialog.retry_button.click()
        self.assertEqual(task['status'], 'waiting_resume')

    def test_offline_preferences_keep_last_model_and_directory_picker(self):
        from task_queue_ui import TaskComposer
        prefs = {'task_model': 'previous-model', 'task_reasoning_effort': 'high',
                 'task_cwd': self.temp.name, 'task_recent_directories': [self.temp.name]}
        composer = TaskComposer(self.service, prefs, lambda: None)
        try:
            composer.apply_snapshot({'task_capabilities_error': 'network offline'})
            self.assertEqual(composer.model.currentData(), 'previous-model')
            self.assertEqual(composer.effort.currentData(), 'high')
            self.assertIn('启动时验证', composer.capability_status.text())
            with patch('task_queue_ui.QFileDialog.getExistingDirectory', return_value=self.temp.name):
                composer.browse_button.click()
            self.assertEqual(composer.cwd.currentText(), self.temp.name)
        finally:
            composer.close()

    def test_small_work_area_keeps_composer_controls_reachable(self):
        self.assertTrue(hasattr(self.hud, 'new_task_button'))
        screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1280, 720))
        with patch.object(self.hud, 'screen', return_value=screen):
            self.hud.new_task_button.click()
            self.app.processEvents()
            self.assertLessEqual(self.hud.height(), 720)
            self.assertGreaterEqual(self.hud.task_scroll.height(), 80)

    def test_escape_collapses_without_losing_prompt_and_repeated_expand_is_stable(self):
        composer = self.composer()
        self.fill(composer, 'keep this draft')
        expanded = self.hud.height()
        self.hud.activateWindow()
        composer.description.setFocus()
        QTest.qWait(50)
        QTest.keyClick(composer.description, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(self.hud.task_scroll.isVisible())
        self.assertFalse(self.hud.hidden)
        self.hud.new_task_button.click()
        self.app.processEvents()
        self.assertEqual(composer.description.toPlainText(), 'keep this draft')
        self.assertEqual(self.hud.height(), expanded)

    def test_invalid_saved_concurrency_recovers_to_three(self):
        self.assertTrue(hasattr(quota_glass, 'normalized_max_concurrent_tasks'))
        for bad in (None, True, False, 2, 11, 3.2, '7', 'oops'):
            self.assertEqual(quota_glass.normalized_max_concurrent_tasks(bad), 3)
        for value in range(3, 11):
            self.assertEqual(quota_glass.normalized_max_concurrent_tasks(value), value)

    def test_queue_edit_retry_priority_and_literal_failure_details(self):
        composer = self.composer()
        self.fill(composer, '<script>task</script>')
        composer.submit_button.click()
        self.hud.open_task_queue()
        dialog = self.hud.task_queue_dialog
        dialog.refresh(self.service.snapshot())
        dialog.table.selectRow(0)
        self.assertIn('<script>task</script>', dialog.detail.toPlainText())
        dialog.priority.setValue(4)
        dialog.priority_button.click()
        self.assertEqual(self.service.snapshot()['new_tasks'][0]['priority'], 4)
        dialog.edit_button.click()
        self.assertEqual(composer.description.toPlainText(), '<script>task</script>')
        composer.description.setPlainText('已修改任务')
        composer.submit_button.click()
        self.assertEqual(self.service.snapshot()['new_tasks'][0]['description'], '已修改任务')

    def test_expanded_panel_stays_within_work_area_and_text_input_does_not_drag(self):
        composer = self.composer()
        self.fill(composer, '\n'.join(['long prompt'] * 100))
        self.app.processEvents()
        area = self.hud.screen().availableGeometry()
        self.assertLessEqual(self.hud.height(), area.height())
        self.assertGreater(self.hud.task_scroll.height(), 100)
        position = self.hud.pos()
        QTest.mouseClick(composer.description.viewport(), Qt.MouseButton.LeftButton)
        QTest.keyClicks(composer.description, ' more')
        self.assertEqual(self.hud.pos(), position)
        self.assertFalse(self.service.enabled)


if __name__ == '__main__':
    unittest.main()

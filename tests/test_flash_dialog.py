import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PyQt5 import QtWidgets as W
from projects import defaults
from workbench import FlashDialog, Workbench


class FlashDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = W.QApplication.instance() or W.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.exe = root / 'JLink.exe'
        self.exe.touch()
        self.hex = root / 'firmware.hex'
        self.hex.write_text(':0400000001020304F2\n:00000001FF\n')
        self.config = dict(defaults(), backend='jlink', chip='STM32F407ZG',
                           jlink_exe=str(self.exe), image=str(self.hex))
        self.parent = W.QWidget()
        self.parent.daplinks = [SimpleNamespace(product_name='DAPLink', unique_id='probe-1')]
        self.dialog = None

    def tearDown(self):
        if self.dialog:
            self.dialog.reject()
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def create(self, **changes):
        self.config.update(changes)
        self.dialog = FlashDialog(self.config, self.parent)
        return self.dialog

    def test_hex_without_elf_or_saved_project(self):
        dialog = self.create()
        self.assertTrue(dialog.start.isEnabled())
        self.assertTrue(dialog.address.isHidden())
        self.assertTrue(dialog.jlink_row.isHidden())
        dialog.accept()
        self.assertEqual(dialog.result(), W.QDialog.Accepted)
        self.assertEqual(dialog.config['elf'], '')
        self.assertEqual(dialog.image.size, 4)

    def test_missing_commander_can_be_fixed_in_dialog(self):
        dialog = self.create(jlink_exe=str(self.exe) + '.missing')
        self.assertFalse(dialog.start.isEnabled())
        self.assertFalse(dialog.jlink_row.isHidden())
        dialog.jlink_exe.setText(str(self.exe))
        self.assertTrue(dialog.start.isEnabled())

    def test_bin_requires_address(self):
        binary = Path(self.temp.name) / 'firmware.bin'
        binary.write_bytes(b'1234')
        dialog = self.create(image=str(binary))
        self.assertFalse(dialog.address.isHidden())
        self.assertFalse(dialog.start.isEnabled())
        dialog.address.setText('0x08000000')
        self.assertTrue(dialog.start.isEnabled())
        self.assertEqual(dialog.image.segments[0][0], 0x08000000)

    def test_daplink_configuration_in_same_dialog(self):
        dialog = self.create(backend='daplink', probe_uid='probe-1')
        self.assertFalse(dialog.start.isEnabled())
        dialog.fields['openocd'].setText(str(self.exe))
        dialog.fields['target_config'].setText('target/stm32f4x.cfg')
        self.assertTrue(dialog.start.isEnabled())
        self.assertFalse(dialog.native)
        self.assertEqual(dialog.config['probe_uid'], 'probe-1')

    def test_protected_jlink_keeps_openocd_and_rejects_overlap(self):
        dialog = self.create(protected_ranges=[[0, 4]], openocd=str(self.exe),
                             target_config='target/stm32f4x.cfg')
        self.assertFalse(dialog.native)
        self.assertFalse(dialog.start.isEnabled())
        self.assertEqual(dialog.config['protected_ranges'], [[0, 4]])
        dialog.protected.setPlainText('0x1000 - 0x2000')
        self.assertTrue(dialog.start.isEnabled())
        self.assertFalse(dialog.native)

    def test_cancel_does_not_mutate_original_config(self):
        dialog = self.create()
        dialog.chip.setText('OTHER_CHIP')
        dialog.probe.setCurrentIndex(1)
        dialog.reject()
        self.assertEqual(self.config['chip'], 'STM32F407ZG')
        self.assertEqual(self.config['backend'], 'jlink')

    def test_openocd_discovery_refresh_keeps_target_and_manual_scripts(self):
        root = Path(self.temp.name)
        installs = []
        for name, target in [('a', 'stm32f4x'), ('b', 'stm32f1x')]:
            install = root / name
            (install / 'bin').mkdir(parents=True)
            (install / 'bin/openocd.exe').touch()
            scripts = install / 'share/openocd/scripts'
            (scripts / 'target').mkdir(parents=True)
            (scripts / ('target/' + target + '.cfg')).touch()
            installs.append((install / 'bin/openocd.exe', scripts))
        dialog = self.create(backend='daplink', probe_uid='probe-1',
                             openocd=str(installs[0][0]), target_config='custom.cfg')
        selector = dialog.fields['target_config']
        self.assertEqual(selector.itemText(0), 'target/stm32f4x.cfg')
        self.assertEqual(selector.text(), 'custom.cfg')
        dialog.fields['openocd'].setText(str(installs[1][0]))
        self.assertEqual(selector.itemText(0), 'target/stm32f1x.cfg')
        self.assertEqual(selector.text(), 'custom.cfg')
        self.assertEqual(Path(dialog.config['scripts_dir']).resolve(), installs[1][1].resolve())
        dialog.fields['scripts_dir'].setText(str(installs[0][1]))
        dialog.fields['openocd'].setText(str(root / 'missing.exe'))
        self.assertEqual(dialog.fields['scripts_dir'].text(), str(installs[0][1]))
        self.assertEqual(selector.itemText(0), 'target/stm32f4x.cfg')

    def test_reset_selection_is_remembered(self):
        dialog = self.create()
        dialog.reset_config.setCurrentIndex(2)
        dialog.accept()
        view = Mock()
        view.capture_project.return_value = defaults()
        view.project = defaults()
        view.worker = None
        with patch('workbench.FlashDialog', return_value=dialog), \
                patch.object(dialog, 'exec', return_value=W.QDialog.Accepted):
            Workbench.prepare_flash(view)
        self.assertEqual(view.project['reset_config'], 'srst_only srst_nogate connect_assert_srst')

    def test_entry_opens_dialog_without_settings_or_saving(self):
        view = Mock()
        view.capture_project.return_value = defaults()
        with patch('workbench.FlashDialog') as dialog:
            dialog.return_value.exec.return_value = W.QDialog.Rejected
            Workbench.prepare_flash(view)
            dialog.assert_called_once_with(view.capture_project.return_value, view)
        view.edit_settings.assert_not_called()
        view.save_current_project.assert_not_called()
        view.launch_flash.assert_not_called()

    def test_accepted_configuration_is_remembered_before_launch(self):
        dialog = self.create()
        dialog.accept()
        view = Mock()
        view.capture_project.return_value = defaults()
        view.project = defaults()
        view.worker = None
        with patch('workbench.FlashDialog', return_value=dialog), \
                patch.object(dialog, 'exec', return_value=W.QDialog.Accepted):
            Workbench.prepare_flash(view)
        self.assertEqual(Path(view.project['jlink_exe']), self.exe.resolve())
        self.assertEqual(view.project['image'], str(self.hex))
        self.assertEqual(view.project['backend'], 'jlink')
        view.cmbDLL.setCurrentIndex.assert_called_once_with(0)
        view.launch_flash.assert_called_once_with()
        view.save_current_project.assert_not_called()


if __name__ == '__main__':
    unittest.main()

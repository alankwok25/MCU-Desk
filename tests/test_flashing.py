import threading
import unittest
from unittest.mock import Mock

from flashing import Image, program_image


class FlashHaltTests(unittest.TestCase):
    def test_failed_reset_or_halt_never_reads_or_writes_flash(self):
        for failing in ('reset init', 'wait_halt 3000'):
            with self.subTest(command=failing):
                server = Mock()
                def command(value, **kwargs):
                    if value == failing:
                        raise RuntimeError('TARGET: stm32f4x.cpu - Not halted')
                server.command.side_effect = command
                server.diagnostic_log.return_value = 'debug reset timeout'
                with self.assertRaisesRegex(RuntimeError, '复位并暂停芯片失败') as raised:
                    program_image({}, Image('test.bin', [(0x08000000, b'1234')], ''),
                                  threading.Event(), Mock(), Mock(return_value=server))
                self.assertIn('debug reset timeout', str(raised.exception))
                server.sectors.assert_not_called()
                server.close.assert_called_once()
                self.assertTrue(all(call.args[0] in ('reset init', 'wait_halt 3000')
                                    for call in server.command.call_args_list))

    def test_halt_confirmed_before_reading_flash_layout(self):
        server = Mock()
        def sectors():
            self.assertEqual([c.args[0] for c in server.command.call_args_list],
                             ['reset init', 'wait_halt 3000'])
            raise RuntimeError('stop at layout')
        server.sectors.side_effect = sectors
        server.diagnostic_log.return_value = ''
        with self.assertRaisesRegex(RuntimeError, '读取 Flash 布局失败'):
            program_image({}, Image('test.bin', [(0x08000000, b'1234')], ''),
                          threading.Event(), Mock(), Mock(return_value=server))
        server.sectors.assert_called_once()


if __name__ == '__main__':
    unittest.main()

"""Bounded probe enumeration outside the UI process; never opens a debug session."""
import multiprocessing as mp
from time import monotonic


def enumerate_probes(sender):
    try:
        from pyocd.probe.aggregator import DebugProbeAggregator
        probes = DebugProbeAggregator.get_all_connected_probes()
        sender.send(([(p.unique_id, p.product_name) for p in probes], ''))
    except Exception as exc:
        sender.send(([], str(exc)))
    finally:
        sender.close()


class ProbeDiscovery:
    def __init__(self, timeout=8, target=enumerate_probes):
        self.timeout, self.target = timeout, target
        self.process = self.receiver = None
        self.deadline = 0

    @property
    def running(self):
        return self.process is not None

    def start(self):
        if self.running:
            return
        ctx = mp.get_context('spawn')
        self.receiver, sender = ctx.Pipe(duplex=False)
        self.process = ctx.Process(target=self.target, args=(sender,), daemon=True)
        self.deadline = monotonic() + self.timeout
        try:
            self.process.start()
        except Exception:
            self.stop()
            raise
        finally:
            sender.close()

    def poll(self):
        if not self.running:
            return None
        try:
            if self.receiver.poll():
                result = self.receiver.recv()
            elif not self.process.is_alive():
                result = ([], '探针检测进程提前退出，请点击刷新重试。')
            elif monotonic() >= self.deadline:
                result = ([], '探针检测超时，请检查 USB 连接后点击刷新重试。')
            else:
                return None
        except (EOFError, OSError):
            result = ([], '探针检测进程未返回结果，请点击刷新重试。')
        self.stop()
        return result

    def stop(self):
        process, receiver = self.process, self.receiver
        self.process = self.receiver = None
        if receiver is not None:
            receiver.close()
        if process is not None:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(0.5)
                if process.is_alive():
                    process.kill()
                    process.join(0.5)
            process.close()

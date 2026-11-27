import threading
import unittest

from local_llm.locking import AliasDownloadLocks


class AliasDownloadLocksTests(unittest.TestCase):
    def test_lock_for_is_stable_per_alias(self) -> None:
        locks = AliasDownloadLocks()

        first = locks.lock_for("alias-a")

        self.assertIs(locks.lock_for("alias-a"), first)

    def test_lock_for_returns_distinct_locks_for_distinct_aliases(self) -> None:
        locks = AliasDownloadLocks()

        self.assertIsNot(locks.lock_for("alias-a"), locks.lock_for("alias-b"))

    def test_lock_for_is_stable_under_concurrent_access(self) -> None:
        locks = AliasDownloadLocks()
        barrier = threading.Barrier(4)
        results = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=5)
            lock = locks.lock_for("alias-a")
            with results_lock:
                results.append(lock)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 4)
        self.assertIs(results[0], results[1])
        self.assertIs(results[0], results[2])
        self.assertIs(results[0], results[3])


if __name__ == "__main__":
    unittest.main()
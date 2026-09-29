from __future__ import annotations

import errno

import pytest

from vast_broker.journal import _acquire_windows_lock


def test_windows_journal_lock_retries_only_contention_until_acquired(tmp_path):
    lock_file = tmp_path / "request.lock"
    attempts = 0
    sleeps = []

    class FakeMsvcrt:
        LK_NBLCK = 2

        @staticmethod
        def locking(_fd, mode, length):
            nonlocal attempts
            assert mode == FakeMsvcrt.LK_NBLCK
            assert length == 1
            attempts += 1
            if attempts <= 12:
                raise OSError(errno.EACCES, "byte is locked")

    with lock_file.open("a+b") as handle:
        _acquire_windows_lock(
            handle,
            FakeMsvcrt,
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

    assert attempts == 13
    assert sleeps == [0.05] * 12

    class BrokenMsvcrt:
        LK_NBLCK = 2

        @staticmethod
        def locking(_fd, _mode, _length):
            raise OSError(errno.EBADF, "invalid file descriptor")

    with lock_file.open("a+b") as handle:
        with pytest.raises(OSError) as raised:
            _acquire_windows_lock(
                handle,
                BrokenMsvcrt,
                sleep_fn=lambda seconds: sleeps.append(seconds),
            )

    assert raised.value.errno == errno.EBADF
    assert sleeps == [0.05] * 12

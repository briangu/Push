from __future__ import print_function

import threading
import time
import weakref
import socket
import os

from pysyncobj import replicated, SyncObjConsumer, SyncObj


class _ReplHostManagerImpl(SyncObjConsumer):
    def __init__(self, autoUnlockTime):
        super(_ReplHostManagerImpl, self).__init__()
        self.__locks = {}
        self.__autoUnlockTime = autoUnlockTime

    @replicated
    def acquire(self, lockID, clientID, currentTime):
        existingLock = self.__locks.get(lockID, None)
        # Auto-unlock old lock
        if existingLock is not None:
            if currentTime - existingLock[1] > self.__autoUnlockTime:
                existingLock = None
        # Acquire lock if possible
        if existingLock is None or existingLock[0] == clientID:
            self.__locks[lockID] = (clientID, currentTime)
            return True
        # Lock already acquired by someone else
        return False

    @replicated
    def prolongate(self, clientID, currentTime):
        for lockID in list(self.__locks):
            lockClientID, lockTime = self.__locks[lockID]

            if currentTime - lockTime > self.__autoUnlockTime:
                del self.__locks[lockID]
                continue

            if lockClientID == clientID:
                self.__locks[lockID] = (clientID, currentTime)

    @replicated
    def release(self, lockID, clientID):
        existingLock = self.__locks.get(lockID, None)
        if existingLock is not None and existingLock[0] == clientID:
            del self.__locks[lockID]

    def isAcquired(self, lockID, clientID, currentTime):
        existingLock = self.__locks.get(lockID, None)
        if existingLock is not None:
            if existingLock[0] == clientID:
                if currentTime - existingLock[1] < self.__autoUnlockTime:
                    return True
        return False

    def isOwned(self, lockID, clientID, currentTime):
        existingLock = self.__locks.get(lockID, None)
        if existingLock is not None:
            if currentTime - existingLock[1] < self.__autoUnlockTime:
                return True
        return False

    def rawData(self):
        return self.__locks.copy()


class ReplHostManager(object):

    def __init__(self, autoUnlockTime, selfID = None):
        """Replicated Lock Manager. Allow to acquire / release distributed locks.

        :param autoUnlockTime: lock will be released automatically
            if no response from holder for more than autoUnlockTime seconds
        :type autoUnlockTime: float
        :param selfID: (optional) - unique id of current lock holder.
        :type selfID: str
        """
        self.__lockImpl = _ReplHostManagerImpl(autoUnlockTime)
        if selfID is None:
            selfID = '%s:%d:%d' % (socket.gethostname(), os.getpid(), id(self))
        self.__selfID = selfID
        self.__autoUnlockTime = autoUnlockTime
        self.__mainThread = threading.current_thread()
        self.__initialised = threading.Event()
        self.__destroying = False
        self.__lastProlongateTime = 0
        self.__thread = threading.Thread(target=ReplHostManager._autoAcquireThread, args=(weakref.proxy(self),))
        self.__thread.start()
        while not self.__initialised.is_set():
            pass

    def _consumer(self):
        return self.__lockImpl

    def destroy(self):
        """Destroy should be called before destroying ReplLockManager"""
        self.__destroying = True

    def _autoAcquireThread(self):
        self.__initialised.set()
        try:
            while True:
                if not self.__mainThread.is_alive():
                    break
                if self.__destroying:
                    break
                time.sleep(0.1)
                if time.time() - self.__lastProlongateTime < float(self.__autoUnlockTime) / 4.0:
                    continue
                syncObj = self.__lockImpl._syncObj
                if syncObj is None:
                    continue
                if syncObj._getLeader() is not None:
                    self.__lastProlongateTime = time.time()
                    self.__lockImpl.prolongate(self.__selfID, time.time())
        except ReferenceError:
            pass

    def tryAcquire(self, lockID, callback=None, sync=False, timeout=None):
        """Attempt to acquire lock.

        :param lockID: unique lock identifier.
        :type lockID: str
        :param sync: True - to wait until lock is acquired or failed to acquire.
        :type sync: bool
        :param callback: if sync is False - callback will be called with operation result.
        :type callback: func(opResult, error)
        :param timeout: max operation time (default - unlimited)
        :type timeout: float
        :return True if acquired, False - somebody else already acquired lock
        """
        attemptTime = time.time()
        if sync:
            acquireRes = self.__lockImpl.acquire(lockID, self.__selfID, attemptTime, callback=callback, sync=sync, timeout=timeout)
            acquireTime = time.time()
            if acquireRes:
                if acquireTime - attemptTime > self.__autoUnlockTime / 2.0:
                    acquireRes = False
                    self.__lockImpl.release(lockID, self.__selfID, sync=sync)
            return acquireRes

        def asyncCallback(acquireRes, errCode):
            if acquireRes:
                acquireTime = time.time()
                if acquireTime - attemptTime > self.__autoUnlockTime / 2.0:
                    acquireRes = False
                    self.__lockImpl.release(lockID, self.__selfID, sync=False)
            callback(acquireRes, errCode)

        self.__lockImpl.acquire(lockID, self.__selfID, attemptTime, callback=asyncCallback, sync=sync, timeout=timeout)

    def isAcquired(self, lockID):
        """Check if lock is acquired by ourselves.

        :param lockID: unique lock identifier.
        :type lockID: str
        :return True if lock is acquired by ourselves.
         """
        return self.__lockImpl.isAcquired(lockID, self.__selfID, time.time())

    def isOwned(self, lockID):
        return self.__lockImpl.isOwned(lockID, self.__selfID, time.time())

    def rawData(self):
        return self.__lockImpl.rawData()

    def release(self, lockID, callback=None, sync=False, timeout=None):
        """
        Release previously-acquired lock.

        :param lockID:  unique lock identifier.
        :type lockID: str
        :param sync: True - to wait until lock is released or failed to release.
        :type sync: bool
        :param callback: if sync is False - callback will be called with operation result.
        :type callback: func(opResult, error)
        :param timeout: max operation time (default - unlimited)
        :type timeout: float
        """
        self.__lockImpl.release(lockID, self.__selfID, callback=callback, sync=sync, timeout=timeout)

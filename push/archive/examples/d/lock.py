from __future__ import print_function

import threading
import time
import weakref

from pysyncobj import SyncObj, replicated

# on change issue
# https://github.com/bakwc/PySyncObj/issues/70

class LockImpl(SyncObj):
    def __init__(self, selfAddress, partnerAddrs, autoUnlockTime, conf, on_replicate):
        super(LockImpl, self).__init__(selfAddress, partnerAddrs, conf=conf)
        self.__selfClientID = selfAddress
        self.__locks = {}
        self.__autoUnlockTime = autoUnlockTime
        self.__verbose = True
        self.__counter = 0
        self.on_replicate = on_replicate or None

    @replicated
    def incCounter(self):
        self.__counter += 1
        return self.__counter

    def getCounter(self):
        return self.__counter

    @replicated
    def resetCounter(self):
        self.__counter = 0
        return self.__counter


    @replicated
    def acquire(self, lockPath, clientID, currentTime):
        if self.__verbose:
            print(f"{threading.get_ident()} acquire: {lockPath}, {clientID}, {currentTime}")
        if self.on_replicate is not None:
            self.on_replicate("acquire", lockPath, clientID, currentTime)
        existingLock = self.__locks.get(lockPath, None)
        # Auto-unlock old lock
        if existingLock is not None:
            if currentTime - existingLock[1] > self.__autoUnlockTime:
                existingLock = None
        # Acquire lock if possible
        if existingLock is None or existingLock[0] == clientID:
            self.__locks[lockPath] = (clientID, currentTime)
            return True
        # Lock already acquired by someone else
        return False

    @replicated
    def ping(self, clientID, currentTime):
        # if self.__verbose:
        #     print(f"ping: {clientID}, {currentTime}, {self.__locks}")
        for lockPath in list(self.__locks.keys()):
            lockClientID, lockTime = self.__locks[lockPath]

            if currentTime - lockTime > self.__autoUnlockTime:
                del self.__locks[lockPath]
                continue

            if lockClientID == clientID:
                self.__locks[lockPath] = (clientID, currentTime)

    @replicated
    def release(self, lockPath, clientID):
        self.on_replicate("release", lockPath, clientID)
        if self.__verbose:
            print(f"{threading.get_ident()} release: {lockPath} {clientID}")
        existingLock = self.__locks.get(lockPath, None)
        if existingLock is not None and existingLock[0] == clientID:
            del self.__locks[lockPath]

    @replicated
    def toggle_verbose(self):
        self.__verbose = not self.__verbose
        print(f"{threading.get_ident()} verbose: {self.__verbose}")

    def isOwned(self, lockPath, clientID, currentTime):
        existingLock = self.__locks.get(lockPath, None)
        # if self.__verbose:
        #     print(existingLock, clientID)
        if existingLock is not None:
            if existingLock[0] == clientID:
                if currentTime - existingLock[1] < self.__autoUnlockTime:
                    return True
        return False

    def isAcquired(self, lockPath, clientID, currentTime):
        existingLock = self.__locks.get(lockPath, None)
        # if self.__verbose:
        #     print(existingLock, clientID)
        if existingLock is not None:
            if currentTime - existingLock[1] < self.__autoUnlockTime:
                return True
        return False


class Lock(object):
    def __init__(self, selfAddress, partnerAddrs, autoUnlockTime, conf=None, on_replicate=None):
        self.__lockImpl = LockImpl(selfAddress, partnerAddrs, autoUnlockTime, conf=conf, on_replicate=self.on_replicate if on_replicate is not None else None)
        self.__selfID = selfAddress
        self.__autoUnlockTime = autoUnlockTime
        self.__mainThread = threading.current_thread()
        self.__initialised = threading.Event()
        self.__thread = threading.Thread(target=Lock._autoAcquireThread, args=(weakref.proxy(self),))
        self.__thread.start()
        self.on_replicate_fn = on_replicate
        while not self.__initialised.is_set():
            pass


    def on_replicate(self, *args, **kwargs):
        if self.on_replicate_fn is not None:
            self.on_replicate_fn(self, *args, **kwargs)

    def _autoAcquireThread(self):
        print(f"{threading.get_ident()} _autoAcquireThread")
        self.__initialised.set()
        try:
            while True:
                if not self.__mainThread.is_alive():
                    break
                time.sleep(float(self.__autoUnlockTime) / 4.0)
                if self.__lockImpl._getLeader() is not None:
                    self.__lockImpl.ping(self.__selfID, time.time())
        except ReferenceError:
            pass

    def subscribe(self, fn):
        self.__lockImpl.subscribe(fn)

    def tryAcquireLock(self, path):
        self.__lockImpl.acquire(path, self.__selfID, time.time())

    def isAcquired(self, path):
        return self.__lockImpl.isAcquired(path, self.__selfID, time.time())

    def isOwned(self, path):
        return self.__lockImpl.isOwned(path, self.__selfID, time.time())

    def release(self, path):
        self.__lockImpl.release(path, self.__selfID)

    def getStatus(self):
        return self.__lockImpl.getStatus()

    def toggle_verbose(self):
        self.__lockImpl.toggle_verbose()

    def onTick(self):
        self.__lockImpl._onTick(timeToWait=0)

    def getCounter(self):
        return self.__lockImpl.getCounter()

    def incCounter(self):
        return self.__lockImpl.incCounter()

    def resetCounter(self):
        return self.__lockImpl.resetCounter()
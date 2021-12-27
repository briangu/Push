import asyncio
import multiprocessing
import sys
import threading
from multiprocessing import process, util

import dill
import numpy as np
import pandas as pd
import psutil
from pysyncobj import SyncObj, replicated, replicated_sync, SyncObjConsumer
from pysyncobj.batteries import ReplDict

from push.mgr.code_util import load_src
from push.mgr.qm import QueueManager
from push.mgr.strategy import Strategy
from push.mgr.task import DoTask

import tornado.web
import tornado.ioloop
import tornado.httpserver
import tornado.gen


print("starting")


class MyReplDict(ReplDict):

    def __init__(self, on_set=None):
        super(MyReplDict, self).__init__()
        self.on_set = on_set

    @replicated_sync
    def set_sync(self, key, value):
        self.set(key, value, _doApply=True)

    @replicated
    def set(self, key, value):
        super().set(key, value, _doApply=True)
        if self.on_set is not None:
            self.on_set(key, value)


kvstore = MyReplDict()


class ReplTimeseries(SyncObjConsumer):
    def __init__(self, on_append = None):
        super(ReplTimeseries, self).__init__()
        self.__data = dict()
        self.__index_data = list()
        self.__on_append = on_append

    @replicated
    def reset(self):
        self.__data = dict()
        self.__index_data = list()

    @replicated
    def append(self, idx_data, keys, data):
        self.__index_data.append(idx_data)
        for key, key_data in zip(keys, data):
            col = self.__data.get(key)
            if col is None:
                col = list()
                self.__data[key] = col
            key_data = key_data if isinstance(key_data, list) else [key_data]
            col.append(key_data)
        if self.__on_append is not None:
            self.__on_append(idx_data, keys, data)

    def flatten(self, keys=None):
        keys = keys or list(self.__data.keys())
        df = pd.DataFrame(columns=keys, index=self.__index_data)
        for key in keys:
            df[key] = np.concatenate(self.__data[key])
        return df


def process_ts_updates(idx_data, keys, data):
    print(f"post-processing: {idx_data} {keys}")


repl_ts = ReplTimeseries(on_append=process_ts_updates)

selfAddr = sys.argv[1]  # "localhost:10000"
partners = sys.argv[2:]  # ["localhost:10001", "localhost:10002"]
sync_lock = SyncObj(selfAddr, partners, consumers=[kvstore, repl_ts])


class DoRegister:
    def apply(self, name, src):
        src = dill.loads(src)
        q = src()
        QueueManager.register(name, callable=lambda: q)


dr = DoRegister()


class DoRegisterCallback:
    def apply(self, name, src):
        global onrep
        src = dill.loads(src)
        if isinstance(src, type):
            q = src()
            onrep.handle_map[name] = q.apply if hasattr(q, 'apply') else q
        else:
            onrep.handle_map[name] = src


drc = DoRegisterCallback()
QueueManager.register("do_register_callback", callable=lambda: drc)


class DoLambda:
    def apply(self, src, *args, **kwargs):
        src = load_src(kvstore, src)
        return src(*args, **kwargs)


dl = DoLambda()


class DoRegistry:
    def apply(self):
        return list(QueueManager._registry.keys())


dreg = DoRegistry()


class DoLocaleCapabilities:
    def apply(self):
        return {
            'cpu_count': multiprocessing.cpu_count(),
            'virtual_memory': psutil.virtual_memory(),
            'GPUs': []
        }


class DoKvStore():
    def set(self, k, v):
        global kvstore
        # kvstore.set(k, v)
        print(k, v)

    def get(self, k):
        global kvstore
        # return kvstore.get(k)
        print(k)


dkvs = DoKvStore()

dotask = DoTask(kvstore)

dlc = DoLocaleCapabilities()

print(dlc.apply())

QueueManager.register('do_register', callable=lambda: dr)
QueueManager.register('apply_lambda', callable=lambda: dl)
QueueManager.register('get_registry', callable=lambda: dreg)
QueueManager.register('kvstore', callable=lambda: kvstore)
QueueManager.register('tasks', callable=lambda: dotask)
QueueManager.register('ts', callable=lambda: repl_ts)
QueueManager.register('locale_capabilities', callable=lambda: dlc)

# TODO: one set of strategies and replicate it into a repllist
symbols = ['MSFT', 'TWTR', 'EBAY', 'CVX', 'W', 'GOOG', 'FB']
strategy_capabilities = [None, 'GPU']
np.random.seed(0)
strategies = [Strategy(id=str(i), name=f"s_{i}", symbols=np.random.choice(symbols, 2), capabilities=np.random.choice(strategy_capabilities)) for i in range(10)]

print(f"booting: ")
print(f"status: {sync_lock.getStatus()}")
print(f"self: {sync_lock.selfNode}")
print(f"others: {sync_lock.otherNodes}")
all = [sync_lock.selfNode, *sync_lock.otherNodes]
all = sorted(all, key=lambda x: x.id)
cluster_size = len(all)
my_partition = all.index(sync_lock.selfNode)
print(my_partition)
print([x for x in all])
print(f"owned strategies: {[x for x in strategies if hash(x) % cluster_size == my_partition]}")

# Start up
mgr_port = (int(sys.argv[1].split(":")[1]) % 1000) + 50000
print(f"manager port: {mgr_port}")


def serve_forever(mgr_port, auth_key):
    m = QueueManager(address=('', mgr_port), authkey=auth_key)
    mgmt_server = m.get_server()
    mgmt_server.stop_event = threading.Event()
    process.current_process()._manager_server = mgmt_server
    try:
        accepter = threading.Thread(target=mgmt_server.accepter)
        accepter.daemon = True
        accepter.start()
        return m, accepter
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(e)


def on_get(self):
    self.write("hello")
    self.write(f"keys: {self.kvstore.keys()}")
    self.finish()


class MainHandler(tornado.web.RequestHandler):

    def initialize(self, kvstore):
        if kvstore is None:
            m = QueueManager(address=('', 50000), authkey=b'password')
            m.connect()
            self.kvstore = m.kvstore()
        else:
            self.kvstore = kvstore
        if "on_get_v" not in self.kvstore:
            self.kvstore.set("on_get_v", 1)
            self.kvstore.set("on_get_c", dill.dumps(on_get))

    @tornado.gen.coroutine
    def get(self):
        on_get_v = self.kvstore.get("on_get_v")
        if on_get_v is not None:
            kv_on_get = self.kvstore.get(f"on_get_v{on_get_v}")
            if kv_on_get is not None:
                kv_on_get = load_src(self.kvstore, kv_on_get)
                kv_on_get(self)


def make_app(kvstore):
    return tornado.web.Application([
       ("/", MainHandler, {'kvstore': kvstore})
        # ("/", MainHandler)
        # ("/counter", CounterHandler, {'sync_lock': sync_lock}),
        # ("/status", StatusHandler, {'sync_lock': sync_lock}),
        # ("/toggle", ToggleHandler, {'sync_lock': sync_lock}),
    ])


# TODO: i think this code can be rewritten to use asyncio / twisted
m, mt = serve_forever(mgr_port, b'password')

webserver = tornado.httpserver.HTTPServer(make_app(kvstore))
port = 11000 + my_partition
print(f"my port: {port}")
webserver.listen(port)
# webserver.start(2)

print(f"starting webserver")
# tornado.ioloop.IOLoop.current().start()
loop = asyncio.get_event_loop()
try:
    loop.run_forever()
finally:
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()
print(f"stopping webserver")

mt.join()
import typing

import tornado.gen
import tornado.httpserver
import tornado.ioloop
import tornado.web
from pysyncobj.batteries import ReplList
from tornado.routing import Router

from push.batteries import ReplSyncDict, ReplTimeseries, ReplVersionedDict, ReplTaskManager, CodeStoreLoader, ReplLockDataManager
from push.loader import load_src
from push.push_manager import PushManager
from push.task_manager import TaskManager


class Handle404(tornado.web.RequestHandler):
    def get(self):
        self.set_status(404)
        self.write('404 Not Found')


# https://stackoverflow.com/questions/47970574/tornado-routing-to-a-base-handler
class MyRouter(Router):
    def __init__(self, store, app, prefix=None):
        self.store = store
        self.app = app
        self.prefix = f"kvstore:{prefix or '/web'}"

    def find_handler(self, request, **kwargs):
        try:
            handler = load_src(self.store, f"{self.prefix}{request.path}") or Handle404
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(e)
            handler = Handle404

        return self.app.get_handler_delegate(request, handler)


def make_app(kvstore):
    return MyRouter(kvstore, tornado.web.Application())


# TODO: this could be a replicated command ReplLambda / ReplCommand that runs on all hosts
class DoRegister:
    def __init__(self, store):
        self.store = store

    def apply(self, name, src):
        src = load_src(self.store, src)
        q = src()
        PushManager.register(name, callable=lambda l=q: l)


# exposed in boot_common
repl_code_store = ReplVersionedDict()
tm = TaskManager(repl_code_store)
repl_kvstore = ReplSyncDict(on_set=tm.on_event_handler("process_kv_updates"))
repl_ts = ReplTimeseries(on_append=tm.on_event_handler("process_ts_updates"))
repl_strategies = ReplList()
repl_task_manager = ReplTaskManager(repl_kvstore, tm)


def main() -> (typing.List[object], typing.Dict[str, object]):
    tm.start_event_handlers()

    boot_globals = dict()
    boot_globals['repl_kvstore'] = repl_kvstore
    boot_globals['repl_code_store'] = repl_code_store
    boot_globals['repl_tasks'] = repl_task_manager
    boot_globals['local_tasks'] = tm
    boot_globals['local_register'] = DoRegister(repl_code_store)
    boot_globals['repl_ts'] = repl_ts
    boot_globals['repl_strategies'] = repl_strategies

    CodeStoreLoader.install_importer({'repl_code_store': repl_code_store})

    return boot_globals, make_app(repl_code_store)
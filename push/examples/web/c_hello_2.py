import sys

import tornado.web

from push.push_manager import PushManager

m = PushManager(address=('', int(sys.argv[1])), authkey=b'password')
m.connect()

repl_code_store = m.repl_code_store()


# curl localhost:11000/
class HelloWorldHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(f"Hello, World! (v2) [{repl_code_store.get_head()}]\n")


repl_code_store.set("/web/", HelloWorldHandler, sync=True)

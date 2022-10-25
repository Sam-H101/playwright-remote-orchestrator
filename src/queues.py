import persistqueue

class queues():
    queue = None
    def __init__(self):
        #self.queue = persistqueue.SQLiteAckQueue('queue.db', auto_commit=True)
        run = False
        while run == False:
            try:
                self.queue = persistqueue.SQLiteQueue('queue.db')
                run = True
            except Exception:
                run = False
    def get_item(self):
        itm = self.queue.get()
        return itm

    def ack_item(self, item):
        pass

    def put_item(self, item):
        self.queue.put(item)


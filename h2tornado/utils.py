class CancelContext(object):
    def __init__(self):
        self.cancelled = False

    def __call__(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


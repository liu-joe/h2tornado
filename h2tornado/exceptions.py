class H2TornadoException(Exception):
    pass


class StreamResetException(H2TornadoException):
    pass


class ConnectionError(H2TornadoException):
    pass
